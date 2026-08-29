from __future__ import annotations

from pathlib import Path

from app.services.product_acquisition import ProductAcquisitionEngine


MAGENTO_SHELL = '<html><body><div id="root" data-media-backend="https://media.example.test"></div><script src="/client.abc123.js"></script></body></html>'


class FakeMagentoClient:
    def __init__(self, **_: object) -> None:
        self.posts: list[dict[str, object]] = []

    def get_html(self, _: str) -> str:
        return MAGENTO_SHELL

    def post_json(self, _: str, payload: dict[str, object]) -> dict[str, object]:
        self.posts.append(payload)
        query = str(payload.get("query") or "")
        if "urlResolver" in query:
            return {"data": {"urlResolver": {"id": "390", "type": "CATEGORY", "relative_url": "bedroom/all-beds"}}}
        return {
            "data": {
                "products": {
                    "total_count": 2,
                    "items": [
                        {
                            "uid": "Mjc3MzQ=",
                            "sku": "MXWL.FABRIC.BDRM.BED",
                            "name": "Maxwell Bed",
                            "url_key": "bedroom/all-beds/maxwell-custom-upholstered-bed-storage-option",
                            "url_suffix": None,
                            "image": {"url": "https://media.example.test/maxwell.jpg"},
                            "small_image": {"url": "https://media.example.test/maxwell-small.jpg"},
                            "url_rewrites": [{"url": "bedroom/all-beds/maxwell-custom-upholstered-bed-storage-option"}],
                            "description": {"html": "Bed frame: 67.5\" W x 88.5\" D x 55\" H"},
                            "short_description": {"html": "Upholstered Bed"},
                        },
                        {
                            "uid": "ODYyMg==",
                            "sku": "GRAM.FABRIC.BDRM.BED",
                            "name": "Graham Bed",
                            "url_key": "bedroom/all-beds/graham-custom-upholstered-bed-storage-option",
                            "url_suffix": None,
                            "image": {"url": "https://media.example.test/graham.jpg"},
                            "small_image": {"url": "https://media.example.test/graham-small.jpg"},
                            "url_rewrites": [{"url": "bedroom/all-beds/graham-custom-upholstered-bed-storage-option"}],
                            "description": {"html": "Bed frame: 70\" W x 90\" D x 54\" H"},
                            "short_description": {"html": "Upholstered Bed"},
                        },
                    ],
                }
            }
        }


def test_magento_pwa_shell_discovers_products_from_selected_category(tmp_path: Path) -> None:
    client = FakeMagentoClient()
    engine = ProductAcquisitionEngine(
        source_url="https://example.test/bedroom/all-beds",
        site_key="example.test",
        source_type="DIRECT_BRAND",
        categories=[{
            "category_id": "magento_390",
            "canonical_name": "Beds",
            "source_url": "https://example.test/bedroom/all-beds",
            "selected": True,
        }],
        workspace=tmp_path,
        browser_session_dir=tmp_path / "browser",
        client_factory=lambda **_: client,
    )

    products = engine.discover(2)

    assert [item.source_name for item in products] == ["Maxwell Bed", "Graham Bed"]
    assert products[0].canonical_url == "https://example.test/bedroom/all-beds/maxwell-custom-upholstered-bed-storage-option"
    assert products[0].image_url.endswith("maxwell.jpg")
    assert products[0].acquisition == "MAGENTO_GRAPHQL"
    assert products[0].evidence["graphql_category_id"] == "390"
    assert products[0].evidence["media_binding_status"] == "COMPATIBLE"
    assert products[0].dimensions == {"width": 67.5, "depth": 88.5, "height": 55.0}
    assert any("urlResolver" in str(payload.get("query")) for payload in client.posts)
    assert any("ProductsByCategory" in str(payload.get("query")) for payload in client.posts)
