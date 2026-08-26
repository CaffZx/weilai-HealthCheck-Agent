from __future__ import annotations

from fastapi.testclient import TestClient

from integrations.rollout import RolloutPolicy
from web.backend.deps import get_rollout_policy, get_runtime_health, get_runtime_queries
from web.backend.main import app


class FakeWorkspaceQueries:
    def workspace_summary(self):
        return {
            "batches": 1,
            "jobs": 2,
            "runs": 1,
            "signals": 3,
            "active_signals": 2,
            "due_signals": 0,
            "latest_batch": None,
        }

    def list_batches(self, **_kwargs):
        return {"items": [], "page": 1, "page_size": 20, "total": 0, "pages": 0}

    def list_jobs(self, **_kwargs):
        return {"items": [], "page": 1, "page_size": 20, "total": 0, "pages": 0}

    def list_runs(self, **_kwargs):
        return {"items": [], "page": 1, "page_size": 20, "total": 0, "pages": 0}

    def list_signals(self, **_kwargs):
        return {"items": [], "page": 1, "page_size": 20, "total": 0, "pages": 0}

    def get_raw_facts(self, _run_id, *, include_payload=True):
        assert include_payload is False
        return [{
            "fact_key": "stock",
            "tool_name": "erp_listing_stock_alert",
            "request_hash": "sha256:" + "1" * 64,
            "response_content_hash": "sha256:" + "2" * 64,
            "status": "SUCCESS",
            "is_core": True,
            "row_count": 1,
            "latency_ms": 12,
        }]


class FakeHealth:
    def snapshot(self):
        return {
            "status": "HEALTHY",
            "database": {"status": "AVAILABLE"},
            "metrics": {"jobs": {}, "outbox": {}, "scheduler_leases": 0},
            "alerts": [],
        }


def test_runtime_serves_mysql_v2_workspace_and_assets():
    with TestClient(app) as client:
        root = client.get("/", follow_redirects=False)
        page = client.get("/ui")
        script = client.get("/ui/assets/workspace-v2.js")

    assert root.status_code == 307
    assert root.headers["location"] == "/ui"
    assert page.status_code == 200
    assert "业务巡检 Agent" in page.text
    assert "/api/tasks" not in page.text
    assert script.status_code == 200
    assert "/api/v1/patrol/workspace/overview" in script.text
    assert "data-batch-signals" in script.text
    assert "signalBatchId" in script.text


def test_workspace_overview_is_mysql_v2_read_only_shape():
    app.dependency_overrides[get_runtime_queries] = FakeWorkspaceQueries
    app.dependency_overrides[get_runtime_health] = FakeHealth
    app.dependency_overrides[get_rollout_policy] = lambda: RolloutPolicy(stage="INTERNAL_ONLY")
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/patrol/workspace/overview")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["summary"]["signals"] == 3
    assert response.json()["rollout"]["stage"] == "INTERNAL_ONLY"
    assert response.json()["rollout"]["patrol_execution_enabled"] is False


def test_workspace_fact_endpoint_never_returns_raw_payloads():
    app.dependency_overrides[get_runtime_queries] = FakeWorkspaceQueries
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/patrol/workspace/runs/pr_0123456789abcdef01234567/facts"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    fact = response.json()["facts"][0]
    assert fact["fact_key"] == "stock"
    assert "request_json" not in fact
    assert "response_json" not in fact
    assert "extracted_data_json" not in fact
