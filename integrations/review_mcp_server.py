from __future__ import annotations

import os
from collections.abc import Callable

from mcp.server import MCPServer

from core.control_center_contracts import ReviewRequest, ReviewResult
from integrations.review import ReviewService


def create_review_mcp_server(
    service: ReviewService,
    *,
    enabled: Callable[[], bool] | None = None,
) -> MCPServer:
    runtime_enabled = enabled or (
        lambda: os.getenv("REVIEW_MCP_ENABLED", "").strip().lower()
        in {"1", "true", "yes"}
    )
    server = MCPServer(
        "weilai-healthcheck-review",
        version="2.0.0",
        instructions=(
            "中控只提交复盘任务上下文；巡检 Agent 自行读取基线、调用事实 MCP、"
            "完成复盘并直接返回最终结果。"
        ),
    )

    @server.tool(
        name="review_patrol_result",
        description="根据巡检侧 MySQL 基线与实时 MCP 事实执行复盘并同步返回结果",
        structured_output=True,
    )
    async def review_patrol_result(payload: ReviewRequest) -> ReviewResult:
        if not runtime_enabled():
            raise RuntimeError("review MCP is disabled before the integration contract is frozen")
        return await service.review(payload)

    return server


def main() -> None:
    from web.backend.deps import get_review_service, load_settings

    def enabled() -> bool:
        feature_enabled = bool(
            load_settings().get("feature_flags", {}).get("review_mcp_enabled")
        )
        environment_enabled = os.getenv("REVIEW_MCP_ENABLED", "").strip().lower() in {
            "1", "true", "yes",
        }
        return feature_enabled and environment_enabled

    server = create_review_mcp_server(get_review_service(), enabled=enabled)
    server.run(
        "streamable-http",
        host=os.getenv("REVIEW_MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("REVIEW_MCP_PORT", "8791")),
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
