from __future__ import annotations

import argparse
import asyncio

from web.backend.deps import (
    get_control_center_delivery_worker,
    load_settings,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="MySQL control-center MCP delivery worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=2)
    args = parser.parse_args()
    enabled = bool(
        load_settings().get("feature_flags", {}).get("control_center_delivery_enabled")
    )
    if not enabled:
        raise SystemExit(
            "control_center_delivery_enabled=false: control-center delivery is disabled"
        )
    # CC delivery uses its own feature flag (control_center_delivery_enabled),
    # NOT result_delivery_enabled (which controls the deprecated INSPECTION_RUN pipeline).
    # The two pipelines are independent per docs/32-Outbox双管道错位排障与执行方案.md.
    if not enabled:
        raise SystemExit("control_center_delivery_enabled is required")

    async def run() -> None:
        worker = get_control_center_delivery_worker()
        while True:
            result = await worker.process_once()
            if args.once:
                return
            if result.outbox_id is None:
                await asyncio.sleep(max(args.poll_interval, 0.1))

    asyncio.run(run())


if __name__ == "__main__":
    main()
