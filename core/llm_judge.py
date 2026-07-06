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
严格返回一个 JSON 对象（不要有多余文字），schema：
{{
  "anomalies": [
    {{
      "code": "异常点位代码或名称",
      "category": "所属大类（模块 2）",
      "severity": "S0 | S1 | S2",
      "reason": "命中理由（引用知识库小节号）",
      "base_score": 数字,
      "final_score": 数字
    }}
  ],
  "product_execution_score": 数字,
  "priority": "P0 | P1 | P2",
  "priority_reason": "为什么落这一档",
  "suggested_actions": ["按模块 6 输出的处理建议，每条一句"],
  "task_card": {{
    "title": "任务卡标题（模块 7）",
    "brief": "一句话摘要"
  }}
}}
若数据不足以判定，"anomalies" 返回空数组，priority 返回 "P2"，在 priority_reason 里说明数据缺口。
"""

USER_TEMPLATE = """# 待判定产品

## Config（ERP t_advert_agent_decision_config）
```json
{config_json}
```

## MCP 数据（近 30 天）
```json
{mcp_json}
```

请依照 system 中的知识库和参数，输出判定结果 JSON。
"""


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
        ),
    }


def judge(config_row: dict, mcp_bundle: dict, model: str | None = None) -> dict:
    prompt = build_prompt(config_row, mcp_bundle)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {
            "dry_run": True,
            "note": "未设置 ANTHROPIC_API_KEY，返回即将发送的 prompt。",
            "system_length": len(prompt["system"]),
            "user_length": len(prompt["user"]),
            "system_preview": prompt["system"][:1200] + "\n...(截断)",
            "user_preview": prompt["user"][:1500],
        }

    import anthropic
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=model or "claude-opus-4-8",
        max_tokens=4096,
        temperature=0,
        system=prompt["system"],
        messages=[{"role": "user", "content": prompt["user"]}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    # 尝试解析 JSON
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = {"raw": text, "parse_error": True}
    return {
        "dry_run": False,
        "model": resp.model,
        "usage": {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
        "judgment": parsed,
    }
