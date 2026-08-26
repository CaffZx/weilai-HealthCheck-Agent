from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from .deps import load_settings


def require_review_mcp_access(
    authorization: str | None,
    *,
    settings: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
) -> None:
    runtime_settings = settings or load_settings()
    runtime_environment = environment or os.environ
    config = runtime_settings.get("review_mcp", {}) or {}
    enabled_variable = str(config.get("enabled_env", "REVIEW_MCP_ENABLED")).strip()
    environment_enabled = runtime_environment.get(enabled_variable, "").strip().lower()
    if not runtime_settings.get("feature_flags", {}).get("review_mcp_enabled") or (
        environment_enabled not in {"1", "true", "yes"}
    ):
        raise HTTPException(status_code=503, detail="Review MCP is disabled")

    token_variable = str(config.get("public_token_env", "REVIEW_MCP_PUBLIC_TOKEN")).strip()
    expected = runtime_environment.get(token_variable, "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="MCP authentication is not configured")

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid MCP credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
