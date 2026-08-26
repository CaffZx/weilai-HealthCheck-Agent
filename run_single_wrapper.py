import os
import sys
from dotenv import load_dotenv

load_dotenv("/opt/weilai-HealthCheck-Agent-v2.0/.env")

sys.argv = [
    "run_single_real_patrol",
    "--shop-id", "42453",
    "--shop-account", "am_example_us",
    "--site-code", "AMAZON_US",
    "--parent-asin", "US-无袖圆领连体衣",
    "--parent-seller-sku", "US-WXYLLTY-0106",
    "--owner-user-id", "467",
    "--owner-name", "467",
]

from scripts.run_single_real_patrol import main

raise SystemExit(main())
