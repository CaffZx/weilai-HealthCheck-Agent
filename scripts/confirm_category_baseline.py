from __future__ import annotations

import argparse

from integrations.category_baseline import MySqlCategoryBaselineService
from integrations.database import create_database_engine


def main() -> None:
    parser = argparse.ArgumentParser(
        description="confirm a first-observed category baseline"
    )
    parser.add_argument("operating_unit_id")
    parser.add_argument("child_asin")
    parser.add_argument("--confirmed-by", required=True)
    args = parser.parse_args()

    engine = create_database_engine()
    try:
        MySqlCategoryBaselineService(engine).confirm(
            args.operating_unit_id,
            args.child_asin,
            confirmed_by=args.confirmed_by,
        )
    finally:
        engine.dispose()
    print("category baseline confirmed")


if __name__ == "__main__":
    main()
