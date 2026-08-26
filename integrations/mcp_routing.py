from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class McpRoute:
    provider: str
    url: str
    token: str
    automatic_fallback: bool


@dataclass(frozen=True, slots=True)
class SupplementalMcpRoute:
    provider: str
    url: str
    token: str
    enabled: bool


class McpRouteConfigurationError(ValueError):
    pass


def _first_environment_value(
    environment: Mapping[str, str],
    *variable_names: Any,
) -> str:
    for variable_name in variable_names:
        name = str(variable_name or "").strip()
        value = environment.get(name, "").strip() if name else ""
        if value:
            return value
    return ""


def resolve_primary_mcp_route(
    settings: Mapping[str, Any],
    environment: Mapping[str, str],
) -> McpRoute:
    config = settings.get("mcp", {}) or {}
    provider = str(config.get("provider", "AZLISTING")).strip().upper()
    url = _first_environment_value(
        environment,
        config.get("url_env"),
        config.get("legacy_url_env"),
    ) or str(config.get("default_url", "")).strip()
    token = _first_environment_value(
        environment,
        config.get("token_env"),
        config.get("legacy_token_env"),
    )
    return McpRoute(
        provider=provider,
        url=url,
        token=token,
        automatic_fallback=bool(config.get("automatic_fallback", False)),
    )


def require_azlisting_primary(route: McpRoute) -> McpRoute:
    if route.provider != "AZLISTING":
        raise McpRouteConfigurationError("primary MCP provider must be AZLISTING")
    if route.automatic_fallback:
        raise McpRouteConfigurationError("cross-contract MCP fallback must remain disabled")
    return route


def resolve_supplemental_mcp_route(
    settings: Mapping[str, Any],
    environment: Mapping[str, str],
) -> SupplementalMcpRoute:
    config = settings.get("supplemental_mcp", {}) or {}
    return SupplementalMcpRoute(
        provider=str(config.get("provider", "STREAMABLE")).strip().upper(),
        url=(
            _first_environment_value(environment, config.get("url_env"))
            or str(config.get("default_url", "")).strip()
        ),
        token=_first_environment_value(environment, config.get("token_env")),
        enabled=bool(config.get("enabled", False)),
    )
