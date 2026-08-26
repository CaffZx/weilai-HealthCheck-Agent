"""公网 MCP 反向代理的 Bearer Token 校验入口。"""
from __future__ import annotations

from fastapi import APIRouter, Header, Response

from ..review_mcp_access import require_review_mcp_access

router = APIRouter(prefix="/api/internal", include_in_schema=False)


@router.get("/mcp-auth", status_code=204)
def verify_mcp_token(authorization: str | None = Header(default=None)) -> Response:
    """仅供 Nginx auth_request 子请求调用，校验复盘 MCP 独立令牌。"""
    require_review_mcp_access(authorization)
    return Response(status_code=204)
