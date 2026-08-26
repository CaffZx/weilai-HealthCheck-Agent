"""导出巡检 Agent 当前共享 JSON Schema。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.contracts import InspectionSignal, OperatingFactSnapshot  # noqa: E402
from core.control_center_contracts import (  # noqa: E402
    ReviewRequest,
    ReviewResult,
    SubmitPatrolBatchRequest,
    SubmitPatrolBatchResult,
)
from core.inspection_run import InspectionRun  # noqa: E402
from core.result_envelope import (  # noqa: E402
    InspectionFeedbackEvent,
    InspectionResultAck,
    InspectionResultEnvelope,
)

CONTRACTS_DIR = ROOT / "contracts"

BASE_URI = "https://schemas.amazon-ops.local/contracts"

#: 模型 → 导出文件名。改文件名视为破坏性变更。
EXPORTS: list[tuple[type, str, str]] = [
    (
        InspectionRun,
        "inspection-run.v1.schema.json",
        "巡检单经营单元运行记录",
    ),
    (
        InspectionResultEnvelope,
        "inspection-result-envelope.v1.schema.json",
        "巡检 Agent 向结果接收端投递的纯巡检结果封套",
    ),
    (
        InspectionFeedbackEvent,
        "inspection-feedback-event.v1.schema.json",
        "中控向巡检 Agent 回传的处理反馈事件",
    ),
    (
        InspectionResultAck,
        "inspection-result-ack.v1.schema.json",
        "结果接收端确认纯巡检结果已完成业务接收的 ACK",
    ),
    (
        OperatingFactSnapshot,
        "operating-fact-snapshot.v1.schema.json",
        "冻结的统一经营事实快照",
    ),
    (
        InspectionSignal,
        "inspection-signal.v2.projection.json",
        "本项目对 inspection-signal.v2 的 Pydantic 投影（对照用，权威在 upstream/）",
    ),
    (
        SubmitPatrolBatchRequest,
        "control-center-submit-patrol-batch.v2.schema.json",
        "巡检 Agent 调用中控 submit_patrol_batch 的 v2.0 业务参数",
    ),
    (
        SubmitPatrolBatchResult,
        "control-center-submit-patrol-batch-result.v1.schema.json",
        "中控 submit_patrol_batch 的结构化业务结果",
    ),
    (
        ReviewRequest,
        "patrol-review-request.v1.schema.json",
        "中控调用巡检复盘 MCP 时提交的任务上下文",
    ),
    (
        ReviewResult,
        "patrol-review-result.v1.schema.json",
        "巡检 Agent 内部完成复盘后同步返回的最终结果",
    ),
]


def build_schema(model: type, filename: str, description: str) -> dict:
    schema = model.model_json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"{BASE_URI}/{filename}"
    schema["description"] = description
    return schema


def dump(schema: dict) -> str:
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="导出巡检 Agent 共享 JSON Schema")
    parser.add_argument("--check", action="store_true", help="只校验磁盘内容是否最新")
    args = parser.parse_args()

    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []

    for model, filename, description in EXPORTS:
        content = dump(build_schema(model, filename, description))
        target = CONTRACTS_DIR / filename

        if args.check:
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                stale.append(filename)
            continue

        target.write_text(content, encoding="utf-8")
        print(f"exported {filename}")

    if args.check:
        if stale:
            print("以下 Schema 与代码模型不一致，请重新导出：", file=sys.stderr)
            for name in stale:
                print(f"  - {name}", file=sys.stderr)
            print("  运行：python -m scripts.export_contracts", file=sys.stderr)
            return 1
        print("all exported schemas are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
