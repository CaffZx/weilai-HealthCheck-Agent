"""低风险批量抓取商品主图。

默认单线程、请求间隔 1.5 秒；空结果或异常不会覆盖已有主图。
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Any

from data import fixture_loader
from data import local_store as store
from data import sync_daily


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first_image(payload: Any) -> str | None:
    for item in _walk(payload):
        candidates = item.get("galleryThumbnails")
        if isinstance(candidates, list):
            for url in candidates:
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    return url
    return None


def _site_name(site_code: str) -> str:
    return site_code if site_code.startswith("Amazon_") else f"Amazon_{site_code or 'US'}"


_SITE_DOMAINS = {
    "Amazon_US": "www.amazon.com",
    "Amazon_UK": "www.amazon.co.uk",
    "Amazon_DE": "www.amazon.de",
    "Amazon_FR": "www.amazon.fr",
    "Amazon_IT": "www.amazon.it",
    "Amazon_ES": "www.amazon.es",
    "Amazon_NL": "www.amazon.nl",
    "Amazon_CA": "www.amazon.ca",
    "Amazon_AU": "www.amazon.com.au",
    "Amazon_JP": "www.amazon.co.jp",
}


def _product_url(parent_asin: str, site_code: str) -> str:
    site_name = _site_name(site_code)
    domain = _SITE_DOMAINS.get(site_name, "www.amazon.com")
    return f"https://{domain}/dp/{parent_asin}"


def fetch_one(parent_asin: str, shop_account: str, site_code: str) -> tuple[str, str | None]:
    site_name = _site_name(site_code)
    status, payload = sync_daily.mcp_call("pangolinfo_api_sync_Extract", {
        "asin": parent_asin,
        "url": _product_url(parent_asin, site_name),
        "parserName": "amzProductDetail",
        "siteCode": site_name,
    })
    if status != "OK":
        return status, None
    return status, _first_image(payload)


def run(limit: int = 10, interval: float = 1.5) -> dict[str, int]:
    store.init_db()
    configs = {c["fixture_key"]: c for c in fixture_loader.load_configs()}
    shop_map = fixture_loader.load_shop_map()
    products = []
    for key in fixture_loader.list_keys():
        cfg = configs.get(key, {})
        shop = shop_map.get(str(cfg.get("shop_id", "")), {})
        account = shop.get("account")
        if account:
            products.append((cfg["parent_asin"], account, cfg.get("site_code") or "US"))
        if len(products) >= limit:
            break

    stats = {"selected": len(products), "saved": 0, "empty": 0, "error": 0}
    for index, (parent_asin, account, site_code) in enumerate(products):
        try:
            status, image_url = fetch_one(parent_asin, account, site_code)
            if image_url:
                store.update_listing_image_url(
                    parent_asin, account, site_code, image_url,
                    "pangolinfo_api_sync_Extract.galleryThumbnails",
                )
                stats["saved"] += 1
                outcome = "saved"
            elif status == "OK":
                stats["empty"] += 1
                outcome = "empty"
            else:
                stats["error"] += 1
                outcome = status.lower()
            print(f"[{index + 1}/{len(products)}] {parent_asin}@{account} {outcome}")
        except Exception as exc:
            stats["error"] += 1
            print(f"[{index + 1}/{len(products)}] {parent_asin}@{account} error={exc}")
        if index + 1 < len(products):
            time.sleep(interval)
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--interval", type=float, default=1.5)
    args = parser.parse_args()
    print(json.dumps(run(args.limit, args.interval), ensure_ascii=False))
