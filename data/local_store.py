"""本地数据缓存库 (SQLite) 访问层。

设计：所有 MCP 原生字段原封不动进 data(JSON)；热点字段用生成列(native 名)暴露。
加新字段：promote_field() 一行搞定，或直接用 json_extract 查。

用法：
    from data import local_store as store
    store.init_db()
    store.upsert_daily_sales(asin, parent_asin, shop_account, site_code, "2026-06-15", row_dict)
    rows = store.recent_daily_sales(asin, days=7)          # 近7天序列
    store.promote_field("daily_product_sales", "自然订单占比")  # 加新字段
"""
from __future__ import annotations
import sqlite3, json, datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "local" / "healthcheck.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
FREEZE_DAYS = 14  # 归因窗口，超过则冻结不再重取


class ActiveObservationExistsError(RuntimeError):
    """同一异常已有尚未结束的效果观察。"""


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")   # 锁等待 30s，避免批巡 8 线程 + API 并发写时立即 database is locked
    c.execute("PRAGMA journal_mode=WAL")      # 读写不互斥，进一步降低锁冲突（要求 DB 在本地盘）
    c.execute("PRAGMA foreign_keys=ON")
    return c


def connect() -> sqlite3.Connection:
    """公开连接工厂：带 busy_timeout(30s)/WAL/外键。web 层写路径统一走这里，避免并发 database is locked。"""
    return _conn()


def init_db() -> None:
    """建表（幂等）。"""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with _conn() as c:
        c.executescript(sql)
        task_action_columns = {row[1] for row in c.execute("PRAGMA table_info(task_action)")}
        if "maintenance_id" not in task_action_columns:
            c.execute("ALTER TABLE task_action ADD COLUMN maintenance_id INTEGER")
        c.execute("CREATE INDEX IF NOT EXISTS idx_task_action_maintenance ON task_action(maintenance_id)")
        observation_columns = {row[1] for row in c.execute("PRAGMA table_info(observation_case)")}
        for name, definition in {
            "schedule_rule_id": "TEXT",
            "schedule_rule_version": "TEXT",
            "follow_up_type": "TEXT",
            "schedule_source": "TEXT NOT NULL DEFAULT 'legacy'",
            "default_observation_at": "TEXT",
            "default_next_inspection_at": "TEXT",
            "expected_available_at": "TEXT",
            "schedule_override_reason": "TEXT",
        }.items():
            if name not in observation_columns:
                c.execute(f"ALTER TABLE observation_case ADD COLUMN {name} {definition}")
        _migrate_observation_cases(c)
        maintenance_event_columns = {row[1] for row in c.execute("PRAGMA table_info(product_maintenance_event)")}
        if maintenance_event_columns and "variant" not in maintenance_event_columns:
            c.execute("ALTER TABLE product_maintenance_event ADD COLUMN variant TEXT")
        c.execute("""
            UPDATE product_maintenance_event
            SET variant=COALESCE((
                SELECT e.命中变体 FROM event_pool e
                WHERE e.唯一识别=product_maintenance_event.event_uid
            ), '')
            WHERE variant IS NULL
        """)
        _migrate_shop_scoped_tables(c, sql)
        columns = {row[1] for row in c.execute("PRAGMA table_info(event_pool)")}
        if "inspection_result_id" not in columns:
            c.execute("ALTER TABLE event_pool ADD COLUMN inspection_result_id INTEGER")
        c.execute("CREATE INDEX IF NOT EXISTS idx_event_result ON event_pool(inspection_result_id)")
        _migrate_task_action_statuses(c)
        _repair_event_history_integrity(c)
        _repair_pending_review_statuses(c)
        _repair_active_observation_duplicates(c)
        # 先收敛历史重复数据，再加业务唯一约束；否则老库会在启动时建索引失败。
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_observation_case_one_active_event
            ON observation_case(event_uid)
            WHERE status IN ('观察中', '待运营确认')
        """)


def _migrate_observation_cases(conn: sqlite3.Connection) -> None:
    """将旧产品级观察拆成异常级历史记录，只执行一次。"""
    marker = "observation_case_from_product_maintenance_v1"
    if conn.execute("SELECT 1 FROM app_migration WHERE migration_key=?", (marker,)).fetchone():
        return
    conn.execute("""
        INSERT OR IGNORE INTO observation_case (
          parent_asin, shop_account, event_uid, completion_action_id, user_id,
          issue, severity, variant, result, actual_action, notes,
          baseline_result_id, baseline_snapshot, executed_at, observation_at,
          next_inspection_at, inspection_time_source, status, agent_effect,
          agent_summary, agent_observed_at, agent_payload, confirmed_at,
          confirmed_by, confirmation, created_at, updated_at
        )
        SELECT pm.parent_asin, pm.shop_account, me.event_uid,
               (SELECT a.id FROM task_action a
                WHERE a.maintenance_id=pm.id AND a.event_uid=me.event_uid
                  AND a.action_type='完成'
                ORDER BY a.id DESC LIMIT 1),
               pm.user_id, me.issue, me.severity, me.variant,
               pm.result, pm.actual_action, pm.notes,
               me.baseline_result_id, me.baseline_snapshot, pm.executed_at,
               pm.observation_at, pm.next_inspection_at, pm.inspection_time_source,
               pm.status, pm.agent_effect, pm.agent_summary, pm.agent_observed_at,
               pm.agent_payload, pm.confirmed_at, pm.confirmed_by, pm.confirmation,
               pm.created_at, pm.updated_at
        FROM product_maintenance pm
        JOIN product_maintenance_event me ON me.maintenance_id=pm.id
    """)
    conn.execute(
        "INSERT INTO app_migration (migration_key, details) VALUES (?, ?)",
        (marker, json.dumps({"source": "product_maintenance"}, ensure_ascii=False)),
    )


def _migrate_task_action_statuses(conn: sqlite3.Connection) -> None:
    """一次性将旧工作台操作回填到 event_pool 的正式状态机。"""
    marker = "event_status_backfill_v1"
    if conn.execute("SELECT 1 FROM app_migration WHERE migration_key=?", (marker,)).fetchone():
        return

    rows = conn.execute("""
        SELECT a.event_uid, a.action_type, a.review_at, a.notes, a.created_at,
               e.id, e.当前状态, e.严重度
        FROM task_action a
        JOIN event_pool e ON e.唯一识别=a.event_uid
        WHERE a.id IN (SELECT MAX(id) FROM task_action GROUP BY event_uid)
    """).fetchall()
    targets = {
        "标记处理中": "处理中",
        "完成": "已处理待复扫",
        "不处理": "忽略",
        "待复查": "新发现",
    }
    changed = 0
    for row in rows:
        target = targets.get(row["action_type"])
        if not target or target == row["当前状态"]:
            continue
        conn.execute("""
            UPDATE event_pool
            SET 当前状态=?, 下一次复查时间=CASE WHEN ?='已处理待复扫' THEN ? ELSE 下一次复查时间 END,
                复查时间来源=CASE WHEN ?='已处理待复扫' THEN 'manual' ELSE 复查时间来源 END,
                上次处理动作=?, 上次处理时间=?, 更新时间=datetime('now','localtime')
            WHERE id=?
        """, (target, target, row["review_at"], target, row["action_type"], row["created_at"], row["id"]))
        conn.execute("""
            INSERT INTO event_state_log
              (event_id, 变更前状态, 变更后状态, 变更前严重度, 变更后严重度,
               变更类型, 变更原因, 操作人, 上下文)
            VALUES (?,?,?,?,?,'历史回填','根据旧工作台最新操作回填正式状态','系统','{}')
        """, (row["id"], row["当前状态"], target, row["严重度"], row["严重度"]))
        changed += 1
    conn.execute(
        "INSERT INTO app_migration (migration_key, details) VALUES (?, ?)",
        (marker, json.dumps({"changed_events": changed}, ensure_ascii=False)),
    )


def _repair_event_history_integrity(conn: sqlite3.Connection) -> None:
    """清理早期无父事件的状态日志，并记录一次性迁移结果。"""
    marker = "event_history_integrity_v1"
    if conn.execute("SELECT 1 FROM app_migration WHERE migration_key=?", (marker,)).fetchone():
        return

    orphan_count = conn.execute("""
        SELECT COUNT(*)
        FROM event_state_log AS log
        LEFT JOIN event_pool AS event ON event.id=log.event_id
        WHERE event.id IS NULL
    """).fetchone()[0]
    if orphan_count:
        conn.execute("""
            DELETE FROM event_state_log
            WHERE NOT EXISTS (
                SELECT 1 FROM event_pool WHERE event_pool.id=event_state_log.event_id
            )
        """)
    conn.execute(
        "INSERT INTO app_migration (migration_key, details) VALUES (?, ?)",
        (marker, json.dumps({"removed_orphan_state_logs": orphan_count}, ensure_ascii=False)),
    )


def _repair_pending_review_statuses(conn: sqlite3.Connection) -> None:
    """将旧版“待复查”操作恢复为正式待观察状态，避免任务与历史记录冲突。"""
    marker = "event_pending_review_backfill_v1"
    if conn.execute("SELECT 1 FROM app_migration WHERE migration_key=?", (marker,)).fetchone():
        return

    rows = conn.execute("""
        SELECT e.id, e.当前状态, e.严重度, a.created_at
        FROM event_pool e
        JOIN task_action a ON a.id=(
            SELECT id FROM task_action
            WHERE event_uid=e.唯一识别
            ORDER BY id DESC LIMIT 1
        )
        WHERE e.当前状态='新发现' AND a.action_type='待复查'
    """).fetchall()
    for row in rows:
        conn.execute("""
            UPDATE event_pool
            SET 当前状态='待观察', 上次处理动作='待复查', 上次处理时间=?,
                更新时间=datetime('now','localtime')
            WHERE id=?
        """, (row["created_at"], row["id"]))
        conn.execute("""
            INSERT INTO event_state_log
              (event_id, 变更前状态, 变更后状态, 变更前严重度, 变更后严重度,
               变更类型, 变更原因, 操作人, 上下文)
            VALUES (?,?,?,?,?,'历史回填','根据旧版待复查操作恢复待观察状态','系统','{}')
        """, (row["id"], row["当前状态"], "待观察", row["严重度"], row["严重度"]))
    conn.execute(
        "INSERT INTO app_migration (migration_key, details) VALUES (?, ?)",
        (marker, json.dumps({"changed_events": len(rows)}, ensure_ascii=False)),
    )


def _repair_active_observation_duplicates(conn: sqlite3.Connection) -> int:
    """历史数据中每个异常只保留一条有效观察，其他记录保留为可追溯历史。"""
    rows = conn.execute("""
        SELECT id, event_uid, confirmation, updated_at
        FROM observation_case
        WHERE status IN ('观察中', '待运营确认')
        ORDER BY event_uid, id
    """).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["event_uid"], []).append(row)

    replaced = 0
    for event_uid, cases in grouped.items():
        if len(cases) < 2:
            continue
        # “继续观察”表示运营已确认原观察周期应延续，优先于后续误建的观察记录。
        keep = max(
            cases,
            key=lambda case: (
                case["confirmation"] == "继续观察",
                case["updated_at"] or "",
                case["id"],
            ),
        )
        duplicate_ids = [case["id"] for case in cases if case["id"] != keep["id"]]
        conn.executemany("""
            UPDATE observation_case
            SET status='已被重复观察替代', updated_at=datetime('now','localtime')
            WHERE id=?
        """, [(case_id,) for case_id in duplicate_ids])
        replaced += len(duplicate_ids)
    return replaced


def _migrate_shop_scoped_tables(conn: sqlite3.Connection, schema_sql: str) -> None:
    """将历史表从 ASIN 粒度安全迁移为店铺粒度。"""
    expected = {
        "daily_product_sales": ["asin", "shop_account", "stat_date"],
        "daily_ad_product": ["asin", "shop_account", "stat_date"],
        "sales_child": ["asin", "parent_asin", "shop_account"],
        "daily_natural_ad_flow": ["asin", "shop_account", "stat_date", "is_summary"],
    }
    statements = schema_sql.splitlines()
    for table, expected_pk in expected.items():
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not info:
            continue
        current_pk = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        if current_pk == expected_pk:
            continue

        start = next((i for i, line in enumerate(statements)
                      if line.startswith(f"CREATE TABLE IF NOT EXISTS {table} (")), None)
        if start is None:
            raise RuntimeError(f"找不到 {table} 的 schema 定义")
        end = next(i for i in range(start + 1, len(statements))
                   if statements[i].strip() == ");")
        create_sql = "\n".join(statements[start:end + 1])
        temp = f"{table}__shop_scoped"
        create_sql = create_sql.replace(
            f"CREATE TABLE IF NOT EXISTS {table}",
            f"CREATE TABLE {temp}",
            1,
        )
        conn.execute(f'DROP TABLE IF EXISTS "{temp}"')
        conn.execute(create_sql)

        columns = [row[1] for row in conn.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
                   if row[6] == 0]
        names = ",".join(f'"{column}"' for column in columns)
        conn.execute(f'INSERT INTO "{temp}" ({names}) SELECT {names} FROM "{table}"')
        conn.execute(f'DROP TABLE "{table}"')
        conn.execute(f'ALTER TABLE "{temp}" RENAME TO "{table}"')

        if table == "daily_product_sales":
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dps_parent ON daily_product_sales(parent_asin, stat_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dps_shop ON daily_product_sales(shop_account)")
        elif table == "daily_ad_product":
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dap_parent ON daily_ad_product(parent_asin, stat_date)")
        elif table == "sales_child":
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sc_parent ON sales_child(parent_asin)")
        else:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_naf_parent ON daily_natural_ad_flow(parent_asin, stat_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_naf_shop ON daily_natural_ad_flow(shop_account)")


def _j(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False)


def normalize_site_code(site_code: str | None) -> str | None:
    """统一站点代码为 Amazon_US 形式。"""
    if not site_code:
        return site_code
    return site_code if site_code.startswith("Amazon_") else f"Amazon_{site_code}"


def upsert_inspection_result(batch_no: str, parent_asin: str, shop_account: str,
                             site_code: str | None, card: dict,
                             result_status: str = "SUCCESS") -> None:
    """持久化单产品巡检结果；同批次重试覆盖同一产品记录。"""
    priority_info = card.get("优先级信息") or {}
    with _conn() as c:
        c.execute(
            """INSERT INTO inspection_result
               (batch_no,parent_asin,shop_account,site_code,result_status,priority,score,
                anomaly_count,result_json)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(batch_no,parent_asin,shop_account) DO UPDATE SET
                 site_code=excluded.site_code, result_status=excluded.result_status,
                 priority=excluded.priority, score=excluded.score,
                 anomaly_count=excluded.anomaly_count, result_json=excluded.result_json,
                 updated_at=datetime('now','localtime')""",
            (batch_no, parent_asin, shop_account, normalize_site_code(site_code),
             result_status, priority_info.get("执行优先级"),
             priority_info.get("产品执行分数", 0), len(card.get("异常明细") or []),
             _j(card)),
        )


def query_latest_inspection_results() -> dict[tuple[str, str], dict]:
    """读取每个产品/店铺最新的成功巡检结果，失败结果不能覆盖业务展示。"""
    with _conn() as c:
        rows = c.execute(
            """SELECT r.*
               FROM inspection_result r
               JOIN (
                 SELECT parent_asin, shop_account, MAX(id) AS max_id
                 FROM inspection_result
                 WHERE result_status='SUCCESS'
                 GROUP BY parent_asin, shop_account
               ) latest ON latest.max_id=r.id"""
        ).fetchall()
    results = {}
    for row in rows:
        item = dict(row)
        try:
            item["result_json"] = json.loads(item["result_json"])
        except (TypeError, json.JSONDecodeError):
            item["result_json"] = {}
        results[(item["parent_asin"], item["shop_account"])] = item
    return results


def link_inspection_events(batch_no: str, parent_asin: str, shop_account: str) -> int:
    """将指定产品本批次事件关联到对应巡检结果。"""
    with _conn() as c:
        result = c.execute(
            "SELECT id FROM inspection_result WHERE batch_no=? AND parent_asin=? AND shop_account=?",
            (batch_no, parent_asin, shop_account),
        ).fetchone()
        if not result:
            return 0
        cur = c.execute(
            """UPDATE event_pool SET inspection_result_id=?
               WHERE 最近巡检批次=? AND 父ASIN=? AND 店铺账号=?""",
            (result["id"], batch_no, parent_asin, shop_account),
        )
        return cur.rowcount


def create_product_maintenance(
    conn: sqlite3.Connection,
    *, parent_asin: str, shop_account: str, user_id: int | None,
    result: str | None, actual_action: str | None, notes: str | None,
    observation_at: str, next_inspection_at: str,
    events: list[sqlite3.Row],
) -> int:
    """在现有产品维护事务中创建产品级观察记录及异常基线。"""
    def row_value(row: sqlite3.Row, name: str):
        """从历史/精简查询返回的 Row 中安全读取字段。"""
        return row[name] if name in row.keys() else None

    conn.execute("""
        UPDATE product_maintenance
        SET status='已被新维护替代', updated_at=datetime('now','localtime')
        WHERE parent_asin=? AND shop_account=?
          AND status IN ('观察中', '待运营确认')
    """, (parent_asin, shop_account))
    cur = conn.execute("""
        INSERT INTO product_maintenance
          (parent_asin, shop_account, user_id, result, actual_action, notes,
           observation_at, next_inspection_at)
        VALUES (?,?,?,?,?,?,?,?)
    """, (parent_asin, shop_account, user_id, result, actual_action, notes,
           observation_at, next_inspection_at))
    maintenance_id = cur.lastrowid
    for event in events:
        baseline_snapshot = None
        inspection_result_id = row_value(event, "inspection_result_id")
        issue = row_value(event, "问题点位")
        severity = row_value(event, "严重度")
        event_uid = row_value(event, "唯一识别")
        variant = row_value(event, "命中变体")
        if inspection_result_id:
            result_row = conn.execute(
                "SELECT result_json FROM inspection_result WHERE id=?",
                (inspection_result_id,),
            ).fetchone()
            if result_row:
                try:
                    card = json.loads(result_row["result_json"] or "{}")
                    detail = next((item for item in card.get("异常明细") or []
                                   if issue is not None
                                   and severity is not None
                                   and item.get("问题点位") == issue
                                   and item.get("该条严重度") == severity
                                   and (item.get("命中变体") or "") == (variant or "")), None)
                    baseline_snapshot = json.dumps(
                        (detail or {}).get("数据快照"), ensure_ascii=False
                    ) if (detail or {}).get("数据快照") is not None else None
                except (TypeError, json.JSONDecodeError):
                    baseline_snapshot = None
        conn.execute("""
            INSERT INTO product_maintenance_event
              (maintenance_id, event_uid, issue, severity, variant, baseline_result_id, baseline_snapshot)
            VALUES (?,?,?,?,?,?,?)
        """, (maintenance_id, event_uid, issue, severity, variant,
               inspection_result_id, baseline_snapshot))
    conn.execute("""
        INSERT INTO observation_report (maintenance_id, agent_status)
        VALUES (?, '待观察')
    """, (maintenance_id,))
    return maintenance_id


def create_observation_case(
    conn: sqlite3.Connection,
    *, event: sqlite3.Row, completion_action_id: int, user_id: int | None,
    result: str | None, actual_action: str | None, notes: str | None,
    observation_at: str, next_inspection_at: str, schedule: dict | None = None,
) -> int:
    """为一次单异常完成建立可独立复盘的观察周期。"""
    def value(name: str):
        return event[name] if name in event.keys() else None

    baseline_snapshot = None
    result_id = value("inspection_result_id")
    if result_id:
        row = conn.execute(
            "SELECT result_json FROM inspection_result WHERE id=?", (result_id,)
        ).fetchone()
        if row:
            try:
                card = json.loads(row["result_json"] or "{}")
                issue = next(
                    (item for item in card.get("异常明细") or []
                     if item.get("问题点位") == value("问题点位")
                     and (item.get("命中变体") or "") == (value("命中变体") or "")),
                    None,
                )
                if issue and issue.get("数据快照") is not None:
                    baseline_snapshot = json.dumps(issue["数据快照"], ensure_ascii=False)
            except (TypeError, json.JSONDecodeError):
                baseline_snapshot = None
    event_uid = value("唯一识别")
    active = conn.execute("""
        SELECT id FROM observation_case
        WHERE event_uid=? AND status IN ('观察中', '待运营确认')
        LIMIT 1
    """, (event_uid,)).fetchone()
    if active:
        raise ActiveObservationExistsError(f"异常 {event_uid} 已有进行中的效果观察（#{active['id']}）")
    schedule = schedule or {}
    try:
        cursor = conn.execute("""
            INSERT INTO observation_case (
              parent_asin, shop_account, event_uid, completion_action_id, user_id,
              issue, severity, variant, result, actual_action, notes,
              baseline_result_id, baseline_snapshot, observation_at, next_inspection_at,
              schedule_rule_id, schedule_rule_version, follow_up_type, schedule_source,
              default_observation_at, default_next_inspection_at, expected_available_at,
              schedule_override_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            value("父ASIN"), value("店铺账号"), event_uid, completion_action_id, user_id,
            value("问题点位"), value("严重度"), value("命中变体"), result, actual_action, notes,
            result_id, baseline_snapshot, observation_at, next_inspection_at,
            schedule.get("rule_id"), schedule.get("rule_version"), schedule.get("follow_up_type"),
            schedule.get("source", "legacy"), schedule.get("default_observation_at"),
            schedule.get("default_next_inspection_at"), schedule.get("expected_available_at"),
            schedule.get("override_reason"),
        ))
    except sqlite3.IntegrityError as error:
        active = conn.execute("""
            SELECT id FROM observation_case
            WHERE event_uid=? AND status IN ('观察中', '待运营确认')
            LIMIT 1
        """, (event_uid,)).fetchone()
        if active:
            raise ActiveObservationExistsError(f"异常 {event_uid} 已有进行中的效果观察") from error
        raise
    return cursor.lastrowid


def backfill_inspection_event_links() -> int:
    """按批次、ASIN、店铺补齐已有事件的巡检结果关联。"""
    with _conn() as c:
        cur = c.execute(
            """UPDATE event_pool
               SET inspection_result_id=(
                 SELECT r.id FROM inspection_result r
                 WHERE r.batch_no=event_pool.最近巡检批次
                   AND r.parent_asin=event_pool.父ASIN
                   AND r.shop_account=event_pool.店铺账号
               )
               WHERE 最近巡检批次 IS NOT NULL
                 AND inspection_result_id IS NULL
                 AND EXISTS (
                   SELECT 1 FROM inspection_result r
                   WHERE r.batch_no=event_pool.最近巡检批次
                     AND r.parent_asin=event_pool.父ASIN
                     AND r.shop_account=event_pool.店铺账号
                 )"""
        )
        return cur.rowcount


# ---------------- 每日类（单日窗口循环写入） ----------------
def upsert_daily_sales(asin, parent_asin, shop_account, site_code, stat_date, row: dict) -> None:
    frozen = _is_frozen(stat_date)
    with _conn() as c:
        c.execute("""INSERT INTO daily_product_sales(asin,parent_asin,shop_account,site_code,stat_date,data,is_frozen)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(asin,shop_account,stat_date) DO UPDATE SET
                       data=excluded.data, fetched_at=datetime('now','localtime'), is_frozen=excluded.is_frozen
                     WHERE daily_product_sales.is_frozen=0""",
                  (asin, parent_asin, shop_account, site_code, stat_date, _j(row), frozen))


def upsert_daily_ad(asin, parent_asin, shop_account, site_code, stat_date, row: dict) -> None:
    frozen = _is_frozen(stat_date)
    with _conn() as c:
        c.execute("""INSERT INTO daily_ad_product(asin,parent_asin,shop_account,site_code,stat_date,data,is_frozen)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(asin,shop_account,stat_date) DO UPDATE SET
                       data=excluded.data, fetched_at=datetime('now','localtime'), is_frozen=excluded.is_frozen
                     WHERE daily_ad_product.is_frozen=0""",
                  (asin, parent_asin, shop_account, site_code, stat_date, _j(row), frozen))


def _is_frozen(stat_date: str) -> int:
    try:
        d = datetime.date.fromisoformat(stat_date)
    except ValueError:
        return 0
    return 1 if (datetime.date.today() - d).days > FREEZE_DAYS else 0


# ---------------- 快照类（每日/每周刷） ----------------
def upsert_sales_child(asin, parent_asin, shop_account, seller_sku, stat_month, row: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO sales_child(asin,parent_asin,shop_account,seller_sku,stat_month,data)
                     VALUES(?,?,?,?,?,?)
                     ON CONFLICT(asin,parent_asin,shop_account) DO UPDATE SET
                       data=excluded.data, seller_sku=excluded.seller_sku,
                       stat_month=excluded.stat_month, fetched_at=datetime('now','localtime')""",
                  (asin, parent_asin, shop_account, seller_sku, stat_month, _j(row)))


def upsert_listing_baseline(parent_asin, shop_account, site_code, row: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO listing_baseline(parent_asin,shop_account,site_code,data)
                     VALUES(?,?,?,?)
                     ON CONFLICT(parent_asin,shop_account) DO UPDATE SET
                       data=excluded.data, site_code=excluded.site_code, fetched_at=datetime('now','localtime')""",
                  (parent_asin, shop_account, normalize_site_code(site_code), _j(row)))


def update_listing_image_url(parent_asin: str, shop_account: str, site_code: str,
                             image_url: str, source: str) -> None:
    """仅更新已有商品快照的主图字段，不覆盖其他快照数据。"""
    with _conn() as c:
        existing = c.execute(
            "SELECT data FROM listing_baseline WHERE parent_asin=? AND shop_account=?",
            (parent_asin, shop_account),
        ).fetchone()
        try:
            data = json.loads(existing["data"] or "{}") if existing else {}
        except (TypeError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["主图URL"] = image_url
        data["主图URL来源"] = source
        data["主图URL抓取时间"] = datetime.datetime.now().isoformat(timespec="seconds")
        c.execute(
            """INSERT INTO listing_baseline(parent_asin,shop_account,site_code,data)
               VALUES(?,?,?,?)
               ON CONFLICT(parent_asin,shop_account) DO UPDATE SET
                 site_code=excluded.site_code, data=excluded.data,
                 fetched_at=datetime('now','localtime')""",
            (parent_asin, shop_account, normalize_site_code(site_code), _j(data)),
        )


def upsert_stock_summary(parent_asin, shop_account, row: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO stock_summary(parent_asin,shop_account,data)
                     VALUES(?,?,?)
                     ON CONFLICT(parent_asin,shop_account) DO UPDATE SET
                       data=excluded.data, fetched_at=datetime('now','localtime')""",
                  (parent_asin, shop_account, _j(row)))


def upsert_product_tags(asin, shop_account, seller_sku, row: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO product_tags(asin,shop_account,seller_sku,data)
                     VALUES(?,?,?,?)
                     ON CONFLICT(asin,shop_account) DO UPDATE SET
                       data=excluded.data, seller_sku=excluded.seller_sku, fetched_at=datetime('now','localtime')""",
                  (asin, shop_account, seller_sku, _j(row)))


def upsert_ad_target(asin, shop_account, 目标ACOS, 目标每日预算, 源更新时间=None) -> None:
    """广告目标覆写值落本地（来源 MySQL app_db）。全量覆盖式写入。"""
    with _conn() as c:
        c.execute("""INSERT INTO ad_target(asin,shop_account,"目标ACOS","目标每日预算","源更新时间")
                     VALUES(?,?,?,?,?)
                     ON CONFLICT(asin,shop_account) DO UPDATE SET
                       "目标ACOS"=excluded."目标ACOS",
                       "目标每日预算"=excluded."目标每日预算",
                       "源更新时间"=excluded."源更新时间",
                       fetched_at=datetime('now','localtime')""",
                  (asin, shop_account or "", 目标ACOS, 目标每日预算, 源更新时间))


def get_ad_target(asin: str, shop_account: str) -> dict | None:
    """按 (asin,shop) → (asin,'') 降级查目标值；无则 None。"""
    with _conn() as c:
        for a, s in ((asin, shop_account or ""), (asin, "")):
            r = c.execute('SELECT * FROM ad_target WHERE asin=? AND shop_account=? LIMIT 1',
                          (a, s)).fetchone()
            if r:
                return dict(r)
    return None


def clear_ad_target() -> int:
    """清空 ad_target（全量重灌前调用）。返回删除行数。"""
    with _conn() as c:
        cur = c.execute("DELETE FROM ad_target")
        return cur.rowcount


def upsert_competitor(parent_asin, shop_account, competitor_asin, row: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO competitors(parent_asin,shop_account,competitor_asin,data)
                     VALUES(?,?,?,?)
                     ON CONFLICT(parent_asin,competitor_asin) DO UPDATE SET
                       data=excluded.data, fetched_at=datetime('now','localtime')""",
                  (parent_asin, shop_account, competitor_asin, _j(row)))


def upsert_keyword_flow(parent_asin, asin, shop_account, keyword, source, row: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO keyword_flow(parent_asin,asin,shop_account,keyword,source,data)
                     VALUES(?,?,?,?,?,?)
                     ON CONFLICT(shop_account,keyword,source,asin) DO UPDATE SET
                       data=excluded.data, parent_asin=excluded.parent_asin, fetched_at=datetime('now','localtime')""",
                  (parent_asin, asin or "", shop_account, keyword, source, _j(row)))


def log_sync(tool, target_key, stat_date, status, rows_count=0, note="") -> None:
    with _conn() as c:
        c.execute("""INSERT INTO sync_log(tool,target_key,stat_date,status,rows_count,note)
                     VALUES(?,?,?,?,?,?)""", (tool, target_key, stat_date, status, rows_count, note))


# ---------------- 新数据源 (B 方案 · azlisting-mcpserver) ----------------
def upsert_natural_ad_flow(asin, parent_asin, shop_account, seller_sku, parent_seller_sku,
                            stat_date, is_summary, row: dict) -> None:
    """每日自然/广告订单流。来源 erp_listing_natural_advert_flow。"""
    frozen = _is_frozen(stat_date)
    with _conn() as c:
        c.execute("""INSERT INTO daily_natural_ad_flow
                     (asin, parent_asin, shop_account, seller_sku, parent_seller_sku,
                      stat_date, is_summary, data, is_frozen)
                     VALUES(?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(asin, shop_account, stat_date, is_summary) DO UPDATE SET
                       data=excluded.data, fetched_at=datetime('now','localtime'),
                       is_frozen=excluded.is_frozen, is_summary=excluded.is_summary
                     WHERE daily_natural_ad_flow.is_frozen=0""",
                  (asin, parent_asin, shop_account, seller_sku, parent_seller_sku,
                   stat_date, 1 if is_summary else 0, _j(row), frozen))


def upsert_monthly_goal(parent_asin, parent_seller_sku, shop_account, month_str, row: dict) -> None:
    """月度目标。来源 erp_listing_monthly_goal。"""
    with _conn() as c:
        c.execute("""INSERT INTO monthly_goal
                     (parent_asin, parent_seller_sku, shop_account, month_str, data)
                     VALUES(?,?,?,?,?)
                     ON CONFLICT(parent_asin, shop_account, month_str) DO UPDATE SET
                       data=excluded.data, parent_seller_sku=excluded.parent_seller_sku,
                       fetched_at=datetime('now','localtime')""",
                  (parent_asin, parent_seller_sku, shop_account, month_str, _j(row)))


def query_monthly_goals(parent_asin: str, shop_account: str, month_str: str | None = None) -> list[dict]:
    """读取父 ASIN 的月度目标；可按月份精确筛选。"""
    sql = "SELECT * FROM monthly_goal WHERE parent_asin=? AND shop_account=?"
    params: list[str] = [parent_asin, shop_account]
    if month_str:
        sql += " AND month_str=?"
        params.append(month_str)
    sql += " ORDER BY month_str"
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("data") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            item.update(payload)
        result.append(item)
    return result


def upsert_stock_alert(parent_asin, parent_seller_sku, shop_account, row: dict) -> None:
    """库存预警全维度。来源 erp_listing_stock_alert。"""
    with _conn() as c:
        c.execute("""INSERT INTO stock_alert
                     (parent_asin, parent_seller_sku, shop_account, data)
                     VALUES(?,?,?,?)
                     ON CONFLICT(parent_asin, shop_account) DO UPDATE SET
                       data=excluded.data, parent_seller_sku=excluded.parent_seller_sku,
                       fetched_at=datetime('now','localtime')""",
                  (parent_asin, parent_seller_sku, shop_account, _j(row)))


def upsert_inventory_cost(parent_asin, parent_seller_sku, shop_account,
                          child_asin, seller_sku, fn_sku, report_month, row: dict) -> None:
    """超龄仓租费用（子ASIN 粒度）。来源 erp_listing_inventory_cost_analysis。
    row 里的 longTermStorageFees 数组由本函数汇总为 数量/费用 存入独立列。"""
    fees = row.get("longTermStorageFees") or []
    汇总数量 = sum((f.get("qtyCharged") or 0) for f in fees)
    汇总费用 = sum((f.get("amountCharged") or 0) for f in fees)
    with _conn() as c:
        c.execute("""INSERT INTO inventory_cost
                     (parent_asin, parent_seller_sku, shop_account, child_asin, seller_sku, fn_sku,
                      report_month, data, "汇总超龄库存数", "汇总超龄仓租费")
                     VALUES(?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(parent_asin, child_asin, report_month) DO UPDATE SET
                       data=excluded.data,
                       seller_sku=excluded.seller_sku, fn_sku=excluded.fn_sku,
                       parent_seller_sku=excluded.parent_seller_sku,
                       "汇总超龄库存数"=excluded."汇总超龄库存数",
                       "汇总超龄仓租费"=excluded."汇总超龄仓租费",
                       fetched_at=datetime('now','localtime')""",
                  (parent_asin, parent_seller_sku, shop_account, child_asin, seller_sku, fn_sku,
                   report_month, _j(row), 汇总数量, 汇总费用))


def upsert_product_info(parent_asin, parent_seller_sku, shop_account, row: dict) -> None:
    """产品信息（五点/标题/类目/变体主题）。来源 erp_listing_product_info。"""
    with _conn() as c:
        c.execute("""INSERT INTO listing_product_info
                     (parent_asin, parent_seller_sku, shop_account, data)
                     VALUES(?,?,?,?)
                     ON CONFLICT(parent_asin, shop_account) DO UPDATE SET
                       data=excluded.data,
                       parent_seller_sku=excluded.parent_seller_sku,
                       fetched_at=datetime('now','localtime')""",
                  (parent_asin, parent_seller_sku, shop_account, _j(row)))


def upsert_child_price_promo(child_asin, parent_asin, shop_account, site_code,
                              snapshot_date, row: dict) -> None:
    """子体实时价格促销快照。来源 erp_listing_price_promotion_analysis。
    row.price 是 '$12.99' 字符串，本函数剥出 price_usd 供判定用。"""
    # 从 "$12.99" 剥出 12.99；异常价格（空/'-'/中文币种）返回 None
    price_str = (row.get("price") or "").strip()
    price_usd = None
    if price_str:
        import re as _re
        m = _re.search(r"[\d,]+\.?\d*", price_str.replace(",", ""))
        if m:
            try:
                price_usd = float(m.group().replace(",", ""))
            except ValueError:
                price_usd = None
    with _conn() as c:
        c.execute("""INSERT INTO child_price_promo
                     (child_asin, parent_asin, shop_account, site_code,
                      snapshot_date, data, price_usd)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(child_asin, snapshot_date) DO UPDATE SET
                       data=excluded.data,
                       parent_asin=excluded.parent_asin,
                       shop_account=excluded.shop_account,
                       site_code=excluded.site_code,
                       price_usd=excluded.price_usd,
                       fetched_at=datetime('now','localtime')""",
                  (child_asin, parent_asin, shop_account, site_code,
                   snapshot_date, _j(row), price_usd))


def upsert_listing_inspection_snapshot(parent_asin: str, parent_seller_sku: str | None,
                                       shop_account: str, source: str, data) -> None:
    snapshot_date = datetime.date.today().isoformat()
    with _conn() as c:
        c.execute("""INSERT INTO listing_inspection_snapshot
                     (parent_asin, parent_seller_sku, shop_account, snapshot_date, source, data)
                     VALUES(?,?,?,?,?,?)
                     ON CONFLICT(parent_asin, shop_account, snapshot_date, source) DO UPDATE SET
                       parent_seller_sku=excluded.parent_seller_sku,
                       data=excluded.data,
                       fetched_at=datetime('now','localtime')""",
                  (parent_asin, parent_seller_sku, shop_account, snapshot_date, source,
                   json.dumps(data, ensure_ascii=False)))


def upsert_keyword_rank(parent_asin: str, shop_account: str, child_asin: str | None,
                        keyword: str, site_code: str | None, stat_date: str,
                        nature_rank, sp_rank=None, is_core: int = 1, row: dict | None = None) -> None:
    """关键词逐日排名（卡位）。来源 erp_listing_asin_keyword_rank_history。"""
    with _conn() as c:
        c.execute("""INSERT INTO keyword_rank_daily
                     (parent_asin, shop_account, child_asin, keyword, site_code,
                      stat_date, nature_rank, sp_rank, is_core, data)
                     VALUES(?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(parent_asin, shop_account, keyword, stat_date) DO UPDATE SET
                       child_asin=excluded.child_asin, site_code=excluded.site_code,
                       nature_rank=excluded.nature_rank, sp_rank=excluded.sp_rank,
                       is_core=excluded.is_core, data=excluded.data,
                       fetched_at=datetime('now','localtime')""",
                  (parent_asin, shop_account, child_asin, keyword, site_code,
                   stat_date, nature_rank, sp_rank, is_core,
                   json.dumps(row, ensure_ascii=False) if row else None))


def query_core_keyword_ranks(parent_asin: str, shop_account: str, days: int = 8) -> list[dict]:
    """取核心词近 N 天逐日自然排名，按日期升序返回 [{stat_date, nature_rank}]。"""
    with _conn() as c:
        rows = c.execute("""SELECT stat_date, nature_rank
                            FROM keyword_rank_daily
                            WHERE parent_asin=? AND shop_account=? AND is_core=1
                              AND nature_rank IS NOT NULL
                              AND stat_date >= date('now','localtime',?)
                            ORDER BY stat_date ASC""",
                         (parent_asin, shop_account, f"-{days} day")).fetchall()
    return [{"stat_date": r["stat_date"], "nature_rank": r["nature_rank"]} for r in rows]


def query_listing_inspection_snapshots(parent_asin: str, shop_account: str) -> dict[str, object]:
    with _conn() as c:
        rows = c.execute("""SELECT source, data
                            FROM listing_inspection_snapshot
                            WHERE parent_asin=? AND shop_account=?
                            ORDER BY snapshot_date DESC, fetched_at DESC""",
                         (parent_asin, shop_account)).fetchall()
    result: dict[str, object] = {}
    for row in rows:
        if row["source"] in result:
            continue
        try:
            result[row["source"]] = json.loads(row["data"])
        except (TypeError, json.JSONDecodeError):
            result[row["source"]] = None
    return result


def query_previous_listing_inspection_snapshot(parent_asin: str, shop_account: str,
                                               source: str) -> object | None:
    with _conn() as c:
        rows = c.execute("""SELECT data
                            FROM listing_inspection_snapshot
                            WHERE parent_asin=? AND shop_account=? AND source=?
                            ORDER BY snapshot_date DESC, fetched_at DESC
                            LIMIT 2""", (parent_asin, shop_account, source)).fetchall()
    if len(rows) < 2:
        return None
    try:
        return json.loads(rows[1]["data"])
    except (TypeError, json.JSONDecodeError):
        return None


def query_latest_inventory_cost(parent_asin: str, shop_account: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("""SELECT * FROM inventory_cost
                            WHERE parent_asin=? AND shop_account=?
                              AND report_month=(SELECT MAX(report_month) FROM inventory_cost
                                                WHERE parent_asin=? AND shop_account=?)""",
                         (parent_asin, shop_account, parent_asin, shop_account)).fetchall()
    return [dict(row) for row in rows]


def query_child_order_averages(parent_asin: str, shop_account: str, days: int = 30) -> dict[str, float]:
    with _conn() as c:
        rows = c.execute("""SELECT asin, AVG(COALESCE("totalOrderNum", 0)) AS daily_orders
                            FROM daily_natural_ad_flow
                            WHERE parent_asin=? AND shop_account=? AND is_summary=0
                              AND stat_date >= date('now', ?)
                            GROUP BY asin""",
                         (parent_asin, shop_account, f"-{max(days - 1, 0)} days")).fetchall()
    return {row["asin"]: float(row["daily_orders"] or 0) for row in rows if row["asin"]}


def upsert_listing_page_snapshot(parent_asin: str, shop_account: str,
                                 site_code: str | None, row: dict) -> None:
    """保存前台商品详情的规范化快照，供内容/可购性/价格促销巡检使用。"""
    with _conn() as c:
        c.execute("""INSERT INTO listing_page_snapshot
                     (parent_asin, shop_account, site_code, data)
                     VALUES(?,?,?,?)
                     ON CONFLICT(parent_asin, shop_account) DO UPDATE SET
                       site_code=excluded.site_code, data=excluded.data,
                       fetched_at=datetime('now','localtime')""",
                  (parent_asin, shop_account, normalize_site_code(site_code), _j(row)))


# ---------------- 读取（供 daily_monitor / 判定引擎用） ----------------
def recent_natural_ad_flow(parent_asin: str, days: int = 30) -> list[dict]:
    """拉近 N 天父ASIN下所有子体的自然/广告流，按 stat_date 降序。"""
    with _conn() as c:
        rows = c.execute("""SELECT * FROM daily_natural_ad_flow
                            WHERE parent_asin=?
                            ORDER BY stat_date DESC, asin""",
                         (parent_asin,)).fetchall()
    return [dict(r) for r in rows]


def query_parent_daily_orders(parent_asin: str, days: int = 30) -> list[dict]:
    """父ASIN每日订单/流量指标聚合（来源 daily_natural_ad_flow）。
    仅输出整数字段（订单数/点击/曝光）与占比。
    支撑 §3.6 销量异常 · §3.8 自然流量异常 · §3.12 放量未执行 条件5。

    ⚠️ 币种说明：daily_natural_ad_flow 的金额字段是 CNY（内部固定汇率6.6），
    本函数**故意不输出金额**。金额字段一律从 query_parent_daily_money() 取（USD）。
    """
    with _conn() as c:
        rows = c.execute("""
            SELECT
              parent_asin, shop_account, stat_date,
              CAST(SUM("adOrderNum") AS INTEGER)        AS 广告订单数,
              CAST(SUM("totalOrderNum") AS INTEGER)     AS 总订单数,
              CAST(SUM("naturalOrderNum") AS INTEGER)   AS 自然订单数,
              CAST(SUM("adClick") AS INTEGER)           AS 广告点击,
              CAST(SUM("adImpressions") AS INTEGER)     AS 广告曝光,
              CAST(SUM("adSaleNum") AS INTEGER)         AS 广告销量,
              CASE WHEN SUM("totalOrderNum") > 0
                   THEN CAST(SUM("naturalOrderNum") AS REAL) / SUM("totalOrderNum") END AS 自然订单占比
            FROM daily_natural_ad_flow
            WHERE parent_asin=? AND is_summary=0
            GROUP BY parent_asin, shop_account, stat_date
            ORDER BY stat_date DESC
            LIMIT ?
        """, (parent_asin, days)).fetchall()
    return [dict(r) for r in rows]


def query_parent_daily_money(parent_asin: str, days: int = 30) -> list[dict]:
    """父ASIN每日金额指标（来源 daily_product_sales，USD 美元）。
    支撑 §3.6 ACOS/广告花费异常 · §3.7 目标偏离 等所有涉及金额的判定。
    """
    with _conn() as c:
        rows = c.execute("""
            SELECT
              parent_asin, shop_account, stat_date,
              "广告花费"     AS 广告花费_USD,
              "广告销售额"    AS 广告销售额_USD,
              "全部销售额"    AS 全部销售额_USD,
              "全部单量"     AS 全部单量,
              "全部销量"     AS 全部销量,
              "广告单量"     AS 广告单量,
              "ACOS"        AS ACOS,
              "毛利率"       AS 毛利率
            FROM daily_product_sales
            WHERE asin=?
            ORDER BY stat_date DESC
            LIMIT ?
        """, (parent_asin, days)).fetchall()
    return [dict(r) for r in rows]


# ---------------- 读取（供 rule_engine 用） ----------------
def recent_daily_sales(asin: str, days: int = 7) -> list[dict]:
    with _conn() as c:
        rows = c.execute("""SELECT * FROM daily_product_sales WHERE asin=?
                            ORDER BY stat_date DESC LIMIT ?""", (asin, days)).fetchall()
    return [dict(r) for r in rows]


def recent_daily_ad(asin: str, days: int = 7) -> list[dict]:
    with _conn() as c:
        rows = c.execute("""SELECT * FROM daily_ad_product WHERE asin=?
                            ORDER BY stat_date DESC LIMIT ?""", (asin, days)).fetchall()
    return [dict(r) for r in rows]


def get_one(table: str, **where) -> dict | None:
    cond = " AND ".join(f'"{k}"=?' for k in where)
    with _conn() as c:
        r = c.execute(f"SELECT * FROM {table} WHERE {cond} LIMIT 1", tuple(where.values())).fetchone()
    return dict(r) if r else None


def has_daily(asin: str, stat_date: str, shop_account: str | None = None) -> bool:
    with _conn() as c:
        if shop_account:
            r = c.execute("SELECT 1 FROM daily_product_sales WHERE asin=? AND shop_account=? AND stat_date=?",
                          (asin, shop_account, stat_date)).fetchone()
        else:
            r = c.execute("SELECT 1 FROM daily_product_sales WHERE asin=? AND stat_date=?",
                          (asin, stat_date)).fetchone()
    return r is not None


# ---------------- 聚合查询（供 daily_monitor 用） ----------------
def query_parent_daily_sales(parent_asin: str, days: int = 30,
                             shop_account: str | None = None) -> list[dict]:
    """获取父ASIN下所有子体的每日销售数据，同一天多子体汇总为一条。
    返回按 stat_date 降序排列，含所有生成列。"""
    with _conn() as c:
        rows = c.execute("""
            SELECT
                parent_asin,
                shop_account,
                site_code,
                stat_date,
                SUM("全部单量")    AS "全部单量",
                SUM("全部销量")    AS "全部销量",
                SUM("全部销售额")  AS "全部销售额",
                SUM("广告花费")    AS "广告花费",
                SUM("广告销售额")  AS "广告销售额",
                SUM("广告单量")    AS "广告单量",
                -- 加权平均：Σ(毛利率×销售额) / Σ(销售额)；分母为 0 时返回 NULL
                SUM("毛利率" * "全部销售额") / NULLIF(SUM("全部销售额"), 0) AS "毛利率",
                CASE WHEN SUM("广告花费") > 0 AND SUM("广告销售额") > 0
                     THEN SUM("广告花费") / SUM("广告销售额")
                     ELSE NULL END  AS "ACOS"
            FROM daily_product_sales
            WHERE parent_asin=?
              AND (? IS NULL OR shop_account=?)
            GROUP BY stat_date, shop_account
            ORDER BY stat_date DESC
            LIMIT ?
        """, (parent_asin, shop_account, shop_account, days)).fetchall()
    return [dict(r) for r in rows]


def query_active_products() -> list[dict]:
    """获取所有活跃产品：从 daily_product_sales 去重 (parent_asin, shop_account, site_code)。"""
    with _conn() as c:
        rows = c.execute("""
            SELECT DISTINCT parent_asin, shop_account, MAX(site_code) AS site_code
            FROM daily_product_sales
            WHERE parent_asin IS NOT NULL
            GROUP BY parent_asin, shop_account
            ORDER BY parent_asin
        """).fetchall()
    return [dict(r) for r in rows]


def query_sales_children(parent_asin: str, shop_account: str | None = None) -> list[dict]:
    """获取某父ASIN的所有子体销售/目标数据（sales_child 表）。"""
    with _conn() as c:
        if shop_account:
            rows = c.execute(
                'SELECT * FROM sales_child WHERE parent_asin=? AND shop_account=?',
                (parent_asin, shop_account)
            ).fetchall()
        else:
            rows = c.execute(
                'SELECT * FROM sales_child WHERE parent_asin=?',
                (parent_asin,)
            ).fetchall()
    return [dict(r) for r in rows]


def query_child_asins(parent_asin: str, shop_account: str) -> list[str]:
    """读取父产品下可用于外部商品抓取的真实子 ASIN。"""
    rows = query_sales_children(parent_asin, shop_account)
    return [
        row["asin"] for row in rows
        if isinstance(row.get("asin"), str) and row["asin"].startswith("B")
        and len(row["asin"]) == 10
    ]


def query_image_urls(parent_asins: list[str] | None = None) -> dict[str, str]:
    """{父ASIN: 主图URL} 映射。传 parent_asins 只查这批；不传查全部。
    只包含 data.主图URL 非空的父ASIN。"""
    sql = "SELECT parent_asin, json_extract(data,'$.主图URL') AS url FROM listing_baseline WHERE url IS NOT NULL"
    params: tuple = ()
    if parent_asins:
        placeholders = ",".join("?" * len(parent_asins))
        sql += f" AND parent_asin IN ({placeholders})"
        params = tuple(parent_asins)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return {r["parent_asin"]: r["url"] for r in rows if r["url"]}


def query_image_urls_by_shop(
    parent_asins: list[str] | None = None,
) -> dict[tuple[str, str], str]:
    """返回按 (父ASIN, 店铺账号) 隔离的主图映射。"""
    sql = """SELECT parent_asin, shop_account,
                    json_extract(data,'$.主图URL') AS url
             FROM listing_baseline
             WHERE json_extract(data,'$.主图URL') IS NOT NULL
               AND json_extract(data,'$.主图URL') != ''"""
    params: tuple = ()
    if parent_asins:
        placeholders = ",".join("?" * len(parent_asins))
        sql += f" AND parent_asin IN ({placeholders})"
        params = tuple(parent_asins)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return {(r["parent_asin"], r["shop_account"]): r["url"] for r in rows}


def query_product_names(parent_asins: list[str] | None = None) -> dict[str, str]:
    """{父ASIN: 商品标题} 映射。用于前端跳转到广告决策 agent 时携带 productName。"""
    sql = "SELECT parent_asin, json_extract(data,'$.标题') AS title FROM listing_baseline WHERE title IS NOT NULL"
    params: tuple = ()
    if parent_asins:
        placeholders = ",".join("?" * len(parent_asins))
        sql += f" AND parent_asin IN ({placeholders})"
        params = tuple(parent_asins)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return {r["parent_asin"]: r["title"] for r in rows if r["title"]}


def query_product_names_by_shop() -> dict[tuple[str, str], str]:
    """返回按 (父ASIN, 店铺账号) 隔离的商品标题。"""
    with _conn() as c:
        rows = c.execute(
            """SELECT parent_asin, shop_account,
                      json_extract(data,'$.标题') AS title
               FROM listing_baseline
               WHERE json_extract(data,'$.标题') IS NOT NULL
                 AND json_extract(data,'$.标题') != ''"""
        ).fetchall()
    return {(r["parent_asin"], r["shop_account"]): r["title"] for r in rows}


def query_product_snapshots(parent_asin: str, shop_account: str) -> dict | None:
    """获取巡检所需的本地快照；每个数据域缺失时保留 None。"""
    result = {"listing": None, "stock": None, "tags": None, "product_info": None,
              "page": None, "price_promo": [], "inspection": {}, "inventory_cost": []}
    with _conn() as c:
        lb = c.execute(
            'SELECT * FROM listing_baseline WHERE parent_asin=? AND shop_account=?',
            (parent_asin, shop_account)
        ).fetchone()
        if lb:
            result["listing"] = dict(lb)

        ss = c.execute(
            'SELECT * FROM stock_summary WHERE parent_asin=? AND shop_account=?',
            (parent_asin, shop_account)
        ).fetchone()
        if ss:
            result["stock"] = dict(ss)

        pt = c.execute(
            'SELECT * FROM product_tags WHERE asin=? AND shop_account=?',
            (parent_asin, shop_account)
        ).fetchone()
        if pt:
            result["tags"] = dict(pt)

        pi = c.execute(
            'SELECT * FROM listing_product_info WHERE parent_asin=? AND shop_account=?',
            (parent_asin, shop_account)
        ).fetchone()
        if pi:
            result["product_info"] = dict(pi)
        page = c.execute(
            'SELECT * FROM listing_page_snapshot WHERE parent_asin=? AND shop_account=?',
            (parent_asin, shop_account)
        ).fetchone()
        if page:
            result["page"] = json.loads(page["data"] or "{}")
        promos = c.execute("""SELECT child_asin, data, price_usd, snapshot_date, fetched_at,
                                    coupon, strikethroughPrice, savingsPercentage
                             FROM child_price_promo
                             WHERE parent_asin=? AND shop_account=?
                               AND snapshot_date=(SELECT MAX(snapshot_date) FROM child_price_promo
                                                  WHERE parent_asin=? AND shop_account=?)""",
                          (parent_asin, shop_account, parent_asin, shop_account)).fetchall()
        result["price_promo"] = [dict(row) for row in promos]
    result["inspection"] = query_listing_inspection_snapshots(parent_asin, shop_account)
    result["inventory_cost"] = query_latest_inventory_cost(parent_asin, shop_account)
    # 全部缺失 → None
    if all(v is None for v in result.values()):
        return None
    return result


# ---------------- 加新字段 ----------------
def promote_field(table: str, field: str, sqltype: str = "REAL") -> None:
    """把 data 里的某原生字段提升为可查询生成列（立即对全部历史行生效，零回填）。"""
    with _conn() as c:
        try:
            c.execute(f'''ALTER TABLE {table} ADD COLUMN "{field}" {sqltype}
                          GENERATED ALWAYS AS (json_extract(data,'$."{field}"')) VIRTUAL''')
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise


def stats() -> dict:
    """各表行数概览。包含 MCP 缓存表 + 事件池 3 张表。"""
    tables = [
        # MCP 缓存表
        "daily_product_sales", "daily_ad_product", "sales_child", "listing_baseline",
        "stock_summary", "product_tags", "competitors", "keyword_flow", "sync_log",
        # 事件池 3 张表
        "event_pool", "event_state_log", "inspection_batch",
    ]
    out = {}
    with _conn() as c:
        for t in tables:
            try:
                out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                out[t] = None       # 表还没建
    return out


if __name__ == "__main__":
    init_db()
    print("DB:", DB_PATH)
    print("表行数:", stats())
