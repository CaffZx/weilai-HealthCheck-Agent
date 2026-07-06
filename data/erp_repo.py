"""ERP MySQL 只读/写。表名从 config/datasources.yaml 读，方便迁移。"""
from __future__ import annotations
import os, yaml, pymysql
from pathlib import Path

CFG = yaml.safe_load(Path(__file__).resolve().parent.parent.joinpath("config/datasources.yaml").read_text())["erp"]
T = CFG["tables"]


def _conn():
    return pymysql.connect(
        host=CFG["host"], port=CFG["port"], user=CFG["user"],
        password=os.environ[CFG["password_env"]], database=CFG["database"],
        charset=CFG["charset"], connect_timeout=10,
    )


def fetch_decision_configs(enabled_only: bool = True, limit: int | None = None) -> list[dict]:
    sql = f"SELECT * FROM {T['decision_config']}"
    conds = []
    if enabled_only:
        conds.append("enabled=1")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _conn() as c, c.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(sql)
        return cur.fetchall()
