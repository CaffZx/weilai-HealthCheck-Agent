from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from web.backend.deps import get_rollout_policy, get_runtime_worker


def main() -> None:
    parser = argparse.ArgumentParser(description="MySQL patrol runtime worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if not get_rollout_policy().patrol_execution_enabled:
        raise SystemExit("rollout.stage=INTERNAL_ONLY: patrol worker is disabled")

    async def run() -> None:
        worker = get_runtime_worker()
        if args.once:
            count = await worker.drain()
            print(f"processed {count}")
            return
        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_number, shutdown.set)
            except NotImplementedError:
                pass
        await worker.run_forever(
            shutdown=shutdown,
            poll_interval=args.poll_interval,
        )

    asyncio.run(run())


if __name__ == "__main__":
    main()
