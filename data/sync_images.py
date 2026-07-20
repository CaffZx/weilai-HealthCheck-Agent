"""低风险批量抓取商品主图。

默认单线程、请求间隔 1.5 秒；空结果或异常不会覆盖已有主图。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import threading
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


def _image_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _image_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _image_strings(child)


def _first_image(payload: Any) -> str | None:
    for item in _walk(payload):
        for key in ("highResolutionImages", "galleryThumbnails", "image", "imageUrl", "image_url", "mainImage", "main_image", "mainImageUrl", "main_image_url", "hiResImage"):
            value = item.get(key)
            for nested in _image_strings(value):
                if nested.startswith(("http://", "https://")):
                    return nested
    return None


def _first_product_result(payload: Any) -> dict | None:
    """从 pangolinfo 的多层返回中取第一条商品详情。"""
    for item in _walk(payload):
        results = item.get("results") if isinstance(item, dict) else None
        if isinstance(results, list) and results and isinstance(results[0], dict):
            return results[0]
    return None


def _page_snapshot(payload: Any) -> dict | None:
    detail = _first_product_result(payload)
    if not detail:
        return None
    gallery = detail.get("highResolutionImages") or detail.get("images") or []
    product_description = detail.get("productDescription") or []
    aplus_count = sum(
        len(item.get("images") or []) for item in product_description
        if isinstance(item, dict)
    )
    strikethrough = detail.get("strikethroughPrice")
    if isinstance(strikethrough, dict):
        strikethrough = strikethrough.get("value")
    return {
        "main_image_url": detail.get("image") or _first_image(detail),
        "gallery_count": len(gallery),
        "aplus_image_count": aplus_count,
        "has_cart": detail.get("has_cart", detail.get("hasCart")),
        "in_stock": detail.get("inStock"),
        "price": detail.get("price"),
        "coupon": detail.get("coupon"),
        "strikethrough_price": strikethrough,
        "promotion_summary": detail.get("promotionSummary"),
        "variant_details": detail.get("variantDetails") or [],
    }


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


def fetch_one(parent_asin: str, shop_account: str, site_code: str,
              request_asin: str | None = None,
              session: sync_daily.MCPSession | None = None) -> tuple[str, str | None]:
    site_name = _site_name(site_code)
    if site_name not in _SITE_DOMAINS:
        return "ERR", None
    caller = session.call if session is not None else sync_daily.mcp_call
    request_asin = request_asin or parent_asin
    status, payload = caller("pangolinfo_api_sync_Extract", {
        "asin": request_asin,
        "url": _product_url(request_asin, site_name),
        "parserName": "amzProductDetail",
        "siteCode": site_name,
    })
    if status != "OK":
        return status, None
    return status, _first_image(payload)


def fetch_page_snapshot(parent_asin: str, site_code: str, request_asin: str,
                        session: sync_daily.MCPSession | None = None) -> tuple[str, dict | None]:
    site_name = _site_name(site_code)
    if site_name not in _SITE_DOMAINS:
        return "ERR", None
    caller = session.call if session is not None else sync_daily.mcp_call
    status, payload = caller("pangolinfo_api_sync_Extract", {
        "asin": request_asin,
        "url": _product_url(request_asin, site_name),
        "parserName": "amzProductDetail",
        "siteCode": site_name,
    })
    return status, _page_snapshot(payload) if status == "OK" else None


def run(limit: int = 10, interval: float = 1.5, offset: int = 0,
        concurrency: int = 1, skip_existing: bool = True) -> dict[str, int]:
    store.init_db()
    configs = {c["fixture_key"]: c for c in fixture_loader.load_configs()}
    shop_map = fixture_loader.load_shop_map()
    products = []
    seen_products = set()
    for key in fixture_loader.list_keys():
        cfg = configs.get(key, {})
        shop = shop_map.get(str(cfg.get("shop_id", "")), {})
        account = shop.get("account")
        parent_asin = cfg.get("parent_asin")
        product_key = (parent_asin, account)
        if account and parent_asin and product_key not in seen_products:
            seen_products.add(product_key)
            site_code = shop.get("site_code") or cfg.get("site_code") or "US"
            products.append((parent_asin, account, site_code))

    if skip_existing:
        image_map = store.query_image_urls_by_shop()
        products = [
            item for item in products
            if (item[0], item[1]) not in image_map
        ]
    products = products[offset:offset + limit]

    stats = {"selected": len(products), "saved": 0, "empty": 0, "error": 0}
    worker_state = threading.local()

    def process(item):
        index, parent_asin, account, site_code = item
        try:
            if not hasattr(worker_state, "session"):
                worker_state.session = sync_daily.MCPSession()
                worker_state.last_request = 0.0
            wait_for = interval - (time.monotonic() - worker_state.last_request)
            if wait_for > 0:
                time.sleep(wait_for)
            session = worker_state.session
            is_standard_asin = bool(re.fullmatch(r"B[A-Z0-9]{9}", parent_asin))
            child_asins = store.query_child_asins(parent_asin, account)
            request_asin = parent_asin if is_standard_asin else (child_asins[0] if child_asins else parent_asin)
            status, page = fetch_page_snapshot(parent_asin, site_code, request_asin, session)
            image_url = (page or {}).get("main_image_url")
            if not image_url:
                attempted = {request_asin}
                for child_asin in child_asins[:3]:
                    if child_asin in attempted:
                        continue
                    attempted.add(child_asin)
                    status, page = fetch_page_snapshot(parent_asin, site_code, child_asin, session)
                    image_url = (page or {}).get("main_image_url")
                    if image_url:
                        break
            if page:
                store.upsert_listing_page_snapshot(parent_asin, account, site_code, page)
            worker_state.last_request = time.monotonic()
            if image_url:
                store.update_listing_image_url(
                    parent_asin, account, site_code, image_url,
                    "pangolinfo_api_sync_Extract.galleryThumbnails",
                )
                outcome = "saved"
            elif status == "OK":
                outcome = "empty"
            else:
                outcome = "error"
            return outcome, f"[{index + 1}/{len(products)}] {parent_asin}@{account} {outcome}"
        except Exception as exc:
            return "error", f"[{index + 1}/{len(products)}] {parent_asin}@{account} error={exc}"

    tasks = [(index, parent_asin, account, site_code)
             for index, (parent_asin, account, site_code) in enumerate(products)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for outcome, message in pool.map(process, tasks):
            stats[outcome] += 1
            print(message, flush=True)
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--no-skip-existing", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(
        args.limit, args.interval, args.offset, args.concurrency,
        skip_existing=not args.no_skip_existing,
    ), ensure_ascii=False), flush=True)
