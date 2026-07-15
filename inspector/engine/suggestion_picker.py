"""R6 处理建议话术选取器 —— 对齐 knowledge/05_模块4_运营输出规范.md 风格。

输入：命中异常（含 触发字段、命中变体、变体重要性）
输出：{ 判断依据, 处理建议 } 一句话事实 + 一句话动作，替换旧的后端技术串。

设计：
  - 话术模板放 R6_处理建议.yaml，可热改
  - 分支路由：一个点位下可有多套模板（如 变体价差 有 尺码/颜色）
  - 缺占位符 → 用 "—" 兜底，不抛异常
  - 未收录的点位 → 返回 None，调用方回落到旧行为
"""
from __future__ import annotations
import functools
import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_R6_PATH = Path(__file__).resolve().parents[1] / "rules" / "R6_处理建议.yaml"


@functools.lru_cache(maxsize=1)
def 加载R6() -> dict:
    if not _R6_PATH.exists():
        log.warning("R6_处理建议.yaml 不存在，处理建议将回落到旧模板")
        return {}
    with _R6_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class _安全字典(dict):
    """format_map 遇到缺失 key 时用 '—' 占位而不是 KeyError。"""
    def __missing__(self, key):
        return "—"


def _格式化(模板: str, 上下文: dict[str, Any]) -> str:
    return (模板 or "").strip().format_map(_安全字典(上下文))


def _选分支(点位配置: dict, 命中异常) -> dict | None:
    """按 触发字段.分类 / 命中依据 关键字选分支模板。"""
    if not isinstance(点位配置, dict):
        return None

    # 无分支：配置本身就是模板
    if "判断依据" in 点位配置:
        return 点位配置

    触发 = getattr(命中异常, "触发字段", {}) or {}
    分类 = 触发.get("分类") or ""

    # 变体价差：命中依据里带"尺码分支/颜色分支"
    命中依据 = getattr(命中异常, "命中依据", "") or ""
    if not 分类:
        if "尺码分支" in 命中依据: 分类 = "尺码"
        elif "颜色分支" in 命中依据: 分类 = "颜色"

    return 点位配置.get(分类) if 分类 else None


def 选取(命中异常) -> dict[str, str] | None:
    """现象即原因型：从 命中异常 挑话术，返回 {判断依据, 处理建议}；未收录返回 None。"""
    r6 = 加载R6()
    点位配置 = r6.get(命中异常.问题点位)
    if not 点位配置:
        return None

    模板 = _选分支(点位配置, 命中异常)
    if not 模板 or "判断依据" not in 模板:
        return None

    ctx: dict[str, Any] = dict(命中异常.触发字段 or {})
    ctx["命中变体"] = 命中异常.命中变体 or ""
    ctx["变体重要性"] = 命中异常.变体重要性 or ""
    ctx.setdefault("命中依据", 命中异常.命中依据 or "")

    # 常用衍生
    if "基准价" in ctx and "对比价" in ctx and "绝对差距" not in ctx:
        try:
            ctx["绝对差距"] = round(abs(float(ctx["对比价"]) - float(ctx["基准价"])), 2)
        except (TypeError, ValueError):
            pass
    for k in ("基准价", "对比价", "绝对差距"):
        if isinstance(ctx.get(k), (int, float)):
            ctx[k] = f"{float(ctx[k]):.2f}"

    模板_判 = _裁前缀(模板.get("判断依据", ""), ctx)
    模板_建 = _裁前缀(模板.get("处理建议", ""), ctx)
    return {
        "具体表现": _格式化(模板.get("具体表现", ""), ctx),
        "判断依据": _格式化(模板_判, ctx),
        "处理建议": _格式化(模板_建, ctx),
    }


def _裁前缀(模板文本: str, ctx: dict) -> str:
    """模板级预处理：命中变体缺失时把 '{变体重要性}子体 {命中变体} ' 从模板字面砍掉。
    在 format_map 之前执行，避免正则误伤已渲染的正文。"""
    if ctx.get("命中变体"):
        return 模板文本   # 有变体则原样渲染，变体重要性缺失时读作 "子体 XXX"
    for 前缀 in ("{变体重要性}子体 {命中变体} ", "{变体重要性}子体 {命中变体}"):
        if 模板文本.lstrip().startswith(前缀):
            return 模板文本.lstrip()[len(前缀):]
    return 模板文本


def 选取表现型(判定结果: dict) -> dict[str, str] | None:
    """表现型（daily_monitor 打分产出）：从 dict 挑话术。
    输入 dict 键: 问题点位/严重度/分档名称/判定过程/命中值。
    未收录返回 None，调用方回落到 判定过程。
    """
    r6 = 加载R6()
    点位配置 = r6.get(判定结果.get("问题点位"))
    if not isinstance(点位配置, dict) or "判断依据" not in 点位配置:
        return None
    # 配置缺失类观察，直接把 判定过程 露给运营（"请在辅助决策 agent 设置..."）
    if 判定结果.get("分档名称") == "配置缺失":
        return None

    ctx: dict[str, Any] = {
        "分档名称": 判定结果.get("分档名称") or "—",
        "判定过程": 判定结果.get("判定过程") or "",
    }
    # 这些表现型点位的 命中值 是 0~1 比例，模板里 {命中值} 期望百分比展示
    _百分比点位 = {
        "销量异常", "销量增长提示", "ACOS异常", "广告花费异常",
        "目标偏离", "卡位异常", "退款异常", "链接转化率异常下降",
    }
    命中值 = 判定结果.get("命中值")
    点位 = 判定结果.get("问题点位")
    if isinstance(命中值, dict):
        for k, v in 命中值.items():
            ctx[k] = _美化(k, v)
        ctx["命中值"] = ",".join(f"{k}={v}" for k, v in 命中值.items())
    elif isinstance(命中值, float) and 点位 in _百分比点位:
        ctx["命中值"] = f"{命中值 * 100:.1f}%"
    else:
        ctx["命中值"] = _美化("命中值", 命中值)

    return {
        "具体表现": _格式化(点位配置.get("具体表现", ""), ctx),
        "判断依据": _格式化(点位配置.get("判断依据", ""), ctx),
        "处理建议": _格式化(点位配置.get("处理建议", ""), ctx),
    }


def _美化(键名: str, 值: Any) -> str:
    """把数值转成人话："偏离比"型 → 百分比；其他保留 2 位小数。"""
    if 值 is None:
        return "—"
    if isinstance(值, float):
        # 比例/占比类字段用百分比，其他保留 2 位
        if any(k in 键名 for k in ("比例", "偏离比", "占比", "达成")):
            return f"{值 * 100:.1f}%"
        return f"{值:.2f}"
    return str(值)
