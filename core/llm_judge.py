"""LLM 判定引擎 —— 拿知识库全文 + 热参数 + 单 ASIN 数据，交给 Claude 输出巡检结论。

设计原则：
- 知识库 md 全量塞进 system prompt，改 md 立即生效
- 热参数 rules.yaml 单独嵌入，方便 LLM 引用具体阈值
- 没配 ANTHROPIC_API_KEY 时进入 dry-run 模式，只返回即将发送的 prompt 供人肉审查
"""
from __future__ import annotations
import json, os, yaml
from pathlib import Path
from core import knowledge_loader

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "config/rules.yaml"

SYSTEM_TEMPLATE = """你是一个亚马逊业务巡检 Agent，遵循下方知识库和参数配置对单个产品做异常判定、严重度评级、优先级打分和处理建议输出。

# 知识库（8 个模块，权威判定依据）
{knowledge_bundle}

# 热参数配置（config/rules.yaml，命中阈值以这里为准，与知识库描述冲突时优先此表）
```yaml
{rules_yaml}
```

# 输出要求
严格返回一个 JSON 对象（不要有多余文字），所有文本用**中文**，措辞面向运营同学（说人话，不要机械引用条款号；如需引用规则可放括号里）。schema：
{{
  "headline": "一句话结论，20 字内，运营看到就能理解优先级和主要问题",
  "human_summary": "一段 2-4 句的自然语言总结：这个产品当前的健康状况、需要重点关注什么、为什么这么判",
  "anomalies": [
    {{
      "code": "异常点位（中文名）",
      "category": "所属大类",
      "severity": "S0 | S1 | S2",
      "plain_reason": "用大白话解释为什么命中，一句话，不要引条款号",
      "base_score": 数字,
      "final_score": 数字
    }}
  ],
  "product_execution_score": 数字,
  "priority": "P0 | P1 | P2",
  "priority_label": "当天处理 / 3天内处理 / 7天内处理（跟着 priority 走）",
  "priority_reason": "一句话解释为什么这个档",
  "suggested_actions": ["每条一句大白话，说清楚该做什么、目标是什么"],
  "task_card": {{
    "title": "任务卡标题（不超过 30 字）",
    "brief": "一句话摘要，运营扫一眼就知道要做什么"
  }}
}}
若数据不足以判定："anomalies" 空数组，priority 返回 "P2"，headline 写"数据不足，暂缓处理"，human_summary 说明缺什么数据。
"""

USER_TEMPLATE = """# 待判定产品

## Config（ERP t_advert_agent_decision_config）
```json
{config_json}
```

## MCP 数据（近 30 天聚合）
```json
{mcp_json}
```
{local_block}
请依照 system 中的知识库和参数，输出判定结果 JSON。
"""


def _fmt(v, nd=2):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return v


def _local_enrichment(parent_asin: str) -> str:
    """从本地缓存库拉精确数据（每日序列/基线/目标/库存/标签），拼成给 LLM 的补充块。
    这些数据比 MCP 30 天聚合更精确，专供判定'连续≥3天/近7天均值'类规则。"""
    if not parent_asin:
        return ""
    try:
        from data import local_store as store
    except Exception:
        return ""
    parts = []

    daily = store.recent_daily_sales(parent_asin, days=14)
    if daily:
        lines = ["## 本地库·每日销售序列（近14天，精确到天，用于连续天数/近7天均值判定）",
                 "日期 | 全部单量 | 全部销量 | 广告花费 | ACOS | 毛利率"]
        for r in daily:
            lines.append(f"{r['stat_date']} | {r['全部单量']} | {r['全部销量']} | "
                         f"{_fmt(r['广告花费'])} | {_fmt(r['ACOS'],4)} | {_fmt(r['毛利率'],4)}")
        # 近7天基线
        d7 = daily[:7]
        def avg(k):
            vals = [x[k] for x in d7 if x[k] is not None]
            return round(sum(vals) / len(vals), 4) if vals else None
        lines.append(f"近7天均值 → 单量:{avg('全部单量')} 广告花费:{avg('广告花费')} ACOS:{avg('ACOS')}")
        parts.append("\n".join(lines))

    lb = store.get_one("listing_baseline", parent_asin=parent_asin)
    if lb:
        parts.append("## 本地库·Listing基线\n"
                     f"链接转化率:{lb.get('链接转化率')} 类目转化率:{lb.get('类目转化率')} "
                     f"类目退换货率:{lb.get('类目退换货率')} 16周退款率:{lb.get('16周退款率')} "
                     f"星级:{lb.get('星级')} 评论数:{lb.get('评论数')} "
                     f"大类排名:{lb.get('大类排名')} 小类排名:{lb.get('小类排名')}")

    ss = store.get_one("stock_summary", parent_asin=parent_asin)
    if ss:
        parts.append("## 本地库·FBA库存\n"
                     f"可售:{ss.get('FBA可售库存')} 入库:{ss.get('FBA入库库存')} "
                     f"预留:{ss.get('FBA预留库存')} 不可售:{ss.get('FBA不可售库存')}")

    tg = store.get_one("product_tags", asin=parent_asin)
    if tg:
        parts.append("## 本地库·产品标签\n"
                     f"淡旺季:{tg.get('SEASONALITY')} 产品等级:{tg.get('PRODUCT_GRADE')} "
                     f"目标评分:{tg.get('TARGET_STAR_RATE')} 当前星级:{tg.get('STAR_LEVEL')} "
                     f"上架日:{tg.get('ASIN_START_SALE_DATE')} 库存:{tg.get('STOCK_INVENTORY')}")

    if not parts:
        return ""
    return "\n## 本地缓存库·精确数据（优先采信，比上方 MCP 聚合更准）\n" + "\n\n".join(parts) + "\n"


def _load_bundle() -> str:
    parts = []
    for name in knowledge_loader.list_modules():
        parts.append(f"--- {name} ---\n{knowledge_loader.read_module(name)}")
    return "\n\n".join(parts)


def _load_rules_yaml() -> str:
    return RULES_PATH.read_text(encoding="utf-8")


def build_prompt(config_row: dict, mcp_bundle: dict) -> dict:
    return {
        "system": SYSTEM_TEMPLATE.format(
            knowledge_bundle=_load_bundle(),
            rules_yaml=_load_rules_yaml(),
        ),
        "user": USER_TEMPLATE.format(
            config_json=json.dumps(config_row, ensure_ascii=False, indent=2),
            mcp_json=json.dumps(mcp_bundle, ensure_ascii=False, indent=2),
            local_block=_local_enrichment(config_row.get("parent_asin", "")),
        ),
    }


def _extract_json(text: str) -> dict:
    """LLM 有时会在 JSON 外包一层 ```json ... ```，剥壳后再解析。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
        s = s.strip()
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    # 从 { 到最后 } 之间取
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        s = s[a:b + 1]
    try:
        return json.loads(s)
    except Exception as e:
        return {"raw": text, "parse_error": str(e)}


def judge(config_row: dict, mcp_bundle: dict, model: str | None = None) -> dict:
    prompt = build_prompt(config_row, mcp_bundle)
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {
            "dry_run": True,
            "note": "未设置 DEEPSEEK_API_KEY，返回即将发送的 prompt。",
            "system_length": len(prompt["system"]),
            "user_length": len(prompt["user"]),
            "system_preview": prompt["system"][:1200] + "\n...(截断)",
            "user_preview": prompt["user"][:1500],
        }

    # 走 DeepSeek（OpenAI 兼容）
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
            {"role": "system", "content": prompt["system"]},
            {"role": "user", "content": prompt["user"]},
        ],
    )
    text = resp.choices[0].message.content or ""
    parsed = _extract_json(text)
    return {
        "dry_run": False,
        "model": resp.model,
        "usage": {
            "input_tokens": resp.usage.prompt_tokens,
            "output_tokens": resp.usage.completion_tokens,
        },
        "judgment": parsed,
    }
