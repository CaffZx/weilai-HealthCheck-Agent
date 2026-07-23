import json
import sqlite3
import datetime as dt

import pytest

from data import local_store
from data.review_schedule import resolve_schedule
from data.observation_service import process_due_observations
from web.backend.routers import tasks


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    database_path = tmp_path / "healthcheck.db"
    monkeypatch.setattr(local_store, "DB_PATH", database_path)
    local_store.init_db()
    return database_path


def test_latest_inspection_result_ignores_failed_batch(isolated_store):
    local_store.upsert_inspection_result(
        "success", "PARENT", "shop", "US", {"优先级信息": {"产品执行分数": 91}}, "SUCCESS"
    )
    local_store.upsert_inspection_result(
        "failed", "PARENT", "shop", "US", {"优先级信息": {"产品执行分数": 0}}, "FAILED"
    )

    result = local_store.query_latest_inspection_results()[("PARENT", "shop")]

    assert result["result_status"] == "SUCCESS"
    assert result["score"] == 91


def test_product_maintenance_replaces_pending_and_uses_variant_baseline(isolated_store):
    card = {
        "异常明细": [
            {"问题点位": "评分异常", "该条严重度": "S1", "命中变体": "red", "数据快照": [{"label": "当前评分", "value": "2.1"}]},
            {"问题点位": "评分异常", "该条严重度": "S1", "命中变体": "blue", "数据快照": [{"label": "当前评分", "value": "4.2"}]},
        ]
    }
    local_store.upsert_inspection_result("baseline", "PARENT", "shop", "US", card)
    with sqlite3.connect(isolated_store) as connection:
        connection.row_factory = sqlite3.Row
        result_id = connection.execute("SELECT id FROM inspection_result WHERE batch_no='baseline'").fetchone()["id"]
        connection.execute(
            "INSERT INTO product_maintenance(parent_asin, shop_account, observation_at, next_inspection_at, status) VALUES ('PARENT', 'shop', '2026-07-20', '2026-07-20', '待运营确认')"
        )
        event = {
            "唯一识别": "event-blue",
            "问题点位": "评分异常",
            "严重度": "S1",
            "命中变体": "blue",
            "inspection_result_id": result_id,
        }
        maintenance_id = local_store.create_product_maintenance(
            connection,
            parent_asin="PARENT",
            shop_account="shop",
            user_id=1,
            result="已按建议执行",
            actual_action="优化",
            notes=None,
            observation_at="2026-07-25",
            next_inspection_at="2026-07-25",
            events=[event],
        )
        connection.commit()
        old_status = connection.execute("SELECT status FROM product_maintenance WHERE id=1").fetchone()["status"]
        maintenance_event = connection.execute(
            "SELECT variant, baseline_snapshot FROM product_maintenance_event WHERE maintenance_id=?", (maintenance_id,)
        ).fetchone()

    assert old_status == "已被新维护替代"
    assert maintenance_event["variant"] == "blue"
    assert json.loads(maintenance_event["baseline_snapshot"])[0]["value"] == "4.2"


def test_observation_metric_direction_is_issue_specific():
    effect, _, confidence = tasks._compare_observation_metrics("库存积压", [("FBA 可售库存", 100, 60)])
    assert (effect, confidence) == ("变好", 0.7)

    effect, _, confidence = tasks._compare_observation_metrics("评分异常", [("当前评分", 2.0, 4.0)])
    assert (effect, confidence) == ("变好", 0.7)

    effect, _, confidence = tasks._compare_observation_metrics("目标偏离", [("日均订单", 3, 6)])
    assert (effect, confidence) == ("变好", 0.7)

    effect, _, confidence = tasks._compare_observation_metrics("目标偏离", [("目标差距", 5, 2)])
    assert (effect, confidence) == ("变好", 0.7)

    effect, reason, confidence = tasks._compare_observation_metrics("未知异常", [("未知指标", 1, 2)])
    assert (effect, confidence) == ("数据不足", 0.0)
    assert "缺少" in reason


def _seed_owned_events(database_path, states=("新发现", "新发现")):
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO asin_owner(asin, seller_sku, shop_id, shop_account, principal_user_id) VALUES ('PARENT', 'SKU', 1, 'shop', 7)"
        )
        for index, state in enumerate(states, start=1):
            connection.execute("""
                INSERT INTO event_pool
                  (唯一识别, 店铺账号, 父ASIN, 问题点位, 作用层级, 严重度, 当前状态, 判定依据)
                VALUES (?,?,?,?,?,?,?,?)
            """, (f"event-{index}", "shop", "PARENT", f"异常{index}", "链接级", "S1", state, "{}"))
        connection.commit()


def test_single_completion_creates_its_own_observation_without_waiting_for_product(isolated_store):
    _seed_owned_events(isolated_store)
    review_at = (dt.date.today() + dt.timedelta(days=3)).isoformat()

    response = tasks.api_task_action("event-1", {
        "userId": 7, "action_type": "完成", "review_at": review_at,
        "next_inspection_at": review_at, "actual_action": "调整价格",
    })

    assert response["maintenance_created"] is True
    assert response["observation_id"] is not None
    with sqlite3.connect(isolated_store) as connection:
        case = connection.execute(
            "SELECT event_uid, status, observation_at FROM observation_case"
        ).fetchone()
        states = dict(connection.execute("SELECT 唯一识别, 当前状态 FROM event_pool").fetchall())
    assert case == ("event-1", "观察中", review_at)
    assert states == {"event-1": "已处理待复扫", "event-2": "新发现"}


def test_current_task_counts_exclude_only_events_in_active_observation(isolated_store):
    """有效观察不占当前任务；没有观察记录的待复扫仍必须可见可处理。"""
    _seed_owned_events(isolated_store, states=("已处理待复扫", "新发现"))
    today = dt.date.today().isoformat()
    with sqlite3.connect(isolated_store) as connection:
        action_id = connection.execute(
            "INSERT INTO task_action(event_uid, user_id, action_type) VALUES ('event-1', 7, '完成')"
        ).lastrowid
        connection.execute("""
            INSERT INTO observation_case
              (parent_asin, shop_account, event_uid, completion_action_id, observation_at, next_inspection_at, status)
            VALUES ('PARENT', 'shop', 'event-1', ?, ?, ?, '观察中')
        """, (action_id, today, today))
        connection.execute(
            "INSERT INTO asin_owner(asin, seller_sku, shop_id, shop_account, principal_user_id) VALUES ('PENDING', 'SKU', 1, 'shop', 7)"
        )
        connection.execute("""
            INSERT INTO event_pool
              (唯一识别, 店铺账号, 父ASIN, 问题点位, 作用层级, 严重度, 当前状态, 判定依据)
            VALUES ('pending-event', 'shop', 'PENDING', '历史待复扫', '链接级', 'S1', '已处理待复扫', '{}')
        """)
        connection.execute(
            "INSERT INTO asin_owner(asin, seller_sku, shop_id, shop_account, principal_user_id) VALUES ('CLOSED', 'SKU', 1, 'shop', 7)"
        )
        connection.execute("""
            INSERT INTO event_pool
              (唯一识别, 店铺账号, 父ASIN, 问题点位, 作用层级, 严重度, 当前状态, 判定依据)
            VALUES ('closed-event', 'shop', 'CLOSED', '已关闭异常', '链接级', 'S1', '已关闭', '{}')
        """)
        connection.commit()

    result = tasks.api_tasks_today(userId=7)

    assert result["total_products"] == 2
    assert result["current_task_count"] == 2
    assert set(result["current_task_product_keys"]) == {"PARENT__shop", "PENDING__shop"}
    assert result["all_product_count"] == 3

    with sqlite3.connect(isolated_store) as connection:
        action_id = connection.execute(
            "INSERT INTO task_action(event_uid, user_id, action_type) VALUES ('event-2', 7, '完成')"
        ).lastrowid
        connection.execute("UPDATE event_pool SET 当前状态='已处理待复扫' WHERE 唯一识别='event-2'")
        connection.execute("""
            INSERT INTO observation_case
              (parent_asin, shop_account, event_uid, completion_action_id, observation_at, next_inspection_at, status)
            VALUES ('PARENT', 'shop', 'event-2', ?, ?, ?, '待运营确认')
        """, (action_id, today, today))
        connection.commit()

    result = tasks.api_tasks_today(userId=7)
    assert result["current_task_count"] == 1
    assert result["current_task_product_keys"] == ["PENDING__shop"]

    with sqlite3.connect(isolated_store) as connection:
        connection.execute("""
            INSERT INTO event_pool
              (唯一识别, 店铺账号, 父ASIN, 问题点位, 作用层级, 严重度, 当前状态, 判定依据)
            VALUES ('event-3', 'shop', 'PARENT', '新增异常', '链接级', 'S1', '新发现', '{}')
        """)
        connection.commit()

    result = tasks.api_tasks_today(userId=7)
    assert result["current_task_count"] == 2
    assert set(result["current_task_product_keys"]) == {"PARENT__shop", "PENDING__shop"}


def test_review_schedule_uses_rule_before_manual_override():
    today = dt.date(2026, 7, 23)
    parent = resolve_schedule({"问题点位": "父子体关系异常", "变体重要性": ""}, today=today)
    long_tail = resolve_schedule({"问题点位": "变体掉线", "变体重要性": "长尾色"}, today=today)
    manual = resolve_schedule(
        {"问题点位": "评分异常", "变体重要性": ""},
        {"review_at": "2026-08-08", "next_inspection_at": "2026-08-08"}, today=today,
    )

    assert (parent["rule_id"], parent["observation_at"]) == ("parent_structure_follow_up", "2026-07-24")
    assert (long_tail["rule_id"], long_tail["observation_at"]) == ("long_tail_variant_follow_up", "2026-08-06")
    assert manual["source"] == "manual"
    assert manual["default_observation_at"] == "2026-07-30"


def test_inventory_expected_arrival_overrides_thirty_day_fallback():
    schedule = resolve_schedule(
        {"问题点位": "FBA可售库存为0", "变体重要性": "主要色"},
        {"expected_available_at": "2026-08-01"}, today=dt.date(2026, 7, 23),
    )

    assert schedule["source"] == "expected_arrival"
    assert schedule["observation_at"] == "2026-08-01"
    assert schedule["supports_expected_available_date"] is True


def test_schedule_rejects_inspection_before_review():
    with pytest.raises(Exception) as error:
        resolve_schedule(
            {"问题点位": "评分异常", "变体重要性": ""},
            {"review_at": "2026-08-01", "next_inspection_at": "2026-07-30"},
            today=dt.date(2026, 7, 23),
        )
    assert "不能早于复查时间" in str(error.value)


def test_product_completion_calculates_each_event_schedule_independently(isolated_store):
    _seed_owned_events(isolated_store)
    with sqlite3.connect(isolated_store) as connection:
        connection.execute("UPDATE event_pool SET 问题点位='父子体关系异常' WHERE 唯一识别='event-1'")
        connection.execute("UPDATE event_pool SET 问题点位='评分异常' WHERE 唯一识别='event-2'")
        connection.commit()

    result = tasks.api_product_action({
        "userId": 7, "action_type": "完成产品维护", "parent_asin": "PARENT", "shop_account": "shop",
    })

    schedules = {item["event_uid"]: item for item in result["schedules"]}
    assert schedules["event-1"]["rule_id"] == "parent_structure_follow_up"
    assert schedules["event-2"]["rule_id"] == "review_risk_follow_up"
    assert schedules["event-1"]["observation_at"] != schedules["event-2"]["observation_at"]
    with sqlite3.connect(isolated_store) as connection:
        rows = dict(connection.execute("SELECT event_uid, schedule_rule_id FROM observation_case").fetchall())
    assert rows == {"event-1": "parent_structure_follow_up", "event-2": "review_risk_follow_up"}


def test_api_completion_uses_backend_rule_when_client_omits_dates(isolated_store):
    _seed_owned_events(isolated_store, states=("新发现", "已关闭"))
    with sqlite3.connect(isolated_store) as connection:
        connection.execute("UPDATE event_pool SET 问题点位='父子体关系异常' WHERE 唯一识别='event-1'")
        connection.commit()

    # 直调端点需显式传 None（HTTP 下 FastAPI 会把省略的 Query 参数解析为 None）
    preview = tasks.api_schedule_preview("event-1", userId=7, expected_available_at=None)
    result = tasks.api_task_action("event-1", {"userId": 7, "action_type": "完成"})

    assert preview["observation_at"] == result["observation_at"]
    assert result["schedule"]["source"] == "rule"
    with sqlite3.connect(isolated_store) as connection:
        row = connection.execute("""
            SELECT schedule_rule_id, schedule_source, observation_at, next_inspection_at
            FROM observation_case WHERE event_uid='event-1'
        """).fetchone()
    assert row == ("parent_structure_follow_up", "rule", preview["observation_at"], preview["observation_at"])


def test_confirming_one_observation_does_not_change_another_event(isolated_store):
    _seed_owned_events(isolated_store, states=("已处理待复扫", "已处理待复扫"))
    with sqlite3.connect(isolated_store) as connection:
        action = connection.execute("""
            INSERT INTO task_action(event_uid, user_id, action_type, review_at)
            VALUES ('event-1', 7, '完成', ?)
        """, (dt.date.today().isoformat(),))
        case_id = connection.execute("""
            INSERT INTO observation_case
              (parent_asin, shop_account, event_uid, completion_action_id, issue, severity,
               observation_at, next_inspection_at, status, agent_effect)
            VALUES ('PARENT', 'shop', 'event-1', ?, '异常1', 'S1', ?, ?, '待运营确认', '变好')
        """, (action.lastrowid, dt.date.today().isoformat(), dt.date.today().isoformat())).lastrowid
        connection.commit()

    tasks.api_confirm_observation(case_id, {"userId": 7, "confirmation": "保留当前动作"})

    with sqlite3.connect(isolated_store) as connection:
        states = dict(connection.execute("SELECT 唯一识别, 当前状态 FROM event_pool").fetchall())
    assert states == {"event-1": "已关闭", "event-2": "已处理待复扫"}


def test_successful_inspection_proactively_generates_due_observation_conclusion(isolated_store):
    _seed_owned_events(isolated_store, states=("已处理待复扫", "新发现"))
    today = dt.date.today().isoformat()
    with sqlite3.connect(isolated_store) as connection:
        connection.execute("UPDATE event_pool SET 问题点位='评分异常' WHERE 唯一识别='event-1'")
        action_id = connection.execute(
            "INSERT INTO task_action(event_uid, user_id, action_type) VALUES ('event-1', 7, '完成')"
        ).lastrowid
        case_id = connection.execute("""
            INSERT INTO observation_case
              (parent_asin, shop_account, event_uid, completion_action_id, issue, severity,
               baseline_snapshot, observation_at, next_inspection_at, status)
            VALUES ('PARENT', 'shop', 'event-1', ?, '评分异常', 'S1', ?, ?, ?, '观察中')
        """, (action_id, json.dumps([{"label": "当前评分", "value": "2.0"}]), today, today)).lastrowid
        connection.commit()

    assert process_due_observations("PARENT", "shop") == []
    local_store.upsert_inspection_result("after", "PARENT", "shop", "US", {
        "异常明细": [{"问题点位": "评分异常", "该条严重度": "S1", "数据快照": [
            {"label": "当前评分", "value": "4.0"},
        ]}],
    }, "SUCCESS")

    assert process_due_observations("PARENT", "shop") == [case_id]
    with sqlite3.connect(isolated_store) as connection:
        case = connection.execute(
            "SELECT status, agent_effect, agent_summary FROM observation_case WHERE id=?", (case_id,)
        ).fetchone()
    assert case[0] == "待运营确认"
    assert case[1] == "变好"
    assert "当前评分" in case[2]


def test_observation_waits_until_the_scheduled_inspection_date(isolated_store):
    _seed_owned_events(isolated_store, states=("已处理待复扫", "新发现"))
    today = dt.date.today()
    inspection_date = (today + dt.timedelta(days=1)).isoformat()
    with sqlite3.connect(isolated_store) as connection:
        action_id = connection.execute(
            "INSERT INTO task_action(event_uid, user_id, action_type) VALUES ('event-1', 7, '完成')"
        ).lastrowid
        case_id = connection.execute("""
            INSERT INTO observation_case
              (parent_asin, shop_account, event_uid, completion_action_id, issue, severity,
               observation_at, next_inspection_at, status)
            VALUES ('PARENT', 'shop', 'event-1', ?, '异常1', 'S1', ?, ?, '观察中')
        """, (action_id, today.isoformat(), inspection_date)).lastrowid
        connection.commit()
    local_store.upsert_inspection_result("early", "PARENT", "shop", "US", {"异常明细": []}, "SUCCESS")

    assert process_due_observations("PARENT", "shop") == []
    with sqlite3.connect(isolated_store) as connection:
        status = connection.execute("SELECT status FROM observation_case WHERE id=?", (case_id,)).fetchone()[0]
    assert status == "观察中"


def test_second_completion_is_rejected_while_an_observation_is_active(isolated_store):
    _seed_owned_events(isolated_store)
    review_at = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    tasks.api_task_action("event-1", {
        "userId": 7, "action_type": "完成", "review_at": review_at,
        "next_inspection_at": review_at,
    })

    with pytest.raises(Exception) as error:
        tasks.api_task_action("event-1", {
            "userId": 7, "action_type": "完成", "review_at": review_at,
            "next_inspection_at": review_at,
        })

    assert getattr(error.value, "status_code", None) == 409
    with sqlite3.connect(isolated_store) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observation_case WHERE event_uid='event-1'").fetchone()[0] == 1


def test_continue_observation_keeps_event_out_of_completion_queue(isolated_store):
    _seed_owned_events(isolated_store, states=("已处理待复扫", "新发现"))
    today = dt.date.today().isoformat()
    with sqlite3.connect(isolated_store) as connection:
        action_id = connection.execute(
            "INSERT INTO task_action(event_uid, user_id, action_type) VALUES ('event-1', 7, '完成')"
        ).lastrowid
        case_id = connection.execute("""
            INSERT INTO observation_case
              (parent_asin, shop_account, event_uid, completion_action_id, issue, severity,
               observation_at, next_inspection_at, status, agent_effect)
            VALUES ('PARENT', 'shop', 'event-1', ?, '异常1', 'S1', ?, ?, '待运营确认', '数据不足')
        """, (action_id, today, today)).lastrowid
        connection.commit()

    tasks.api_confirm_observation(case_id, {"userId": 7, "confirmation": "继续观察"})

    with sqlite3.connect(isolated_store) as connection:
        status = connection.execute("SELECT 当前状态 FROM event_pool WHERE 唯一识别='event-1'").fetchone()[0]
        case_status = connection.execute("SELECT status FROM observation_case WHERE id=?", (case_id,)).fetchone()[0]
    assert status == "已处理待复扫"
    assert case_status == "观察中"
    with pytest.raises(Exception) as error:
        tasks.api_task_action("event-1", {
            "userId": 7, "action_type": "完成",
            "review_at": (dt.date.today() + dt.timedelta(days=3)).isoformat(),
        })
    assert getattr(error.value, "status_code", None) == 409


def test_product_completion_skips_active_observations_without_creating_duplicates(isolated_store):
    _seed_owned_events(isolated_store)
    review_at = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    tasks.api_task_action("event-1", {
        "userId": 7, "action_type": "完成", "review_at": review_at,
        "next_inspection_at": review_at,
    })

    result = tasks.api_product_action({
        "userId": 7, "action_type": "完成产品维护", "parent_asin": "PARENT", "shop_account": "shop",
        "review_at": review_at, "next_inspection_at": review_at,
    })
    assert result["completed_event_uids"] == ["event-2"]
    assert result["active_observation_count"] == 2
    assert result["product_status"] == "已完成"

    repeated = tasks.api_product_action({
        "userId": 7, "action_type": "完成产品维护", "parent_asin": "PARENT", "shop_account": "shop",
        "review_at": review_at, "next_inspection_at": review_at,
    })
    assert repeated["completed_count"] == 0
    assert repeated["active_observation_count"] == 2
    assert repeated["product_status"] == "已完成"
    with sqlite3.connect(isolated_store) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observation_case").fetchone()[0] == 2


def test_active_observation_repair_and_unique_index_keep_one_case_per_event(isolated_store):
    with sqlite3.connect(isolated_store) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("DROP INDEX idx_observation_case_one_active_event")
        action_one = connection.execute(
            "INSERT INTO task_action(event_uid, action_type) VALUES ('duplicate-event', '完成')"
        ).lastrowid
        action_two = connection.execute(
            "INSERT INTO task_action(event_uid, action_type) VALUES ('duplicate-event', '完成')"
        ).lastrowid
        connection.execute("""
            INSERT INTO observation_case
              (parent_asin, shop_account, event_uid, completion_action_id, observation_at, next_inspection_at,
               status, confirmation, updated_at)
            VALUES ('PARENT', 'shop', 'duplicate-event', ?, '2026-07-22', '2026-07-22',
                    '观察中', '继续观察', '2026-07-22 10:00:00')
        """, (action_one,))
        connection.execute("""
            INSERT INTO observation_case
              (parent_asin, shop_account, event_uid, completion_action_id, observation_at, next_inspection_at,
               status, updated_at)
            VALUES ('PARENT', 'shop', 'duplicate-event', ?, '2026-07-22', '2026-07-22',
                    '待运营确认', '2026-07-22 11:00:00')
        """, (action_two,))
        assert local_store._repair_active_observation_duplicates(connection) == 1
        connection.execute("""
            CREATE UNIQUE INDEX idx_observation_case_one_active_event
            ON observation_case(event_uid)
            WHERE status IN ('观察中', '待运营确认')
        """)
        rows = connection.execute("""
            SELECT status, confirmation FROM observation_case
            WHERE event_uid='duplicate-event' ORDER BY id
        """).fetchall()
        assert rows[0]["status"] == "观察中"
        assert rows[1]["status"] == "已被重复观察替代"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO observation_case
                  (parent_asin, shop_account, event_uid, observation_at, next_inspection_at, status)
                VALUES ('PARENT', 'shop', 'duplicate-event', '2026-07-23', '2026-07-23', '观察中')
            """)
