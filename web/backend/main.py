"""FastAPI 后端 —— 数据源为本地 SQLite（sync_all 抓 MCP 落库）。
运行: python -m uvicorn web.backend.main:app --host 0.0.0.0 --port 8000

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
from fastapi import FastAPI, HTTPException, Query, Response, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import datetime as _dt
import json as _json_module

import sqlite3

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
# 缓存是产品级的（key = parent_asin__shop_id），不同用户可能共享同一产品缓存。
# 用户隔离由 /api/asins 的 userId 过滤保证，不在缓存层实现。
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


# --- 数据抓取健康度 ---
@app.get("/api/sync-health")
def sync_health():
    """返回最近一次 check_coverage 生成的健康度报告（logs/health-report-latest.json）。
    没有报告时返回 {status: 'no_report'}。"""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(__file__).resolve().parent.parent.parent / "logs" / "health-report-latest.json"
    if not p.exists():
        return {"status": "no_report", "hint": "尚未执行过 python -m data.check_coverage"}
    try:
        with open(p, encoding="utf-8") as f:
            return _json.load(f)
    except Exception as e:
        return {"status": "read_error", "error": str(e)}


# --- 负责人列表 ---
@app.get("/api/users")
def get_users(response: Response):
    """返回有 ASIN 的负责人清单，供前端下拉框用。
    加 5 分钟浏览器缓存 → iframe 重建时不再重复请求。"""
    response.headers["Cache-Control"] = "private, max-age=300, stale-while-revalidate=600"
    try:
        with sqlite3.connect(local_store.DB_PATH) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("""
                SELECT
                    COALESCE(o.principal_user_id, o.editor_id, NULLIF(o.creator_id,-1)) AS user_id,
                    u.user_name,
                    u.user_account,
                    COUNT(DISTINCT o.asin) AS asin_count
                FROM asin_owner o
                LEFT JOIN sys_user u
                  ON u.id = COALESCE(o.principal_user_id, o.editor_id, NULLIF(o.creator_id,-1))
                WHERE user_id IS NOT NULL AND u.user_name IS NOT NULL
                GROUP BY user_id, u.user_name, u.user_account
                ORDER BY asin_count DESC
            """).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("/api/users 查询失败（表可能未初始化）: %s", e)
        return []


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
def asins(userId: int | None = Query(None, description="按负责人 ID 过滤")):
    """列出产品采样，附带 batch 缓存中的优先级/得分/LLM状态。userId 传入时按负责人过滤。"""
    # 负责人过滤：查出该 userId 拥有的 asin 集合
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
            owned_asins = set()  # 失败时返回空集，不暴露其他用户数据

    configs = {c["fixture_key"]: c for c in fixture_loader.load_configs()}
    smap = fixture_loader.load_shop_map()

    # 走 local_store 抽象查主图 URL 和 商品标题（不直连 sqlite）
    _img_map = local_store.query_image_urls()
    _name_map = local_store.query_product_names()

    out = []
    for key in fixture_loader.list_keys():
        cfg = configs.get(key, {})
        shop = smap.get(cfg.get("shop_id", ""), {})
        parent_asin = cfg.get("parent_asin", "")

        # 负责人过滤
        if owned_asins is not None and parent_asin not in owned_asins:
            continue

        cached = _batch_cache.get(key, {})
        score = cached.get("score", 0)
        priority = cached.get("priority", "")
        llm_status = cached.get("llm_status", "pending")
        anomaly_count = cached.get("anomaly_count", 0)

        # 抽出真异常的 (问题点位, 命中变体) 供前端聚合处理状态
        # 配置待补 是运营配置提示，不算真异常；单独计数供前端标"配置待补"徽章
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
def fixture(key: str, response: Response):
    if key not in fixture_loader.list_keys():
        raise HTTPException(404, "unknown fixture key")
    # 5 分钟浏览器缓存 → iframe 切 tab 回来时命中 disk cache，不再发请求
    response.headers["Cache-Control"] = "private, max-age=300, stale-while-revalidate=600"
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

    # 二次分析：从批量缓存拿代码巡检结果传给 LLM，作为权威事实
    # 如果缓存里没有，先跑一次代码巡检
    code_j = None
    cached = _batch_cache.get(key, {})
    if cached.get("code_judgment"):
        code_j = cached["code_judgment"]
    else:
        try:
            from inspector.inspector_main import 巡检单产品
            from data import local_store as store
            store.init_db()
            code_j = 巡检单产品(key=key, config_row=cfg)
            with _batch_lock:
                _batch_cache[key] = _batch_cache.get(key, {})
                _batch_cache[key].update({
                    "priority": (code_j.get("优先级信息") or {}).get("执行优先级", "P2"),
                    "score": (code_j.get("优先级信息") or {}).get("产品执行分数", 0),
                    "anomaly_count": len(code_j.get("异常明细") or []),
                    "code_judgment": code_j,
                    "llm_status": cached.get("llm_status", "code"),
                })
        except Exception as e:
            log.warning("LLM 二次分析前跑代码巡检失败 key=%s: %s", key, e)

    result = llm_judge.judge(cfg, bundle, code_judgment=code_j)
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


# ============================================================
# 运营工作台 API（Phase 1）
# ============================================================

_MANAGER_MAP_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "manager_map.yaml"

def _load_manager_map() -> dict[int, list[int]]:
    """{主管 user_id: [下属 user_id]}。每次读，改配置不重启。"""
    if not _MANAGER_MAP_PATH.exists():
        return {}
    try:
        with open(_MANAGER_MAP_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return {int(k): [int(x) for x in (v or [])] for k, v in raw.items()}
    except Exception as e:
        log.warning("manager_map.yaml 解析失败: %s", e)
        return {}


_SEV_TO_PRIORITY = {"S0": "P0", "S1": "P1", "S2": "P2"}
_POSITION_TO_TIER = {"P0_PRODUCT": "T0", "P1_PRODUCT": "T1", "P2_PRODUCT": "T2", "P3_PRODUCT": "T3"}
_TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "": 4}
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


def _days_since(iso_ts: str | None) -> int:
    if not iso_ts:
        return 0
    try:
        d = _dt.datetime.fromisoformat(iso_ts).date()
        return max(0, (_dt.date.today() - d).days)
    except Exception:
        return 0


def _resolve_target_user(viewer_id: int | None, target_id: int | None) -> int | None:
    """主管可以查看下属；普通运营只能查自己；target_id=None 表示查自己。"""
    if viewer_id is None:
        return None
    if target_id is None or target_id == viewer_id:
        return viewer_id
    reports = _load_manager_map().get(viewer_id, [])
    if target_id in reports:
        return target_id
    # 权限不足，回退到查自己
    return viewer_id


def _fetch_events_for_user(user_id: int, only_open: bool = True) -> list[dict]:
    """拉出该用户名下所有事件，附带产品定位/持续天数/最新 action 状态。"""
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT e.*
            FROM event_pool e
            WHERE e.父ASIN IN (
                SELECT DISTINCT asin FROM asin_owner
                WHERE COALESCE(principal_user_id, editor_id, NULLIF(creator_id,-1)) = ?
            )
        """, (user_id,)).fetchall()

        # 产品定位（父ASIN → position）
        pos_rows = c.execute("SELECT parent_asin, product_position FROM erp_config").fetchall()
        pos_map = {r["parent_asin"]: r["product_position"] for r in pos_rows}

    name_map = local_store.query_product_names()
    img_map = local_store.query_image_urls()
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row

        # 最新 action per event_uid
        act_rows = c.execute("""
            SELECT event_uid, action_type, result, actual_action, review_at, notes, effect, created_at
            FROM task_action
            WHERE id IN (SELECT MAX(id) FROM task_action GROUP BY event_uid)
        """).fetchall()
        act_map = {r["event_uid"]: dict(r) for r in act_rows}

    out = []
    for r in rows:
        d = dict(r)
        uid = d["唯一识别"]
        latest_action = act_map.get(uid)
        # 状态：优先看 task_action，否则用 event_pool.当前状态
        status = d.get("当前状态") or "新发现"
        if latest_action:
            if latest_action.get("action_type") == "完成":
                status = "已完成"
            elif latest_action.get("action_type") == "不处理":
                status = "已关闭"
            elif latest_action.get("action_type") == "标记处理中":
                status = "处理中"
            elif latest_action.get("action_type") == "待复查":
                status = "待复查"
        if only_open and status in ("已关闭", "已完成"):
            continue

        sev = d.get("严重度") or "S2"
        position = pos_map.get(d.get("父ASIN"), "") or ""
        tier = _POSITION_TO_TIER.get(position, "")
        priority = _SEV_TO_PRIORITY.get(sev, "P2")
        days = _days_since(d.get("首次命中时间"))
        try:
            judge_basis = _json_module.loads(d.get("判定依据") or "{}")
        except Exception:
            judge_basis = {"命中依据": d.get("判定依据") or ""}

        out.append({
            "event_uid": uid,
            "parent_asin": d.get("父ASIN"),
            "product_name": name_map.get(d.get("父ASIN")),
            "image_url": img_map.get(d.get("父ASIN")),
            "parent_sku": d.get("父SKU"),
            "shop_account": d.get("店铺账号"),
            "site": d.get("站点"),
            "category": d.get("异常大类") or "其他",
            "issue": d.get("问题点位"),
            "scope": d.get("作用层级"),
            "variant": d.get("命中变体"),
            "variant_importance": d.get("变体重要性"),
            "severity": sev,
            "priority": priority,
            "score": d.get("单异常执行分数") or 0,
            "position": position,
            "tier": tier,
            "days": days,
            "status": status,
            "first_seen": d.get("首次命中时间"),
            "last_seen": d.get("最近命中时间"),
            "last_action": d.get("上次处理动作"),
            "last_action_at": d.get("上次处理时间"),
            "last_action_by": d.get("上次处理人"),
            "judge_basis": judge_basis,
            "latest_action": latest_action,
        })
    return out


@app.get("/api/reports")
def api_reports(userId: int = Query(..., description="登录人 user_id")):
    """返回登录人的角色 + 下属列表（仅主管有下属）。"""
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        me = c.execute("SELECT id, user_name FROM sys_user WHERE id=?", (userId,)).fetchone()
        if not me:
            return {"self": None, "role": "unknown", "reports": []}
        mm = _load_manager_map()
        report_ids = mm.get(userId, [])
        reports = []
        if report_ids:
            qs = ",".join("?" * len(report_ids))
            rrows = c.execute(f"SELECT id, user_name FROM sys_user WHERE id IN ({qs})", report_ids).fetchall()
            reports = [dict(r) for r in rrows]
        return {
            "self": dict(me),
            "role": "manager" if report_ids else "operator",
            "reports": reports,
        }


@app.get("/api/tasks/today")
def api_tasks_today(
    userId: int = Query(..., description="登录人 user_id"),
    targetId: int | None = Query(None, description="主管查看下属时传下属 user_id"),
):
    """今日任务池：该用户名下所有开放事件，按 P0→P2 / score↓ / T0→T3 / days↓ 排序。"""
    target = _resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    events = _fetch_events_for_user(target, only_open=True)
    events.sort(key=lambda e: (
        _PRIORITY_ORDER.get(e["priority"], 3),
        -float(e["score"] or 0),
        _TIER_ORDER.get(e["tier"], 4),
        -e["days"],
    ))
    # 汇总
    p0 = sum(1 for e in events if e["priority"] == "P0")
    p1 = sum(1 for e in events if e["priority"] == "P1")
    p2 = sum(1 for e in events if e["priority"] == "P2")
    done_today = 0
    with sqlite3.connect(local_store.DB_PATH) as c:
        today = _dt.date.today().isoformat()
        # 归属该 target 的事件，今天有人（自己或主管代操作）标记完成 = done
        done_today = c.execute("""
            SELECT COUNT(DISTINCT a.event_uid) FROM task_action a
            JOIN event_pool e ON e.唯一识别 = a.event_uid
            WHERE a.action_type='完成' AND date(a.created_at)=?
              AND e.父ASIN IN (
                SELECT DISTINCT asin FROM asin_owner
                WHERE COALESCE(principal_user_id, editor_id, NULLIF(creator_id,-1)) = ?
              )
        """, (today, target)).fetchone()[0]
    return {
        "target_user_id": target,
        "total": len(events),
        "p0": p0, "p1": p1, "p2": p2,
        "done_today": done_today,
        "events": events,
    }


@app.get("/api/anomalies")
def api_anomalies(
    userId: int = Query(...),
    targetId: int | None = Query(None),
    priority: str | None = Query(None),
    category: str | None = Query(None),
    status: str | None = Query(None),
    q: str | None = Query(None),
):
    """全部异常池（含已关闭）。同 today 但不过滤开放状态，且支持筛选。"""
    target = _resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    events = _fetch_events_for_user(target, only_open=False)
    if priority:
        events = [e for e in events if e["priority"] == priority]
    if category:
        events = [e for e in events if e["category"] == category]
    if status:
        events = [e for e in events if e["status"] == status]
    if q:
        ql = q.lower()
        events = [e for e in events if ql in (e.get("parent_asin") or "").lower() or ql in (e.get("issue") or "").lower()]
    events.sort(key=lambda e: (
        _PRIORITY_ORDER.get(e["priority"], 3),
        -float(e["score"] or 0),
    ))
    return {"target_user_id": target, "total": len(events), "events": events}


@app.post("/api/tasks/{event_uid}/action")
def api_task_action(event_uid: str, payload: dict = Body(...)):
    """写入一条 task_action。前端每次点"完成/不处理/待复查/备注"都调这里。
    payload: {userId, action_type, result?, actual_action?, review_at?, notes?, before_metrics?, after_metrics?, effect?}"""
    user_id = payload.get("userId")
    action_type = payload.get("action_type")
    if not action_type:
        raise HTTPException(400, "action_type required")
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.execute("""
            INSERT INTO task_action
              (event_uid, user_id, action_type, result, actual_action, review_at, notes, before_metrics, after_metrics, effect)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            event_uid, user_id, action_type,
            payload.get("result"), payload.get("actual_action"),
            payload.get("review_at"), payload.get("notes"),
            _json_module.dumps(payload["before_metrics"], ensure_ascii=False) if payload.get("before_metrics") else None,
            _json_module.dumps(payload["after_metrics"], ensure_ascii=False) if payload.get("after_metrics") else None,
            payload.get("effect"),
        ))
        c.commit()
    return {"ok": True, "event_uid": event_uid}


@app.get("/api/review/due")
def api_review_due(
    userId: int = Query(...),
    targetId: int | None = Query(None),
    period: str = Query("today", description="today|3d|7d|all"),
):
    """调整复盘：已执行过动作且到达复查时间的事件。"""
    target = _resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    today = _dt.date.today()
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT a.*, e.父ASIN, e.父SKU, e.店铺账号, e.问题点位, e.严重度
            FROM task_action a
            JOIN event_pool e ON e.唯一识别 = a.event_uid
            WHERE a.review_at IS NOT NULL
              AND e.父ASIN IN (
                SELECT DISTINCT asin FROM asin_owner
                WHERE COALESCE(principal_user_id, editor_id, NULLIF(creator_id,-1)) = ?
              )
            ORDER BY a.review_at ASC
        """, (target,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        review_at = d.get("review_at")
        try:
            rd = _dt.datetime.fromisoformat(review_at).date() if review_at else None
        except Exception:
            rd = None
        delta = (rd - today).days if rd else None
        if period == "today" and delta != 0:
            continue
        if period == "3d" and (delta is None or not (0 <= delta <= 3)):
            continue
        if period == "7d" and (delta is None or not (0 <= delta <= 7)):
            continue
        out.append(d)
    return {"target_user_id": target, "period": period, "total": len(out), "records": out}


# ============================================================
# 处理记录 + 数据看板（Phase 2）
# ============================================================

_SLA_DAYS = {"P0": 1, "P1": 3, "P2": 7}  # 各优先级处理时限

_ACTION_CATEGORIES = [
    ("补货/库存处理", ["补货", "库存", "断货", "补齐", "转仓"]),
    ("广告收缩", ["广告", "预算", "暂停", "acos", "关键词竞价", "词"]),
    ("价格与优惠调整", ["价格", "coupon", "优惠", "促销", "折扣", "定价"]),
    ("文案/关键词优化", ["文案", "标题", "search terms", "属性", "关键词", "listing"]),
    ("图片调整", ["主图", "图片", "视频", "a+", "副图"]),
]

def _classify_action(text: str) -> str:
    """从实际动作文本推断动作类型。"""
    if not text:
        return "其他"
    t = text.lower()
    for name, keys in _ACTION_CATEGORIES:
        if any(k.lower() in t for k in keys):
            return name
    return "其他"


def _format_record_id(row_id: int, created_at: str) -> str:
    """ACT-YYYYMMDD-NNNN，同日内按 id 后 4 位。"""
    try:
        dt_part = created_at[:10].replace("-", "")
    except Exception:
        dt_part = "00000000"
    return f"ACT-{dt_part}-{row_id:04d}"


def _format_event_id(uid: str, first_seen: str | None) -> str:
    """EVT-YYYYMMDD-<uid前4位>，让运营看到日期。"""
    dt_part = (first_seen or "")[:10].replace("-", "") or "00000000"
    return f"EVT-{dt_part}-{(uid or '')[:4]}"


def _fetch_history_records(target_user_id: int) -> list[dict]:
    """拉出该 target 名下所有处理记录（不含普通"备注"）。"""
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT a.*, e.父ASIN as parent_asin, e.父SKU as parent_sku, e.店铺账号 as shop_account,
                   e.异常大类 as category, e.问题点位 as issue, e.严重度 as severity,
                   e.单异常执行分数 as score, e.首次命中时间 as first_seen
            FROM task_action a
            JOIN event_pool e ON e.唯一识别 = a.event_uid
            WHERE a.action_type IN ('完成', '不处理')
              AND e.父ASIN IN (
                SELECT DISTINCT asin FROM asin_owner
                WHERE COALESCE(principal_user_id, editor_id, NULLIF(creator_id,-1)) = ?
              )
            ORDER BY a.created_at DESC
        """, (target_user_id,)).fetchall()

        # 用户名 map
        uids = {r["user_id"] for r in rows if r["user_id"]}
        users = {}
        if uids:
            qs = ",".join("?" * len(uids))
            for u in c.execute(f"SELECT id, user_name FROM sys_user WHERE id IN ({qs})", list(uids)).fetchall():
                users[u["id"]] = u["user_name"]

        # 「处理后新异常」：同 parent_asin + 异常大类，first_seen > 本次 action created_at
        # 一次拿全部候选事件
        events_after = c.execute("""
            SELECT 唯一识别 as uid, 父ASIN as parent_asin, 异常大类 as category, 问题点位 as issue,
                   首次命中时间 as first_seen, 严重度 as severity
            FROM event_pool
            WHERE 父ASIN IN (
                SELECT DISTINCT asin FROM asin_owner
                WHERE COALESCE(principal_user_id, editor_id, NULLIF(creator_id,-1)) = ?
            )
        """, (target_user_id,)).fetchall()

    # 按 parent_asin+category 聚合，供后续 O(1) 查
    idx = {}
    for e in events_after:
        idx.setdefault((e["parent_asin"], e["category"] or ""), []).append(dict(e))

    name_map = local_store.query_product_names()

    out = []
    for r in rows:
        d = dict(r)
        priority = _SEV_TO_PRIORITY.get(d.get("severity") or "S2", "P2")
        # 判断 SLA：first_seen → created_at 的天数 vs SLA
        try:
            fs = _dt.datetime.fromisoformat(d["first_seen"]).date() if d["first_seen"] else None
            ca = _dt.datetime.fromisoformat(d["created_at"]).date() if d["created_at"] else None
            handle_days = (ca - fs).days if fs and ca else None
        except Exception:
            handle_days = None
        on_time = (handle_days is not None and handle_days <= _SLA_DAYS.get(priority, 7))

        # 记录完整性：result + actual_action 都填 = 完整
        complete = "完整" if (d.get("result") and d.get("actual_action")) else "待补"

        # 后续新异常：同 parent+category 在 created_at 之后的最新事件
        new_event = None
        siblings = idx.get((d["parent_asin"], d["category"] or ""), [])
        for e in siblings:
            if e["uid"] == d["event_uid"]:
                continue
            if e["first_seen"] and d["created_at"] and e["first_seen"] > d["created_at"]:
                new_event = e
                break

        # 复查结论
        if d.get("effect") == "变好":
            review = "已恢复"
        elif d.get("effect") == "变差":
            review = "未恢复"
        else:
            review = "待复查" if d.get("review_at") else "无复查"

        out.append({
            "record_id": _format_record_id(d["id"], d["created_at"]),
            "row_id": d["id"],
            "event_id": _format_event_id(d["event_uid"], d["first_seen"]),
            "event_uid": d["event_uid"],
            "time": d["created_at"],
            "date_key": d["created_at"][:10] if d["created_at"] else "",
            "owner": users.get(d["user_id"], f"用户{d['user_id']}") if d["user_id"] else "-",
            "owner_id": d["user_id"],
            "parent_asin": d["parent_asin"],
            "parent_sku": d["parent_sku"],
            "product_name": name_map.get(d["parent_asin"]),
            "shop_account": d["shop_account"],
            "category": d["category"] or "其他",
            "issue": d["issue"],
            "severity": d["severity"],
            "priority": priority,
            "score": d["score"] or 0,
            "action_type": d["action_type"],
            "result": d["result"],
            "actual_action": d["actual_action"],
            "action_class": _classify_action(d["actual_action"]),
            "review_at": d["review_at"],
            "effect": d["effect"],
            "review": review,
            "notes": d["notes"],
            "complete": complete,
            "handle_days": handle_days,
            "on_time": on_time,
            "new_event": {
                "event_id": _format_event_id(new_event["uid"], new_event["first_seen"]),
                "event_uid": new_event["uid"],
                "issue": new_event["issue"],
                "first_seen": new_event["first_seen"],
                "severity": new_event["severity"],
            } if new_event else None,
        })
    return out


@app.get("/api/history")
def api_history(
    userId: int = Query(...),
    targetId: int | None = Query(None),
    q: str | None = Query(None),
    time_range: str = Query("all", alias="range"),   # all|today|7d|30d
    owner: str | None = Query(None),
    status: str | None = Query(None),   # 完整|待补
    event: str | None = Query(None),    # new|single
):
    target = _resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    records = _fetch_history_records(target)

    today = _dt.date.today()
    def in_range(r):
        if time_range == "all": return True
        try:
            d = _dt.date.fromisoformat(r["date_key"])
        except Exception:
            return False
        delta = (today - d).days
        return {"today": delta == 0, "7d": delta <= 7, "30d": delta <= 30}.get(time_range, True)

    def match(r):
        if not in_range(r): return False
        if q and q.lower() not in f"{r['record_id']}{r['event_id']}{r['parent_asin']}{r['parent_sku'] or ''}{r['product_name'] or ''}{r['issue'] or ''}{r['actual_action'] or ''}".lower():
            return False
        if owner and r["owner"] != owner: return False
        if status and r["complete"] != status: return False
        if event == "new" and not r["new_event"]: return False
        if event == "single" and r["new_event"]: return False
        return True

    filtered = [r for r in records if match(r)]
    proof_count = sum(1 for r in records if r["complete"] == "完整")
    pending = sum(1 for r in records if r["complete"] == "待补")
    with_new = sum(1 for r in records if r["new_event"])
    products = len({r["parent_asin"] for r in records})

    return {
        "target_user_id": target,
        "total": len(filtered),
        "records": filtered,
        "kpi": {
            "proof": proof_count,
            "pending": pending,
            "with_new_event": with_new,
            "products": products,
        },
    }


@app.get("/api/history/{record_id}")
def api_history_detail(record_id: str, userId: int = Query(...), targetId: int | None = Query(None)):
    target = _resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    records = _fetch_history_records(target)
    rec = next((r for r in records if r["record_id"] == record_id), None)
    if not rec:
        raise HTTPException(404, "record not found")

    # 补齐轨迹：该 event_uid 所有 action + 若有 new_event 再拉那条 event
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        acts = c.execute("""
            SELECT id, action_type, result, actual_action, review_at, notes, effect, created_at, user_id
            FROM task_action WHERE event_uid = ? ORDER BY created_at ASC
        """, (rec["event_uid"],)).fetchall()
    rec["timeline"] = [dict(a) for a in acts]
    return rec


@app.get("/api/dashboard")
def api_dashboard(
    userId: int = Query(...),
    targetId: int | None = Query(None),
    days: str = Query("7", alias="range"),     # 7|14|30
):
    target = _resolve_target_user(userId, targetId)
    if target is None:
        raise HTTPException(400, "userId required")
    try:
        days = int(days)
    except Exception:
        days = 7
    today = _dt.date.today()
    since = today - _dt.timedelta(days=days - 1)

    # 全部 action + event（用于计算），用同一个 helper
    records = _fetch_history_records(target)
    all_events = _fetch_events_for_user(target, only_open=False)

    # 期间过滤
    in_period_records = [r for r in records if r["date_key"] >= since.isoformat()]

    # 1) KPI
    completed = sum(1 for r in in_period_records if r["action_type"] == "完成")
    target_num = days * 80  # 参考 demo 每日 80 条目标；实际按人数计更合理，Phase 3 优化
    goal_rate = round(completed / target_num * 100, 1) if target_num else 0
    on_time_cnt = sum(1 for r in in_period_records if r["on_time"] and r["action_type"] == "完成")
    on_time_rate = round(on_time_cnt / completed * 100, 1) if completed else 0
    with_review = [r for r in in_period_records if r["effect"] in ("变好", "变差", "待观察")]
    recovered_cnt = sum(1 for r in with_review if r["effect"] == "变好")
    recovery_rate = round(recovered_cnt / len(with_review) * 100, 1) if with_review else 0
    p0_records = [r for r in in_period_records if r["priority"] == "P0" and r["action_type"] == "完成"]
    p0_ontime = sum(1 for r in p0_records if r["on_time"])
    p0_rate = round(p0_ontime / len(p0_records) * 100, 1) if p0_records else 0
    # 重复异常：同 parent_asin 出现 >= 2 次事件
    from collections import Counter
    asin_counts = Counter(e["parent_asin"] for e in all_events)
    repeat_products = sum(1 for c in asin_counts.values() if c >= 2)
    total_products = len(asin_counts) or 1
    repeat_rate = round(repeat_products / total_products * 100, 1)

    # 2) 每日完成量
    day_map = {(since + _dt.timedelta(days=i)).isoformat(): 0 for i in range(days)}
    for r in in_period_records:
        if r["action_type"] == "完成" and r["date_key"] in day_map:
            day_map[r["date_key"]] += 1
    daily = [{"date": d, "count": c, "target": 80} for d, c in sorted(day_map.items())]

    # 3) 异常大类分布（open + closed 全部）
    cat_counter = Counter()
    cat_products = {}
    for e in all_events:
        cat = e.get("category") or "其他"
        cat_counter[cat] += 1
        cat_products.setdefault(cat, set()).add(e["parent_asin"])
    anomaly = sorted(
        [{"name": k, "events": v, "products": len(cat_products[k])} for k, v in cat_counter.items()],
        key=lambda x: -x["events"],
    )

    # 4) 恢复率趋势（按 date_key 累计到当天的 recovery_rate）
    trend = []
    for i in range(days):
        d = since + _dt.timedelta(days=i)
        cutoff = d.isoformat()
        subset = [r for r in records if r["date_key"] <= cutoff and r["effect"] in ("变好", "变差", "待观察")]
        rec_num = sum(1 for r in subset if r["effect"] == "变好")
        trend.append({
            "date": cutoff,
            "rate": round(rec_num / len(subset) * 100, 1) if subset else 0,
        })

    # 5) 动作有效率
    action_stats = {}
    for r in in_period_records:
        if r["action_type"] != "完成":
            continue
        klass = r["action_class"]
        st = action_stats.setdefault(klass, {"name": klass, "total": 0, "checked": 0, "good": 0})
        st["total"] += 1
        if r["effect"] in ("变好", "变差", "待观察"):
            st["checked"] += 1
            if r["effect"] == "变好":
                st["good"] += 1
    action_effects = sorted(
        [{"name": s["name"], "total": s["total"], "checked": s["checked"],
          "rate": round(s["good"] / s["checked"] * 100, 1) if s["checked"] else 0}
         for s in action_stats.values()],
        key=lambda x: -x["total"],
    )

    # 6) 高频异常产品（>= 2 次异常）
    top_asins = [a for a, c in asin_counts.most_common(20) if c >= 2]
    name_map = local_store.query_product_names()
    repeat_products_list = []
    for asin in top_asins:
        asin_events = [e for e in all_events if e["parent_asin"] == asin]
        latest = max(asin_events, key=lambda e: e.get("last_seen") or "")
        # 该 asin 的处理次数
        actions_cnt = sum(1 for r in records if r["parent_asin"] == asin and r["action_type"] == "完成")
        # 最近效果
        recent_action = next((r for r in records if r["parent_asin"] == asin), None)
        effect = (recent_action or {}).get("effect") or "-"
        repeat_products_list.append({
            "parent_asin": asin,
            "product_name": name_map.get(asin),
            "count": asin_counts[asin],
            "issue": latest.get("issue"),
            "actions": actions_cnt,
            "effect": effect,
            "priority": latest.get("priority"),
            "score": latest.get("score"),
            "days": latest.get("days"),
        })

    return {
        "target_user_id": target,
        "range_days": days,
        "kpi": {
            "completed": completed,
            "target": target_num,
            "goal_rate": goal_rate,
            "on_time_rate": on_time_rate,
            "on_time_count": on_time_cnt,
            "recovery_rate": recovery_rate,
            "recovered": recovered_cnt,
            "not_recovered": sum(1 for r in with_review if r["effect"] == "变差"),
            "watching": sum(1 for r in with_review if r["effect"] == "待观察"),
            "p0_ontime_rate": p0_rate,
            "p0_overdue": max(0, len(p0_records) - p0_ontime),
            "repeat_rate": repeat_rate,
            "repeat_products": repeat_products,
        },
        "daily": daily,
        "anomaly": anomaly,
        "trend": trend,
        "action_effects": action_effects,
        "repeat_products": repeat_products_list[:10],
    }


@app.get("/legacy")
def legacy_page():
    """旧版技术台前端：改判定参数、看知识库、单产品跑判定用。"""
    idx = FRONT / "index.legacy.html"
    if not idx.exists():
        raise HTTPException(404, "legacy frontend missing")
    return FileResponse(idx, headers={"Cache-Control": "private, max-age=300, stale-while-revalidate=600"})


@app.get("/report")
def report_page():
    """ERP iframe 嵌入入口，参数通过 querystring 传入。
    带 HTTP 缓存头 → iframe 每次销毁重建时命中 disk cache，不再发 HTML 请求。"""
    idx = FRONT / "index.html"
    if not idx.exists():
        return {"error": "index not found"}
    return FileResponse(
        idx,
        headers={"Cache-Control": "private, max-age=300, stale-while-revalidate=600"},
    )


# --- 前端静态资源 ---
if FRONT.exists():
    app.mount("/ui", StaticFiles(directory=str(FRONT), html=True), name="frontend")


@app.get("/")
def root():
    """前端入口 —— 加 HTTP 缓存头，让 iframe 重跳时命中浏览器 disk cache。
    max-age=300 → 浏览器 5 分钟内直接用缓存，不发请求。
    stale-while-revalidate=600 → 缓存过期后仍先用旧的，后台重新拉取新的。"""
    idx = FRONT / "index.html"
    if not idx.exists():
        return {"ok": True}
    return FileResponse(
        idx,
        headers={"Cache-Control": "private, max-age=300, stale-while-revalidate=600"},
    )


