from __future__ import annotations

import pytest
from fastapi import HTTPException

from web.backend.review_mcp_access import require_review_mcp_access


def settings(*, enabled: bool = True) -> dict:
    return {
        "feature_flags": {"review_mcp_enabled": enabled},
        "review_mcp": {
            "enabled_env": "REVIEW_MCP_ENABLED",
            "public_token_env": "REVIEW_MCP_PUBLIC_TOKEN",
        },
    }


def test_review_mcp_requires_both_runtime_gates():
    with pytest.raises(HTTPException) as exc_info:
        require_review_mcp_access(
            "Bearer secret",
            settings=settings(enabled=False),
            environment={"REVIEW_MCP_ENABLED": "true", "REVIEW_MCP_PUBLIC_TOKEN": "secret"},
        )
    assert exc_info.value.status_code == 503


def test_review_mcp_requires_independent_token_configuration():
    with pytest.raises(HTTPException) as exc_info:
        require_review_mcp_access(
            "Bearer secret",
            settings=settings(),
            environment={"REVIEW_MCP_ENABLED": "true"},
        )
    assert exc_info.value.status_code == 503


def test_review_mcp_rejects_invalid_bearer_token():
    with pytest.raises(HTTPException) as exc_info:
        require_review_mcp_access(
            "Bearer wrong",
            settings=settings(),
            environment={"REVIEW_MCP_ENABLED": "true", "REVIEW_MCP_PUBLIC_TOKEN": "secret"},
        )
    assert exc_info.value.status_code == 401


def test_review_mcp_accepts_enabled_runtime_and_matching_token():
    require_review_mcp_access(
        "Bearer secret",
        settings=settings(),
        environment={"REVIEW_MCP_ENABLED": "true", "REVIEW_MCP_PUBLIC_TOKEN": "secret"},
    )
