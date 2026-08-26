from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from integrations.rollout import RolloutPolicy
from web.backend import deps
from web.backend.deps import get_feedback_inbox, require_feedback_access
from web.backend.routers import feedback_v1


class FailingInbox:
    def consume(self, event):
        raise AssertionError("unauthorized feedback must not be consumed")


EVENT = {
    "event_id": "security-test-1",
    "signal_id": "is_0123456789abcdef01234567",
    "signal_version": 1,
    "event_type": "HANDLED",
    "source_system": "control-center",
    "occurred_at": "2026-08-03T00:00:00Z",
}


def test_feedback_access_failure_prevents_consumption():
    app = FastAPI()
    app.include_router(feedback_v1.router)
    app.dependency_overrides[get_feedback_inbox] = lambda: FailingInbox()

    def reject():
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="unauthorized")

    app.dependency_overrides[require_feedback_access] = reject
    with TestClient(app) as client:
        response = client.post("/internal/v1/patrol/feedback-events", json=EVENT)
    assert response.status_code == 401


def test_internal_rollout_blocks_feedback_before_consumption(monkeypatch):
    app = FastAPI()
    app.include_router(feedback_v1.router)
    app.dependency_overrides[get_feedback_inbox] = lambda: FailingInbox()
    monkeypatch.setattr(deps, "get_rollout_policy", lambda: RolloutPolicy(stage="INTERNAL_ONLY"))
    monkeypatch.setattr(
        deps,
        "load_settings",
        lambda: {"feature_flags": {"feedback_enabled": True}},
    )

    with TestClient(app) as client:
        response = client.post("/internal/v1/patrol/feedback-events", json=EVENT)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "FEEDBACK_ROLLOUT_BLOCKED"
