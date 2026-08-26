"""公网 /mcp 的鉴权与流式反向代理。"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from ..review_mcp_access import require_review_mcp_access

router = APIRouter(include_in_schema=False)

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}

@router.api_route("/mcp", methods=["GET", "POST", "DELETE"])
async def proxy_mcp(request: Request):
    """将中控复盘请求转发给仅本机监听的 V2 复盘 MCP 服务。"""
    require_review_mcp_access(request.headers.get("authorization"))
    upstream_url = os.getenv("REVIEW_MCP_UPSTREAM_URL", "http://127.0.0.1:8791/mcp")
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP | {"host", "authorization", "content-length"}
    }
    client = httpx.AsyncClient(timeout=None)
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                upstream_url,
                headers=headers,
                content=await request.body(),
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=503, detail="MCP service is unavailable") from exc

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _HOP_BY_HOP | {"content-length"}
    }

    async def close_upstream() -> None:
        await upstream.aclose()
        await client.aclose()

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=response_headers,
        background=BackgroundTask(close_upstream),
    )
