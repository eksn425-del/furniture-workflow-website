from __future__ import annotations

import json
from pathlib import Path

from app.services.brain_provider import BrainSettings, WebsiteBrainProvider
from app.services.native_contracts import TaxonomyCategoryContract
from app.services.native_site_analysis import NativeSiteAnalyzer
from app.services.product_acquisition import NativeBrowserCollector
from workers.scrape.http_client import AccessControlDetected, HttpStatusError, SafeHttpClient


class FakeSiteClient:
    def __init__(self, **_: object) -> None:
        self.urls: list[str] = []

    def get_html(self, url: str) -> str:
        self.urls.append(url)
        return CATEGORY_HTML

    def robots_sitemaps(self, _: str) -> list[str]:
        return []

    def get_sitemap(self, _: str) -> str:
        return "<urlset />"

    def telemetry(self) -> dict[str, object]:
        return {"requests": len(self.urls)}


CATEGORY_HTML = f"""
<html>
  <head><title>Modern Bathroom Vanities - Room &amp; Board</title></head>
  <body>
    <script type="application/ld+json">{json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Product", "sku": "28400", "name": "Berkeley Bathroom Vanities"},
            {"@type": "Product", "sku": "29888", "name": "Emerson Bathroom Vanities"},
            {"@type": "Product", "sku": "28400", "name": "Berkeley Bathroom Vanities"},
        ],
    })}</script>
  </body>
</html>
"""


def test_direct_category_scope_is_retained_and_product_sample_is_estimated(tmp_path: Path) -> None:
    analyzer = NativeSiteAnalyzer(tmp_path, client_factory=FakeSiteClient)

    receipt = analyzer.analyze(
        "https://www.roomandboard.com/catalog/bath/vanities",
        live=True,
        output_dir=tmp_path / "receipt",
    )

    category = next(item for item in receipt["categories"] if item["path"] == "/bath/vanities")
    assert category["canonical_name"] == "Vanities"
    assert category["count_value"] == 2
    assert category["count_kind"] == "ESTIMATED"
    assert any(item["role"] == "bounded_product_jsonld_sample" for item in category["evidence"])


def test_site_root_does_not_turn_missing_count_into_zero(tmp_path: Path) -> None:
    html = '<a href="/catalog/bath">Bath</a>'

    class RootClient(FakeSiteClient):
        def get_html(self, url: str) -> str:
            self.urls.append(url)
            return html

    analyzer = NativeSiteAnalyzer(tmp_path, client_factory=RootClient)
    receipt = analyzer.analyze("https://example.test/", live=True, output_dir=tmp_path / "root")

    category = next(item for item in receipt["categories"] if item["path"] == "/bath")
    assert category["count_value"] is None
    assert category["count_kind"] == "UNKNOWN"


def test_coarsen_does_not_sum_known_children_over_unknown_member() -> None:
    categories = [
        TaxonomyCategoryContract(
            category_id="chairs-a",
            native_name="Dining Chairs",
            canonical_name="Dining Chairs",
            path="/living/dining-chairs",
            source_url="https://example.test/living/dining-chairs",
            count_value=4,
            count_kind="EXACT",
            level=2,
            parent_path="/living",
        ),
        TaxonomyCategoryContract(
            category_id="chairs-b",
            native_name="Lounge Chairs",
            canonical_name="Lounge Chairs",
            path="/living/lounge-chairs",
            source_url="https://example.test/living/lounge-chairs",
            count_value=None,
            count_kind="UNKNOWN",
            level=2,
            parent_path="/living",
        ),
    ]

    merged = NativeSiteAnalyzer._coarsen(categories)

    assert len(merged) == 1
    assert merged[0].canonical_name == "Chairs"
    assert merged[0].count_value is None
    assert merged[0].count_kind == "UNKNOWN"


def test_retryable_server_status_escalates_to_browser_path(tmp_path: Path) -> None:
    class EdgeBlockedClient(FakeSiteClient):
        def get_html(self, _: str) -> str:
            raise HttpStatusError(522, retryable=True)

    analyzer = NativeSiteAnalyzer(tmp_path, client_factory=EdgeBlockedClient)
    receipt = analyzer.analyze("https://example.test/", live=True, output_dir=tmp_path / "blocked")

    assert receipt["status"] == "BROWSER_REQUIRED"
    assert receipt["blocker"]["code"] == "HTTP_522"


def test_preflight_retryable_server_status_keeps_l2_path_open(tmp_path: Path) -> None:
    class EdgeBlockedClient(FakeSiteClient):
        def get_html(self, _: str) -> str:
            raise HttpStatusError(522, retryable=True)

    analyzer = NativeSiteAnalyzer(tmp_path, client_factory=EdgeBlockedClient)
    result = analyzer.preflight("https://example.test/", live=True)

    assert result["status"] == "BROWSER_REQUIRED"
    assert result["blocker"]["code"] == "HTTP_522"
    assert "可见浏览器" in result["next_action"]


def test_script_only_captcha_words_are_not_access_challenge() -> None:
    html = "<html><body><nav>Shop Sofas</nav><script>const captchaLabel='verify you are human';</script></body></html>"
    assert SafeHttpClient._challenge_kind(html) == ""


def test_explicit_visible_challenge_requires_both_text_and_control() -> None:
    class Locator:
        def __init__(self, *, text: str = "", count: int = 0) -> None:
            self.text = text
            self.value = count

        def inner_text(self, **_: object) -> str:
            return self.text

        def count(self) -> int:
            return self.value

        def nth(self, _: int) -> "Locator":
            return self

        def is_visible(self, **_: object) -> bool:
            return self.value > 0

    class Page:
        def title(self) -> str:
            return "Verify you are human"

        def locator(self, selector: str) -> Locator:
            return Locator(text="Verify you are human to continue", count=0) if selector == "body" else Locator(count=1)

    evidence = NativeBrowserCollector._visible_access_evidence(Page())
    assert evidence["visible_challenge_text"] is True
    assert evidence["explicit_challenge_control"] is True
    assert evidence["temporary_failure"] is False


def test_hidden_challenge_widget_does_not_pause_visible_browser() -> None:
    class Locator:
        def __init__(self, *, body: bool = False) -> None:
            self.body = body
        def inner_text(self, **_: object) -> str:
            return "Protected by reCAPTCHA" if self.body else ""
        def count(self) -> int:
            return 1
        def nth(self, _: int) -> "Locator":
            return self
        def is_visible(self, **_: object) -> bool:
            return False
    class Page:
        def title(self) -> str:
            return "Shop custom furniture"
        def locator(self, selector: str) -> Locator:
            return Locator(body=selector == "body")
    evidence = NativeBrowserCollector._visible_access_evidence(Page())
    assert evidence["explicit_challenge_control"] is False


def test_local_agent_access_brain_distinguishes_http_from_visible_challenge() -> None:
    brain = WebsiteBrainProvider(BrainSettings(model_mode="LOCAL_AGENT"))
    http, _ = brain.reason_access(source_url="https://example.test", evidence={"stage": "L1", "reason_code": "HTTP_403"})
    visible, _ = brain.reason_access(source_url="https://example.test", evidence={
        "stage": "L2", "reason_code": "ACCESS_CHALLENGE",
        "visible_challenge_text": True, "explicit_challenge_control": True,
    })
    assert http.access_state == "ESCALATE_L2"
    assert visible.access_state == "HUMAN_REQUIRED"


def test_l1_access_marker_escalates_instead_of_claiming_human(tmp_path: Path) -> None:
    class AccessClient(FakeSiteClient):
        def get_html(self, _: str) -> str:
            raise AccessControlDetected("captcha")

    analyzer = NativeSiteAnalyzer(tmp_path, client_factory=AccessClient)
    receipt = analyzer.analyze("https://example.test/", live=True, output_dir=tmp_path / "access")
    assert receipt["status"] == "BROWSER_REQUIRED"
    assert receipt["blocker"]["code"] == "CAPTCHA"


def test_magento_pwa_uses_authoritative_graphql_category_totals(tmp_path: Path) -> None:
    shell = '<html data-media-backend="https://example.test/media/"><body><div id="root"></div><script src="/client.abcdef123.js"></script></body></html>'

    class MagentoClient(FakeSiteClient):
        def get_html(self, url: str) -> str:
            self.urls.append(url)
            return shell

        def robots_sitemaps(self, _: str) -> list[str]:
            return ["https://example.test/sitemap.xml"]

        def get_sitemap(self, _: str) -> str:
            return "<urlset><url><loc>https://example.test/bedroom</loc></url><url><loc>https://example.test/bedroom/all-beds</loc></url><url><loc>https://example.test/caitlin-by-the-everygirl-leather-custom-sectional-sofa-with-right-chaise</loc></url><url><loc>https://example.test/data-request</loc></url></urlset>"

        def post_json(self, _: str, payload: dict) -> dict:
            query = str(payload.get("query") or "")
            variables = payload.get("variables") if isinstance(payload.get("variables"), dict) else {}
            if "TaxonomyRoot" in query:
                return {"data": {"storeConfig": {"root_category_id": 2}}}
            if "RootCategories" in query:
                return {"data": {"categoryList": [{"id": 10, "name": "Bedroom", "url_path": "bedroom", "level": 2, "children_count": "1"}]}}
            if "ChildCategories" in query:
                return {"data": {"c0": [{"id": 390, "name": "Beds", "url_path": "bedroom/all-beds", "level": 3, "children_count": "0"}]}}
            if "ResolveCategoryUrls" in query:
                data = {}
                for key, value in variables.items():
                    index = key.removeprefix("u")
                    if value == "bedroom":
                        data[f"c{index}"] = {"id": 10, "type": "CATEGORY", "relative_url": value}
                    elif value == "bedroom/all-beds":
                        data[f"c{index}"] = {"id": 390, "type": "CATEGORY", "relative_url": value}
                    else:
                        data[f"c{index}"] = {"id": 999, "type": "PRODUCT", "relative_url": value}
                return {"data": data}
            return {"data": {f"c{key.removeprefix('i')}": {"total_count": 42 if value == "10" else 17} for key, value in variables.items()}}

    analyzer = NativeSiteAnalyzer(tmp_path, client_factory=MagentoClient)
    receipt = analyzer.analyze("https://example.test/", live=True, output_dir=tmp_path / "magento")
    by_path = {item["path"]: item for item in receipt["categories"]}
    assert receipt["evidence"]["l0"]["platform"] == "MAGENTO_PWA"
    assert by_path["/bedroom"]["count_value"] == 42
    assert by_path["/bedroom"]["count_kind"] == "EXACT"
    assert by_path["/bedroom/all-beds"]["count_value"] == 17
    assert any(item["role"] == "magento_graphql_total_count" for item in by_path["/bedroom/all-beds"]["evidence"])
    assert "/data-request" not in by_path
    assert not any("caitlin-by-the-everygirl" in path for path in by_path)
