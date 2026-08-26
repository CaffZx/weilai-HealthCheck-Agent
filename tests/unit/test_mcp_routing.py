import pytest

from integrations.mcp_routing import (
    McpRouteConfigurationError,
    require_azlisting_primary,
    resolve_primary_mcp_route,
    resolve_supplemental_mcp_route,
)


def settings(**overrides):
    config = {
        "provider": "AZLISTING",
        "url_env": "AZLISTING_GATEWAY",
        "legacy_url_env": "MCP_AZLISTING_URL",
        "default_url": "http://azlisting.default/mcp",
        "token_env": "MCP_API_KEY",
        "legacy_token_env": "MCP_AZLISTING_TOKEN",
        "automatic_fallback": False,
    }
    config.update(overrides)
    return {"mcp": config}


def test_primary_route_prefers_azlisting_runtime_variables():
    route = resolve_primary_mcp_route(
        settings(),
        {
            "AZLISTING_GATEWAY": "http://azlisting.primary/mcp",
            "MCP_AZLISTING_URL": "http://azlisting.legacy/mcp",
            "MCP_API_KEY": "primary-token",
            "MCP_AZLISTING_TOKEN": "legacy-token",
            "MCP_SYNC_GATEWAY": "http://streamable.invalid/mcp",
        },
    )

    assert route.provider == "AZLISTING"
    assert route.url == "http://azlisting.primary/mcp"
    assert route.token == "primary-token"
    assert route.automatic_fallback is False


def test_primary_route_keeps_legacy_aliases_compatible():
    route = resolve_primary_mcp_route(
        settings(),
        {
            "MCP_AZLISTING_URL": "http://azlisting.legacy/mcp",
            "MCP_AZLISTING_TOKEN": "legacy-token",
        },
    )

    assert route.url == "http://azlisting.legacy/mcp"
    assert route.token == "legacy-token"


def test_primary_route_never_selects_supplemental_gateway():
    route = resolve_primary_mcp_route(
        settings(),
        {"MCP_SYNC_GATEWAY": "http://streamable.invalid/mcp"},
    )

    assert route.url == "http://azlisting.default/mcp"
    assert route.token == ""


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "STREAMABLE"},
        {"automatic_fallback": True},
    ],
)
def test_primary_route_hard_rejects_cross_contract_configuration(overrides):
    route = resolve_primary_mcp_route(settings(**overrides), {})

    with pytest.raises(McpRouteConfigurationError):
        require_azlisting_primary(route)


def test_supplemental_route_is_explicit_and_disabled_by_default():
    route = resolve_supplemental_mcp_route(
        {
            **settings(),
            "supplemental_mcp": {
                "provider": "STREAMABLE",
                "url_env": "MCP_SYNC_GATEWAY",
                "default_url": "http://streamable.default/mcp",
                "token_env": "MCP_API_KEY",
                "enabled": False,
            },
        },
        {"MCP_API_KEY": "test-token"},
    )

    assert route.provider == "STREAMABLE"
    assert route.url == "http://streamable.default/mcp"
    assert route.token == "test-token"
    assert route.enabled is False
