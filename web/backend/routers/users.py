"""用户 & 主管映射相关接口：/api/users  /api/reports"""
from __future__ import annotations
import logging
import sqlite3

from fastapi import APIRouter, Query, Response

from data import local_store
from ..common import load_manager_map

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


@router.get("/users")
def get_users(response: Response):
    """返回有 ASIN 的负责人清单，供前端下拉框用。加 5 分钟浏览器缓存。"""
    response.headers["Cache-Control"] = "private, max-age=300, stale-while-revalidate=600"
    try:
        with sqlite3.connect(local_store.DB_PATH) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("""
                SELECT
                    COALESCE(o.principal_user_id, o.editor_id, NULLIF(o.creator_id,-1)) AS user_id,
                    u.user_name,
                    u.user_account,
                    COUNT(DISTINCT o.asin) AS asin_count
                FROM asin_owner o
                LEFT JOIN sys_user u
                  ON u.id = COALESCE(o.principal_user_id, o.editor_id, NULLIF(o.creator_id,-1))
                WHERE user_id IS NOT NULL AND u.user_name IS NOT NULL
                GROUP BY user_id, u.user_name, u.user_account
                ORDER BY asin_count DESC
            """).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("/api/users 查询失败: %s", e)
        return []


@router.get("/reports")
def api_reports(userId: int = Query(..., description="登录人 user_id")):
    """返回登录人的角色 + 下属列表（仅主管有下属）。"""
    with sqlite3.connect(local_store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        me = c.execute("SELECT id, user_name FROM sys_user WHERE id=?", (userId,)).fetchone()
        if not me:
            return {"self": None, "role": "unknown", "reports": []}
        manager_map = load_manager_map()
        report_ids = manager_map.get(userId, [])
        reports = []
        if report_ids:
            qs = ",".join("?" * len(report_ids))
            rrows = c.execute(f"SELECT id, user_name FROM sys_user WHERE id IN ({qs})", report_ids).fetchall()
            reports = [dict(r) for r in rrows]
        return {
            "self": dict(me),
            "role": "manager" if userId in manager_map else "operator",
            "reports": reports,
        }
