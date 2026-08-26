from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "accesstoekn",
    "accestoekn",
    "refreshtoken",
)
_SENSITIVE_KEYS = {
    "apikey",
    "authorization",
    "cookie",
    "key",
    "privatekey",
    "sessionid",
    "xapikey",
}
_TEXT_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)sk-starsrock-[a-z0-9_-]{16,}"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|pwd|secret|authorization)"
        r"\s*[:=]\s*[\"']?)[^,\s\"'}]+"
    ),
)


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_sensitive_mcp_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    return normalized in _SENSITIVE_KEYS or any(
        part in normalized for part in _SENSITIVE_KEY_PARTS
    )


def sanitize_mcp_text(value: str) -> str:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        sanitized_payload = sanitize_mcp_payload(parsed)
        if sanitized_payload != parsed:
            return json.dumps(
                sanitized_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return value
    sanitized = value
    for pattern in _TEXT_PATTERNS:
        sanitized = pattern.sub(
            lambda match: (
                f"{match.group(1)}[REDACTED]"
                if match.lastindex and match.lastindex >= 1
                else "[REDACTED]"
            ),
            sanitized,
        )
    return sanitized


def sanitize_mcp_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: sanitize_mcp_payload(item)
            for key, item in value.items()
            if not is_sensitive_mcp_key(key)
        }
    if isinstance(value, list):
        return [sanitize_mcp_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_mcp_payload(item) for item in value)
    if isinstance(value, str):
        return sanitize_mcp_text(value)
    return value
