"""LLM 处理建议生成器 —— 用 AI 替换 R6 YAML 模板，结合知识库话术输出建议。

设计原则：
  - 批量调用：一次产品巡检的所有异常打包，一次 LLM 调用全部出建议
  - 知识库约束：LLM 必须使用 knowledge/04_模块3 和 knowledge/05_模块4 中规定的话术
  - 兜底安全：LLM 调用失败 → 返回 None，调用方回退到 R6 YAML
  - 与 llm_judge.py 复用同一 LLM 基础设施（DeepSeek OpenAI 兼容 API）

用法：
  from inspector.engine import llm_suggester

  结果 = llm_suggester.suggest_batch(异常列表, 产品上下文)
  # 返回 list[dict] 或 None（失败时回退 R6）
"""
from __future__ import annotations
import json
import logging
import os
from typing import Any

from core import knowledge_loader

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 知识库片段加载（只加载模块3+4，不含大量不可变内容）
# ---------------------------------------------------------------------------
_KB_CACHE: dict[str, str] = {}


def _kb_module(name_pattern: str) -> str:
    """按文件名模糊匹配加载知识库模块，带缓存。"""
    if name_pattern in _KB_CACHE:
        return _KB_CACHE[name_pattern]
    for n in knowledge_loader.list_modules():
        if name_pattern in n:
            text = knowledge_loader.read_module(n)
            _KB_CACHE[name_pattern] = text
            return text
    log.warning("知识库模块未找到: %s", name_pattern)
    return ""


# ---------------------------------------------------------------------------
# Prompt 构建
# ---------------------------------------------------------------------------

_SUGGEST_SYSTEM = """你是亚马逊运营巡检助手，负责为已检测出的异常点位生成处理建议。

## 你的任务

你会收到一份「代码判定结果列表」，每条异常已经由代码检测确认。
你需要为每条异常输出：
  - 具体表现：4-8 字短标签，一眼看懂是什么问题
  - 判断依据：一句话说清哪个指标/数据异常
  - 处理建议：一句可执行的动作，必须使用知识库中规定的话术，不得自行编造

## 约束

1. **必须使用知识库话术**：知识库模块3和模块4中有规定的话术模板，请按异常点位匹配对应话术，
   不要自己编造建议。如果知识库中没有该点位的精确话术，使用最接近的话术风格。
2. **直接定因型异常**（库存、内容、链接状态、价格促销、合规属性类）：直接用知识库模块3.2的话术。
3. **待查因型异常**（ACOS、销量、广告花费、转化率、自然流量、卡位、退款等）：
   代码判定已给出 判定过程/命中依据，请据此匹配知识库模块3中对应的 root_cause 话术；
   如果代码判定中没有明确的 root_cause，使用该点位在知识库中的兜底话术。
4. **广告类异常**（ACOS、广告花费、卡位、放量未执行）：
   建议中必须包含"建议到广告辅助决策agent检查"。
5. 每条异常的 判断依据 必须包含代码判定提供的关键数据（如命中值、分档名称），不能是泛泛的描述。
6. 输出严格 JSON，不要加解释、不要 markdown 代码块。

## 知识库 · 模块3（查因与处理建议，含规定话术）

[[MODULE_3]]

## 知识库 · 模块4（运营输出规范）

[[MODULE_4]]
"""

_SUGGEST_USER = """## 产品信息
父ASIN: {parent_asin}
店铺: {shop_account}
站点: {site}

## 代码判定结果列表

{anomalies_json}

请为以上 {count} 条异常逐一输出 具体表现、判断依据、处理建议。
输出格式：
{{"results":[{{"index":0,"具体表现":"...","判断依据":"...","处理建议":"..."}},...]}}
index 必须对应上方输入列表的序号。"""


def _build_prompt(anomalies: list[dict], product_context: dict) -> dict[str, str]:
    """构建 system + user prompt。"""
    # 加载知识库模块3和4
    mod3 = _kb_module("模块3") or _kb_module("04")
    mod4 = _kb_module("模块4") or _kb_module("05")

    system = _SUGGEST_SYSTEM.replace("[[MODULE_3]]", mod3).replace("[[MODULE_4]]", mod4)

    # 精简异常数据，只保留 LLM 需要的关键字段
    slim = []
    for i, a in enumerate(anomalies):
        item: dict[str, Any] = {"index": i}
        for k in ("问题点位", "严重度", "作用层级", "命中变体", "变体重要性",
                   "命中依据", "判定过程", "命中值", "分档名称", "类型"):
            v = a.get(k)
            if v is not None and v != "" and v != -1:
                item[k] = v
        slim.append(item)

    user = _SUGGEST_USER.format(
        parent_asin=product_context.get("parent_asin", "未知"),
        shop_account=product_context.get("shop_account", "未知"),
        site=product_context.get("site", "未知"),
        anomalies_json=json.dumps(slim, ensure_ascii=False, indent=2),
        count=len(anomalies),
    )

    return {"system": system, "user": user}


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------

def _call_llm(system: str, user: str, model: str | None = None) -> str | None:
    """调用 DeepSeek API，返回响应文本；失败返回 None。"""
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.warning("未设置 DEEPSEEK_API_KEY，LLM 建议生成跳过")
        return None

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
        resp = client.chat.completions.create(
            model=model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            temperature=0,
            max_tokens=4096,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = resp.choices[0].message.content or ""
        log.info("LLM 建议生成完成 tokens: in=%d out=%d",
                 resp.usage.prompt_tokens if resp.usage else -1,
                 resp.usage.completion_tokens if resp.usage else -1)
        return text
    except Exception as e:
        log.warning("LLM 建议生成调用失败: %s", e)
        return None


def _extract_json(text: str) -> dict | None:
    """从 LLM 响应中提取 JSON。"""
    s = text.strip()
    # 剥离 markdown 代码块
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
        s = s.strip()
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    # 找最外层 {}
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        s = s[a:b + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        log.warning("LLM 建议 JSON 解析失败: %s", e)
        return None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def suggest_batch(
    anomalies: list[dict],
    product_context: dict | None = None,
    model: str | None = None,
) -> list[dict] | None:
    """为一组异常批量生成处理建议。

    Args:
        anomalies: 异常列表，每条 dict 至少包含：
                   - 问题点位 (str)
                   - 严重度 (str)
                   - 命中依据 或 判定过程 (str)
                   可选：命中变体、变体重要性、命中值、分档名称、作用层级、类型
        product_context: 产品上下文 {parent_asin, shop_account, site}
        model: LLM 模型名，默认用环境变量 DEEPSEEK_MODEL

    Returns:
        list[dict]: 与输入同序的建议列表，每条为 {具体表现, 判断依据, 处理建议}
        None: LLM 调用失败，调用方应回退到 R6 YAML
    """
    if not anomalies:
        return []

    ctx = product_context or {}

    # 1. 构建 prompt
    prompt = _build_prompt(anomalies, ctx)

    # 2. 调用 LLM
    text = _call_llm(prompt["system"], prompt["user"], model)
    if not text:
        return None

    # 3. 解析响应
    parsed = _extract_json(text)
    if not parsed:
        return None

    results_raw = parsed.get("results", [])
    if not isinstance(results_raw, list) or len(results_raw) == 0:
        log.warning("LLM 建议返回空 results")
        return None

    # 4. 按 index 对齐，生成与输入同序的结果列表
    n = len(anomalies)
    output: list[dict | None] = [None] * n
    for r in results_raw:
        idx = r.get("index", -1)
        if isinstance(idx, int) and 0 <= idx < n:
            output[idx] = {
                "具体表现": str(r.get("具体表现", "") or anomalies[idx].get("问题点位", "")),
                "判断依据": str(r.get("判断依据", "") or anomalies[idx].get("命中依据", "")),
                "处理建议": str(r.get("处理建议", "") or "待补充"),
            }

    # 5. 检查完整性：如果有任何条目缺失，整个回退（保证一致性）
    if any(o is None for o in output):
        missing = [i for i, o in enumerate(output) if o is None]
        log.warning("LLM 建议缺失 %d/%d 条 (index=%s)，回退 R6",
                    len(missing), n, missing[:5])
        return None

    return output  # type: ignore[return-value]


def suggest_single(
    anomaly: dict,
    all_anomalies: list[dict] | None = None,
    product_context: dict | None = None,
    model: str | None = None,
) -> dict[str, str] | None:
    """为单条异常生成建议（便利函数，内部调用 suggest_batch）。

    建议在 _build_anomaly_details 中批量调用 suggest_batch 而非逐条调用此函数。
    此函数仅用于单条补建议的场景。
    """
    batch = all_anomalies or [anomaly]
    results = suggest_batch(batch, product_context, model)
    if results is None:
        return None
    # 找到对应 index
    idx = batch.index(anomaly) if anomaly in batch else 0
    return results[idx] if idx < len(results) else None
