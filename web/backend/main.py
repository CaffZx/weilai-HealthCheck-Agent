"""本地离线测试台。全部数据来自 tests/fixtures/demo/，无需网络。
运行: python -m uvicorn web.backend.main:app --reload --port 8000

两条判定管线：
  LLM 管线：/api/judge/{key} → llm_judge.judge() → DeepSeek
  代码管线：/api/inspect/{key} → inspector_main.巡检单产品() → 本地判定
  前端同一套 renderJudgment() 渲染两条管线的结果。

【批量缓存】
  默认懒加载，按需触发（config/settings.yaml 里 startup.auto_batch_inspect 打开可启用）。
  llm 触发门槛也走 settings.yaml 的 llm.trigger 段。
"""
from __future__ import annotations
import logging
import threading
import yaml
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from core import knowledge_loader
from core import llm_judge
from data import fixture_loader
from data import local_store
from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)

app = FastAPI(title="weilai-HealthCheck-Agent · 本地测试台")

FRONT = Path(__file__).resolve().parent.parent / "frontend"
_SETTINGS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "settings.yaml"


def _load_settings() -> dict:
    """每次读，方便改配置不重启。"""
    with open(_SETTINGS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# 批量缓存：{key: {priority, score, llm_status, judgment, ...}}
# 内存字典 + JSON 磁盘持久化：进程重启后自动恢复缓存，避免重跑
# ---------------------------------------------------------------------------
_batch_cache: dict = {}
_batch_ready = threading.Event()   # 代码巡检完成
_batch_lock = threading.Lock()

_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "local" / "batch_cache.json"


def _load_batch_cache_from_disk() -> None:
    """启动时加载磁盘缓存到内存。文件不存在或损坏都静默跳过。"""
    global _batch_cache
    if not _CACHE_PATH.exists():
        return
    try:
        import json as _json
        with open(_CACHE_PATH, encoding="utf-8") as f:
            _batch_cache = _json.load(f)
        log.info("批量缓存从磁盘恢复: %d 个产品", len(_batch_cache))
        # 若磁盘有缓存，即视为"已就绪"（用户可通过 POST /api/batch/inspect 重跑）
        if _batch_cache:
            _batch_ready.set()
    except Exception as e:
        log.warning("批量缓存磁盘恢复失败（忽略）: %s", e)


def _persist_batch_cache() -> None:
    """把内存缓存原子写盘。tmp 文件 + rename 避免半写入。"""
    try:
        import json as _json
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        with _batch_lock:
            snapshot = dict(_batch_cache)
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(snapshot, f, ensure_ascii=False, default=str)
        tmp.replace(_CACHE_PATH)
    except Exception as e:
        log.warning("批量缓存磁盘持久化失败: %s", e)


# 启动即加载
_load_batch_cache_from_disk()


def _should_llm(card: dict) -> bool:
    """判断代码判定结果是否值得触发 LLM 深度分析。
    门槛来源：config/settings.yaml → llm.trigger（改数字不改代码）"""
    cfg = _load_settings().get("llm", {}).get("trigger", {})
    最低分数 = cfg.get("最低分数", 90)
    最少S0异常数 = cfg.get("最少S0异常数", 1)
    最少异常数 = cfg.get("最少异常数", 2)

    pri = card.get("优先级信息", {})
    score = pri.get("产品执行分数", 0) or 0
    anomalies = card.get("异常明细", [])
    s0_count = sum(1 for a in anomalies if (a.get("该条严重度") or "") == "S0")

    if score >= 最低分数:
        return True
    return s0_count >= 最少S0异常数 and len(anomalies) >= 最少异常数


def _run_batch_inspect():
    """后台：跑全部产品的代码巡检 → 缓存 → 对标记产品触发 LLM。"""
    global _batch_cache
    from inspector.inspector_main import 巡检单产品
    from data import local_store as store
    store.init_db()

    configs = {c["fixture_key"]: c for c in fixture_loader.load_configs()}
    smap = fixture_loader.load_shop_map()
    keys = fixture_loader.list_keys()

    log.info("批量代码巡检开始: %d 个产品", len(keys))

    llm_queue: list[tuple[str, dict]] = []  # (key, config_row)

    for i, key in enumerate(keys):
        cfg = dict(configs.get(key, {}))
        shop = smap.get(cfg.get("shop_id", ""), {})
        cfg["shop_account"] = shop.get("account", "")
        try:
            card = 巡检单产品(key=key, config_row=cfg)
            pri = card.get("优先级信息", {})
            score = pri.get("产品执行分数", 0)
            priority = pri.get("执行优先级", "P2")
            anomaly_count = len(card.get("异常明细", []))
            with _batch_lock:
                _batch_cache[key] = {
                    "priority": priority,
                    "score": score,
                    "anomaly_count": anomaly_count,
                    "llm_status": "code",        # code | llm_pending | llm_done
                    "llm_score": None,
                    "llm_priority": None,
                    "code_judgment": card,
                    "llm_judgment": None,
                }
            if _should_llm(card):
                llm_queue.append((key, cfg))
        except Exception as e:
            log.warning("代码巡检失败 %s: %s", key, e)
            with _batch_lock:
                _batch_cache[key] = {
                    "priority": "P2", "score": 0,
                    "llm_status": "error",
                    "llm_score": None, "llm_priority": None,
                    "code_judgment": None, "llm_judgment": None,
                    "error": str(e),
                }

        if (i + 1) % 20 == 0:
            log.info("  代码巡检进度: %d/%d", i + 1, len(keys))

    _batch_ready.set()
    _persist_batch_cache()
    log.info("批量代码巡检完成: %d 个产品, %d 个达 LLM 门槛", len(_batch_cache), len(llm_queue))

    # ---- 对达门槛产品自动调 LLM（串行避免打爆 API）；开关走 settings.yaml ----
    if not _load_settings().get("startup", {}).get("auto_batch_llm"):
        log.info("startup.auto_batch_llm=false，跳过批量 LLM（用户可对单产品手动 /api/judge）")
        return

    for key, cfg in llm_queue:
        try:
            with _batch_lock:
                _batch_cache[key]["llm_status"] = "llm_pending"
            bundle = fixture_loader.load_bundle(key)
            result = llm_judge.judge(cfg, bundle)
            j = result.get("judgment", {})
            llm_pri = (j.get("优先级信息") or {}).get("执行优先级", "P2")
            llm_score = (j.get("优先级信息") or {}).get("产品执行分数", 0)
            with _batch_lock:
                # 设计约定：代码判定为权威（score/priority 只来自 code_judgment）
                # LLM 只写入 llm_* 独立字段供文案展示，不覆盖排序键
                _batch_cache[key].update({
                    "llm_status": "llm_done",
                    "llm_score": llm_score,
                    "llm_priority": llm_pri,
                    "llm_judgment": j,
                })
            _persist_batch_cache()
            log.info("LLM 完成 %s: llm_priority=%s llm_score=%s (代码分不覆盖)",
                     key, llm_pri, llm_score)
        except Exception as e:
            log.warning("LLM 失败 %s: %s", key, e)
            with _batch_lock:
                _batch_cache[key]["llm_status"] = "llm_error"

    log.info("LLM 批量完成")


# 启动行为由 config/settings.yaml → startup.auto_batch_inspect 控制
# 默认不自动跑（避免 uvicorn --reload 时双开、避免上线即打爆 LLM API）
# 需要开时改配置文件；也可以调 POST /api/batch/inspect 手动触发
@app.on_event("startup")
def _maybe_auto_batch():
    settings = _load_settings().get("startup", {})
    if not settings.get("auto_batch_inspect"):
        log.info("startup.auto_batch_inspect=false，跳过启动批量巡检（如需触发：POST /api/batch/inspect）")
        _batch_ready.set()      # 让 batch_status API 返回 ready，前端不会一直转
        return
    log.info("startup.auto_batch_inspect=true，启动后台批量巡检")
    threading.Thread(target=_run_batch_inspect, daemon=True).start()


@app.post("/api/batch/inspect")
def batch_inspect_trigger():
    """手动触发批量巡检（前端按钮/运维脚本调用）。已在跑则忽略。"""
    if not _batch_ready.is_set() and _batch_cache:
        return {"status": "already_running", "processed": len(_batch_cache)}
    _batch_ready.clear()
    with _batch_lock:
        _batch_cache.clear()
    threading.Thread(target=_run_batch_inspect, daemon=True).start()
    return {"status": "triggered"}


# --- 知识库 ---
@app.get("/api/knowledge")
def kb_index():
    return {"modules": knowledge_loader.list_modules()}


@app.get("/api/knowledge/{name}")
def kb_read(name: str):
    try:
        return {"name": name, "content": knowledge_loader.read_module(name)}
    except FileNotFoundError:
        raise HTTPException(404, "module not found")


# --- ASIN 列表（含代码/LLM 结果） ---
@app.get("/api/asins")
def asins():
    """列出 70 个采样，附带 batch 缓存中的优先级/得分/LLM状态。"""
    configs = {c["fixture_key"]: c for c in fixture_loader.load_configs()}
    smap = fixture_loader.load_shop_map()

    # 走 local_store 抽象查主图 URL（不直连 sqlite）
    _img_map = local_store.query_image_urls()

    out = []
    for key in fixture_loader.list_keys():
        cfg = configs.get(key, {})
        shop = smap.get(cfg.get("shop_id", ""), {})
        parent_asin = cfg.get("parent_asin", "")

        cached = _batch_cache.get(key, {})
        score = cached.get("score", 0)
        priority = cached.get("priority", "")
        llm_status = cached.get("llm_status", "pending")
        anomaly_count = cached.get("anomaly_count", 0)

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
            # 批量结果
            "priority": priority,
            "score": score,
            "llm_status": llm_status,
            "llm_score": cached.get("llm_score"),
            "anomaly_count": anomaly_count,
            "batch_ready": _batch_ready.is_set(),
        })
    return out


@app.get("/api/fixture/{key}")
def fixture(key: str):
    if key not in fixture_loader.list_keys():
        raise HTTPException(404, "unknown fixture key")
    return {"key": key, "data": fixture_loader.load_bundle(key)}


# --- 判定引擎 ---
def _config_by_key(key: str) -> dict | None:
    for c in fixture_loader.load_configs():
        if c["fixture_key"] == key:
            smap = fixture_loader.load_shop_map().get(c["shop_id"], {})
            c["shop_account"] = smap.get("account")
            return c
    return None


@app.get("/api/batch/status")
def batch_status():
    """查询批量处理进度。"""
    with _batch_lock:
        total = len(_batch_cache)
        llm_done = sum(1 for v in _batch_cache.values() if v.get("llm_status") == "llm_done")
        llm_pending = sum(1 for v in _batch_cache.values() if v.get("llm_status") in ("llm_pending", "llm_error"))
    return {
        "ready": _batch_ready.is_set(),
        "total": total,
        "llm_done": llm_done,
        "llm_pending": llm_pending,
    }


@app.post("/api/judge/{key}")
def judge(key: str):
    cfg = _config_by_key(key)
    if not cfg:
        raise HTTPException(404, "unknown key")
    bundle = fixture_loader.load_bundle(key)
    result = llm_judge.judge(cfg, bundle)
    # 更新缓存
    j = result.get("judgment", {})
    llm_pri = (j.get("优先级信息") or {}).get("执行优先级", "")
    llm_score = (j.get("优先级信息") or {}).get("产品执行分数", 0)
    with _batch_lock:
        if key in _batch_cache:
            # 设计约定：LLM 不覆盖 code 的 score/priority；仅写入独立字段
            _batch_cache[key].update({
                "llm_status": "llm_done",
                "llm_score": llm_score,
                "llm_priority": llm_pri,
                "llm_judgment": j,
            })
    _persist_batch_cache()
    return result


# --- 代码判定管线 ---
@app.post("/api/inspect/{key}")
def inspect_single(key: str):
    """代码巡检单产品：优先返回批量缓存，无缓存时实时跑管线。"""
    # 批量缓存命中 → 秒返
    cached = _batch_cache.get(key, {})
    if cached.get("code_judgment"):
        return {
            "dry_run": False,
            "model": "code-pipeline",
            "usage": None,
            "judgment": cached["code_judgment"],
        }

    cfg = _config_by_key(key)
    if not cfg:
        raise HTTPException(404, "unknown key")
    try:
        from inspector.inspector_main import 巡检单产品
        from data import local_store as store
        store.init_db()
        card = 巡检单产品(key=key, config_row=cfg)
        pri = card.get("优先级信息", {})
        with _batch_lock:
            _batch_cache[key] = {
                "priority": pri.get("执行优先级", "P2"),
                "score": pri.get("产品执行分数", 0),
                "llm_status": "code",
                "llm_score": None,
                "llm_priority": None,
                "code_judgment": card,
                "llm_judgment": None,
            }
        _persist_batch_cache()
        return {
            "dry_run": False,
            "model": "code-pipeline",
            "usage": None,
            "judgment": card,
        }
    except Exception as e:
        log.exception("代码巡检失败 key=%s", key)
        raise HTTPException(500, f"代码巡检失败: {e}")


@app.post("/api/inspect/batch")
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
            "dry_run": False,
            "model": "code-pipeline",
            "usage": None,
            "batch": True,
            "count": len(cards),
            "cards": cards,
        }
    except Exception as e:
        log.exception("代码批量巡检失败")
        raise HTTPException(500, f"代码批量巡检失败: {e}")


# --- 前端静态资源 ---
if FRONT.exists():
    app.mount("/ui", StaticFiles(directory=str(FRONT), html=True), name="frontend")


@app.get("/")
def root():
    idx = FRONT / "index.html"
    return FileResponse(idx) if idx.exists() else {"ok": True}
