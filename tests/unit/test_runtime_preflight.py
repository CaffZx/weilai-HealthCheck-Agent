from __future__ import annotations

from clients.azlisting_contract import AZLISTING_INTERNAL_CONTRACT_VERSION
from scripts import runtime_preflight


class UnavailableEngine:
    def connect(self):
        raise TimeoutError("database unavailable")

    def dispose(self):
        pass


def test_preflight_reports_database_unavailable_without_traceback(monkeypatch, capsys):
    settings = {
        "database": {"url_env": "PATROL_DATABASE_URL"},
        "mcp": {
            "internal_contract_version": AZLISTING_INTERNAL_CONTRACT_VERSION,
            "external_contract_status": "PENDING_OWNER_FREEZE",
        },
        "feature_flags": {
            "mysql_runtime": True,
            "scheduler_enabled": False,
            "result_delivery_enabled": False,
            "feedback_enabled": False,
        },
        "rollout": {"stage": "INTERNAL_ONLY"},
    }
    monkeypatch.setenv("PATROL_DATABASE_URL", "mysql+pymysql://runtime")
    monkeypatch.setattr(runtime_preflight, "load_settings", lambda: settings)
    monkeypatch.setattr(runtime_preflight, "create_database_engine", lambda: UnavailableEngine())
    monkeypatch.setattr("sys.argv", ["runtime_preflight"])
    assert runtime_preflight.main() == 1
    captured = capsys.readouterr()
    assert "FAIL DATABASE_UNAVAILABLE: TimeoutError" in captured.err
    assert "Traceback" not in captured.err
