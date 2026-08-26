# -*- coding: utf-8 -*-
"""数据层健康检查：抓取→归一化→判定→投递全链路（今天 8-25 轮）。"""
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path("/opt/weilai-HealthCheck-Agent-v2.0/.env"))
from sqlalchemy import text
from sqlalchemy.engine import create_engine
from integrations.database import database_url

eng = create_engine(database_url(), pool_pre_ping=True)
W0 = "2026-08-24 16:00:00"   # 今天 00:05 CST 轮起（UTC）
out = []

with eng.connect() as conn:
    # ========== 1. 判定层（run） ==========
    r = conn.execute(text(
        "SELECT status, COUNT(*) FROM t_patrol_run WHERE started_at >= :w0 GROUP BY status"
    ), {"w0": W0}).fetchall()
    tot = sum(x[1] for x in r)
    out.append("【1. 判定层 run】总 %d" % tot)
    for x in r:
        out.append("   %-22s %6d (%.1f%%)" % (x[0], x[1], 100.0 * x[1] / tot if tot else 0))
    err = conn.execute(text(
        "SELECT error_code, COUNT(*) FROM t_patrol_run WHERE started_at >= :w0 AND error_code IS NOT NULL GROUP BY error_code ORDER BY 2 DESC LIMIT 5"
    ), {"w0": W0}).fetchall()
    out.append("   run 错误码: %s" % ([tuple(x) for x in err] or "无"))

    # ========== 2. 抓取层（raw_fact） ==========
    r2 = conn.execute(text(
        "SELECT tool_name, status, COUNT(*) AS c FROM t_patrol_raw_fact "
        "WHERE fetched_at >= :w0 GROUP BY tool_name, status ORDER BY c DESC LIMIT 30"
    ), {"w0": W0}).fetchall()
    out.append("\n【2. 抓取层 raw_fact 按工具/状态】")
    for x in r2:
        out.append("   %-40s %-10s %6d" % (x[0], x[1], x[2]))

    # ========== 3. 归一化层（快照完整度） ==========
    r3 = conn.execute(text(
        "SELECT quality_status, COUNT(*), ROUND(AVG(completeness_score),1) FROM t_patrol_fact_snapshot "
        "WHERE created_at >= :w0 GROUP BY quality_status"
    ), {"w0": W0}).fetchall()
    out.append("\n【3. 归一化层 快照完整度】")
    for x in r3:
        out.append("   %-12s %6d  avg_score=%s" % (x[0], x[1], x[2]))

    # ========== 4. 子体任务 ==========
    r4 = conn.execute(text(
        "SELECT status, COUNT(*) FROM t_patrol_child_fact_task WHERE updated_at >= :w0 GROUP BY status"
    ), {"w0": W0}).fetchall()
    out.append("\n【4. 子体抓取任务】")
    for x in r4:
        out.append("   %-10s %6d" % (x[0], x[1]))

    # ========== 5. 投递层 ==========
    r5 = conn.execute(text(
        "SELECT status, COUNT(*) FROM t_patrol_delivery_outbox "
        "WHERE aggregate_type='CONTROL_CENTER_PATROL_BATCH' AND created_at >= :w0 GROUP BY status"
    ), {"w0": W0}).fetchall()
    out.append("\n【5. 投递层 CC 管道（今日）】")
    for x in r5:
        out.append("   %-10s %6d" % (x[0], x[1]))

    # ========== 6. 信号层 ==========
    r6 = conn.execute(text(
        "SELECT occurrence_type, COUNT(*) FROM t_patrol_signal_occurrence o "
        "JOIN t_patrol_run r ON o.run_id=r.run_id WHERE r.started_at >= :w0 GROUP BY occurrence_type"
    ), {"w0": W0}).fetchall()
    out.append("\n【6. 信号层（今日）】")
    for x in r6:
        out.append("   %-20s %6d" % (x[0], x[1]))

print("\n".join(out))
eng.dispose()
