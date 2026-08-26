from __future__ import annotations

import argparse
import asyncio
from datetime import date

from web.backend.deps import get_patrol_scheduler, get_rollout_policy, load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="MySQL patrol scheduler entry")
    parser.add_argument("command", choices=("daily", "fallback", "due"))
    parser.add_argument("--business-date", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()

    enabled = bool(load_settings().get("feature_flags", {}).get("scheduler_enabled"))
    if not enabled:
        raise SystemExit(
            "scheduler_enabled=false: MCP operating-unit contract is not production-ready"
        )
    if not get_rollout_policy().patrol_execution_enabled:
        raise SystemExit("rollout.stage=INTERNAL_ONLY: scheduler is disabled")

    async def run() -> None:
        scheduler = get_patrol_scheduler()
        if args.command in {"daily", "fallback"}:
            result = await scheduler.create_daily_batch(business_date=args.business_date)
        else:
            result = await scheduler.create_due_batch(business_date=args.business_date)
        print("no batch" if result is None else result[0])

    asyncio.run(run())


if __name__ == "__main__":
    main()
