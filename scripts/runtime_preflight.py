from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import text

from integrations.database import create_database_engine
from integrations.mcp_routing import resolve_primary_mcp_route
from integrations.observability import HealthLevel
from integrations.preflight import validate_runtime_configuration
from web.backend.deps import get_runtime_health, load_settings

EXPECTED_REVISION = "0012_listing_catalog_source"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only patrol runtime preflight")
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="allow DEGRADED health while still rejecting CRITICAL",
    )
    args = parser.parse_args()
    settings = load_settings()
    issues = validate_runtime_configuration(settings, dict(os.environ))
    if issues:
        for issue in issues:
            print(f"FAIL {issue.code}: {issue.message}", file=sys.stderr)
        return 1

    engine = create_database_engine()
    try:
        try:
            with engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM t_patrol_schema_version")
                ).scalar_one()
        except Exception as exc:
            print(
                f"FAIL DATABASE_UNAVAILABLE: {type(exc).__name__}",
                file=sys.stderr,
            )
            return 1
        if revision != EXPECTED_REVISION:
            print(
                f"FAIL SCHEMA_REVISION: expected {EXPECTED_REVISION}, got {revision}",
                file=sys.stderr,
            )
            return 1
        health = get_runtime_health().snapshot()
        status = health["status"]
        if status == HealthLevel.CRITICAL.value or (
            status == HealthLevel.DEGRADED.value and not args.allow_degraded
        ):
            print(f"FAIL RUNTIME_HEALTH: {status}", file=sys.stderr)
            for alert in health.get("alerts", []):
                print(f"  {alert['level']} {alert['code']}: {alert['message']}", file=sys.stderr)
            return 1
        print(
            f"preflight ok: revision={revision} health={status} "
            f"rollout={settings.get('rollout', {}).get('stage', 'INTERNAL_ONLY')} "
            f"mcp_primary={resolve_primary_mcp_route(settings, os.environ).provider}"
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
