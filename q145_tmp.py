# -*- coding: utf-8 -*-
"""全量统计广告配置标签覆盖（keyset 分页，避免内存爆）。"""
import json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path("/opt/weilai-HealthCheck-Agent-v2.0/.env"))
from sqlalchemy import text
from sqlalchemy.engine import create_engine
from integrations.database import database_url

eng = create_engine(database_url(), pool_pre_ping=True)
W0 = "2026-08-23 16:00:00"

n_tot = n_warn = n_cfg = 0
FIELDS = ["productPosition", "productStage", "seasonType", "operatingMode",
          "advertPurposes", "targetKeywordTypes", "advertDirectionTypes", "dayRange"]
counts = {f: 0 for f in FIELDS}
values = {f: {} for f in FIELDS}

last = W0
with eng.connect() as conn:
    while True:
        rows = conn.execute(text(
            "SELECT fetched_at, extracted_data_json FROM t_patrol_raw_fact "
            "WHERE tool_name='erp_listing_advert_agent_config' AND fetched_at > :last "
            "ORDER BY fetched_at LIMIT 400"
        ), {"last": last}).fetchall()
        if not rows:
            break
        for fetched_at, e in rows:
            n_tot += 1
            try:
                arr = json.loads(e) if e else []
                item = arr[0] if isinstance(arr, list) and arr else arr
                if not isinstance(item, dict):
                    continue
                if "warn" in item:
                    n_warn += 1
                    continue
                n_cfg += 1
                for f in FIELDS:
                    v = item.get(f)
                    if v:
                        counts[f] += 1
                        values[f][str(v)[:24]] = values[f].get(str(v)[:24], 0) + 1
            except Exception:
                pass
        last = rows[-1][0]
        if n_tot > 20000:
            break

print("广告配置响应总数:", n_tot, "| warn(未配置):", n_warn, "| 真配置:", n_cfg, "(%.1f%%)" % (100.0 * n_cfg / n_tot if n_tot else 0))
print("\n【标签字段覆盖（真配置内）】")
for f in FIELDS:
    print("  %-22s %6d / %d  (%.1f%%)" % (f, counts[f], n_cfg, 100.0 * counts[f] / n_cfg if n_cfg else 0))
print("\n【主要值分布】")
for f in FIELDS[:7]:
    top = sorted(values[f].items(), key=lambda x: -x[1])[:5]
    print("  %s: %s" % (f, ", ".join("%s×%d" % (k, v) for k, v in top)))
eng.dispose()
