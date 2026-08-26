"""HTTP 接口集成测试。

验证 patrol_v1 的入队、幂等、身份校验，以及旧 review_v1 的退役响应。
用 FastAPI TestClient，不起真实服务。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from integrations.rollout import RolloutPolicy
from integrations.runtime_queue import InMemoryRuntimeQueue
from web.backend.deps import (
    get_rollout_policy,
    get_runtime_health,
    get_runtime_queries,
    get_runtime_queue,
)
from web.backend.routers import patrol_v1, review_v1

pytestmark = pytest.mark.integration


class FakeRuntimeQueries:
    def __init__(self):
        self.runs = {}
        self.signals = {}
        self.raw_facts = {}

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def get_signal(self, signal_id):
        return self.signals.get(signal_id)

    def get_raw_facts(self, run_id, *, include_payload=True):
        facts = self.raw_facts.get(run_id, [])
        if include_payload:
            return facts
        return [
            {key: value for key, value in fact.items() if not key.endswith("_json")}
            for fact in facts
        ]

    def get_catalog_owner_user_ids(self, operating_unit_id):
        return []


@pytest.fixture
def client():
    """只挂 v1 路由，避免旧路由的 DB 依赖干扰。"""
    app = FastAPI()
    app.include_router(patrol_v1.router)
    app.include_router(review_v1.router)

    runtime_queue = InMemoryRuntimeQueue()
    queries = FakeRuntimeQueries()

    app.dependency_overrides[get_runtime_queue] = lambda: runtime_queue
    app.dependency_overrides[get_runtime_queries] = lambda: queries
    app.dependency_overrides[get_rollout_policy] = lambda: RolloutPolicy(stage="FULL")
    app.dependency_overrides[get_runtime_health] = lambda: type(
        "FakeHealth",
        (),
        {
            "snapshot": lambda self: {
                "status": "HEALTHY",
                "metrics": {"jobs": runtime_queue.stats()},
                "alerts": [],
            }
        },
    )()

    with TestClient(app) as test_client:
        test_client.queue = runtime_queue
        test_client.queries = queries
        yield test_client


PATROL_BODY = {
    "operating_unit": {
        "shop_id": 1622,
        "site_code": "us",
        "parent_asin": " b0test123 ",
        "shop_account": "test-shop-account",
        "parent_seller_sku": "PSKU-001",
    },
    "trigger_type": "MANUAL",
}

HEADERS = {
    "X-Request-Id": "REQ-API-1",
    "X-Trace-Id": "TRACE-API-1",
    "X-Actor-Id": "scheduler",
}


def test_create_patrol_run_accepts_and_normalizes_identity(client):
    response = client.post("/api/v1/patrol/runs", json=PATROL_BODY, headers=HEADERS)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "ACCEPTED"
    # 服务端派生身份，调用方传的大小写和空格被归一化
    assert body["operating_unit_id"] == "ou_" + body["operating_unit_id"][3:]
    assert len(body["operating_unit_id"]) == 27
    batch = client.queue.get_batch(client.queue.get_job(body["job_id"])["batch_id"])
    assert batch["trigger_type"] == "MANUAL"


def test_manual_api_rejects_scheduler_trigger_type(client):
    response = client.post(
        "/api/v1/patrol/runs",
        json={**PATROL_BODY, "trigger_type": "DAILY_SCHEDULE"},
        headers={**HEADERS, "X-Request-Id": "REQ-NOT-MANUAL"},
    )

    assert response.status_code == 422
    assert client.queue.stats().get("PENDING", 0) == 0


def test_internal_rollout_rejects_manual_run_without_enqueuing(client):
    client.app.dependency_overrides[get_rollout_policy] = lambda: RolloutPolicy(
        stage="INTERNAL_ONLY"
    )

    response = client.post(
        "/api/v1/patrol/runs",
        json=PATROL_BODY,
        headers={**HEADERS, "X-Request-Id": "REQ-INTERNAL-BLOCKED"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "PATROL_DISABLED"
    assert client.queue.stats().get("PENDING", 0) == 0


def test_shadow_rollout_rejects_manual_shop_outside_allowlist(client):
    client.app.dependency_overrides[get_rollout_policy] = lambda: RolloutPolicy(
        stage="SHADOW", allowlisted_shop_ids=[9999]
    )

    response = client.post(
        "/api/v1/patrol/runs",
        json=PATROL_BODY,
        headers={**HEADERS, "X-Request-Id": "REQ-SHADOW-BLOCKED"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "SHOP_NOT_ALLOWLISTED"
    assert client.queue.stats().get("PENDING", 0) == 0


def test_same_request_id_returns_same_job(client):
    first = client.post("/api/v1/patrol/runs", json=PATROL_BODY, headers=HEADERS).json()
    second = client.post("/api/v1/patrol/runs", json=PATROL_BODY, headers=HEADERS).json()
    assert first["job_id"] == second["job_id"]


def test_same_request_id_different_body_conflicts(client):
    client.post("/api/v1/patrol/runs", json=PATROL_BODY, headers=HEADERS)
    changed = {
        **PATROL_BODY,
        "operating_unit": {**PATROL_BODY["operating_unit"], "parent_asin": "B0OTHER"},
    }
    response = client.post("/api/v1/patrol/runs", json=changed, headers=HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_same_request_id_different_trace_conflicts(client):
    client.post("/api/v1/patrol/runs", json=PATROL_BODY, headers=HEADERS)
    response = client.post(
        "/api/v1/patrol/runs",
        json=PATROL_BODY,
        headers={**HEADERS, "X-Trace-Id": "TRACE-API-CHANGED"},
    )
    assert response.status_code == 409


def test_same_request_id_different_initialization_state_conflicts(client):
    client.post("/api/v1/patrol/runs", json=PATROL_BODY, headers=HEADERS)
    response = client.post(
        "/api/v1/patrol/runs",
        json={**PATROL_BODY, "initialization_ready": False},
        headers=HEADERS,
    )
    assert response.status_code == 409


def test_invalid_identity_is_rejected(client):
    bad = {
        **PATROL_BODY,
        "operating_unit": {**PATROL_BODY["operating_unit"], "parent_asin": "  "},
    }
    response = client.post(
        "/api/v1/patrol/runs", json=bad, headers={**HEADERS, "X-Request-Id": "REQ-BAD"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_OPERATING_UNIT"


def test_missing_request_id_header_is_rejected(client):
    response = client.post("/api/v1/patrol/runs", json=PATROL_BODY)
    assert response.status_code == 422


def test_client_cannot_inject_operating_unit_id(client):
    """身份必须由服务端算，不接受调用方直接给。"""
    body = {
        **PATROL_BODY,
        "operating_unit": {
            **PATROL_BODY["operating_unit"],
            "operating_unit_id": "ou_deadbeefdeadbeefdeadbeef",
        },
    }
    response = client.post(
        "/api/v1/patrol/runs", json=body, headers={**HEADERS, "X-Request-Id": "REQ-INJECT"},
    )
    assert response.status_code == 422


def test_get_patrol_run_returns_job_state(client):
    created = client.post(
        "/api/v1/patrol/runs", json=PATROL_BODY,
        headers={**HEADERS, "X-Request-Id": "REQ-GET"},
    ).json()
    response = client.get(f"/api/v1/patrol/runs/{created['job_id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "PENDING"
    assert response.json()["request_id"] == "REQ-GET"

    canonical = client.get(f"/api/v1/patrol/jobs/{created['job_id']}")
    assert canonical.status_code == 200
    assert canonical.json()["job_id"] == created["job_id"]


def test_get_unknown_job_is_404(client):
    response = client.get("/api/v1/patrol/runs/job_missing")
    assert response.status_code == 404


def test_get_patrol_run_returns_runtime_query(client):
    run_id = "pr_0123456789abcdef01234567"
    client.queries.runs[run_id] = {
        "run_id": run_id,
        "status": "COMPLETED",
        "signal_count": 1,
        "delivery": {"status": "SUPPRESSED"},
    }

    response = client.get(f"/api/v1/patrol/runs/{run_id}")

    assert response.status_code == 200
    assert response.json()["delivery"]["status"] == "SUPPRESSED"


def test_get_patrol_signal_returns_current_state_and_history(client):
    signal_id = "is_0123456789abcdef01234567"
    client.queries.signals[signal_id] = {
        "signal": {"signal_id": signal_id, "signal_state": "NEW", "version": 1},
        "occurrences": [{"occurrence_type": "DETECTED"}],
    }

    response = client.get(f"/api/v1/patrol/signals/{signal_id}")

    assert response.status_code == 200
    assert response.json()["signal"]["version"] == 1
    assert response.json()["occurrences"][0]["occurrence_type"] == "DETECTED"


def test_get_patrol_raw_facts_returns_complete_archive(client):
    run_id = "pr_0123456789abcdef01234567"
    client.queries.raw_facts[run_id] = [{
        "raw_fact_id": "rf_0123456789abcdef01234567",
        "run_id": run_id,
        "operating_unit_id": "ou_0123456789abcdef01234567",
        "fact_key": "stock",
        "tool_name": "erp_listing_stock_alert",
        "status": "SUCCESS",
        "request_json": {"parentAsin": "B0TEST123"},
        "response_json": {"structuredContent": {"data": []}},
        "extracted_data_json": [],
    }]

    response = client.get(f"/api/v1/patrol/runs/{run_id}/facts")

    assert response.status_code == 200
    assert response.json()["fact_count"] == 1
    assert response.json()["facts"][0]["response_json"] is not None


def test_get_unknown_runtime_objects_are_404(client):
    assert client.get("/api/v1/patrol/runs/pr_missing").status_code == 404
    assert client.get("/api/v1/patrol/signals/is_missing").status_code == 404


def test_get_patrol_batch_returns_real_aggregates(client):
    created = client.post(
        "/api/v1/patrol/runs",
        json=PATROL_BODY,
        headers={**HEADERS, "X-Request-Id": "REQ-BATCH"},
    ).json()
    job = client.queue.get_job(created["job_id"])
    response = client.get(f"/api/v1/patrol/batches/{job['batch_id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "RUNNING"
    assert response.json()["total_count"] == 1
    assert response.json()["pending_count"] == 1


def test_get_unknown_batch_is_404(client):
    response = client.get("/api/v1/patrol/batches/batch_missing")
    assert response.status_code == 404


def test_patrol_health_reports_queue_stats(client):
    client.post(
        "/api/v1/patrol/runs", json=PATROL_BODY,
        headers={**HEADERS, "X-Request-Id": "REQ-HEALTH"},
    )
    response = client.get("/api/v1/patrol/health")
    assert response.status_code == 200
    assert response.json()["status"] == "HEALTHY"
    assert response.json()["metrics"]["jobs"]["PENDING"] >= 1


def test_patrol_metrics_are_prometheus_text(client):
    response = client.get("/api/v1/patrol/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "patrol_health_status 0" in response.text


# ---------------------------------------------------------------------------
# 已退役复盘接口
# ---------------------------------------------------------------------------
def test_legacy_review_create_is_gone(client):
    response = client.post("/internal/v1/reviews", json={"any": "payload"})

    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "LEGACY_REVIEW_RETIRED"


def test_legacy_review_job_query_is_gone(client):
    response = client.get("/internal/v1/reviews/job_legacy")

    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "LEGACY_REVIEW_RETIRED"
