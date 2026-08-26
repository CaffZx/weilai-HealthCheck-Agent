from __future__ import annotations

import os

import pytest

from scripts.verify_mysql_s3 import main as verify_s3

pytestmark = pytest.mark.integration


def test_mysql_pure_patrol_main_path() -> None:
    if not os.environ.get("PATROL_DATABASE_URL"):
        pytest.skip("PATROL_DATABASE_URL is not configured")
    verify_s3()
