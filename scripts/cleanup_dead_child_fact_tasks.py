"""清理终态失败任务：仅删 DEAD，且不破坏快照外键。"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text

from integrations.database import database_url


def main() -> int:
    eng = create_engine(database_url(), pool_pre_ping=True)
    with eng.begin() as c:
        before = dict(
            c.execute(
                text("SELECT status, COUNT(*) c FROM t_patrol_child_fact_task GROUP BY status")
            ).fetchall()
        )
        print("before_tasks", before)

        linked = c.execute(
            text(
                """
                SELECT COUNT(*) FROM t_patrol_child_fact_task t
                WHERE t.status = 'DEAD'
                  AND EXISTS (
                    SELECT 1 FROM t_patrol_child_fact_snapshot s
                    WHERE s.task_id = t.task_id
                  )
                """
            )
        ).scalar()
        print("dead_with_snapshot_fk", linked)

        r1 = c.execute(
            text(
                """
                DELETE FROM t_patrol_child_fact_task
                WHERE status = 'DEAD'
                  AND NOT EXISTS (
                    SELECT 1 FROM t_patrol_child_fact_snapshot s
                    WHERE s.task_id = t_patrol_child_fact_task.task_id
                  )
                """
            )
        )
        print("deleted_unlinked_dead_tasks", r1.rowcount)

        # 仍被快照引用的 DEAD：保留行但清空错误信息，避免队列噪音；不删以免伤快照
        r2 = c.execute(
            text(
                """
                UPDATE t_patrol_child_fact_task
                SET last_error_code = NULL,
                    last_error_message = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = UTC_TIMESTAMP()
                WHERE status = 'DEAD'
                  AND EXISTS (
                    SELECT 1 FROM t_patrol_child_fact_snapshot s
                    WHERE s.task_id = t_patrol_child_fact_task.task_id
                  )
                """
            )
        )
        print("kept_dead_linked_to_snapshot", r2.rowcount)

        r3 = c.execute(text("DELETE FROM t_patrol_delivery_outbox WHERE status = 'DEAD'"))
        print("deleted_outbox_dead", r3.rowcount)

        after = dict(
            c.execute(
                text("SELECT status, COUNT(*) c FROM t_patrol_child_fact_task GROUP BY status")
            ).fetchall()
        )
        print("after_tasks", after)
        print(
            "after_outbox_dead",
            c.execute(
                text("SELECT COUNT(*) FROM t_patrol_delivery_outbox WHERE status = 'DEAD'")
            ).scalar(),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
