from dotenv import load_dotenv
import os, json
from sqlalchemy import create_engine, text

load_dotenv("/opt/weilai-HealthCheck-Agent-v2.0/.env")
eng = create_engine(os.environ["PATROL_DATABASE_URL"])

TAG_KEYS = [
    "productLevel",
    "productStage",
    "seasonStage",
    "adPurpose",
    "targetKeywordStrategy",
    "adDirection",
]

with eng.connect() as c:
    rows = c.execute(
        text(
            "SELECT payload_json FROM t_patrol_delivery_outbox "
            "WHERE aggregate_type = :at AND delivery_ack_json IS NOT NULL AND created_at > :t"
        ),
        {"at": "CONTROL_CENTER_PATROL_BATCH", "t": "2026-08-12 17:40:00"},
    ).mappings().all()

    total_units = 0
    tag_counts = {k: 0 for k in TAG_KEYS}
    any_tag = 0
    no_tag = 0
    full_tag = 0

    for row in rows:
        p = row["payload_json"]
        if isinstance(p, str):
            p = json.loads(p)
        for u in p.get("units", []):
            total_units += 1
            tags = u.get("tags", {}) or {}
            has_any = False
            has_all = True
            for k in TAG_KEYS:
                if tags.get(k):
                    tag_counts[k] += 1
                    has_any = True
                else:
                    has_all = False
            if has_any:
                any_tag += 1
            else:
                no_tag += 1
            if has_all:
                full_tag += 1

    print("=== 产品标签覆盖率（本次全量 batch 投递的 unit）===")
    print("总 unit 数:", total_units)
    print()
    print("各标签字段覆盖率:")
    for k in TAG_KEYS:
        cnt = tag_counts[k]
        pct = cnt / total_units * 100 if total_units else 0
        print("  %-22s %d / %d  (%.1f%%)" % (k, cnt, total_units, pct))
    print()
    print("有至少一个标签的 unit:", any_tag, "(%.1f%%)" % (any_tag / total_units * 100 if total_units else 0))
    print("完全无标签的 unit:", no_tag, "(%.1f%%)" % (no_tag / total_units * 100 if total_units else 0))
    print("6个标签全有的 unit:", full_tag, "(%.1f%%)" % (full_tag / total_units * 100 if total_units else 0))
