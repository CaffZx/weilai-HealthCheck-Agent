from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clients.azlisting_contract import (
    AZLISTING_EXTERNAL_CONTRACT_STATUS,
    AZLISTING_INTERNAL_CONTRACT_VERSION,
    AzListingExternalContractStatus,
)
from clients.streamable_contract import (
    STREAMABLE_TOOL_CONTRACTS,
    SupplementalContractStatus,
)
from integrations.mcp_operating_units import parse_principal_scopes_json
from integrations.mcp_routing import (
    resolve_primary_mcp_route,
    resolve_supplemental_mcp_route,
)


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    code: str
    message: str


def validate_runtime_configuration(
    settings: dict[str, Any],
    environment: dict[str, str],
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    flags = settings.get("feature_flags", {})
    rollout = settings.get("rollout", {})
    scheduler = settings.get("scheduler", {})
    mcp_config = settings.get("mcp", {}) or {}

    if str(mcp_config.get("internal_contract_version", "")) != (
        AZLISTING_INTERNAL_CONTRACT_VERSION
    ):
        issues.append(PreflightIssue(
            "MCP_INTERNAL_CONTRACT_VERSION_INVALID",
            "AZListing MCP internal contract version does not match runtime code",
        ))
    if str(mcp_config.get("external_contract_status", "")).upper() != (
        AZLISTING_EXTERNAL_CONTRACT_STATUS.value
    ):
        issues.append(PreflightIssue(
            "MCP_EXTERNAL_CONTRACT_STATUS_INVALID",
            "AZListing MCP external contract status does not match runtime code",
        ))

    operating_unit_strategy = str(
        mcp_config.get("operating_unit_strategy", "QUERY_PAGE")
    ).upper()
    if operating_unit_strategy not in {"QUERY_PAGE", "PRINCIPAL_FOLLOW_UP", "PRINCIPAL_DIRECTORY"}:
        issues.append(PreflightIssue(
            "MCP_OPERATING_UNIT_STRATEGY_INVALID",
            "AZListing operating unit strategy is not supported",
        ))
    elif operating_unit_strategy == "PRINCIPAL_FOLLOW_UP":
        scopes_env = str(mcp_config.get("principal_scopes_env", "")).strip()
        scopes_value = environment.get(scopes_env, "") if scopes_env else ""
        if not scopes_value.strip():
            issues.append(PreflightIssue(
                "MCP_PRINCIPAL_SCOPES_MISSING",
                "principal follow-up strategy requires an authoritative scope mapping",
            ))
        else:
            try:
                parse_principal_scopes_json(scopes_value)
            except ValueError:
                issues.append(PreflightIssue(
                    "MCP_PRINCIPAL_SCOPES_INVALID",
                    "principal scope mapping is invalid",
                ))
    elif operating_unit_strategy == "PRINCIPAL_DIRECTORY":
        directory_url_env = str(mcp_config.get("directory_url_env", "")).strip()
        directory_token_env = str(mcp_config.get("directory_token_env", "")).strip()
        if not directory_url_env or not environment.get(directory_url_env, "").strip():
            issues.append(PreflightIssue(
                "MCP_DIRECTORY_URL_MISSING",
                "principal directory MCP URL is not configured",
            ))
        if not directory_token_env or not environment.get(directory_token_env, "").strip():
            issues.append(PreflightIssue(
                "MCP_DIRECTORY_TOKEN_MISSING",
                "principal directory MCP token is not configured",
            ))

    def require_env(section: str, key: str, code: str) -> None:
        variable = str(settings.get(section, {}).get(key, "")).strip()
        if not variable or not environment.get(variable, "").strip():
            issues.append(PreflightIssue(code, f"{section}.{key} is not configured"))

    require_env("database", "url_env", "DATABASE_URL_MISSING")
    supplemental_config = settings.get("supplemental_mcp", {}) or {}
    supplemental_route = resolve_supplemental_mcp_route(settings, environment)
    if supplemental_route.enabled:
        if supplemental_route.provider != "STREAMABLE":
            issues.append(PreflightIssue(
                "SUPPLEMENTAL_MCP_PROVIDER_INVALID",
                "supplemental MCP provider must be STREAMABLE",
            ))
        if not supplemental_route.url:
            issues.append(PreflightIssue(
                "SUPPLEMENTAL_MCP_URL_MISSING",
                "supplemental MCP URL is not configured",
            ))
        if not supplemental_route.token:
            issues.append(PreflightIssue(
                "SUPPLEMENTAL_MCP_TOKEN_MISSING",
                "supplemental MCP token is not configured",
            ))
        if str(supplemental_config.get("contract_policy", "")).upper() != "FROZEN_ONLY":
            issues.append(PreflightIssue(
                "SUPPLEMENTAL_MCP_POLICY_INVALID",
                "supplemental MCP contract policy must be FROZEN_ONLY",
            ))
        enabled_tools = supplemental_config.get("enabled_tools") or []
        if not enabled_tools:
            issues.append(PreflightIssue(
                "SUPPLEMENTAL_MCP_TOOLS_EMPTY",
                "supplemental MCP requires explicit enabled_tools",
            ))
        for tool_name in enabled_tools:
            contract = STREAMABLE_TOOL_CONTRACTS.get(str(tool_name))
            if contract is None:
                issues.append(PreflightIssue(
                    "SUPPLEMENTAL_MCP_TOOL_UNKNOWN",
                    f"supplemental MCP tool is not registered: {tool_name}",
                ))
            elif contract.status is not SupplementalContractStatus.FROZEN:
                issues.append(PreflightIssue(
                    "SUPPLEMENTAL_MCP_CONTRACT_NOT_FROZEN",
                    f"supplemental MCP contract is not frozen: {tool_name}",
                ))
    if flags.get("scheduler_enabled"):
        if AZLISTING_EXTERNAL_CONTRACT_STATUS is not AzListingExternalContractStatus.FROZEN:
            issues.append(PreflightIssue(
                "MCP_EXTERNAL_CONTRACT_NOT_FROZEN",
                "AZListing MCP owner contract is not frozen",
            ))
        mcp_route = resolve_primary_mcp_route(settings, environment)
        if mcp_route.provider != "AZLISTING":
            issues.append(PreflightIssue(
                "MCP_PRIMARY_PROVIDER_INVALID",
                "primary MCP provider must be AZLISTING",
            ))
        if not mcp_route.url:
            issues.append(PreflightIssue("MCP_URL_MISSING", "primary MCP URL is not configured"))
        if not mcp_route.token:
            issues.append(PreflightIssue(
                "MCP_TOKEN_MISSING",
                "primary AZListing MCP token is not configured",
            ))
        if mcp_route.automatic_fallback:
            issues.append(PreflightIssue(
                "MCP_AUTOMATIC_FALLBACK_FORBIDDEN",
                "cross-contract MCP fallback must remain disabled",
            ))
        if str(settings.get("mcp", {}).get("tool_policy", "")).upper() != "AUDITED_ONLY":
            issues.append(PreflightIssue(
                "MCP_TOOL_POLICY_INVALID",
                "AZListing MCP tool policy must be AUDITED_ONLY",
            ))
        try:
            lease_ttl = int(scheduler.get("lease_ttl_seconds", 600))
            heartbeat = int(scheduler.get("lease_heartbeat_seconds", lease_ttl // 3))
        except (TypeError, ValueError):
            lease_ttl = heartbeat = 0
        if lease_ttl <= 0 or heartbeat <= 0 or heartbeat >= lease_ttl:
            issues.append(PreflightIssue(
                "SCHEDULER_LEASE_HEARTBEAT_INVALID",
                "scheduler heartbeat must be positive and shorter than lease ttl",
            ))
    if flags.get("result_delivery_enabled"):
        require_env("result_sink", "url_env", "RESULT_SINK_URL_MISSING")
        require_env("result_sink", "token_env", "RESULT_SINK_TOKEN_MISSING")
    if flags.get("control_center_delivery_enabled"):
        control_center_config = settings.get("control_center_mcp", {}) or {}
        control_center_url_env = str(
            control_center_config.get("url_env", "")
        ).strip()
        control_center_url = (
            environment.get(control_center_url_env, "").strip()
            if control_center_url_env
            else ""
        ) or str(control_center_config.get("default_url", "")).strip()
        if not control_center_url:
            issues.append(PreflightIssue(
                "CONTROL_CENTER_MCP_URL_MISSING",
                "control_center_mcp URL is not configured",
            ))
        require_env(
            "control_center_mcp",
            "token_env",
            "CONTROL_CENTER_MCP_TOKEN_MISSING",
        )
    if flags.get("operating_mode_lookup_enabled"):
        mode_config = settings.get("operating_mode_mcp", {}) or {}
        if not str(mode_config.get("evaluate_tool", "")).strip():
            issues.append(PreflightIssue(
                "OPERATING_MODE_MCP_EVALUATE_TOOL_MISSING",
                "operating_mode_mcp.evaluate_tool must be configured (e.g. get_evaluate)",
            ))
        require_env(
            "operating_mode_mcp",
            "url_env",
            "OPERATING_MODE_MCP_URL_MISSING",
        )
        require_env(
            "operating_mode_mcp",
            "token_env",
            "OPERATING_MODE_MCP_TOKEN_MISSING",
        )
    if flags.get("feedback_enabled"):
        require_env("feedback", "token_env", "FEEDBACK_TOKEN_MISSING")
    if flags.get("review_mcp_enabled"):
        require_env("review_mcp", "public_token_env", "REVIEW_MCP_TOKEN_MISSING")
        enabled_env = str(settings.get("review_mcp", {}).get("enabled_env", "")).strip()
        if environment.get(enabled_env, "").strip().lower() not in {"1", "true", "yes"}:
            issues.append(PreflightIssue(
                "REVIEW_MCP_RUNTIME_DISABLED",
                "review MCP feature flag requires its runtime environment gate",
            ))
    if not flags.get("mysql_runtime"):
        issues.append(PreflightIssue("MYSQL_RUNTIME_DISABLED", "MySQL runtime must be enabled"))
    stage = str(rollout.get("stage", "INTERNAL_ONLY")).upper()
    allowed_stages = {"INTERNAL_ONLY", "SHADOW", "CANARY", "FULL"}
    if stage not in allowed_stages:
        issues.append(PreflightIssue("ROLLOUT_STAGE_INVALID", "rollout.stage is invalid"))
    allowlisted_shop_ids = rollout.get("allowlisted_shop_ids") or []
    if stage in {"SHADOW", "CANARY"} and not allowlisted_shop_ids:
        issues.append(PreflightIssue(
            "ROLLOUT_ALLOWLIST_EMPTY",
            f"{stage} rollout requires allowlisted_shop_ids",
        ))
    external_flags = (
        "scheduler_enabled",
        "result_delivery_enabled",
        "control_center_delivery_enabled",
        "feedback_enabled",
        "review_mcp_enabled",
    )
    if stage == "INTERNAL_ONLY" and any(flags.get(name) for name in external_flags):
        issues.append(PreflightIssue(
            "INTERNAL_EXTERNAL_PATH_ENABLED",
            "INTERNAL_ONLY rollout requires all external paths to be disabled",
        ))
    if stage == "SHADOW" and not flags.get("scheduler_enabled"):
        issues.append(PreflightIssue(
            "SHADOW_SCHEDULER_DISABLED",
            "SHADOW rollout requires scheduler_enabled",
        ))
    if stage == "SHADOW" and flags.get("result_delivery_enabled"):
        issues.append(PreflightIssue(
            "SHADOW_DELIVERY_ENABLED",
            "SHADOW rollout must not deliver results",
        ))
    if stage == "SHADOW" and flags.get("control_center_delivery_enabled"):
        issues.append(PreflightIssue(
            "SHADOW_CONTROL_CENTER_DELIVERY_ENABLED",
            "SHADOW rollout must not deliver patrol batches to control center",
        ))
    if stage == "SHADOW" and flags.get("feedback_enabled"):
        issues.append(PreflightIssue(
            "SHADOW_FEEDBACK_ENABLED",
            "SHADOW rollout must not consume production feedback",
        ))
    if stage == "CANARY" and not all(
        flags.get(name)
        for name in ("scheduler_enabled", "result_delivery_enabled", "feedback_enabled")
    ):
        issues.append(PreflightIssue(
            "CANARY_PATHS_DISABLED",
            "CANARY rollout requires scheduler, result delivery, and feedback",
        ))
    if stage == "FULL" and not any(
        flags.get(name)
        for name in (
            "scheduler_enabled",
            "result_delivery_enabled",
            "control_center_delivery_enabled",
            "feedback_enabled",
            "review_mcp_enabled",
        )
    ):
        issues.append(PreflightIssue(
            "FULL_ROLLOUT_FLAGS_DISABLED",
            "FULL rollout requires at least one production runtime path",
        ))
    return issues
