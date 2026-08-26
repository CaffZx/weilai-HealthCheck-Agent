"""模拟周一判定：用定时巡检快照统计子体数据可产生的异常。"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, UTC
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text

from core.contracts import OperatingFactSnapshot, OperatingUnitRef
from core.enums import SignalType
from integrations.database import database_url
from inspector.legacy_rule_adapter import (
    LegacyRuleAdapter,
    _detect_crawler_points,
    _detect_runtime_facts,
    _merge_child_scope_signals,
    build_aggregate,
    build_legacy_snapshot_dict,
)

W = sys.argv[1] if len(sys.argv) > 1 else "2026-08-21 16:00:00"
SAMPLE = int(sys.argv[2]) if len(sys.argv) > 2 else 0

engine = create_engine(database_url(), pool_pre_ping=True)

adapter = LegacyRuleAdapter(
    child_points_enabled=True,
    keyword_rank_points_enabled=False,
    compliance_points_enabled=False,
)

NORM_KEYS = (
    "identity", "sales", "traffic", "profit", "inventory",
    "price", "quality", "execution_history",
)


def load_runs(limit: int = 0):
    q = """
    SELECT r.run_id, r.operating_unit_id, s.normalized_summary_json, s.snapshot_id,
           s.quality_status, c.site_code, c.parent_asin, c.shop_id, c.parent_seller_sku
    FROM t_patrol_run r
    JOIN t_patrol_fact_snapshot s ON s.run_id = r.run_id
    JOIN t_patrol_operating_unit_catalog c ON c.operating_unit_id = r.operating_unit_id
    WHERE r.started_at >= :w
    ORDER BY r.started_at DESC
    """
    if limit:
        q += f" LIMIT {limit}"
    with engine.connect() as conn:
        return conn.execute(text(q), {"w": W}).mappings().all()


def parse_norm(raw) -> dict:
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw or {})


def has_child_frontend(norm: dict) -> bool:
    children = (norm.get("identity") or {}).get("children") or []
    return any((c.get("frontend") or {}).get("evaluated") for c in children)


def has_price_promo(norm: dict) -> bool:
    children = (norm.get("identity") or {}).get("children") or []
    return any((c.get("price_promotion") or {}).get("evaluated") for c in children)


def build_snapshot(row, norm: dict) -> OperatingFactSnapshot | None:
    unit = OperatingUnitRef.derive(
        shop_id=int(row["shop_id"]),
        site_code=row["site_code"],
        parent_asin=row["parent_asin"],
        parent_seller_sku=row["parent_seller_sku"],
    )
    snap_json = {
        "snapshot_id": row["snapshot_id"],
        "operating_unit": unit.model_dump(mode="json"),
        "as_of_time": datetime.now(UTC).isoformat(),
        "content_hash": "sha256:simulated0000000000000000000000000000000000000000000000000000",
        "quality_status": row["quality_status"] or "COMPLETE",
        "completeness_score": 1.0,
        "source_refs": [],
        "data_gaps": [],
        **{k: norm.get(k, {}) for k in NORM_KEYS},
    }
    try:
        return OperatingFactSnapshot.model_validate(snap_json)
    except Exception:
        return None


def inspect_monday(snapshot: OperatingFactSnapshot) -> list:
    """等同周一：ERP 库存/合规 + 爬虫类前台点位 + R3 表现型。"""
    legacy = build_legacy_snapshot_dict(snapshot)
    aggregate = build_aggregate(snapshot)
    erp_hits = _detect_runtime_facts(
        legacy,
        aggregate,
        adapter.r2_config,
        adapter.r3_config,
        compliance_points_enabled=False,
    )
    crawler_hits = _detect_crawler_points(
        legacy, aggregate, adapter.r2_config, adapter.r3_config
    )
    # 合并 R2 命中并走 inspect 同款过滤
    from inspector.issue_codes import issue_code_for
    from inspector.legacy_rule_adapter import SEVERITY_MAP
    from core.enums import Severity, SignalState
    from core.contracts import AnomalySignal
    from inspector.engine import anomaly_detector as R2mod
    from uuid import uuid4

    signals = []
    for hit in [*erp_hits, *crawler_hits]:
        point = getattr(hit, "问题点位", None)
        if not point:
            continue
        child = getattr(hit, "命中变体", None)
        severity = SEVERITY_MAP.get(
            getattr(hit, "默认严重度", None) or "", Severity.DATA_INSUFFICIENT,
        )
        signals.append(AnomalySignal(
            signal_id=f"is_{uuid4().hex[:24]}",
            category=adapter._category(point),
            point_code=point,
            issue_code=issue_code_for(point),
            target_type="CHILD_ASIN" if child else "PARENT_ASIN",
            target_id=child or snapshot.operating_unit.parent_asin,
            child_asins=[child] if child else [],
            severity=severity,
            signal_type=SignalType.ANOMALY,
            description=getattr(hit, "命中依据", "") or point,
            reason=getattr(hit, "命中依据", "") or "",
            metrics={},
            evidence_refs=[snapshot.snapshot_id],
            lifecycle_status=SignalState.NEW,
            detected_by="R2",
        ))
    signals.extend(adapter._run_performance(snapshot, aggregate))
    unavailable = set(adapter.unavailable_points({
        "identity": snapshot.identity,
        "price": snapshot.price,
        "quality": snapshot.quality,
        "traffic": snapshot.traffic,
    }))
    filtered = [s for s in signals if s.point_code not in unavailable]
    return _merge_child_scope_signals(filtered, snapshot.operating_unit.parent_asin)


def main() -> None:
    runs = load_runs(SAMPLE)
    print(f"定时轮成功 run 数: {len(runs)}")

    hits = Counter()
    severity = Counter()
    unavailable_agg = Counter()
    units_with_frontend = 0
    units_with_promo = 0
    units_any_anomaly = 0
    crawler_only = Counter()
    sample_hits: dict[str, list[str]] = defaultdict(list)

    for row in runs:
        norm = parse_norm(row["normalized_summary_json"])
        if has_child_frontend(norm):
            units_with_frontend += 1
        if has_price_promo(norm):
            units_with_promo += 1
        for p in adapter.unavailable_points(norm):
            unavailable_agg[p] += 1

        snapshot = build_snapshot(row, norm)
        if snapshot is None:
            continue

        try:
            legacy = build_legacy_snapshot_dict(snapshot)
            agg = build_aggregate(snapshot)
            for h in _detect_crawler_points(
                legacy, agg, adapter.r2_config, adapter.r3_config
            ):
                crawler_only[h.问题点位] += 1
        except Exception:
            pass

        unit_signals = inspect_monday(snapshot)
        if unit_signals:
            units_any_anomaly += 1
        for sig in unit_signals:
            if sig.signal_type != SignalType.ANOMALY:
                continue
            hits[sig.point_code] += 1
            sev = sig.severity.value if hasattr(sig.severity, "value") else str(sig.severity)
            severity[sev] += 1
            if len(sample_hits[sig.point_code]) < 2:
                sample_hits[sig.point_code].append(row["parent_asin"])

    print("\n=== 子体数据覆盖（快照内） ===")
    print(f"  child_detail evaluated: {units_with_frontend}/{len(runs)}")
    print(f"  price_promotion evaluated: {units_with_promo}/{len(runs)}")

    print("\n=== 不可判点位（单元累计） ===")
    for p, n in unavailable_agg.most_common(15):
        print(f"  {p}: {n}")

    print("\n=== 模拟周一巡检异常产出（R2+R3，已过滤不可判） ===")
    print(f"  有异常单元: {units_any_anomaly}/{len(runs)}")
    for pt, n in hits.most_common(30):
        samples = ", ".join(sample_hits[pt])
        print(f"  {pt}: {n}  样例={samples}")

    print("\n=== 严重度 ===")
    for s, n in severity.most_common():
        print(f"  {s}: {n}")

    print("\n=== R2 爬虫类原始命中（过滤前） ===")
    for pt, n in crawler_only.most_common(20):
        print(f"  {pt}: {n}")


if __name__ == "__main__":
    main()
