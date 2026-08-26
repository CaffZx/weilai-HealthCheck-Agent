from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from clients.mcp_client import McpToolResult, request_hash
from core.errors import McpContractInvalid
from core.operating_unit import derive_operating_unit_id, normalize_identity
from facts.collector import CORE_KEYS, TOOL_NAMES
from facts.contract_validator import fact_contract_errors
from facts.normalizer import FactNormalizer
from facts.quality import FactQualityService
from integrations.mcp_operating_units import LIST_UNITS_TOOL, McpOperatingUnitProvider

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "contracts" / "mcp-input-acceptance-sample.v1.schema.json"
CONTRACT_VERSION = "amazon_ops.mcp_input_acceptance.v1"
CRITICAL_NORMALIZED_FIELDS = (
    "identity.children",
    "identity.storefront.source_asin",
    "sales.daily_rows",
    "inventory.fba_available",
)


class SampleMcpClient:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = {int(page["pageNo"]): page for page in pages}

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> McpToolResult:
        if tool_name != LIST_UNITS_TOOL:
            raise McpContractInvalid(f"unexpected sample tool: {tool_name}")
        page_no = int(arguments["pageNo"])
        if page_no not in self.pages:
            raise McpContractInvalid(f"sample is missing operating-unit page {page_no}")
        page = self.pages[page_no]
        return McpToolResult(
            tool_name=tool_name,
            data=[page],
            raw={"structuredContent": {"data": [page]}},
            request_hash=request_hash(tool_name, arguments),
        )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpContractInvalid(f"sample cannot be read: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise McpContractInvalid("sample root must be an object")
    return payload


def _schema_errors(payload: dict[str, Any]) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        f"{'.'.join(str(item) for item in error.absolute_path) or '$'}: {error.message}"
        for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    ]


def _sample_unit_id(identity: dict[str, Any]) -> str:
    shop_id, _, parent_asin, parent_seller_sku = normalize_identity(
        identity.get("shopId"),
        identity.get("siteCode"),
        identity.get("parentAsin"),
        identity.get("parentSellerSku"),
    )
    return derive_operating_unit_id(shop_id, parent_asin, parent_seller_sku)


def _fact_results(samples: dict[str, list[dict[str, Any]]]) -> dict[str, McpToolResult]:
    return {
        key: McpToolResult(
            tool_name=tool_name,
            data=samples[tool_name],
            raw={"structuredContent": {"data": samples[tool_name]}},
            request_hash=request_hash(tool_name, {"acceptance_sample": True}),
        )
        for key, tool_name in TOOL_NAMES.items()
        if tool_name in samples
    }


def _core_fact_errors(
    unit,
    raw: dict[str, McpToolResult],
    normalized: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    for key in sorted(CORE_KEYS):
        if raw[key].is_empty:
            errors.append(f"CORE_FACT_EMPTY:{TOOL_NAMES[key]}")
    if normalized.get("inventory", {}).get("fba_available") is None:
        errors.append("CORE_FIELD_MISSING:inventory.fba_available")
    if not normalized.get("sales", {}).get("daily_rows"):
        errors.append("CORE_FIELD_MISSING:sales.daily_rows")
    return errors


def _field_coverage(normalized: dict[str, Any]) -> dict[str, bool]:
    identity = normalized.get("identity", {})
    return {
        "identity.children": bool(identity.get("children")),
        "identity.storefront.source_asin": bool(
            identity.get("storefront", {}).get("source_asin")
        ),
        "sales.daily_rows": bool(normalized.get("sales", {}).get("daily_rows")),
        "inventory.fba_available": (
            normalized.get("inventory", {}).get("fba_available") is not None
        ),
    }


async def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    schema_errors = _schema_errors(payload)
    if schema_errors:
        return {
            "status": "REJECTED",
            "contract_version": payload.get("contract_version"),
            "errors": [f"SCHEMA:{message}" for message in schema_errors],
        }

    pages = payload["operating_unit_pages"]
    page_size = int(pages[0]["pageSize"])
    try:
        units = await McpOperatingUnitProvider(
            SampleMcpClient(pages),
            page_size=page_size,
        ).list_all_active_units()
        sample_unit_id = _sample_unit_id(payload["fact_sample_identity"])
    except (McpContractInvalid, ValueError) as exc:
        return {
            "status": "REJECTED",
            "contract_version": CONTRACT_VERSION,
            "errors": [f"OPERATING_UNIT_CONTRACT:{exc}"],
        }

    selected = next(
        (unit for unit in units if unit.operating_unit_id == sample_unit_id),
        None,
    )
    if selected is None:
        return {
            "status": "REJECTED",
            "contract_version": CONTRACT_VERSION,
            "active_operating_unit_count": len(units),
            "errors": ["FACT_SAMPLE_UNIT_NOT_FOUND"],
        }

    inspection_date = date.fromisoformat(payload["as_of"])
    raw = _fact_results(payload["fact_samples"])
    contract_errors = fact_contract_errors(selected, raw)
    if contract_errors:
        return {
            "status": "REJECTED",
            "contract_version": CONTRACT_VERSION,
            "active_operating_unit_count": len(units),
            "sample_operating_unit_id": selected.operating_unit_id,
            "errors": contract_errors,
        }
    normalized, source_refs = FactNormalizer().normalize(
        selected,
        raw,
        as_of=inspection_date,
    )
    gaps = FactQualityService().check(selected, raw, normalized)
    core_errors = _core_fact_errors(selected, raw, normalized)
    blocking_gaps = [gap for gap in gaps if gap.blocking]
    errors = (
        core_errors
        + [f"BLOCKING_GAP:{gap.code}:{gap.field}" for gap in blocking_gaps]
    )
    unresolved = normalized.get("_meta", {}).get("unresolved_fields", [])
    field_coverage = _field_coverage(normalized)
    if tuple(field_coverage) != CRITICAL_NORMALIZED_FIELDS:
        raise RuntimeError("critical MCP acceptance fields are out of sync")
    status = "REJECTED" if errors else ("ACCEPTED_WITH_GAPS" if gaps else "ACCEPTED")
    return {
        "status": status,
        "contract_version": CONTRACT_VERSION,
        "inspection_date": inspection_date.isoformat(),
        "as_of": normalized["_meta"]["as_of"],
        "window_start": normalized["_meta"]["window_start"],
        "window_end": normalized["_meta"]["window_end"],
        "active_operating_unit_count": len(units),
        "sample_operating_unit_id": selected.operating_unit_id,
        "validated_fact_tools": sorted(result.tool_name for result in raw.values()),
        "source_ref_count": len(source_refs),
        "critical_field_coverage": field_coverage,
        "blocking_gap_count": len(blocking_gaps),
        "non_blocking_gaps": [
            {
                "code": gap.code,
                "field": gap.field,
                "source_tool": gap.source_tool,
            }
            for gap in gaps
            if not gap.blocking
        ],
        "unresolved_fields": unresolved,
        "errors": errors,
    }


def validate_file(path: Path) -> dict[str, Any]:
    try:
        payload = _load_json(path)
    except McpContractInvalid as exc:
        return {
            "status": "REJECTED",
            "contract_version": None,
            "errors": [str(exc)],
        }
    return asyncio.run(validate_payload(payload))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a redacted MCP input sample")
    parser.add_argument("sample", type=Path)
    args = parser.parse_args()
    result = validate_file(args.sample)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result["status"] == "REJECTED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
