from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.backend.legacy_sqlite_guard import LegacySqliteReadOnlyMiddleware
from web.backend.main import app as runtime_app


def create_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(LegacySqliteReadOnlyMiddleware)

    @app.get("/api/tasks/today")
    async def read_tasks() -> dict:
        return {"ok": True}

    @app.post("/api/tasks/assign")
    async def write_tasks() -> dict:
        raise AssertionError("legacy SQLite write handler must not run")

    @app.post("/api/inspect/B0TEST")
    async def write_inspection() -> dict:
        raise AssertionError("legacy SQLite inspection handler must not run")

    return TestClient(app)


def test_legacy_sqlite_read_routes_are_gone():
    with create_client() as client:
        response = client.get("/api/tasks/today")

    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "SQLITE_RETIRED"


def test_legacy_sqlite_task_write_is_gone():
    with create_client() as client:
        response = client.post("/api/tasks/assign")

    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "SQLITE_RETIRED"


def test_legacy_sqlite_inspection_write_is_gone():
    with create_client() as client:
        response = client.post("/api/inspect/B0TEST")

    assert response.status_code == 410


def test_legacy_sqlite_retirement_has_no_configuration_bypass():
    app = FastAPI()
    app.add_middleware(LegacySqliteReadOnlyMiddleware)

    @app.post("/api/tasks/assign")
    async def write_tasks() -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post("/api/tasks/assign")

    assert response.status_code == 410


def test_runtime_app_retires_legacy_read_and_write_routes():
    with TestClient(runtime_app) as client:
        assert client.get("/api/tasks/today").status_code == 410
        assert client.post("/api/inspect/B0TEST").status_code == 410
        assert client.get("/api/v1/health").status_code == 200
