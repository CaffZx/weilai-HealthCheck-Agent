# -*- coding: utf-8 -*-
"""确认 0 入队根因：product_info asin=null 占比 + 缺快照单元 vs 入队。"""
import os, json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path("/opt/weilai-HealthCheck-Agent-v2.0/.env"))
from integrations.database import database_url
from sqlalchemy import text
from sqlalchemy.engine import create_engine

eng = create_engine(database_url(), pool_pre_ping=True)
W = "2026-08-21 16:00:00"
out = []
with eng.connect() as conn:
    # 1) 今天定时轮 product_info 响应采样：asin 有值 vs null
    rows = conn.execute(text(
        "SELECT response_json FROM t_patrol_raw_fact WHERE fact_key='product_info' "
        "AND fetched_at >= :w ORDER BY fetched_at DESC LIMIT 100"
    ), {"w": W}).fetchall()
    ok_asin, null_asin, parse_fail = 0, 0, 0
    null_sample = None
    for (r,) in rows:
        s = str(r)
        try:
            d = json.loads(s)
            txt = d["content"][0]["text"]
            inner = json.loads(txt)
            data = inner.get("data", [])
            if not data:
                parse_fail += 1
                continue
            child_rows = [x for x in data if x.get("sellerSku")]
            if child_rows and all(not (x.get("asin") or "").strip() for x in child_rows):
                null_asin += 1
                if null_sample is None:
                    null_sample = child_rows[0]
            else:
                ok_asin += 1
        except Exception:
            parse_fail += 1
    out.append("product_info 采样 %d: asin正常=%d asin=null=%d 解析失败=%d" % (
        len(rows), ok_asin, null_asin, parse_fail))
    if null_sample:
        out.append("asin=null 样本行键: %s" % list(null_sample.keys())[:20])
        out.append("样本: %s" % json.dumps(null_sample, ensure_ascii=False)[:200])

    # 2) 缺新鲜快照单元数（今天定时轮跑过但快照不新鲜）
    missing = conn.execute(text(
        "SELECT COUNT(DISTINCT c.operating_unit_id) FROM t_patrol_operating_unit_catalog c "
        "LEFT JOIN t_patrol_child_fact_snapshot s ON s.operating_unit_id=c.operating_unit_id AND s.expires_at>=NOW() "
        "WHERE s.operating_unit_id IS NULL"
    )).scalar()
    out.append("\n缺新鲜快照单元: %d" % missing)

    # 3) 今天定时轮的 run 里，缺快照单元占多少（这些应该入队但没入）
    runs_today = conn.execute(text(
        "SELECT COUNT(DISTINCT operating_unit_id) FROM t_patrol_run WHERE started_at >= :w"
    ), {"w": W}).scalar()
    out.append("今天定时轮 run 单元: %d" % runs_today)

    # 4) 8-15 僵尸任务与 8-19 任务状态
    z = conn.execute(text(
        "SELECT status, COUNT(*) FROM t_patrol_child_fact_task WHERE created_at >= '2026-08-15 12:00:00' AND created_at < '2026-08-15 13:00:00' GROUP BY status"
    )).fetchall()
    out.append("8-15 任务状态: %s" % [tuple(r) for r in z])
print("\n".join(out))
eng.dispose()
