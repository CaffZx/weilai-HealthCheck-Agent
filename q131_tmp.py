# -*- coding: utf-8 -*-
"""查往中控投递的内容：outbox 状态 + 最近 DELIVERED 载荷的 proposal 分布。"""
import os, json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path("/opt/weilai-HealthCheck-Agent-v2.0/.env"))
from integrations.database import database_url
from sqlalchemy import text
from sqlalchemy.engine import create_engine

eng = create_engine(database_url(), pool_pre_ping=True)
out = []
with eng.connect() as conn:
    # 1) outbox 总量与状态
    rows = conn.execute(text(
        "SELECT aggregate_type, status, COUNT(*) FROM t_patrol_delivery_outbox "
        "WHERE created_at >= '2026-08-21 00:00:00' GROUP BY aggregate_type, status"
    )).fetchall()
    out.append("== 今日 outbox 按类型+状态 ==")
    for r in rows:
        out.append("  %s | %s | %d" % (r[0], r[1], r[2]))
    # 2) CC 管道 DELIVERED 最近一条的载荷
    row = conn.execute(text(
        "SELECT payload_json, delivery_ack_json FROM t_patrol_delivery_outbox "
        "WHERE aggregate_type='CONTROL_CENTER_PATROL_BATCH' AND status='DELIVERED' "
        "ORDER BY created_at DESC LIMIT 1"
    )).fetchone()
    if row:
        p = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        units = p.get("units") or []
        out.append("\n== 最近 DELIVERED CC 批次 ==")
        out.append("patrolBatchNo: %s | 单元数: %d" % (p.get("patrolBatchNo"), len(units)))
        ack = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        out.append("中控 ACK: %s" % json.dumps(ack, ensure_ascii=False)[:120])
        # proposal.details 分布
        from collections import Counter
        cnt = Counter()
        samples = {"real": [], "gap": []}
        for u in units[:50]:
            details = (u.get("proposal") or {}).get("details") or []
            for d in details:
                reason = str(d.get("reason") or "")
                uid = str(d.get("anomalyUid") or "")
                if "DATA-GAP" in uid or "未检出" in reason:
                    cnt["缺口占位(REPAIR_DATA_AND_REVIEW)"] += 1
                    if len(samples["gap"]) < 1:
                        samples["gap"].append({"uid": uid[:40], "reason": reason[:60], "action": d.get("action")})
                else:
                    cnt["真实异常(MANUAL_REVIEW_ONLY)"] += 1
                    if len(samples["real"]) < 2:
                        samples["real"].append({"uid": uid[:40], "reason": reason[:60], "action": d.get("action")})
        out.append("proposal.details 分布（前50单元）:")
        for k, v in cnt.most_common():
            out.append("  %s: %d" % (k, v))
        out.append("\n真实异常样例:")
        for s in samples["real"]:
            out.append("  %s" % json.dumps(s, ensure_ascii=False))
        out.append("缺口占位样例:")
        for s in samples["gap"]:
            out.append("  %s" % json.dumps(s, ensure_ascii=False))
        # 3) 单元字段构成
        u0 = units[0]
        out.append("\n单元字段: %s" % list(u0.keys()))
        out.append("proposal 字段: %s" % list((u0.get("proposal") or {}).keys()))
        fs = u0.get("factSnapshots")
        out.append("factSnapshots 数量: %d" % (len(fs) if isinstance(fs, list) else 0))
        om = u0.get("operatingMetric")
        out.append("operatingMetric 字段: %s" % (list(om.keys()) if isinstance(om, dict) else type(om).__name__))
print("\n".join(out))
eng.dispose()
