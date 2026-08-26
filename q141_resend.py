# -*- coding: utf-8 -*-
"""补投 8-23/8-24 被 control_center_delivery_enabled=false 跳过的 CC 批次。
机制：对每个被 skip 的 batch_id 重新调用 _deliver_to_control_center →
publisher.enqueue → 写 outbox PENDING → 常驻 worker 自动投递到中控。
幂等：中控按 batch_id-P<part> 去重；被 skip 的批次从未投过，无重复风险。
"""
import asyncio
import sys
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path("/opt/weilai-HealthCheck-Agent-v2.0")
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlalchemy.engine import create_engine
from integrations.database import database_url
from scripts.run_real_patrol_batch import _deliver_to_control_center

CUTOFF = "2026-08-22 16:00:00"  # 8-23 00:05 CST 定时轮起（含 8-24 轮）


async def main() -> int:
    eng = create_engine(database_url(), pool_pre_ping=True)
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT batch_id, COUNT(DISTINCT CONCAT(shop_id, '|', parent_asin, '|', parent_seller_sku)) AS units "
            "FROM t_patrol_job "
            "WHERE created_at >= :cut AND batch_id LIKE 'batch_%' "
            "GROUP BY batch_id ORDER BY MIN(created_at)"
        ), {"cut": CUTOFF}).fetchall()
    total = len(rows)
    print(f"待补投批次: {total}")
    if total == 0:
        return 0

    sem = asyncio.Semaphore(8)
    ok, fail = 0, []

    async def one(batch_id: str, units: int):
        nonlocal ok
        async with sem:
            try:
                ids = await _deliver_to_control_center(batch_id, units)
                ok += 1
                print(f"OK   {batch_id} units={units} outbox={len(ids)}", flush=True)
            except Exception as exc:
                fail.append((batch_id, str(exc)[:140]))
                print(f"FAIL {batch_id} {str(exc)[:140]}", flush=True)

    await asyncio.gather(*(one(r[0], r[1]) for r in rows))
    print(f"\n成功: {ok} / {total}  失败: {len(fail)}")
    for b, e in fail:
        print("  ", b, e)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
