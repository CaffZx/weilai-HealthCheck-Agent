"""按 catalog 每个经营单元的完整参数逐条精确查询 erp_amazon_listing_query_page 拿负责人，命中即落库。
低并发 + 调用间隔 + 429 限流自动等待，避免触发上游限流。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, "/opt/weilai-HealthCheck-Agent-v2.0")

import sqlalchemy as sa
from clients.mcp_client import ReusableMcpSessionClient
from integrations.database import create_database_engine
from integrations.repositories.tables import patrol_operating_unit_catalog

_RATE_LIMIT_RE = re.compile(r"请\s*(\d+(?:\.\d+)?)\s*(MS|毫秒|秒|S|SECONDS|SECOND)", re.IGNORECASE)


def _rate_limit_wait_seconds(message: str) -> float | None:
    m = _RATE_LIMIT_RE.search(message or "")
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).upper()
    if unit in ("MS",):
        return value / 1000.0
    return value


async def run(*, limit: int, offset: int, concurrency: int, min_interval: float, max_retries: int, apply: bool) -> dict:
    token = os.environ["MCP_API_KEY"]
    gateway = os.environ.get("AZLISTING_GATEWAY", "http://mcp-gateway.example.com/mcp")
    engine = create_database_engine()

    with engine.connect() as connection:
        query = sa.select(
            patrol_operating_unit_catalog.c.operating_unit_id,
            patrol_operating_unit_catalog.c.shop_id,
            patrol_operating_unit_catalog.c.shop_account_ref,
            patrol_operating_unit_catalog.c.parent_asin,
            patrol_operating_unit_catalog.c.parent_seller_sku,
            patrol_operating_unit_catalog.c.owner_user_ids_json,
        ).order_by(patrol_operating_unit_catalog.c.operating_unit_id)
        if offset:
            query = query.offset(offset)
        if limit:
            query = query.limit(limit)
        rows = connection.execute(query).mappings().all()

    units = [dict(r) for r in rows]
    print(f"selected units: {len(units)} (limit={limit} offset={offset} concurrency={concurrency} interval={min_interval}s)", flush=True)

    results: dict[str, set[int]] = {}
    stats = Counter()
    t0 = time.monotonic()
    lock = asyncio.Lock()
    _last_call_at = 0.0

    async def throttle() -> None:
        nonlocal _last_call_at
        async with lock:
            elapsed = time.monotonic() - _last_call_at
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
            _last_call_at = time.monotonic()

    async def probe(unit: dict) -> None:
        args = {
            "shopAccount": unit["shop_account_ref"] or "",
            "asin": unit["parent_asin"],
            "sellerSku": unit["parent_seller_sku"] or "",
            "pageNo": 1,
            "pageSize": 20,
        }
        last_err = ""
        for attempt in range(1, max_retries + 1):
            await throttle()
            try:
                r = await client.call_tool("erp_amazon_listing_query_page", args)
                recs = r.data[0].get("records", []) if r.data else []
                uid = None
                for rec in recs:
                    v = rec.get("userId")
                    if v not in (None, "", 0):
                        uid = int(v)
                        break
                if uid:
                    async with lock:
                        results.setdefault(unit["operating_unit_id"], set()).add(uid)
                        stats["with_owner"] += 1
                else:
                    async with lock:
                        stats["no_owner"] += 1
                return
            except Exception as exc:
                last_err = str(exc)
                wait = _rate_limit_wait_seconds(last_err) or 2.0 * (2 ** (attempt - 1))
                if "429" in last_err or "rate" in last_err.lower() or "Server returned an error" in last_err:
                    async with lock:
                        stats["retry"] += 1
                    await asyncio.sleep(min(wait, 30))
                    continue
                # 非限流错误：立即重试一次后放弃
                if attempt < max_retries:
                    await asyncio.sleep(wait)
                    continue
                async with lock:
                    stats["error"] += 1
                    if stats["error"] <= 10:
                        print(f"  probe error {unit['operating_unit_id']}: {last_err[:160]}", flush=True)

    client = ReusableMcpSessionClient(gateway, token, timeout_seconds=90)
    sem = asyncio.Semaphore(concurrency)
    async def guarded(unit: dict) -> None:
        async with sem:
            await probe(unit)

    async with client:
        await asyncio.gather(*(guarded(u) for u in units))

    elapsed = time.monotonic() - t0
    print(f"elapsed={elapsed:.1f}s stats={dict(stats)}", flush=True)

    if not apply:
        return {"selected": len(units), "stats": dict(stats), "elapsed_s": round(elapsed, 1), "applied": False}

    updated = 0
    with engine.begin() as connection:
        for unit in units:
            owners = results.get(unit["operating_unit_id"])
            if not owners:
                continue
            existing = {
                int(v) for v in (unit["owner_user_ids_json"] or []) if str(v).isdigit() and int(v) > 0
            }
            merged = sorted(existing | owners)
            if merged == sorted(existing):
                continue
            connection.execute(
                sa.update(patrol_operating_unit_catalog)
                .where(patrol_operating_unit_catalog.c.operating_unit_id == unit["operating_unit_id"])
                .values(owner_user_ids_json=merged)
            )
            updated += 1
    print(f"updated catalog rows: {updated}", flush=True)
    return {"selected": len(units), "stats": dict(stats), "updated": updated, "elapsed_s": round(elapsed, 1), "applied": True}


def main() -> int:
    parser = argparse.ArgumentParser(description="Precise per-unit owner backfill (low concurrency)")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--min-interval", type=float, default=1.2)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(
        limit=args.limit, offset=args.offset, concurrency=args.concurrency,
        min_interval=args.min_interval, max_retries=args.max_retries, apply=args.apply,
    ))
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
