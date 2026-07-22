import json
import sqlite3

import pytest

from data import local_store
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
