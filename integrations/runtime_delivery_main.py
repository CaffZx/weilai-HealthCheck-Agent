from __future__ import annotations

import argparse
import asyncio

from web.backend.deps import get_result_delivery_worker, get_rollout_policy, load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="MySQL inspection result delivery worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=2)
    args = parser.parse_args()
    if not load_settings().get("feature_flags", {}).get("result_delivery_enabled"):
        raise SystemExit(
            "result_delivery_enabled=false: result receiver contract is not production-ready"
        )
    if not get_rollout_policy().result_delivery_enabled:
        raise SystemExit("rollout stage does not permit result delivery")

    async def run() -> None:
        worker = get_result_delivery_worker()
        while True:
            result = await worker.process_once()
            if args.once:
                return
            if result.outbox_id is None:
                await asyncio.sleep(max(args.poll_interval, 0.1))

    asyncio.run(run())


if __name__ == "__main__":
    main()
