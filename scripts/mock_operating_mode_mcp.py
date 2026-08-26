from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from clients.operating_mode_mcp import (
    CurrentOperatingMode,
    CurrentOperatingModeResult,
    CurrentOperatingModesResult,
    OperatingModeKey,
)

ROOT = Path(__file__).resolve().parent.parent
MOCK_PATH = ROOT / "contracts/examples/operating-mode-mcp.v1.mock.json"


def load_lookup_records(path: Path = MOCK_PATH) -> dict[tuple[str, str, str], Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("isSimulated") is not True:
        raise ValueError("operating mode mock data must be marked as simulated")
    scenarios = payload["getCurrentOperatingMode"]
    records: dict[tuple[str, str, str], Any] = {}
    for scenario in scenarios.values():
        arguments = scenario["arguments"]
        key = (
            str(arguments["shop_id"]).strip(),
            str(arguments["parent_asin"]).strip().upper(),
            str(arguments["parent_seller_sku"]).strip(),
        )
        if key in records:
            raise ValueError("operating mode mock contains a duplicate business key")
        records[key] = scenario["result"]
    return records


def create_mock_operating_mode_server(
    records: dict[tuple[str, str, str], Any] | None = None,
) -> MCPServer:
    lookup_records = records or load_lookup_records()
    server = MCPServer(
        "amazon-operating-mode-agent-mock",
        version="1.0.0-mock",
        instructions="仅用于离线合同联调；所有结果均为模拟数据，不连接数据库。",
    )

    @server.tool(
        name="get_current_operating_mode",
        description="按经营单元业务键返回固定模拟经营模式；不会执行判断或写入数据库",
        structured_output=True,
    )
    async def get_current_operating_mode(
        shop_id: str,
        parent_asin: str,
        parent_seller_sku: str,
    ) -> CurrentOperatingModeResult:
        key = (shop_id.strip(), parent_asin.strip().upper(), parent_seller_sku.strip())
        result = lookup_records.get(key)
        current = CurrentOperatingMode.model_validate(result) if result is not None else None
        return CurrentOperatingModeResult(result=current)

    @server.tool(
        name="get_current_operating_modes",
        description="按经营单元业务键批量返回固定模拟经营模式",
        structured_output=True,
    )
    async def get_current_operating_modes(
        operating_units: list[OperatingModeKey],
    ) -> CurrentOperatingModesResult:
        results = []
        missing = []
        for unit in operating_units:
            key = (unit.shop_id, unit.parent_asin.upper(), unit.parent_seller_sku)
            result = lookup_records.get(key)
            if result is None:
                missing.append(unit)
            else:
                results.append(CurrentOperatingMode.model_validate(result))
        return CurrentOperatingModesResult(
            requested_count=len(operating_units),
            found_count=len(results),
            missing_count=len(missing),
            results=results,
            missing_operating_units=missing,
        )

    return server


def main() -> None:
    server = create_mock_operating_mode_server()
    server.run(
        "streamable-http",
        host=os.getenv("OPERATING_MODE_MOCK_HOST", "127.0.0.1"),
        port=int(os.getenv("OPERATING_MODE_MOCK_PORT", "8801")),
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
