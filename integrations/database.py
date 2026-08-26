from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url


def database_url(value: str | None = None) -> URL:
    raw = (value or os.environ.get("PATROL_DATABASE_URL", "")).strip()
    if not raw:
        raise RuntimeError("PATROL_DATABASE_URL is required")
    url = make_url(raw)
    if url.get_backend_name() != "mysql":
        raise ValueError("PATROL_DATABASE_URL must use MySQL")
    return url


def create_database_engine(value: str | None = None) -> Engine:
    return create_engine(
        database_url(value),
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_timeout=30,
        connect_args={"connect_timeout": 10},
        isolation_level="READ COMMITTED",
        future=True,
    )
