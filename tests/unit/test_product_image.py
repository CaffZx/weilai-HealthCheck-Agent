from __future__ import annotations

from datetime import datetime

from integrations.product_image import MySqlProductImageService


class FakeResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class FakeConnection:
    def __init__(self, engine):
        self.engine = engine

    def execute(self, statement):
        if getattr(statement, "is_insert", False):
            values = dict(statement._values)
            resolved = {
                key: getattr(value, "value", value) for key, value in values.items()
            }
            existing = self.engine.row
            if existing is None:
                self.engine.row = resolved
            else:
                for key in (
                    "image_url",
                    "source_tool",
                    "source_fetched_at",
                    "last_observed_at",
                ):
                    existing[key] = resolved[key]
            return FakeResult(None)
        return FakeResult(self.engine.row)


class FakeTransaction:
    def __init__(self, engine):
        self.connection = FakeConnection(engine)

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeEngine:
    def __init__(self):
        self.row = None

    def begin(self):
        return FakeTransaction(self)


def _normalized(image_url=None, source=None):
    return {
        "identity": {
            "storefront": {
                "main_image_url": image_url,
                "main_image_source": source,
                "main_image_fetched_at": None,
                "main_image_cached": False,
            },
        },
    }


def test_nonempty_product_image_is_saved_and_returned(binding):
    engine = FakeEngine()
    service = MySqlProductImageService(engine)
    observed_at = datetime(2026, 8, 6, 8, 30)

    result = service.retain_latest(
        binding,
        _normalized(
            "https://m.media-amazon.com/images/I/product.jpg",
            "erp_asin_full_detail.images.main",
        ),
        observed_at=observed_at,
    )

    storefront = result["identity"]["storefront"]
    assert storefront["main_image_url"].endswith("product.jpg")
    assert storefront["main_image_cached"] is False
    assert engine.row["parent_seller_sku"] == binding.parent_seller_sku


def test_empty_refresh_reuses_previous_product_image(binding):
    engine = FakeEngine()
    service = MySqlProductImageService(engine)
    service.retain_latest(
        binding,
        _normalized(
            "https://m.media-amazon.com/images/I/product.jpg",
            "erp_listing_product_info.picUrl",
        ),
        observed_at=datetime(2026, 8, 5, 8, 30),
    )

    result = service.retain_latest(
        binding,
        _normalized(),
        observed_at=datetime(2026, 8, 6, 8, 30),
    )

    storefront = result["identity"]["storefront"]
    assert storefront["main_image_url"].endswith("product.jpg")
    assert storefront["main_image_source"] == "erp_listing_product_info.picUrl"
    assert storefront["main_image_cached"] is True


def test_store_logo_is_not_considered_when_product_image_is_empty(binding):
    engine = FakeEngine()
    service = MySqlProductImageService(engine)
    normalized = _normalized()
    normalized["identity"]["store_logo_url"] = "https://example.test/logo.jpg"

    result = service.retain_latest(binding, normalized)

    assert result["identity"]["storefront"]["main_image_url"] is None
    assert engine.row is None
