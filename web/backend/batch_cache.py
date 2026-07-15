"""批量代码巡检 + LLM 的缓存单例。

设计：
  - 单进程内存字典 + JSON 磁盘持久化
  - 缓存粒度：产品级 (key = parent_asin__shop_id)，不做用户隔离
  - 用户隔离由 /api/asins 的 userId 过滤保证
  - 进程重启后从 batch_cache.json 恢复
"""
from __future__ import annotations
import json
import logging
import threading
from pathlib import Path

from data import fixture_loader
from .common import ROOT, load_settings

log = logging.getLogger(__name__)

# ---- 全局状态 ----
_cache: dict = {}
ready = threading.Event()          # 代码巡检完成
lock = threading.Lock()

CACHE_PATH = ROOT / "data" / "local" / "batch_cache.json"


def get_cache() -> dict:
    """给外部只读访问用；写入请走 with lock: cache[k] = ..."""
    return _cache


def set_entry(key: str, value: dict) -> None:
    with lock:
        _cache[key] = value


def update_entry(key: str, updates: dict) -> None:
    with lock:
        _cache.setdefault(key, {}).update(updates)


def clear() -> None:
    with lock:
        _cache.clear()


def load_from_disk() -> None:
    """启动时加载磁盘缓存到内存。文件不存在或损坏都静默跳过。"""
    global _cache
    if not CACHE_PATH.exists():
        return
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            _cache = json.load(f)
        log.info("批量缓存从磁盘恢复: %d 个产品", len(_cache))
        if _cache:
            ready.set()
    except Exception as e:
        log.warning("批量缓存磁盘恢复失败（忽略）: %s", e)


def persist() -> None:
    """把内存缓存原子写盘。tmp 文件 + rename 避免半写入。"""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        with lock:
            snapshot = dict(_cache)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, default=str)
        tmp.replace(CACHE_PATH)
    except Exception as e:
        log.warning("批量缓存磁盘持久化失败: %s", e)


def should_llm(card: dict) -> bool:
    """判断代码判定结果是否值得触发 LLM。门槛来自 config/settings.yaml → llm.trigger"""
    cfg = load_settings().get("llm", {}).get("trigger", {})
    最低分数 = cfg.get("最低分数", 90)
    最少S0异常数 = cfg.get("最少S0异常数", 1)
    最少异常数 = cfg.get("最少异常数", 2)

    pri = card.get("优先级信息", {})
    score = pri.get("产品执行分数", 0) or 0
    anomalies = card.get("异常明细", [])
    s0 = sum(1 for a in anomalies if (a.get("该条严重度") or "") == "S0")
    if score >= 最低分数:
        return True
    return s0 >= 最少S0异常数 and len(anomalies) >= 最少异常数


def run_batch_inspect() -> None:
    """后台：跑全部产品的代码巡检 → 缓存 → 对达门槛产品触发 LLM。"""
    from inspector.inspector_main import 巡检单产品
    from core import llm_judge
    from data import local_store as store
    store.init_db()

    configs = {c["fixture_key"]: c for c in fixture_loader.load_configs()}
    smap = fixture_loader.load_shop_map()
    keys = fixture_loader.list_keys()

    log.info("批量代码巡检开始: %d 个产品", len(keys))

    llm_queue: list[tuple[str, dict]] = []

    for i, key in enumerate(keys):
        cfg = dict(configs.get(key, {}))
        shop = smap.get(cfg.get("shop_id", ""), {})
        cfg["shop_account"] = shop.get("account", "")
        try:
            card = 巡检单产品(key=key, config_row=cfg)
            pri = card.get("优先级信息", {})
            set_entry(key, {
                "priority": pri.get("执行优先级", "P2"),
                "score": pri.get("产品执行分数", 0),
                "anomaly_count": len(card.get("异常明细", [])),
                "llm_status": "code",
                "llm_score": None,
                "llm_priority": None,
                "code_judgment": card,
                "llm_judgment": None,
            })
            if should_llm(card):
                llm_queue.append((key, cfg))
        except Exception as e:
            log.warning("代码巡检失败 %s: %s", key, e)
            set_entry(key, {
                "priority": "P2", "score": 0,
                "llm_status": "error",
                "llm_score": None, "llm_priority": None,
                "code_judgment": None, "llm_judgment": None,
                "error": str(e),
            })
        if (i + 1) % 20 == 0:
            log.info("  代码巡检进度: %d/%d", i + 1, len(keys))

    ready.set()
    persist()
    log.info("批量代码巡检完成: %d 个产品, %d 个达 LLM 门槛", len(_cache), len(llm_queue))

    if not load_settings().get("startup", {}).get("auto_batch_llm"):
        log.info("startup.auto_batch_llm=false，跳过批量 LLM（用户可对单产品手动 /api/judge）")
        return

    for key, cfg in llm_queue:
        try:
            update_entry(key, {"llm_status": "llm_pending"})
            bundle = fixture_loader.load_bundle(key)
            result = llm_judge.judge(cfg, bundle)
            j = result.get("judgment", {})
            llm_pri = (j.get("优先级信息") or {}).get("执行优先级", "P2")
            llm_score = (j.get("优先级信息") or {}).get("产品执行分数", 0)
            update_entry(key, {
                "llm_status": "llm_done",
                "llm_score": llm_score,
                "llm_priority": llm_pri,
                "llm_judgment": j,
            })
            persist()
            log.info("LLM 完成 %s: llm_priority=%s llm_score=%s (代码分不覆盖)", key, llm_pri, llm_score)
        except Exception as e:
            log.warning("LLM 失败 %s: %s", key, e)
            update_entry(key, {"llm_status": "llm_error"})
    log.info("LLM 批量完成")


# 启动即加载磁盘缓存
load_from_disk()
