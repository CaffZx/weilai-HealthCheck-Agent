from __future__ import annotations

import os

import pytest

from scripts.verify_mysql_lifecycle import main as verify_lifecycle
from scripts.verify_mysql_s2 import main as verify_runtime

pytestmark = pytest.mark.integration


def require_mysql() -> None:
    if not os.environ.get("PATROL_DATABASE_URL"):
        pytest.skip("PATROL_DATABASE_URL is not configured")


def test_mysql_runtime_queue_and_scheduler_lease():
    require_mysql()
    verify_runtime()


def test_mysql_signal_lifecycle():
    require_mysql()
    verify_lifecycle()
