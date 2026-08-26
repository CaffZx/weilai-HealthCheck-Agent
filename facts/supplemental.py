from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from clients.mcp_client import McpToolResult
from clients.streamable_contract import (
    STREAMABLE_TOOL_CONTRACTS,
    SupplementalContractStatus,
)
from core.contracts import sha256_json


class SupplementalFactsDisabled(RuntimeError):
    pass


class SupplementalContractNotFrozen(RuntimeError):
    pass


class SupplementalMcpPort(Protocol):
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> McpToolResult: ...


@dataclass(frozen=True, slots=True)
class SupplementalFactBatch:
    tool_name: str
    domain: str
    rows: list[dict[str, Any]]
    source_content_hash: str
    mapped_content_hash: str
    request_hash: str
    latency_ms: int
    warning: str | None
    fetched_at: datetime | None


def _response_rows(result: McpToolResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in result.data:
        nested = item.get("rows") if isinstance(item, dict) else None
        if isinstance(nested, list):
            rows.extend(row for row in nested if isinstance(row, dict))
        elif isinstance(item, dict):
            rows.append(item)
    return rows


def map_streamable_result(tool_name: str, result: McpToolResult) -> SupplementalFactBatch:
    contract = STREAMABLE_TOOL_CONTRACTS.get(tool_name)
    if contract is None:
        raise SupplementalContractNotFrozen(f"unregistered supplemental tool: {tool_name}")
    mapped_rows = [
        {
            key: value
            for key, value in row.items()
            if key in contract.allowed_row_fields and value is not None
        }
        for row in _response_rows(result)
    ]
    return SupplementalFactBatch(
        tool_name=tool_name,
        domain=contract.domain,
        rows=mapped_rows,
        source_content_hash=result.content_hash,
        mapped_content_hash=sha256_json(mapped_rows),
        request_hash=result.request_hash,
        latency_ms=result.latency_ms,
        warning=result.warning,
        fetched_at=result.fetched_at,
    )


class StreamableSupplementalAdapter:
    def __init__(self, client: SupplementalMcpPort, *, enabled: bool = False) -> None:
        self.client = client
        self.enabled = enabled

    async def collect(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> SupplementalFactBatch:
        if not self.enabled:
            raise SupplementalFactsDisabled("supplemental MCP facts are disabled")
        contract = STREAMABLE_TOOL_CONTRACTS.get(tool_name)
        if contract is None or contract.status is not SupplementalContractStatus.FROZEN:
            raise SupplementalContractNotFrozen(
                f"supplemental MCP contract is not frozen: {tool_name}"
            )
        return map_streamable_result(
            tool_name,
            await self.client.call_tool(tool_name, arguments),
        )
