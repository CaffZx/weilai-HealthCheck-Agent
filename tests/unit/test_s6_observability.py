from __future__ import annotations

from clients.azlisting_contract import AZLISTING_INTERNAL_CONTRACT_VERSION
from integrations.observability import (
    HealthLevel,
    HealthThresholds,
    evaluate_health,
    prometheus_metrics,
)
from integrations.preflight import validate_runtime_configuration


def healthy_metrics() -> dict:
    return {
        "jobs": {},
        "outbox": {},
        "feedback": {},
        "runs": {},
        "stale_running_jobs": 0,
        "oldest_pending_job_age_seconds": None,
        "oldest_pending_outbox_age_seconds": None,
    }


def test_empty_runtime_is_healthy():
    status, alerts = evaluate_health(healthy_metrics(), HealthThresholds())
    assert status is HealthLevel.HEALTHY
    assert alerts == []


def test_dead_or_stale_work_is_degraded():
    """DEAD jobs are terminal; STALE RUNNING self-heals via reclaim_stale — neither
    should block runtime health as CRITICAL. Only DEAD outbox stays CRITICAL because
    it means data will not reach the control center."""
    metrics = healthy_metrics()
    metrics["jobs"] = {"DEAD": 1}
    metrics["stale_running_jobs"] = 1
    status, alerts = evaluate_health(metrics, HealthThresholds())
    assert status is HealthLevel.DEGRADED
    assert {item["code"] for item in alerts} == {"DEAD_JOBS", "STALE_RUNNING_JOBS"}
    assert all(item["level"] == HealthLevel.DEGRADED.value for item in alerts)


def test_dead_outbox_stays_critical():
    metrics = healthy_metrics()
    metrics["outbox"] = {"DEAD": 1}
    status, alerts = evaluate_health(metrics, HealthThresholds())
    assert status is HealthLevel.CRITICAL
    assert {item["code"] for item in alerts} == {"DEAD_OUTBOX"}


def test_backlog_and_rejected_feedback_are_degraded():
    metrics = healthy_metrics()
    metrics["jobs"] = {"PENDING": 10}
    metrics["outbox"] = {"PENDING": 5}
    metrics["feedback"] = {"REJECTED": 1}
    metrics["oldest_pending_outbox_age_seconds"] = 601
    status, alerts = evaluate_health(
        metrics,
        HealthThresholds(
            pending_jobs_warn=10,
            pending_outbox_warn=100,
            oldest_pending_outbox_warn_minutes=10,
        ),
    )
    assert status is HealthLevel.DEGRADED
    assert {item["code"] for item in alerts} == {
        "JOB_BACKLOG",
        "OUTBOX_WAIT_TOO_LONG",
        "FEEDBACK_REJECTED",
    }


def test_prometheus_export_contains_health_and_queue_metrics():
    output = prometheus_metrics({
        "status": "DEGRADED",
        "metrics": {
            "jobs": {"PENDING": 2},
            "outbox": {"DEAD": 1},
            "active_signals": 3,
        },
    })
    assert "patrol_health_status 1" in output
    assert 'patrol_jobs_total{status="PENDING"} 2' in output
    assert 'patrol_outbox_total{status="DEAD"} 1' in output
    assert "patrol_active_signals 3" in output


def test_suppressed_shadow_outbox_is_not_an_alert():
    metrics = healthy_metrics()
    metrics["outbox"] = {"SUPPRESSED": 1000}
    status, alerts = evaluate_health(metrics, HealthThresholds())
    assert status is HealthLevel.HEALTHY
    assert alerts == []


def test_archived_jobs_and_batches_are_not_runtime_alerts():
    metrics = healthy_metrics()
    metrics["jobs"] = {"ARCHIVED": 20}
    metrics["runs"] = {}
    status, alerts = evaluate_health(metrics, HealthThresholds())
    assert status is HealthLevel.HEALTHY
    assert alerts == []


def base_settings() -> dict:
    return {
        "database": {"url_env": "PATROL_DATABASE_URL"},
        "mcp": {
            "provider": "AZLISTING",
            "url_env": "MCP_URL",
            "token_env": "MCP_TOKEN",
            "tool_policy": "AUDITED_ONLY",
            "internal_contract_version": AZLISTING_INTERNAL_CONTRACT_VERSION,
            "external_contract_status": "PENDING_OWNER_FREEZE",
            "operating_unit_strategy": "QUERY_PAGE",
        },
        "result_sink": {"url_env": "SINK_URL", "token_env": "SINK_TOKEN"},
        "feedback": {"token_env": "FEEDBACK_TOKEN"},
        "scheduler": {"lease_ttl_seconds": 600, "lease_heartbeat_seconds": 200},
        "feature_flags": {
            "mysql_runtime": True,
            "scheduler_enabled": False,
            "result_delivery_enabled": False,
            "control_center_delivery_enabled": False,
            "feedback_enabled": False,
        },
        "rollout": {"stage": "INTERNAL_ONLY", "allowlisted_shop_ids": []},
    }


def operating_mode_settings() -> dict:
    return {
        "url_env": "MODE_URL",
        "token_env": "MODE_TOKEN",
        "evaluate_tool": "get_evaluate",
    }


def test_internal_only_preflight_needs_database_only():
    issues = validate_runtime_configuration(
        base_settings(),
        {"PATROL_DATABASE_URL": "mysql+pymysql://runtime"},
    )
    assert issues == []


def test_principal_strategy_requires_authoritative_scope_mapping():
    settings = base_settings()
    settings["mcp"].update(
        operating_unit_strategy="PRINCIPAL_FOLLOW_UP",
        principal_scopes_env="AZLISTING_PRINCIPAL_SCOPES_JSON",
    )

    issues = validate_runtime_configuration(
        settings,
        {"PATROL_DATABASE_URL": "mysql+pymysql://runtime"},
    )

    assert {item.code for item in issues} == {"MCP_PRINCIPAL_SCOPES_MISSING"}


def test_principal_strategy_accepts_complete_authoritative_scope_mapping():
    settings = base_settings()
    settings["mcp"].update(
        operating_unit_strategy="PRINCIPAL_FOLLOW_UP",
        principal_scopes_env="AZLISTING_PRINCIPAL_SCOPES_JSON",
    )

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "AZLISTING_PRINCIPAL_SCOPES_JSON": (
                '[{"principalName":"redacted","principalUserId":35,"shopId":1001,'
                '"shopAccount":"redacted-account","siteCode":"Amazon_US"}]'
            ),
        },
    )

    assert issues == []


def test_enabled_external_paths_require_their_own_credentials():
    settings = base_settings()
    settings["rollout"]["stage"] = "FULL"
    settings["feature_flags"].update(
        scheduler_enabled=True,
        result_delivery_enabled=True,
        feedback_enabled=True,
    )
    issues = validate_runtime_configuration(
        settings,
        {"PATROL_DATABASE_URL": "mysql+pymysql://runtime"},
    )
    assert {item.code for item in issues} == {
        "MCP_EXTERNAL_CONTRACT_NOT_FROZEN",
        "MCP_URL_MISSING",
        "MCP_TOKEN_MISSING",
        "RESULT_SINK_URL_MISSING",
        "RESULT_SINK_TOKEN_MISSING",
        "FEEDBACK_TOKEN_MISSING",
    }


def test_enabled_operating_mode_lookup_requires_read_only_contract_and_credentials():
    settings = base_settings()
    settings["rollout"]["stage"] = "FULL"
    settings["feature_flags"]["operating_mode_lookup_enabled"] = True
    settings["operating_mode_mcp"] = {
        "url_env": "MODE_URL",
        "token_env": "MODE_TOKEN",
        "query_tool": "get_current_operating_modes",
        "missing_trigger_tool": "evaluate_operating_mode",
        "job_status_tool": "get_operating_mode_evaluation_job",
        "manual_evaluate_forbidden": False,
    }

    issues = validate_runtime_configuration(
        settings,
        {"PATROL_DATABASE_URL": "mysql+pymysql://runtime"},
    )

    assert {item.code for item in issues} == {
        "FULL_ROLLOUT_FLAGS_DISABLED",
        "OPERATING_MODE_MCP_EVALUATE_TOOL_MISSING",
        "OPERATING_MODE_MCP_URL_MISSING",
        "OPERATING_MODE_MCP_TOKEN_MISSING",
    }


def test_enabled_operating_mode_lookup_accepts_read_only_contract():
    settings = base_settings()
    settings["rollout"]["stage"] = "FULL"
    settings["feature_flags"]["operating_mode_lookup_enabled"] = True
    settings["operating_mode_mcp"] = operating_mode_settings()

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "MODE_URL": "https://mode.test/mcp",
            "MODE_TOKEN": "token",
        },
    )

    assert {item.code for item in issues} == {"FULL_ROLLOUT_FLAGS_DISABLED"}


def test_enabled_scheduler_rejects_invalid_lease_heartbeat():
    settings = base_settings()
    settings["rollout"] = {"stage": "SHADOW", "allowlisted_shop_ids": [101]}
    settings["feature_flags"]["scheduler_enabled"] = True
    settings["scheduler"] = {"lease_ttl_seconds": 60, "lease_heartbeat_seconds": 60}

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "MCP_URL": "https://mcp.test",
            "MCP_TOKEN": "token",
        },
    )

    assert {item.code for item in issues} == {
        "MCP_EXTERNAL_CONTRACT_NOT_FROZEN",
        "SCHEDULER_LEASE_HEARTBEAT_INVALID",
    }


def test_enabled_scheduler_requires_azlisting_as_primary_provider():
    settings = base_settings()
    settings["rollout"] = {"stage": "SHADOW", "allowlisted_shop_ids": [101]}
    settings["feature_flags"]["scheduler_enabled"] = True
    settings["mcp"].update(provider="STREAMABLE", default_url="https://streamable.test")

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "MCP_TOKEN": "token",
        },
    )

    assert {item.code for item in issues} == {
        "MCP_EXTERNAL_CONTRACT_NOT_FROZEN",
        "MCP_PRIMARY_PROVIDER_INVALID",
    }


def test_enabled_scheduler_forbids_cross_contract_automatic_fallback():
    settings = base_settings()
    settings["rollout"] = {"stage": "SHADOW", "allowlisted_shop_ids": [101]}
    settings["feature_flags"]["scheduler_enabled"] = True
    settings["mcp"].update(
        provider="AZLISTING",
        default_url="https://azlisting.test",
        automatic_fallback=True,
    )

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "MCP_TOKEN": "token",
        },
    )

    assert {item.code for item in issues} == {
        "MCP_AUTOMATIC_FALLBACK_FORBIDDEN",
        "MCP_EXTERNAL_CONTRACT_NOT_FROZEN",
    }


def test_supplemental_gateway_cannot_satisfy_primary_mcp_token_requirement():
    settings = base_settings()
    settings["rollout"] = {"stage": "SHADOW", "allowlisted_shop_ids": [101]}
    settings["feature_flags"]["scheduler_enabled"] = True
    settings["mcp"].update(provider="AZLISTING", default_url="https://azlisting.test")

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "MCP_SYNC_GATEWAY": "https://streamable.test",
        },
    )

    assert {item.code for item in issues} == {
        "MCP_EXTERNAL_CONTRACT_NOT_FROZEN",
        "MCP_TOKEN_MISSING",
    }


def test_enabled_scheduler_requires_audited_only_tool_policy():
    settings = base_settings()
    settings["rollout"] = {"stage": "SHADOW", "allowlisted_shop_ids": [101]}
    settings["feature_flags"]["scheduler_enabled"] = True
    settings["mcp"].update(
        default_url="https://azlisting.test",
        tool_policy="ALLOW_ANY",
    )

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "MCP_TOKEN": "token",
        },
    )

    assert {item.code for item in issues} == {
        "MCP_EXTERNAL_CONTRACT_NOT_FROZEN",
        "MCP_TOOL_POLICY_INVALID",
    }


def test_enabled_scheduler_is_blocked_until_mcp_owner_contract_is_frozen():
    settings = base_settings()
    settings["rollout"] = {"stage": "SHADOW", "allowlisted_shop_ids": [101]}
    settings["feature_flags"]["scheduler_enabled"] = True
    settings["mcp"].update(default_url="https://azlisting.test")

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "MCP_TOKEN": "token",
        },
    )

    assert {item.code for item in issues} == {"MCP_EXTERNAL_CONTRACT_NOT_FROZEN"}


def test_preflight_rejects_mcp_internal_contract_version_drift():
    settings = base_settings()
    settings["mcp"]["internal_contract_version"] = "stale"

    issues = validate_runtime_configuration(
        settings,
        {"PATROL_DATABASE_URL": "mysql+pymysql://runtime"},
    )

    assert {item.code for item in issues} == {"MCP_INTERNAL_CONTRACT_VERSION_INVALID"}


def test_enabled_supplemental_mcp_rejects_draft_contract():
    settings = base_settings()
    settings["supplemental_mcp"] = {
        "provider": "STREAMABLE",
        "url_env": "MCP_SYNC_GATEWAY",
        "token_env": "MCP_TOKEN",
        "enabled": True,
        "contract_policy": "FROZEN_ONLY",
        "enabled_tools": ["product_sales"],
    }

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "MCP_SYNC_GATEWAY": "https://streamable.test",
            "MCP_TOKEN": "token",
        },
    )

    assert {item.code for item in issues} == {
        "SUPPLEMENTAL_MCP_CONTRACT_NOT_FROZEN",
    }


def test_enabled_supplemental_mcp_requires_explicit_tools_and_token():
    settings = base_settings()
    settings["supplemental_mcp"] = {
        "provider": "STREAMABLE",
        "default_url": "https://streamable.test",
        "token_env": "MCP_TOKEN",
        "enabled": True,
        "contract_policy": "FROZEN_ONLY",
        "enabled_tools": [],
    }

    issues = validate_runtime_configuration(
        settings,
        {"PATROL_DATABASE_URL": "mysql+pymysql://runtime"},
    )

    assert {item.code for item in issues} == {
        "SUPPLEMENTAL_MCP_TOKEN_MISSING",
        "SUPPLEMENTAL_MCP_TOOLS_EMPTY",
    }


def test_internal_only_rejects_any_enabled_external_path():
    settings = base_settings()
    settings["feature_flags"]["result_delivery_enabled"] = True
    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "SINK_URL": "https://sink.invalid",
            "SINK_TOKEN": "test-token",
        },
    )
    assert {item.code for item in issues} == {"INTERNAL_EXTERNAL_PATH_ENABLED"}


def test_internal_only_allows_read_only_operating_mode_lookup():
    settings = base_settings()
    settings["feature_flags"]["operating_mode_lookup_enabled"] = True
    settings["operating_mode_mcp"] = operating_mode_settings()

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "MODE_URL": "https://mode.test/mcp",
            "MODE_TOKEN": "token",
        },
    )

    assert issues == []


def test_control_center_delivery_requires_dedicated_credentials_and_rollout():
    settings = base_settings()
    settings["feature_flags"]["control_center_delivery_enabled"] = True
    settings["control_center_mcp"] = {
        "url_env": "CONTROL_CENTER_MCP_URL",
        "token_env": "CONTROL_CENTER_MCP_TOKEN",
    }

    issues = validate_runtime_configuration(
        settings,
        {"PATROL_DATABASE_URL": "mysql+pymysql://runtime"},
    )

    assert {item.code for item in issues} == {
        "CONTROL_CENTER_MCP_URL_MISSING",
        "CONTROL_CENTER_MCP_TOKEN_MISSING",
        "INTERNAL_EXTERNAL_PATH_ENABLED",
    }


def test_control_center_delivery_accepts_configured_default_url():
    settings = base_settings()
    settings["feature_flags"]["control_center_delivery_enabled"] = True
    settings["control_center_mcp"] = {
        "url_env": "CONTROL_CENTER_MCP_URL",
        "default_url": "https://mcp-gateway.example.com/opsloop/mcp",
        "token_env": "CONTROL_CENTER_MCP_TOKEN",
    }

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "CONTROL_CENTER_MCP_TOKEN": "token",
        },
    )

    assert {item.code for item in issues} == {"INTERNAL_EXTERNAL_PATH_ENABLED"}


def test_full_rollout_accepts_control_center_only_delivery():
    settings = base_settings()
    settings["rollout"]["stage"] = "FULL"
    settings["feature_flags"]["control_center_delivery_enabled"] = True
    settings["control_center_mcp"] = {
        "url_env": "CONTROL_CENTER_MCP_URL",
        "default_url": "https://mcp-gateway.example.com/opsloop/mcp",
        "token_env": "CONTROL_CENTER_MCP_TOKEN",
    }

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "CONTROL_CENTER_MCP_TOKEN": "token",
        },
    )

    assert issues == []


def test_shadow_forbids_control_center_delivery_even_with_credentials():
    settings = base_settings()
    settings["rollout"] = {"stage": "SHADOW", "allowlisted_shop_ids": [101]}
    settings["feature_flags"].update(
        scheduler_enabled=True,
        control_center_delivery_enabled=True,
    )
    settings["control_center_mcp"] = {
        "url_env": "CONTROL_CENTER_MCP_URL",
        "token_env": "CONTROL_CENTER_MCP_TOKEN",
    }

    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "MCP_URL": "https://mcp.test",
            "MCP_TOKEN": "token",
            "CONTROL_CENTER_MCP_URL": "https://control-center.test/mcp",
            "CONTROL_CENTER_MCP_TOKEN": "token",
        },
    )

    assert {item.code for item in issues} == {
        "MCP_EXTERNAL_CONTRACT_NOT_FROZEN",
        "SHADOW_CONTROL_CENTER_DELIVERY_ENABLED",
    }


def test_shadow_requires_scheduler_and_forbids_delivery_and_feedback():
    settings = base_settings()
    settings["rollout"] = {"stage": "SHADOW", "allowlisted_shop_ids": [101]}
    settings["feature_flags"].update(
        result_delivery_enabled=True,
        feedback_enabled=True,
    )
    issues = validate_runtime_configuration(
        settings,
        {
            "PATROL_DATABASE_URL": "mysql+pymysql://runtime",
            "SINK_URL": "https://sink.invalid",
            "SINK_TOKEN": "test-token",
            "FEEDBACK_TOKEN": "test-token",
        },
    )
    assert {item.code for item in issues} == {
        "SHADOW_SCHEDULER_DISABLED",
        "SHADOW_DELIVERY_ENABLED",
        "SHADOW_FEEDBACK_ENABLED",
    }


def test_canary_requires_shop_allowlist_and_full_requires_a_production_path():
    settings = base_settings()
    settings["rollout"]["stage"] = "CANARY"
    issues = validate_runtime_configuration(
        settings,
        {"PATROL_DATABASE_URL": "mysql+pymysql://runtime"},
    )
    assert {item.code for item in issues} == {
        "ROLLOUT_ALLOWLIST_EMPTY",
        "CANARY_PATHS_DISABLED",
    }

    settings["rollout"]["stage"] = "FULL"
    issues = validate_runtime_configuration(
        settings,
        {"PATROL_DATABASE_URL": "mysql+pymysql://runtime"},
    )
    assert {item.code for item in issues} == {"FULL_ROLLOUT_FLAGS_DISABLED"}
