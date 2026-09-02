from __future__ import annotations

from pathlib import Path

import pytest

from app.services.product_acquisition import ProductAcquisitionEngine, ProductSupplyExhausted, _parse_dimension_text
from workers.scrape.http_client import HttpStatusError, RobotsDenied


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


def test_parse_circular_product_diameter_and_height_dimensions() -> None:
    assert _parse_dimension_text('Overall dimensions: 26" Diam. x 63.25" H') == (
        {"width": 26.0, "depth": 26.0, "height": 63.25},
        "in",
    )
    assert _parse_dimension_text('Overall dimensions: 65.75"h. x 30"diam.') == (
        {"width": 30.0, "depth": 30.0, "height": 65.75},
        "in",
    )


def test_parse_width_height_depth_dimensions() -> None:
    assert _parse_dimension_text('Dining chair dimensions: 27.5" W x 31.5" H x 25" D') == (
        {"width": 27.5, "depth": 25.0, "height": 31.5},
        "in",
    )


def test_collection_open_graph_metadata_is_not_promoted_to_product_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        def __init__(self, **_: object) -> None:
            pass

        def get_html(self, _: str) -> str:
            return (
                '<html><head><title>Living Storage</title>'
                '<meta property="og:image" content="/cdn/collections/living-storage.jpg">'
                '</head><body><h1>Living Storage</h1></body></html>'
            )

    monkeypatch.setattr(
        "app.services.product_acquisition.NativeBrowserCollector.get_html",
        lambda _collector, _url: '<html><head><title>Living Storage</title><meta property="og:image" content="/cdn/collections/living-storage.jpg"></head><body><h1>Living Storage</h1></body></html>',
    )

    engine = ProductAcquisitionEngine(
        source_url="https://shop.example/collections/living_storage",
        site_key="shop.example",
        source_type="DIRECT_BRAND",
        categories=[{
            "category_id": "living-storage",
            "canonical_name": "Living Storage",
            "source_url": "https://shop.example/collections/living_storage",
            "selected": True,
        }],
        workspace=tmp_path,
        browser_session_dir=tmp_path / "browser",
        client_factory=lambda **_: Client(),
    )

    with pytest.raises(ProductSupplyExhausted):
        engine.discover(1)


def test_dynamic_category_shell_escalates_to_l2_for_product_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    category_html = '<html><head><title>Living Storage</title></head><body><div id="app"></div></body></html>'
    dynamic_html = '<html><body><a href="/products/chair-one">Chair One</a></body></html>'
    detail_html = """
    <h1>Chair One</h1>
    <script type="application/ld+json">
    {"@type":"Product","name":"Chair One","sku":"CHAIR-ONE","url":"/products/chair-one","image":"/media/chair-one.png"}
    </script><p>24 W x 26 D x 31 H in</p>
    """

    class Client:
        def __init__(self, **_: object) -> None:
            self.urls: list[str] = []

        def get_html(self, url: str) -> str:
            self.urls.append(url)
            return detail_html if "/products/" in url else category_html

    client = Client()
    monkeypatch.setattr(
        "app.services.product_acquisition.NativeBrowserCollector.get_html",
        lambda _collector, _url: dynamic_html,
    )
    engine = ProductAcquisitionEngine(
        source_url="https://shop.example/collections/living_storage",
        site_key="shop.example",
        source_type="DIRECT_BRAND",
        categories=[{
            "category_id": "living-storage",
            "canonical_name": "Living Storage",
            "source_url": "https://shop.example/collections/living_storage",
            "selected": True,
        }],
        workspace=tmp_path,
        browser_session_dir=tmp_path / "browser",
        client_factory=lambda **_: client,
    )

    products = engine.discover(1)

    assert len(products) == 1
    assert products[0].source_name == "Chair One"
    assert products[0].canonical_url == "https://shop.example/products/chair-one"


def test_method_or_edge_access_status_escalates_selected_scope_to_l2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class BlockedClient:
        def __init__(self, **_: object) -> None:
            pass

        def get_html(self, _: str) -> str:
            raise HttpStatusError(405, retryable=False)

    monkeypatch.setattr(
        "app.services.product_acquisition.NativeBrowserCollector.get_html",
        lambda _collector, _url: "<html><body><a href='/products/chair-one'>Chair One</a></body></html>",
    )
    engine = ProductAcquisitionEngine(
        source_url="https://shop.example/collections/chairs",
        site_key="shop.example",
        source_type="DIRECT_BRAND",
        categories=[{
            "category_id": "chairs",
            "canonical_name": "Chairs",
            "source_url": "https://shop.example/collections/chairs",
            "selected": True,
        }],
        workspace=tmp_path,
        browser_session_dir=tmp_path / "browser",
        client_factory=lambda **_: BlockedClient(),
    )

    html, acquisition = engine._get_html("https://shop.example/collections/chairs")

    assert "chair-one" in html
    assert acquisition == "L2_BROWSER"


def test_generic_html_cursor_advances_to_next_page_and_checkpoints_exhaustion(tmp_path: Path) -> None:
    detail_template = """
    <h1>{name}</h1>
    <script type="application/ld+json">
    {{"@type":"Product","name":"{name}","sku":"{sku}","url":"/products/{slug}","image":"/media/{slug}.png"}}
    </script><p>24 W x 26 D x 31 H in</p>
    """

    class Client:
        def __init__(self, **_: object) -> None:
            self.urls: list[str] = []

        def get_html(self, url: str) -> str:
            self.urls.append(url)
            if "/products/" in url:
                slug = url.rstrip("/").rsplit("/", 1)[-1]
                return detail_template.format(name=slug.title(), sku=slug.upper(), slug=slug)
            if "page=2" in url:
                return '<nav class="pagination"><a href="/collections/chairs?page=1">1</a><span aria-current="page">2</span><a href="/products/chair-two">Chair Two</a></nav>'
            return '<nav class="pagination"><span aria-current="page">1</span><a rel="next" href="/collections/chairs?page=2">Next</a><a href="/products/chair-one">Chair One</a></nav>'

    client = Client()
    engine = ProductAcquisitionEngine(
        source_url="https://shop.example/collections/chairs",
        site_key="shop.example",
        source_type="DIRECT_BRAND",
        categories=[{
            "category_id": "cat-chairs",
            "canonical_name": "Chairs",
            "source_url": "https://shop.example/collections/chairs",
            "selected": True,
        }],
        workspace=tmp_path,
        browser_session_dir=tmp_path / "browser",
        client_factory=lambda **_: client,
    )

    products = engine.discover(2)

    assert [item.source_name for item in products] == ["Chair-One", "Chair-Two"]
    checkpoint = engine._read()
    cursor = next(iter(checkpoint["scope_cursors"].values()))
    assert cursor["visited"] is True
    assert cursor["exhausted"] is True
    assert cursor["pages_fetched"] == 2
    assert any("page=2" in url for url in client.urls)
    assert "https://shop.example/collections/chairs" in checkpoint["visited_scopes"]
    assert "https://shop.example/collections/chairs" in checkpoint["exhausted_scopes"]


def test_robots_denial_never_upgrades_to_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        def __init__(self, **_: object) -> None:
            pass

        def get_html(self, _: str) -> str:
            raise RobotsDenied("robots.txt disallows URL")

    def fail_browser(*_: object, **__: object) -> str:
        raise AssertionError("robots denial must not invoke L2 browser")

    monkeypatch.setattr("app.services.product_acquisition.NativeBrowserCollector.get_html", fail_browser)
    engine = ProductAcquisitionEngine(
        source_url="https://shop.example/collections/chairs",
        site_key="shop.example",
        source_type="DIRECT_BRAND",
        categories=[{"category_id": "chairs", "canonical_name": "Chairs", "source_url": "https://shop.example/collections/chairs"}],
        workspace=tmp_path,
        browser_session_dir=tmp_path / "browser",
        client_factory=lambda **_: Client(),
    )

    with pytest.raises(RobotsDenied):
        engine.discover(1)


def test_products_without_machine_cursor_remain_resumable_not_exhausted() -> None:
    html = '<nav class="pagination"><span aria-current="page">1</span><a href="/products/chair-one">Chair One</a><button aria-label="Next page">Next</button></nav>'

    next_url, explicit_end = ProductAcquisitionEngine._pagination_cursor(
        "https://shop.example/collections/chairs",
        "https://shop.example/collections/chairs",
        html,
    )

    assert next_url is None
    assert explicit_end is False


def test_disabled_next_control_is_an_explicit_end() -> None:
    html = '<nav class="pagination"><span aria-current="page">2</span><a href="?page=1">1</a><button aria-label="Next page" disabled>Next</button></nav>'

    next_url, explicit_end = ProductAcquisitionEngine._pagination_cursor(
        "https://shop.example/collections/chairs",
        "https://shop.example/collections/chairs?page=2",
        html,
    )

    assert next_url is None
    assert explicit_end is True
