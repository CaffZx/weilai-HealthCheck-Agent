import json
from datetime import date
from pathlib import Path

from clients.azlisting_contract import (
    AZLISTING_ALLOWED_TOOLS,
    AZLISTING_EXTENSION_TOOL_NAMES,
    AZLISTING_FACT_TOOL_NAMES,
    AZLISTING_LIST_UNITS_BY_PRINCIPAL_TOOL,
    AZLISTING_LIST_UNITS_TOOL,
    azlisting_contract_manifest,
)
from core.operating_unit import OperatingUnitBinding
from facts.collector import TOOL_NAMES, FactCollector
from integrations.mcp_operating_units import LIST_UNITS_TOOL


def test_azlisting_allowlist_matches_all_and_only_runtime_tools():
    assert TOOL_NAMES == AZLISTING_FACT_TOOL_NAMES
    assert LIST_UNITS_TOOL == AZLISTING_LIST_UNITS_TOOL
    assert AZLISTING_ALLOWED_TOOLS == frozenset({
        LIST_UNITS_TOOL,
        AZLISTING_LIST_UNITS_BY_PRINCIPAL_TOOL,
        *TOOL_NAMES.values(),
        *AZLISTING_EXTENSION_TOOL_NAMES.values(),
    })
    assert len(AZLISTING_ALLOWED_TOOLS) == 19


def test_azlisting_machine_contract_manifest_is_current():
    path = Path(__file__).resolve().parents[2] / "contracts" / "azlisting-mcp-input.internal.v3.json"

    assert json.loads(path.read_text(encoding="utf-8")) == azlisting_contract_manifest()


def test_collector_request_fields_match_machine_contract():
    unit = OperatingUnitBinding(
        shop_id=1,
        shop_account="redacted-shop",
        site_code="US",
        parent_asin="B0PARENT",
        parent_seller_sku="PARENT-SKU",
    )
    calls = FactCollector(
        client=None,
        disabled_fact_keys=frozenset(),
    ).build_calls(unit, date(2026, 8, 4))
    manifest = azlisting_contract_manifest()

    assert {
        key: sorted(arguments)
        for key, (_, arguments) in calls.items()
    } == {
        key: sorted(contract["request_fields"])
        for key, contract in manifest["fact_tools"].items()
    }
