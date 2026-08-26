"""在已有 fact_snapshot 上重跑规则，验证「评分异常」接入（不重采集、不重投递）。"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text

from core.contracts import OperatingFactSnapshot, OperatingUnitRef
from core.enums import SignalType
from integrations.database import database_url
from inspector.legacy_rule_adapter import LegacyRuleAdapter

# 默认：今天 00:00 CST 起的巡检 run（UTC 前一天 16:00）
WINDOW_START = sys.argv[1] if len(sys.argv) > 1 else "2026-08-23 16:00:00"
SAMPLE_LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 0

NORM_KEYS = (
    "identity", "sales", "traffic", "profit", "inventory",
    "price", "quality", "execution_history",
)

adapter = LegacyRuleAdapter(
    child_points_enabled=True,
    keyword_rank_points_enabled=False,
    compliance_points_enabled=False,
)


def _parse_norm(raw) -> dict:
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw or {})


def _build_snapshot(row, norm: dict) -> OperatingFactSnapshot | None:
    unit = OperatingUnitRef.derive(
        shop_id=int(row["shop_id"]),
        site_code=row["site_code"],
        parent_asin=row["parent_asin"],
        parent_seller_sku=row["parent_seller_sku"],
    )
    payload = {
        "snapshot_id": row["snapshot_id"],
        "operating_unit": unit.model_dump(mode="json"),
        "as_of_time": datetime.now(UTC).isoformat(),
        "content_hash": "sha256:verify00000000000000000000000000000000000000000000000000",
        "quality_status": row["quality_status"] or "COMPLETE",
        "completeness_score": 1.0,
        "source_refs": [],
        "data_gaps": [],
        **{k: norm.get(k, {}) for k in NORM_KEYS},
    }
    try:
        return OperatingFactSnapshot.model_validate(payload)
    except Exception:
        return None


def main() -> int:
    engine = create_engine(database_url(), pool_pre_ping=True)
    q = """
        SELECT r.run_id, r.operating_unit_id, s.normalized_summary_json, s.snapshot_id,
               s.quality_status, c.site_code, c.parent_asin, c.shop_id, c.parent_seller_sku
        FROM t_patrol_run r
        JOIN t_patrol_fact_snapshot s ON s.run_id = r.run_id
        JOIN t_patrol_operating_unit_catalog c ON c.operating_unit_id = r.operating_unit_id
        WHERE r.started_at >= :w
        ORDER BY r.started_at
    """
    if SAMPLE_LIMIT:
        q += f" LIMIT {SAMPLE_LIMIT}"

    stats = Counter()
    samples: list[str] = []
    skipped = 0

    with engine.connect() as conn:
        rows = conn.execute(text(q), {"w": WINDOW_START}).mappings().all()

    for row in rows:
        norm = _parse_norm(row["normalized_summary_json"])
        quality = norm.get("quality") or {}
        star = quality.get("star_rating")
        target = quality.get("target_star_rating")
        if star is None:
            stats["no_star_rating"] += 1
        if target is None:
            stats["no_target_star"] += 1
        if star is not None and target is not None:
            stats["both_present"] += 1
            if star < target:
                stats["would_be_below_target"] += 1

        snapshot = _build_snapshot(row, norm)
        if snapshot is None:
            skipped += 1
            continue

        signals = adapter.inspect(snapshot)
        rating_hits = [
            s for s in signals
            if s.point_code == "评分异常" and s.signal_type == SignalType.ANOMALY
        ]
        if rating_hits:
            stats["rating_anomaly_signals"] += len(rating_hits)
            hit = rating_hits[0]
            if len(samples) < 10:
                samples.append(
                    f"{row['operating_unit_id']} asin={row['parent_asin']} "
                    f"star={star} target={target} sev={hit.severity}"
                )
        stats["runs_checked"] += 1

    print(f"window_start_utc={WINDOW_START}")
    print(f"runs_checked={stats['runs_checked']} skipped_build={skipped}")
    print(f"no_star_rating={stats['no_star_rating']} no_target={stats['no_target_star']}")
    print(f"both_present={stats['both_present']} below_target={stats['would_be_below_target']}")
    print(f"rating_anomaly_signals={stats['rating_anomaly_signals']}")
    if samples:
        print("samples:")
        for line in samples:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
