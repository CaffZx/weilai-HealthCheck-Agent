from integrations.pangolinfo_product import (
    extract_main_product_image,
    is_amazon_product_image_url,
    pangolinfo_arguments,
)


def test_extracts_explicit_amazon_product_image_before_gallery():
    payload = [{
        "results": [{
            "image": "https://m.media-amazon.com/images/I/main.jpg",
            "galleryThumbnails": [
                "https://m.media-amazon.com/images/I/thumb.jpg",
            ],
            "video": {"cover": "https://m.media-amazon.com/images/I/video.jpg"},
        }],
    }]

    assert extract_main_product_image(payload).endswith("main.jpg")


def test_gallery_is_safe_fallback_but_non_amazon_logo_is_rejected():
    payload = [{
        "results": [{
            "image": "https://cdn.example.test/shop-logo.jpg",
            "galleryThumbnails": [
                "https://m.media-amazon.com/images/I/product.jpg",
            ],
        }],
    }]

    assert extract_main_product_image(payload).endswith("product.jpg")
    assert not is_amazon_product_image_url("https://cdn.example.test/shop-logo.jpg")


def test_pangolinfo_arguments_use_site_specific_product_url():
    assert pangolinfo_arguments("B0TEST1234", "Amazon_UK") == {
        "asin": "B0TEST1234",
        "url": "https://www.amazon.co.uk/dp/B0TEST1234",
        "parserName": "amzProductDetail",
        "siteCode": "Amazon_UK",
    }
