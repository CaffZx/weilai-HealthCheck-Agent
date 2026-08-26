from dotenv import load_dotenv
import os
from sqlalchemy import create_engine, text

load_dotenv("/opt/weilai-HealthCheck-Agent-v2.0/.env")
eng = create_engine(os.environ["PATROL_DATABASE_URL"])

with eng.begin() as c:
    r = c.execute(
        text(
            "UPDATE t_patrol_job SET status = :arch, locked_by = NULL, locked_at = NULL "
            "WHERE shop_id = :s AND status IN (:p, :r)"
        ),
        {"arch": "ARCHIVED", "s": 42453, "p": "PENDING", "r": "RUNNING"},
    )
    print("清理僵尸 job:", r.rowcount)

    for row in c.execute(
        text("SELECT status, COUNT(*) FROM t_patrol_job WHERE shop_id = :s GROUP BY status"),
        {"s": 42453},
    ):
        print("  ", row)
