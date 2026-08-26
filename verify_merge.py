"""验证：子 ASIN 异常合并为经营单元级。

对真实快照重建 OperatingFactSnapshot，重跑 LegacyRuleAdapter().inspect，
对比 DB 里旧的按子 ASIN 输出的信号数 vs 新合并后的信号数，打印每个点位的命中子体。
"""
from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

load_dotenv("/opt/weilai-HealthCheck-Agent-v2.0/.env")
sys.path.insert(0, "/opt/weilai-HealthCheck-Agent-v2.0")

from core.contracts import (
    DataGap,
    OperatingFactSnapshot,
    OperatingUnitRef,
    SourceRef,
)
from core.enums import DataQualityStatus
from inspector.legacy_rule_adapter import LegacyRuleAdapter
from scripts.submit_real_patrol_to_control_center import _load_snapshot
from web.backend.deps import get_database_engine

TARGET_OU = "ou_717ccd497bae7e38a94b49a8"


def _reconstruct(snapshot_dict: dict) -> OperatingFactSnapshot:
    norm = snapshot_dict.get("normalized_summary_json") or {}
    identity = norm.get("identity") or {}
    unit = OperatingUnitRef.derive(
        shop_id=identity.get("shop_id"),
        site_code=identity.get("site_code"),
        parent_asin=identity.get("parent_asin"),
        parent_seller_sku=identity.get("parent_seller_sku"),
    )
    return OperatingFactSnapshot(
        snapshot_id=snapshot_dict["snapshot_id"],
        operating_unit=unit,
        as_of_time=snapshot_dict["as_of"],
        content_hash=snapshot_dict["content_hash"],
        quality_status=DataQualityStatus(snapshot_dict["quality_status"]),
        completeness_score=float(snapshot_dict["completeness_score"] or 0),
        identity=identity,
        sales=norm.get("sales") or {},
        traffic=norm.get("traffic") or {},
        profit=norm.get("profit") or {},
        inventory=norm.get("inventory") or {},
        price=norm.get("price") or {},
        quality=norm.get("quality") or {},
        execution_history=norm.get("execution_history") or {},
        source_refs=[SourceRef.model_validate(r) for r in (snapshot_dict.get("source_refs_json") or [])],
        data_gaps=[DataGap.model_validate(g) for g in (snapshot_dict.get("data_gaps_json") or [])],
    )


def main() -> int:
    engine = get_database_engine()
    with engine.connect() as conn:
        row = conn.execute(
            __import__("sqlalchemy").text(
                "SELECT run_id FROM t_patrol_fact_snapshot "
                "WHERE operating_unit_id=:ou ORDER BY created_at DESC LIMIT 1"
            ),
            {"ou": TARGET_OU},
        ).mappings().one()
        snapshot_dict = _load_snapshot(conn, row["run_id"])

        old_count = conn.execute(
            __import__("sqlalchemy").text(
                "SELECT COUNT(*) FROM t_patrol_signal "
                "WHERE operating_unit_id=:ou AND signal_type='ANOMALY' "
                "AND issue_code IN ('ATTRIBUTE_MISMATCH','APLUS_CONTENT_ABNORMAL')"
            ),
            {"ou": TARGET_OU},
        ).scalar()

    snapshot = _reconstruct(snapshot_dict)
    merged = LegacyRuleAdapter().inspect(snapshot)

    print(f"目标经营单元: {TARGET_OU}")
    print(f"父 ASIN: {snapshot.operating_unit.parent_asin}")
    print(f"DB 旧信号数(属性+A+): {old_count}")
    print(f"新合并后信号数: {len(merged)}")
    print()
    print("=== 合并后的异常（按点位，命中子体数）===")
    for s in merged:
        if s.signal_type.value == "ANOMALY":
            print(f"  {s.point_code:12s} target={s.target_type:11s} "
                  f"命中子体数={len(s.child_asins)}")
            if s.child_asins:
                print(f"    子体: {', '.join(s.child_asins[:5])}{'...' if len(s.child_asins) > 5 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
