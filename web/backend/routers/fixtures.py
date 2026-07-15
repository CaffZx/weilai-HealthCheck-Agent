"""产品 fixture & 数据健康接口：/api/asins  /api/fixture/{key}  /api/sync-health"""
from __future__ import annotations
import json
import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Response

from data import fixture_loader, local_store
from .. import batch_cache
from ..common import ROOT

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


@router.get("/sync-health")
def sync_health():
    """返回最近一次 check_coverage 生成的健康度报告。"""
    p = ROOT / "logs" / "health-report-latest.json"
    if not p.exists():
        return {"status": "no_report", "hint": "尚未执行过 python -m data.check_coverage"}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"status": "read_error", "error": str(e)}


@router.get("/asins")
def asins(userId: int | None = Query(None, description="按负责人 ID 过滤")):
    """列出产品采样，附带 batch 缓存中的优先级/得分/LLM状态。"""
    owned_asins: set[str] | None = None
    if userId is not None:
        try:
            with sqlite3.connect(local_store.DB_PATH) as c:
                rows = c.execute("""
                    SELECT DISTINCT asin FROM asin_owner
                    WHERE COALESCE(principal_user_id, editor_id, NULLIF(creator_id,-1)) = ?
                """, (userId,)).fetchall()
            owned_asins = {r[0] for r in rows}
        except Exception as e:
            log.warning("/api/asins userId 过滤失败: %s", e)
            owned_asins = set()

    configs = {c["fixture_key"]: c for c in fixture_loader.load_configs()}
    smap = fixture_loader.load_shop_map()
    _img_map = local_store.query_image_urls()
    _name_map = local_store.query_product_names()
    cache = batch_cache.get_cache()

    out = []
    for key in fixture_loader.list_keys():
        cfg = configs.get(key, {})
        shop = smap.get(cfg.get("shop_id", ""), {})
        parent_asin = cfg.get("parent_asin", "")
        if owned_asins is not None and parent_asin not in owned_asins:
            continue

        cached = cache.get(key, {})

        # 抽出真异常的 (问题点位, 命中变体)
        anomaly_points: list[dict] = []
        config_pending_count = 0
        for a in ((cached.get("code_judgment") or {}).get("异常明细") or []):
            if a.get("该条严重度") == "配置待补":
                config_pending_count += 1
                continue
            anomaly_points.append({
                "问题点位": a.get("问题点位", ""),
                "命中变体": a.get("命中变体", "") or "",
                "异常类型": a.get("异常类型", ""),
            })

        out.append({
            "key": key,
            "parent_asin": parent_asin,
            "parent_seller_sku": cfg.get("parent_seller_sku"),
            "shop_id": cfg.get("shop_id"),
            "shop_account": shop.get("account"),
            "site_code": cfg.get("site_code"),
            "product_stage": cfg.get("product_stage"),
            "product_position": cfg.get("product_position"),
            "target_acos_suggest": cfg.get("target_acos_suggest"),
            "daily_budget_suggest": cfg.get("daily_budget_suggest"),
            "image_url": _img_map.get(parent_asin),
            "product_name": _name_map.get(parent_asin),
            "anomaly_points": anomaly_points,
            "config_pending_count": config_pending_count,
            "last_inspect_at": ((cached.get("code_judgment") or {}).get("判定时间") or None),
            "priority": cached.get("priority", ""),
            "score": cached.get("score", 0),
            "llm_status": cached.get("llm_status", "pending"),
            "llm_score": cached.get("llm_score"),
            "anomaly_count": cached.get("anomaly_count", 0),
            "batch_ready": batch_cache.ready.is_set(),
        })
    return out


@router.get("/fixture/{key}")
def fixture(key: str, response: Response):
    if key not in fixture_loader.list_keys():
        raise HTTPException(404, "unknown fixture key")
    response.headers["Cache-Control"] = "private, max-age=300, stale-while-revalidate=600"
    return {"key": key, "data": fixture_loader.load_bundle(key)}
