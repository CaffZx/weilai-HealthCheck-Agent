from __future__ import annotations

import hashlib
import os
from typing import Any

from mcp.server import MCPServer

from core.control_center_contracts import (
    SubmitPatrolBatchRequest,
    SubmitPatrolBatchResult,
)


def create_mock_control_center_server() -> MCPServer:
    server = MCPServer(
        "control-center-mock",
        version="2.0.0-mock",
        instructions="仅用于本地合同联调；校验巡检批次后返回模拟接收回执。",
    )

    @server.tool(
        name="submit_patrol_batch",
        description="校验并接收模拟巡检批次，不连接真实中控数据库",
        structured_output=True,
    )
    async def submit_patrol_batch(
        patrolBatchNo: str,
        units: list[dict[str, Any]],
    ) -> SubmitPatrolBatchResult:
        request = SubmitPatrolBatchRequest.model_validate(
            {"patrolBatchNo": patrolBatchNo, "units": units}
        )
        results = []
        for unit in request.units:
            shop_id, parent_asin, parent_seller_sku = unit.business_key
            digest = hashlib.sha256(
                "\x1f".join(unit.business_key).encode("utf-8")
            ).hexdigest()[:16]
            results.append(
                {
                    "shopId": shop_id,
                    "parentAsin": parent_asin,
                    "parentSellerSku": parent_seller_sku,
                    "status": "SUCCESS",
                    "code": "MOCK_ACCEPTED",
                    "listingId": f"SIM-LISTING-{digest}",
                    "proposalId": f"SIM-PROPOSAL-{digest}",
                    "message": "模拟中控已通过正式合同校验并接收巡检结果",
                }
            )
        return SubmitPatrolBatchResult.model_validate(
            {
                "patrolBatchNo": request.patrol_batch_no,
                "receivedCount": len(results),
                "successCount": len(results),
                "partialSuccessCount": 0,
                "failedCount": 0,
                "results": results,
            }
        )

    return server


def main() -> None:
    server = create_mock_control_center_server()
    server.run(
        "streamable-http",
        host=os.getenv("CONTROL_CENTER_MOCK_HOST", "127.0.0.1"),
        port=int(os.getenv("CONTROL_CENTER_MOCK_PORT", "8757")),
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
