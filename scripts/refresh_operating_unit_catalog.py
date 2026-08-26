from __future__ import annotations

import argparse
import asyncio
import logging

from web.backend.deps import get_operating_unit_provider


async def refresh() -> int:
    result = await get_operating_unit_provider().refresh()
    print(
        f"catalog refreshed id={result.catalog_id} units={result.unit_count} "
        f"source_records={result.source_record_count} "
        f"unresolved_parent={result.unresolved_parent_count} "
        f"fetched_at={result.fetched_at.isoformat()}"
    )
    return 0


def main() -> int:
    argparse.ArgumentParser(
        description="Refresh the full active operating-unit catalog from AZ MCP"
    ).parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(refresh())


if __name__ == "__main__":
    raise SystemExit(main())
