from __future__ import annotations

import argparse
import asyncio

from web.backend.deps import get_child_fact_worker


def main() -> None:
    parser = argparse.ArgumentParser(description="asynchronous child-fact worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()

    async def run() -> None:
        worker = get_child_fact_worker()
        if args.once:
            await worker.process_once()
            return
        while True:
            processed = await worker.process_once()
            if not processed:
                await asyncio.sleep(args.poll_interval)

    asyncio.run(run())


if __name__ == "__main__":
    main()
