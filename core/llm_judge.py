"""LLM 判定引擎 —— 拿知识库全文 + 热参数 + 单 ASIN 数据，交给 Claude 输出巡检结论。

设计原则：
- 提示词独立成文件：prompts/system.md、prompts/user.md，改提示词不用碰代码（热加载）
- 知识库 md 全量塞进 system prompt，改 md 立即生效
- 热参数 rules.yaml 单独嵌入，方便 LLM 引用具体阈值
- 没配 DEEPSEEK_API_KEY 时进入 dry-run 模式，只返回即将发送的 prompt 供人肉审查

提示词占位符（在 prompts/*.md 里用 [[占位符]] 书写）：
  system.md：[[KNOWLEDGE_BUNDLE]] [[RULES_YAML]]
  user.md：  [[CONFIG_JSON]] [[MCP_JSON]] [[LOCAL_BLOCK]]
"""
from __future__ import annotations
import json, os, yaml
from pathlib import Path
from core import knowledge_loader

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "config/rules.yaml"
PROMPTS_DIR = ROOT / "prompts"


def _load_prompt(name: str) -> str:
    """读取 prompts/<name>.md（每次现读，改文件即生效）。"""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _fill(template: str, mapping: dict) -> str:
    """把 [[KEY]] 占位符替换成实际内容（不用 str.format，避免 JSON 花括号转义）。"""
    out = template
    for k, v in mapping.items():
        out = out.replace(f"[[{k}]]", str(v))
    return out


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
        "system": _fill(_load_prompt("system"), {
            "KNOWLEDGE_BUNDLE": _load_bundle(),
            "RULES_YAML": _load_rules_yaml(),
        }),
        "user": _fill(_load_prompt("user"), {
            "CONFIG_JSON": json.dumps(config_row, ensure_ascii=False, indent=2),
            "MCP_JSON": json.dumps(mcp_bundle, ensure_ascii=False, indent=2),
            "LOCAL_BLOCK": _local_enrichment(config_row.get("parent_asin", "")),
        }),
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
