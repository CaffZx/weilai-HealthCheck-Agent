"""补抓子体扩展事实：分批选单元 → 重置 DEAD → product_info 入队 → 监控 worker。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text

from core.operating_unit import OperatingUnitBinding
from facts.collector import DEFAULT_DISABLED_FACT_KEYS, FactCollector
from integrations.database import database_url
from web.backend.deps import (
    _storefront_zip_codes,
    get_child_fact_service,
    get_mcp_client,
    get_supplemental_mcp_adapter,
    load_settings,
)


def _select_units(count: int, offset: int, *, all_units: bool) -> list[dict]:
    engine = create_engine(database_url(), pool_pre_ping=True)
    missing_filter = ""
    if not all_units:
        missing_filter = """
          AND NOT EXISTS (
            SELECT 1 FROM t_patrol_child_fact_snapshot s
            WHERE s.operating_unit_id = c.operating_unit_id
              AND s.domain = 'child_detail'
              AND s.expires_at >= NOW()
          )
        """
    q = text(f"""
        SELECT c.operating_unit_id, c.shop_id, c.site_code, c.parent_asin,
               c.parent_seller_sku, c.shop_account_ref, c.owner_user_ids_json
        FROM t_patrol_operating_unit_catalog c
        WHERE c.site_code IN ('AMAZON_US', 'AMAZON_UK', 'AMAZON_DE')
        {missing_filter}
        ORDER BY c.operating_unit_id
        LIMIT :limit OFFSET :offset
    """)
    with engine.connect() as conn:
        rows = conn.execute(q, {"limit": count, "offset": offset}).mappings().all()
    return [dict(r) for r in rows]


def _reset_dead_tasks(unit_ids: list[str]) -> int:
    if not unit_ids:
        return 0
    engine = create_engine(database_url(), pool_pre_ping=True)
    placeholders = ", ".join(f":u{i}" for i in range(len(unit_ids)))
    params: dict = {f"u{i}": uid for i, uid in enumerate(unit_ids)}
    q = text(f"""
        UPDATE t_patrol_child_fact_task
        SET status = 'PENDING', retry_count = 0, next_attempt_at = UTC_TIMESTAMP(),
            locked_by = NULL, locked_at = NULL,
            last_error_code = NULL, last_error_message = NULL,
            updated_at = UTC_TIMESTAMP()
        WHERE operating_unit_id IN ({placeholders})
          AND status = 'DEAD'
          AND domain IN ('child_detail', 'price_promotion')
    """)
    with engine.begin() as conn:
        return conn.execute(q, params).rowcount or 0


def _task_stats(unit_ids: list[str], since_utc: str) -> dict[str, int]:
    if not unit_ids:
        return {}
    engine = create_engine(database_url(), pool_pre_ping=True)
    placeholders = ", ".join(f":u{i}" for i in range(len(unit_ids)))
    params: dict = {f"u{i}": uid for i, uid in enumerate(unit_ids)}
    params["since"] = since_utc
    q = text(f"""
        SELECT status, COUNT(*) cnt
        FROM t_patrol_child_fact_task
        WHERE operating_unit_id IN ({placeholders})
          AND domain IN ('child_detail', 'price_promotion')
          AND updated_at >= :since
        GROUP BY status
    """)
    with engine.connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _quality_report(unit_ids: list[str], since_utc: str) -> None:
    engine = create_engine(database_url(), pool_pre_ping=True)
    placeholders = ", ".join(f":u{i}" for i in range(len(unit_ids)))
    params: dict = {f"u{i}": uid for i, uid in enumerate(unit_ids)}
    params["since"] = since_utc
    q = text(f"""
        SELECT response_json FROM t_patrol_child_fact_snapshot
        WHERE operating_unit_id IN ({placeholders})
          AND domain = 'child_detail'
          AND fetched_at >= :since
    """)
    with engine.connect() as conn:
        rows = conn.execute(q, params).fetchall()
    ok = empty_shell = usable = 0
    for (resp,) in rows:
        ok += 1
        try:
            row = (
                json.loads(json.loads(str(resp))["content"][0]["text"]).get("data") or [{}]
            )[0]
            bonus = row.get("bonus") or {}
            imgs = row.get("images") or {}
            has_title = bool(bonus.get("title"))
            has_main = bool(imgs.get("main"))
            has_link = (
                isinstance(row.get("linkStatus"), dict)
                and row["linkStatus"].get("inStock") is not None
            )
            if not has_title and not has_main and not has_link:
                empty_shell += 1
            else:
                usable += 1
        except Exception:
            empty_shell += 1
    total = len(rows)
    if not total:
        print("  质量: 无新 child_detail 快照")
        return
    print(
        f"  质量: 快照={total} 可用(有title/main/link)={usable} ({100*usable/total:.1f}%) "
        f"空壳={empty_shell} ({100*empty_shell/total:.1f}%)"
    )


def _binding(row: dict) -> OperatingUnitBinding:
    owners = row.get("owner_user_ids_json") or []
    if isinstance(owners, str):
        owners = json.loads(owners)
    owner_ids = tuple(int(x) for x in owners if x and int(x) > 0)
    if not owner_ids:
        raise ValueError("owner_user_ids empty")
    return OperatingUnitBinding(
        shop_id=int(row["shop_id"]),
        shop_account=str(row["shop_account_ref"]),
        site_code=str(row["site_code"]),
        parent_asin=str(row["parent_asin"]),
        parent_seller_sku=str(row["parent_seller_sku"]),
        owner_user_ids=owner_ids,
    )


async def _enqueue_unit(
    row: dict,
    collector: FactCollector,
    child_service,
    sem: asyncio.Semaphore,
    as_of: date,
    *,
    force: bool,
) -> tuple[str, int, str | None]:
    async with sem:
        try:
            binding = _binding(row)
            all_calls = collector.build_calls(binding, as_of)
            if "product_info" not in all_calls:
                return row["operating_unit_id"], 0, "product_info unavailable"
            raw = await collector.collect(
                binding, as_of, calls={"product_info": all_calls["product_info"]},
            )
            enqueued = child_service.enqueue_missing(
                binding, collector, raw.get("product_info"), as_of, force=force,
            )
            return row["operating_unit_id"], enqueued, None
        except Exception as exc:
            return row["operating_unit_id"], 0, f"{type(exc).__name__}: {exc}"


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    if not settings.get("feature_flags", {}).get("child_fact_enabled", True):
        print("child_fact_enabled=false，请先打开开关")
        return 1

    units = _select_units(args.count, args.offset, all_units=args.all_units)
    if not units:
        print("没有符合条件的单元")
        return 1
    unit_ids = [u["operating_unit_id"] for u in units]
    label = args.batch_name or f"batch@{args.offset}"
    mode = "全量强制" if args.all_units and args.force else "缺快照补抓"
    print(f"[{label}] 选中 {len(units)} 单元 ({mode}, offset={args.offset})")

    dead_reset = _reset_dead_tasks(unit_ids)
    print(f"[{label}] DEAD→PENDING 重置: {dead_reset} 条")

    ff = settings.get("feature_flags", {})
    inspection = settings.get("inspection", {})
    collector = FactCollector(
        get_mcp_client(),
        lookback_days=int(inspection.get("default_lookback_days", 30)),
        storefront_zip_codes=_storefront_zip_codes(),
        supplemental_adapter=get_supplemental_mcp_adapter(),
        disabled_fact_keys=frozenset(
            inspection.get("disabled_fact_keys") or DEFAULT_DISABLED_FACT_KEYS
        ),
        child_ext_enabled=bool(ff.get("child_fact_enabled", True)),
        keyword_rank_enabled=bool(ff.get("keyword_rank_enabled", True)),
        compliance_enabled=bool(ff.get("compliance_enabled", True)),
    )
    child_service = get_child_fact_service()
    as_of = date.today()
    sem = asyncio.Semaphore(args.concurrency)

    since_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    t0 = time.time()
    results = await asyncio.gather(*[
        _enqueue_unit(u, collector, child_service, sem, as_of, force=args.force)
        for u in units
    ])
    enqueue_sec = time.time() - t0
    total_enqueued = sum(r[1] for r in results)
    errors = [r for r in results if r[2]]
    zero_enqueue = sum(1 for r in results if r[1] == 0 and not r[2])
    print(
        f"[{label}] 入队完成: {enqueue_sec:.1f}s, 任务 {total_enqueued} 条, "
        f"失败 {len(errors)}, 零入队 {zero_enqueue}"
    )

    if args.no_wait:
        print(f"[{label}] (--no-wait) worker 后台消费, since_utc={since_utc}")
        return 0

    deadline = time.time() + args.wait_seconds
    while time.time() < deadline:
        stats = _task_stats(unit_ids, since_utc)
        pending = stats.get("PENDING", 0) + stats.get("RUNNING", 0)
        succeeded = stats.get("SUCCEEDED", 0)
        dead = stats.get("DEAD", 0)
        elapsed = time.time() - t0
        print(
            f"[{label}][{elapsed:.0f}s] PENDING/RUNNING={pending} "
            f"SUCCEEDED={succeeded} DEAD={dead}",
            flush=True,
        )
        if total_enqueued > 0 and pending == 0 and succeeded > 0:
            rate = succeeded / elapsed if elapsed > 0 else 0
            print(f"[{label}] 完成: {succeeded} 成功, {elapsed:.0f}s, {rate:.1f} 条/s")
            _quality_report(unit_ids, since_utc)
            return 0 if dead == 0 or dead / max(succeeded, 1) < 0.01 else 1
        if pending == 0 and total_enqueued == 0:
            break
        await asyncio.sleep(args.poll_interval)

    print(f"[{label}] 超时或未完成, since_utc={since_utc}")
    _quality_report(unit_ids, since_utc)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="补抓子体扩展事实（分批）")
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--wait-seconds", type=int, default=7200)
    parser.add_argument("--poll-interval", type=float, default=30.0)
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--all-units", action="store_true", help="不限缺快照，选 catalog 单元")
    parser.add_argument("--force", action="store_true", help="忽略新鲜快照，强制重入队")
    parser.add_argument("--batch-name", default="")
    args = parser.parse_args()
    if args.count < 1 or args.count > 10000:
        raise SystemExit("count must be 1..10000")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
