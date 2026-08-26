from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from core.operating_unit import canonical_site_code

PANGOLINFO_TOOL = "pangolinfo_api_sync_Extract"
PANGOLINFO_IMAGE_SOURCE = "pangolinfo_api_sync_explicit_main_image"

_SITE_DOMAINS = {
    "AMAZON_US": "www.amazon.com",
    "AMAZON_UK": "www.amazon.co.uk",
    "AMAZON_DE": "www.amazon.de",
    "AMAZON_FR": "www.amazon.fr",
    "AMAZON_IT": "www.amazon.it",
    "AMAZON_ES": "www.amazon.es",
    "AMAZON_NL": "www.amazon.nl",
    "AMAZON_CA": "www.amazon.ca",
    "AMAZON_AU": "www.amazon.com.au",
    "AMAZON_JP": "www.amazon.co.jp",
}
_MAIN_IMAGE_KEYS = (
    "image",
    "mainImage",
    "main_image",
    "mainImageUrl",
    "main_image_url",
    "hiResImage",
)
_GALLERY_KEYS = ("highResolutionImages", "galleryThumbnails", "images")
_AMAZON_IMAGE_HOSTS = frozenset({
    "m.media-amazon.com",
    "images-na.ssl-images-amazon.com",
})


def pangolinfo_arguments(asin: str, site_code: str) -> dict[str, str]:
    canonical_site = canonical_site_code(site_code)
    domain = _SITE_DOMAINS.get(canonical_site)
    if domain is None:
        raise ValueError(f"unsupported Amazon site for Pangolinfo: {site_code}")
    return {
        "asin": asin,
        "url": f"https://{domain}/dp/{asin}",
        "parserName": "amzProductDetail",
        "siteCode": "Amazon_" + canonical_site.removeprefix("AMAZON_"),
    }


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def is_amazon_product_image_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme == "https" and parsed.hostname in _AMAZON_IMAGE_HOSTS


def extract_main_product_image(payload: Any) -> str | None:
    candidates = list(_walk(payload))
    product_results = [
        result
        for item in candidates
        if isinstance(item.get("results"), list)
        for result in item["results"]
        if isinstance(result, dict)
    ]
    search_roots = product_results or candidates
    for keys in (_MAIN_IMAGE_KEYS, _GALLERY_KEYS):
        for item in search_roots:
            for key in keys:
                for value in _strings(item.get(key)):
                    if is_amazon_product_image_url(value):
                        return value.strip()
    return None
