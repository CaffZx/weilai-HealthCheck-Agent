"""判定引擎接口：/api/inspect/{key}, /api/inspect/batch, /api/judge/{key}, /api/batch/status, /api/batch/inspect"""
from __future__ import annotations
import logging
import threading

from fastapi import APIRouter, HTTPException, Query

from core import llm_judge
from data import fixture_loader
from .. import batch_cache
from ..common import config_by_key

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


@router.get("/batch/status")
def batch_status():
    cache = batch_cache.get_cache()
    with batch_cache.lock:
        total = len(cache)
        llm_done = sum(1 for v in cache.values() if v.get("llm_status") == "llm_done")
        llm_pending = sum(1 for v in cache.values() if v.get("llm_status") in ("llm_pending", "llm_error"))
    return {
        "ready": batch_cache.ready.is_set(),
        "total": total,
        "llm_done": llm_done,
        "llm_pending": llm_pending,
    }


@router.post("/batch/inspect")
def batch_inspect_trigger():
    """手动触发批量巡检。已在跑则忽略。"""
    cache = batch_cache.get_cache()
    if not batch_cache.ready.is_set() and cache:
        return {"status": "already_running", "processed": len(cache)}
    batch_cache.ready.clear()
    batch_cache.clear()
    threading.Thread(target=batch_cache.run_batch_inspect, daemon=True).start()
    return {"status": "triggered"}


@router.post("/judge/{key}")
def judge(key: str):
    """LLM 二次分析：从批量缓存拿代码巡检结果作为权威事实。"""
    cfg = config_by_key(key)
    if not cfg:
        raise HTTPException(404, "unknown key")
    bundle = fixture_loader.load_bundle(key)

    cache = batch_cache.get_cache()
    code_j = None
    cached = cache.get(key, {})
    if cached.get("code_judgment"):
        code_j = cached["code_judgment"]
    else:
        try:
            from inspector.inspector_main import 巡检单产品
            from data import local_store as store
            store.init_db()
            code_j = 巡检单产品(key=key, config_row=cfg)
            batch_cache.update_entry(key, {
                "priority": (code_j.get("优先级信息") or {}).get("执行优先级", "P2"),
                "score": (code_j.get("优先级信息") or {}).get("产品执行分数", 0),
                "anomaly_count": len(code_j.get("异常明细") or []),
                "code_judgment": code_j,
                "llm_status": cached.get("llm_status", "code"),
            })
        except Exception as e:
            log.warning("LLM 二次分析前跑代码巡检失败 key=%s: %s", key, e)

    result = llm_judge.judge(cfg, bundle, code_judgment=code_j)
    j = result.get("judgment", {})
    llm_pri = (j.get("优先级信息") or {}).get("执行优先级", "")
    llm_score = (j.get("优先级信息") or {}).get("产品执行分数", 0)
    if key in cache:
        # LLM 不覆盖 code 的 score/priority；仅写入独立字段
        batch_cache.update_entry(key, {
            "llm_status": "llm_done",
            "llm_score": llm_score,
            "llm_priority": llm_pri,
            "llm_judgment": j,
        })
    batch_cache.persist()
    return result


@router.post("/inspect/{key}")
def inspect_single(key: str):
    """代码巡检单产品：优先返回批量缓存，无缓存时实时跑。"""
    cache = batch_cache.get_cache()
    cached = cache.get(key, {})
    if cached.get("code_judgment"):
        return {
            "dry_run": False, "model": "code-pipeline", "usage": None,
            "judgment": cached["code_judgment"],
        }

    cfg = config_by_key(key)
    if not cfg:
        raise HTTPException(404, "unknown key")
    try:
        from inspector.inspector_main import 巡检单产品
        from data import local_store as store
        store.init_db()
        card = 巡检单产品(key=key, config_row=cfg)
        pri = card.get("优先级信息", {})
        batch_cache.set_entry(key, {
            "priority": pri.get("执行优先级", "P2"),
            "score": pri.get("产品执行分数", 0),
            "llm_status": "code",
            "llm_score": None,
            "llm_priority": None,
            "code_judgment": card,
            "llm_judgment": None,
        })
        batch_cache.persist()
        return {"dry_run": False, "model": "code-pipeline", "usage": None, "judgment": card}
    except Exception as e:
        log.exception("代码巡检失败 key=%s", key)
        raise HTTPException(500, f"代码巡检失败: {e}")


@router.post("/inspect/batch")
def inspect_batch(
    scope: str = Query("all", description="all | filter | top"),
    limit: int = Query(10, ge=1, le=70),
    q: str = Query("", description="搜索关键词（scope=filter时生效）"),
):
    """代码巡检批量：返回任务卡列表，按产品执行分数降序。"""
    try:
        from inspector.inspector_main import 巡检批量
        from data import local_store as store
        store.init_db()

        configs = {c["fixture_key"]: c for c in fixture_loader.load_configs()}
        smap = fixture_loader.load_shop_map()
        产品列表 = []
        for k in fixture_loader.list_keys():
            cfg = dict(configs.get(k, {}))
            shop = smap.get(cfg.get("shop_id", ""), {})
            cfg["shop_account"] = shop.get("account", "")
            if scope == "filter" and q:
                hay = " ".join(str(v) for v in cfg.values() if v).lower()
                if q.lower() not in hay:
                    continue
            产品列表.append({"key": k, "config": cfg, "mcp_bundle": None})
            if len(产品列表) >= limit:
                break

        cards = 巡检批量(产品列表, 批次号=None)
        return {
            "dry_run": False, "model": "code-pipeline", "usage": None,
            "batch": True, "count": len(cards), "cards": cards,
        }
    except Exception as e:
        log.exception("代码批量巡检失败")
        raise HTTPException(500, f"代码批量巡检失败: {e}")
