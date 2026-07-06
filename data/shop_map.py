"""shop_id → shop_account 映射。走 StarRocks dwd_shop，进程内缓存。"""
from __future__ import annotations
import os, yaml, pymysql
from functools import lru_cache
from pathlib import Path

CFG = yaml.safe_load(Path(__file__).resolve().parent.parent.joinpath("config/datasources.yaml").read_text())["starrocks"]


def _conn():
    return pymysql.connect(
        host=CFG["host"], port=CFG["port"], user=CFG["user"],
        password=os.environ[CFG["password_env"]], database=CFG["database"],
        charset="utf8mb4", connect_timeout=10,
    )


@lru_cache(maxsize=1)
def load_all() -> dict[int, dict]:
    with _conn() as c, c.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT id, account, shop_name, platform_code, platform_site_code, waste_flag FROM dwd_shop")
        return {r["id"]: r for r in cur.fetchall()}


def account(shop_id: int) -> str | None:
    m = load_all().get(int(shop_id))
    return m["account"] if m else None
