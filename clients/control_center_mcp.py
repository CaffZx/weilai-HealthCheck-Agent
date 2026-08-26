from __future__ import annotations

import json
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from core.control_center_contracts import SubmitPatrolBatchRequest, SubmitPatrolBatchResult
from core.errors import ControlCenterUnavailable


class ControlCenterContractError(ValueError):
    pass


def _structured_content(result: Any) -> dict[str, Any]:
    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
    if payload.get("isError") is True or payload.get("is_error") is True:
        raise ControlCenterContractError("control center rejected submit_patrol_batch")
    structured = payload.get("structuredContent") or payload.get("structured_content")
    if isinstance(structured, dict):
        return structured
    for item in payload.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                parsed = json.loads(item.get("text") or "{}")
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise ControlCenterContractError("submit_patrol_batch returned no structured result")


class ControlCenterPatrolMcpClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_seconds: float = 15,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not url.strip():
            raise ValueError("control center MCP URL is required")
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            headers={
                "Accept": "application/json, text/event-stream",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
            timeout=httpx.Timeout(timeout_seconds, read=timeout_seconds * 2),
        )

    async def aclose(self) -> None:
        if self._owns_client and not self.client.is_closed:
            await self.client.aclose()

    async def submit_patrol_batch(
        self,
        request: SubmitPatrolBatchRequest,
    ) -> SubmitPatrolBatchResult:
        try:
            async with streamable_http_client(self.url, http_client=self.client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "submit_patrol_batch",
                        request.model_dump(mode="json", by_alias=True),
                    )
        except Exception as exc:
            raise ControlCenterUnavailable("control center MCP unavailable") from exc
        response = SubmitPatrolBatchResult.model_validate(_structured_content(result))
        if response.patrol_batch_no != request.patrol_batch_no:
            raise ControlCenterContractError("control center returned another patrol batch")
        requested_keys = {unit.business_key for unit in request.units}
        received_keys = {
            (item.shop_id, item.parent_asin, item.parent_seller_sku)
            for item in response.results
        }
        if received_keys != requested_keys:
            raise ControlCenterContractError(
                "control center receipt business keys do not match the request"
            )
        return response
