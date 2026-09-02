"""Website-owned product discovery with durable scope and browser handoff.

The collector never broad-crawls a marketplace.  It consumes only the selected
category/scope URLs from the persisted Job contract, keeps a durable cursor,
and escalates public-page challenges to one scoped headed-browser session.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from workers.scrape.http_client import (
    AccessControlDetected,
    HttpStatusError,
    NetworkPolicyError,
    RequestBudgetExceeded,
    RobotsDenied,
    SafeHttpClient,
)
from packages.workflow_core.source_identity import (
    media_asset_identity,
    media_binding_status,
    normalize_identity_fields,
    scope_status,
)
from packages.workflow_core.source_policy import resolve_source_policy


SOURCE_TYPES = {
    "DIRECT_BRAND",
    "MULTI_BRAND_RETAILER",
    "MULTI_CATEGORY_RETAILER",
    "MARKETPLACE",
    "SCOPED_CATEGORY",
    "SEARCH_RESULT",
    "UNKNOWN",
}
PRODUCT_PATH = re.compile(r"/(?:products?|product-page|p|item|sku|listing)/[^/?#]+", re.I)
VISIBLE_CHALLENGE_TEXT = re.compile(r"captcha|verify you are human|checking your browser|security challenge|人机验证|访问验证", re.I)
TEMPORARY_FAILURE_TEXT = re.compile(r"technical difficulties|try again later|temporarily unavailable|service unavailable|暂时无法|稍后再试", re.I)
ACCESS_DENIED_TEXT = re.compile(r"access denied|request blocked|restricted access|permission denied|forbidden|访问被拒绝|请求被阻止", re.I)
DIMENSION_RE = re.compile(
    r"(?P<w>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*[w宽]\s*[x×*]\s*"
    r"(?P<d>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*[d深]\s*[x×*]\s*"
    r"(?P<h>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*[h高]",
    re.I,
)
# 官网常见紧凑格式："33"w 35"d 30"h" 或 "33\"w 35\"d 30\"h"
DIMENSION_SPACED_RE = re.compile(
    r"(?P<w>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*[w宽]\s*"
    r"(?P<d>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*[d深]\s*"
    r"(?P<h>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*[h高]",
    re.I,
)
# Some product pages use the equally common W × H × D order.
WIDTH_HEIGHT_DEPTH_RE = re.compile(
    r"(?:overall|整体|total)?[^0-9]{0,40}"
    r"(?P<w>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*[w宽]\s*[x×*]\s*"
    r"(?P<h>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*[h高]\s*[x×*]\s*"
    r"(?P<d>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*[d深]",
    re.I,
)
# 展开后的"Dimensions"标签内容常以 Overall 开头，例如 "Overall: 33"w 35"d 30"h"
OVERALL_DIMENSION_RE = re.compile(
    r"(?:overall|整体|total)[^0-9]{0,40}"
    r"(?P<w>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*[w宽]\s*"
    r"(?P<d>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*[d深]\s*"
    r"(?P<h>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*[h高]",
    re.I,
)
# 圆形灯具等官网常用“直径 × 高度”表达；直径同时作为宽、深，
# 只有在明确出现 Diam/Diameter 与 H 时才接受，避免把普通二维尺寸误判为 3D。
DIAMETER_HEIGHT_RE = re.compile(
    r"(?:overall|整体|total)?[^0-9]{0,40}"
    r"(?P<diam>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*(?:diam(?:eter)?|dia)\.?\s*[x×*]\s*"
    r"(?P<h>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*h\b",
    re.I,
)
HEIGHT_DIAMETER_RE = re.compile(
    r"(?:overall|整体|total)?[^0-9]{0,40}"
    r"(?P<h>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*h\.?\s*[x×*]\s*"
    r"(?P<diam>\d+(?:\.\d+)?)\s*(?:in|inch|\"|cm|mm)?\s*(?:diam(?:eter)?|dia)\.?\b",
    re.I,
)
DIMENSION_TAB_LABELS = ("dimensions", "尺寸", "specifications", "规格")
DIMENSION_UNIT_RE = re.compile(r"(in|inch|cm|mm)", re.I)
MAGENTO_PWA_CLIENT_RE = re.compile(r"(?:^|[\"'])/client\.[^\"']+\.js", re.I)
MAX_ACQUISITION_PAGES = 100


class ProductAcquisitionError(RuntimeError):
    code = "ACQUISITION_FAILED"


class ProductSupplyExhausted(ProductAcquisitionError):
    code = "SUPPLY_EXHAUSTED"


class BrowserRuntimeMissing(ProductAcquisitionError):
    code = "BROWSER_RUNTIME_NOT_INSTALLED"


class BrowserHumanRequired(ProductAcquisitionError):
    code = "HUMAN_REQUIRED"

    def __init__(self, message: str, *, url: str, session_dir: Path, reason_code: str, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.session_dir = Path(session_dir)
        self.reason_code = reason_code
        self.evidence = evidence or {}


class BrowserTemporaryFailure(ProductAcquisitionError):
    code = "TEMPORARY_FAILURE"

    def __init__(self, message: str, *, url: str, session_dir: Path, reason_code: str = "BROWSER_NAVIGATION_FAILED", evidence: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.session_dir = Path(session_dir)
        self.reason_code = reason_code
        self.evidence = evidence or {}


class BrowserAccessDenied(ProductAcquisitionError):
    code = "ACCESS_CHANGE_REQUIRED"

    def __init__(self, message: str, *, url: str, session_dir: Path, reason_code: str = "ACCESS_DENIED", evidence: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.session_dir = Path(session_dir)
        self.reason_code = reason_code
        self.evidence = evidence or {}


@dataclass(slots=True)
class AcquiredProduct:
    source_product_id: str
    canonical_url: str
    source_name: str
    source_brand: str
    category_id: str
    category_group: str
    image_url: str
    dimensions: dict[str, float]
    dimension_unit: str
    source_type: str
    capture_sha256: str
    acquisition: str
    evidence: dict[str, Any]
    identity_fields: dict[str, Any] = field(default_factory=dict)
    media_binding_status: str = "UNKNOWN"
    media_binding_confidence: float = 0.0
    scope_status: str = "UNKNOWN"

    @property
    def identity_key(self) -> str:
        return "|".join((self.source_product_id.casefold(), self.canonical_url.casefold()))


def classify_source_type(source_url: str, page_html: str = "") -> str:
    """Conservative source-type classification; UNKNOWN is preferable to invention."""

    parsed = urlsplit(source_url)
    path = parsed.path.casefold()
    query = parsed.query.casefold()
    text = page_html[:300_000].casefold()
    if "/search" in path or "search=" in query or "query=" in query:
        return "SEARCH_RESULT"
    if any(token in text for token in ("marketplace", "seller profile", "sold by", "multiple sellers")):
        return "MARKETPLACE"
    try:
        configured_kind = resolve_source_policy(source_url).source_kind
    except (OSError, RuntimeError, ValueError):
        configured_kind = ""
    if configured_kind and configured_kind != "UNKNOWN":
        return configured_kind
    if any(token in text for token in ("shop by brand", "brands we carry", "all brands")):
        return "MULTI_BRAND_RETAILER"
    if PRODUCT_PATH.search(path) or any(token in path for token in ("/category", "/collections/", "/catalog/")):
        return "SCOPED_CATEGORY"
    organization_names = {
        str(value).strip().casefold()
        for value in re.findall(r'"(?:brand|manufacturer|name)"\s*:\s*"([^"\\]{2,100})"', text)
    }
    host_label = (parsed.hostname or "").split(".")[0].replace("-", " ")
    if host_label and len(organization_names) <= 2 and host_label in " ".join(organization_names):
        return "DIRECT_BRAND"
    return "UNKNOWN"


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def _json_ld(page_html: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for raw in re.findall(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", page_html, re.I | re.S):
        try:
            payload = json.loads(html_lib.unescape(raw.strip()))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        queue = payload if isinstance(payload, list) else [payload]
        for item in queue:
            if not isinstance(item, dict):
                continue
            values.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                values.extend(child for child in graph if isinstance(child, dict))
    return values


def _first_text(page_html: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, page_html, re.I | re.S)
        if match:
            return html_lib.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
    return ""


def _is_magento_pwa_shell(page_html: str) -> bool:
    """Recognise the Magento PWA shell without treating any generic SPA as Magento."""

    lowered = page_html.casefold()
    has_root = 'id="root"' in lowered or "id='root'" in lowered
    return has_root and "data-media-backend" in lowered and bool(MAGENTO_PWA_CLIENT_RE.search(page_html))


def _magento_product_image(item: dict[str, Any]) -> str:
    for field in ("image", "small_image", "thumbnail"):
        value = item.get(field)
        if isinstance(value, dict) and value.get("url"):
            return str(value["url"]).strip()
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _magento_product_description(item: dict[str, Any]) -> str:
    values: list[str] = []
    for field in ("description", "short_description"):
        value = item.get(field)
        if isinstance(value, dict):
            value = value.get("html") or value.get("value") or ""
        if value:
            values.append(html_lib.unescape(re.sub(r"<[^>]+>", " ", str(value))))
    return " ".join(values)


def _image_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _image_value(next((item for item in value if item), ""))
    if isinstance(value, dict):
        return str(value.get("url") or value.get("contentUrl") or "")
    return ""


def _brand_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    return ""


def _page_item_number(page_html: str) -> str:
    visible = html_lib.unescape(re.sub(r"<[^>]+>", " ", page_html))
    match = re.search(
        r"\b(?:item\s*(?:number|no\.?|#)|itemnumber|product\s*(?:number|no\.?|#))\s*[:#-]?\s*([A-Za-z0-9_-]{2,64})",
        visible,
        re.I,
    )
    return match.group(1).strip() if match else ""


def _product_configuration(product: dict[str, Any]) -> tuple[str, str, bool]:
    """Read explicit configuration/variant evidence without merging IDs."""

    configuration = product.get("configuration_key") or product.get("configuration_id") or product.get("configurationId")
    variant = product.get("variant_key") or product.get("variant_id") or product.get("variantId")
    if isinstance(product.get("isVariantOf"), dict):
        parent = product["isVariantOf"]
        configuration = configuration or parent.get("@id") or parent.get("name")
    if isinstance(product.get("additionalProperty"), list):
        for item in product["additionalProperty"]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").casefold()
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            if any(token in name for token in ("configuration", "config", "variant", "finish", "color", "size")):
                configuration = configuration or value
                variant = variant or value if "variant" in name else variant
    configuration_key = " ".join(str(configuration or "").split())
    variant_key = " ".join(str(variant or "").split())
    return configuration_key, variant_key, bool(configuration_key or variant_key)


def _select_product_json(page_html: str, url: str, products: list[dict[str, Any]]) -> tuple[dict[str, Any], bool, dict[str, str]]:
    """Select the Product node bound to this page instead of taking the first node."""

    if not products:
        return {}, False, {}
    h1 = _first_text(page_html, (r"<h1[^>]*>(.*?)</h1>",))
    page_tail = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].casefold()
    page_item = _page_item_number(page_html)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, product in enumerate(products):
        product_url = str(product.get("url") or "").strip()
        product_tail = urlsplit(product_url).path.rstrip("/").rsplit("/", 1)[-1].casefold() if product_url else ""
        sku = str(product.get("sku") or product.get("productID") or product.get("mpn") or "").strip()
        name = " ".join(str(product.get("name") or "").split())
        score = 0
        if product_url and _canonical_url(urljoin(url, product_url)) == _canonical_url(url):
            score += 10
        if page_tail and product_tail and page_tail == product_tail:
            score += 7
        if page_item and sku and page_item.casefold() == sku.casefold():
            score += 7
        if h1 and name and h1.casefold() == name.casefold():
            score += 6
        elif h1 and name and (h1.casefold() in name.casefold() or name.casefold() in h1.casefold()):
            score += 3
        scored.append((score, -index, product))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected_score, _, selected = scored[0]
    ambiguous = len(scored) > 1 and selected_score == scored[1][0]
    identity_match = bool(selected_score >= 6 or len(products) == 1)
    return selected, identity_match, {
        "page_item_number": page_item,
        "page_h1": h1,
        "selection_score": str(selected_score),
        "jsonld_product_count": str(len(products)),
        "jsonld_product_ambiguous": "true" if ambiguous else "false",
    }


def _l2_browser_channel() -> str | None:
    """由环境变量决定使用哪个真实浏览器内核驱动的 L2 可见会话。

    - 留空/“chromium”：默认打包的 Chromium（隔离、无个人登录态）。
    - “msedge”：调用使用者的真实 Microsoft Edge（独立 profile，验证一次后持久）,
      更贴近普通用户指纹，对严格反爬站点更友好；需要本机装有 Edge。
    - “chrome”：调用真实 Chrome（同样独立 profile）。
    """
    engine = str(os.getenv("WEBSITE_L2_BROWSER_ENGINE", "") or "").strip().casefold()
    if engine in {"msedge", "edge"}:
        return "msedge"
    if engine in {"chrome", "google-chrome"}:
        return "chrome"
    return None


class _VisibleHandoffRequired(Exception):
    """无头模式下检测到需要人工处理的访问验证，要求上层改用同一会话的可见窗口重开。"""

    def __init__(self, *, url: str, session_dir, evidence: dict[str, Any]):
        super().__init__("需要切换到可见浏览器处理访问验证")
        self.url = url
        self.session_dir = session_dir
        self.evidence = evidence



class NativeBrowserCollector:
    """Visible persistent Playwright context; no stealth, proxy rotation, or bypass.

    engine 由 WEBSITE_L2_BROWSER_ENGINE 决定：默认打包 Chromium；设 msedge/chrome
    时改用使用者真实浏览器（独立持久 profile，登录/验证状态随会话目录保留）。
    """

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = Path(session_dir).resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def _open_page(self, playwright, url: str, *, visible: bool = False):
        """启动持久浏览器会话并导航；返回 (context, page)。

        visible=False 时在后台无头运行（不打扰）；只有遇到需要人工处理的
        访问验证时，上层才用 visible=True 重开同一会话的可见窗口。
        """
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        channel = _l2_browser_channel()
        launch_kwargs = {
            "headless": not visible,
            "viewport": {"width": 1440, "height": 1000},
        }
        if channel:
            # 真实浏览器内核（Edge/Chrome），persistent context 用专属会话目录，
            # 不会占用使用者在用的个人 profile，也不会篡改其登录态。
            launch_kwargs["channel"] = channel
        context = playwright.chromium.launch_persistent_context(
            str(self.session_dir),
            **launch_kwargs,
        )
        page = context.pages[0] if context.pages else context.new_page()
        navigation_timeout = max(8_000, min(45_000, int(os.getenv("WEBSITE_L2_NAVIGATION_TIMEOUT_MS", "15000"))))
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout)
        except PlaywrightError:
            # Some commerce sites abort the original navigation while replacing
            # it with a canonical www/client-side route. If a meaningful public
            # page is already visible, continue from that same session.
            page.wait_for_timeout(1200)
            try:
                visible = page.locator("body").inner_text(timeout=1500)
            except Exception:
                visible = ""
            if not str(page.url).startswith(("http://", "https://")) or len(visible.strip()) < 200:
                context.close()
                raise
        page.wait_for_timeout(800)
        return context, page

    def _resolve_challenge(self, page, url: str, *, headless: bool) -> str | None:
        """若页面进入人机验证，等待人工接管；返回放行后的 HTML 或 None（无需放行）。

        无头模式下检测到验证控件时抛出 _VisibleHandoffRequired，让上层改用
        同一持久会话的可见窗口重开后再等待人工处理。
        """
        evidence = self._visible_access_evidence(page)
        if evidence["temporary_failure"]:
            # 临时技术故障：自动重载重试（有界），减少无谓的人工介入；仅持续失败才需人工。
            from playwright.sync_api import Error as PlaywrightError
            navigation_timeout = max(8_000, min(45_000, int(os.getenv("WEBSITE_L2_NAVIGATION_TIMEOUT_MS", "15000"))))
            retries = max(1, min(6, int(os.getenv("WEBSITE_L2_TEMP_FAILURE_RETRIES", "4"))))
            for _ in range(retries):
                page.wait_for_timeout(4000)
                try:
                    page.reload(wait_until="domcontentloaded", timeout=navigation_timeout)
                except PlaywrightError:
                    pass
                page.wait_for_timeout(1500)
                evidence = self._visible_access_evidence(page)
                if not evidence["temporary_failure"]:
                    break
            if evidence["temporary_failure"]:
                raise BrowserTemporaryFailure(
                    "页面显示临时技术故障；已自动重试仍失败，保留同一浏览器会话可稍后恢复重试",
                    url=url, session_dir=self.session_dir, reason_code="TEMPORARY_PAGE_FAILURE", evidence=evidence,
                )
        if evidence["access_denied"] and not evidence["explicit_challenge_control"]:
            raise BrowserAccessDenied(
                "可见浏览器页面拒绝访问，但没有可操作的人机验证控件；需要检查网络、登录或站点授权条件",
                url=url,
                session_dir=self.session_dir,
                reason_code="ACCESS_DENIED",
                evidence=evidence,
            )
        if not evidence["explicit_challenge_control"] or not evidence["visible_challenge_text"]:
            return None
        if headless:
            raise _VisibleHandoffRequired(url=url, session_dir=self.session_dir, evidence=evidence)
        handoff_seconds = max(30, min(900, int(os.getenv("WEBSITE_BROWSER_HANDOFF_SECONDS", "180"))))
        for _ in range(handoff_seconds):
            page.wait_for_timeout(1000)
            evidence = self._visible_access_evidence(page)
            if not evidence["explicit_challenge_control"] or not evidence["visible_challenge_text"]:
                return page.content()
        raise BrowserHumanRequired(
            "可见浏览器的人工接管窗口已结束；请完成允许的验证后点击恢复同一任务",
            url=url,
            session_dir=self.session_dir,
            reason_code="ACCESS_CHALLENGE",
            evidence=evidence,
        )

    def _open_resolved(self, playwright, url: str, *, headless: bool = True):
        """打开 url 的持久会话并处理访问验证，返回 (context, page, released_html)。

        无头模式下先不打扰地跑；检测到需要人工的验证时，自动关闭无头窗口，
        改用同一持久会话的可见窗口重开并等待人工处理，随后返回放行后的 HTML。
        released_html 为 None 表示无需放行，直接读取 page 内容即可。
        """
        context, page = self._open_page(playwright, url, visible=not headless)
        try:
            released = self._resolve_challenge(page, url, headless=headless)
            return context, page, released
        except _VisibleHandoffRequired:
            try:
                context.close()
            except Exception:
                pass
            context, page = self._open_page(playwright, url, visible=True)
            released = self._resolve_challenge(page, url, headless=False)
            return context, page, released

    @staticmethod
    def _visible_access_evidence(page) -> dict[str, Any]:
        try:
            title = page.title()[:500]
        except Exception:
            title = ""
        try:
            visible_text = page.locator("body").inner_text(timeout=2500)[:50_000]
        except Exception:
            visible_text = ""
        selectors = (
            "#px-captcha, .px-captcha, [data-sitekey], iframe[src*='captcha'], iframe[src*='challenge'], "
            "form[id*='challenge'], [class*='hcaptcha'], [class*='recaptcha']"
        )
        try:
            controls = page.locator(selectors)
            explicit_control = any(controls.nth(index).is_visible(timeout=500) for index in range(min(controls.count(), 8)))
        except Exception:
            explicit_control = False
        combined = f"{title}\n{visible_text}"
        return {
            "title": title,
            "visible_text_sample": visible_text[:2000],
            "visible_challenge_text": bool(VISIBLE_CHALLENGE_TEXT.search(combined)),
            "explicit_challenge_control": explicit_control,
            "temporary_failure": bool(TEMPORARY_FAILURE_TEXT.search(combined)),
            "access_denied": bool(ACCESS_DENIED_TEXT.search(combined)),
        }

    def get_html(self, url: str) -> str:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise BrowserRuntimeMissing(
                "Website 原生 L2 浏览器运行时未安装；请按项目文档安装 Playwright Chromium"
            ) from error
        try:
            with sync_playwright() as playwright:
                context, page, released = self._open_resolved(playwright, url, headless=True)
                if released is not None:
                    context.close()
                    return released
                body = page.content()
                context.close()
                return body
        except (BrowserHumanRequired, BrowserTemporaryFailure, BrowserAccessDenied):
            raise
        except PlaywrightError as error:
            message = str(error).casefold()
            if "executable doesn't exist" in message or "playwright install" in message:
                raise BrowserRuntimeMissing(
                    "Website 原生 L2 浏览器缺少 Chromium；请执行 python -m playwright install chromium"
                ) from error
            raise BrowserTemporaryFailure(
                f"浏览器导航暂时失败：{type(error).__name__}",
                url=url,
                session_dir=self.session_dir,
                reason_code="BROWSER_NAVIGATION_FAILED",
            ) from error

    def get_html_batch(self, urls: list[str]) -> dict[str, str]:
        """Read a bounded list of same-site pages in one visible session.

        Count enrichment must not launch one browser per category.  Reusing one
        persistent page keeps the session human-like and lets a single manual
        challenge resolution cover the remainder of the bounded probe.
        """

        if not urls:
            return {}
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise BrowserRuntimeMissing(
                "Website 原生 L2 浏览器运行时未安装；请按项目文档安装 Playwright Chromium"
            ) from error
        context = None
        try:
            with sync_playwright() as playwright:
                context, page, released = self._open_resolved(playwright, urls[0], headless=True)
                pages: dict[str, str] = {urls[0]: released if released is not None else page.content()}
                headless = True
                for url in urls[1:]:
                    navigation_timeout = max(8_000, min(45_000, int(os.getenv("WEBSITE_L2_NAVIGATION_TIMEOUT_MS", "15000"))))
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout)
                        page.wait_for_timeout(450)
                    except PlaywrightError:
                        # One slow category must not discard the taxonomy
                        # already collected from the entry page or block all
                        # remaining bounded probes.
                        continue
                    try:
                        released = self._resolve_challenge(page, url, headless=headless)
                    except _VisibleHandoffRequired:
                        # 后续页面遇到需要人工的验证：切换为同一会话的可见窗口继续。
                        try:
                            context.close()
                        except Exception:
                            pass
                        context, page, released = self._open_resolved(playwright, url, headless=False)
                        headless = False
                    pages[url] = released if released is not None else page.content()
                return pages
        except (BrowserHumanRequired, BrowserTemporaryFailure, BrowserAccessDenied):
            raise
        except PlaywrightError as error:
            message = str(error).casefold()
            if "executable doesn't exist" in message or "playwright install" in message:
                raise BrowserRuntimeMissing(
                    "Website 原生 L2 浏览器缺少 Chromium；请执行 python -m playwright install chromium"
                ) from error
            raise BrowserTemporaryFailure(
                f"浏览器批量导航暂时失败：{type(error).__name__}",
                url=urls[-1],
                session_dir=self.session_dir,
                reason_code="BROWSER_NAVIGATION_FAILED",
            ) from error
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    def extract_dimensions(self, url: str) -> tuple[dict[str, float], str]:
        """用 L2 可见浏览器打开商品详情页，展开 Dimensions 标签并解析官方尺寸。

        返回 (dimensions, unit)；解析失败时 dimensions 为空 dict、unit 为空串。
        主要面向把尺寸放在可折叠"Dimensions/规格"标签里的反爬站点（如 Room & Board）。
        """
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise BrowserRuntimeMissing(
                "Website 原生 L2 浏览器运行时未安装；请按项目文档安装 Playwright Chromium"
            ) from error
        try:
            with sync_playwright() as playwright:
                context, page, released = self._open_resolved(playwright, url, headless=True)
                if released is None:
                    # 优先在 DOM 中直接寻找 Dimensions 标签并点击展开
                    for label in DIMENSION_TAB_LABELS:
                        try:
                            locator = page.get_by_text(label, exact=False).first
                            if locator.count() == 0:
                                continue
                            locator.click(timeout=3500)
                            page.wait_for_timeout(800)
                            break
                        except Exception:
                            continue
                body = page.content()
                context.close()
                visible = re.sub(r"<[^>]+>", " ", body)
                visible = html_lib.unescape(visible)
                return _parse_dimension_text(visible)
        except (BrowserHumanRequired, BrowserTemporaryFailure, BrowserAccessDenied):
            raise
        except PlaywrightError as error:
            message = str(error).casefold()
            if "executable doesn't exist" in message or "playwright install" in message:
                raise BrowserRuntimeMissing(
                    "Website 原生 L2 浏览器缺少 Chromium；请执行 python -m playwright install chromium"
                ) from error
            raise BrowserTemporaryFailure(
                f"尺寸页面导航暂时失败：{type(error).__name__}",
                url=url,
                session_dir=self.session_dir,
                reason_code="BROWSER_NAVIGATION_FAILED",
            ) from error


def _parse_dimension_text(visible: str) -> tuple[dict[str, float], str]:
    """从页面可见文本解析 Overall/紧凑/间距式尺寸，返回 (dimensions, unit)。"""
    for pattern in (OVERALL_DIMENSION_RE, WIDTH_HEIGHT_DEPTH_RE, DIMENSION_SPACED_RE, DIMENSION_RE):
        match = pattern.search(visible)
        if match:
            dimensions = {
                "width": float(match.group("w")),
                "depth": float(match.group("d")),
                "height": float(match.group("h")),
            }
            unit_match = DIMENSION_UNIT_RE.search(match.group(0))
            unit = unit_match.group(0).lower() if unit_match else "source_unit"
            if unit == "source_unit" and "\"" in match.group(0):
                # 官网常见单位符号：33"w 35"d 30"h -> inch
                unit = "in"
            return dimensions, unit
    for pattern in (DIAMETER_HEIGHT_RE, HEIGHT_DIAMETER_RE):
        match = pattern.search(visible)
        if match:
            diameter = float(match.group("diam"))
            height = float(match.group("h"))
            dimensions = {"width": diameter, "depth": diameter, "height": height}
            unit_match = DIMENSION_UNIT_RE.search(match.group(0))
            unit = unit_match.group(0).lower() if unit_match else "source_unit"
            if unit == "source_unit" and "\"" in match.group(0):
                unit = "in"
            return dimensions, unit
    return {}, ""


class ProductAcquisitionEngine:
    """Bounded, checkpointed discovery for selected Website scopes."""

    def __init__(
        self,
        *,
        source_url: str,
        site_key: str,
        source_type: str,
        categories: list[dict[str, Any]],
        workspace: Path,
        browser_session_dir: Path,
        request_budget: int = 120,
        client_factory=SafeHttpClient,
    ) -> None:
        self.source_url = _canonical_url(source_url)
        self.site_key = site_key
        self.source_type = source_type if source_type in SOURCE_TYPES else "UNKNOWN"
        self.categories = self._compact_scopes(categories)
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.browser_session_dir = Path(browser_session_dir).resolve()
        self.checkpoint_path = self.workspace / "acquisition_checkpoint.json"
        self.capture_root = self.workspace / "01_acquisition" / "captures"
        self.capture_root.mkdir(parents=True, exist_ok=True)
        self.client = client_factory(
            source_url=self.source_url,
            request_budget=request_budget,
            timeout=30,
            request_delay=0.15,
        )

    @staticmethod
    def _compact_scopes(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected = [dict(item) for item in categories if item.get("selected", True)]
        # 子级收窄父级：父级被勾选且其下也有勾选的子级时，只保留子级范围，
        # 父级自身不作为范围（父级整棵子树退化为仅勾选的子级）。
        selected_parent_ids = {
            str(item.get("parent_category_id") or "") for item in selected
            if str(item.get("parent_category_id") or "")
        }
        compacted = [
            item for item in selected
            if not (str(item.get("category_id") or "") in selected_parent_ids)
        ]
        # A coarse UI row may represent several native child scopes.  Keep
        # those member URLs so selecting the coarse row does not silently
        # reduce production scope to whichever child happened to be chosen as
        # the representative.
        result: list[dict[str, Any]] = []
        by_url: dict[str, dict[str, Any]] = {}
        for item in compacted:
            url = str(item.get("source_url") or "").strip()
            if not url:
                continue
            evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
            member_urls: list[str] = []
            raw_scope_urls = item.get("scope_urls")
            if isinstance(raw_scope_urls, list):
                member_urls.extend(str(value).strip() for value in raw_scope_urls if str(value).strip())
            for entry in evidence:
                if not isinstance(entry, dict) or entry.get("role") != "coarse_scope_members":
                    continue
                values = entry.get("urls")
                if isinstance(values, list):
                    member_urls.extend(str(value).strip() for value in values if str(value).strip())
            all_urls: list[str] = []
            for value in [url, *member_urls]:
                if value and value not in all_urls:
                    all_urls.append(value)
            item["scope_urls"] = all_urls
            existing = by_url.get(url)
            if existing is None:
                by_url[url] = item
                result.append(item)
                continue
            merged = list(existing.get("scope_urls") or [])
            for value in all_urls:
                if value not in merged:
                    merged.append(value)
            existing["scope_urls"] = merged
        return result

    def _scope_entries(self) -> list[dict[str, Any]]:
        scopes = self.categories or [{
            "category_id": "scope_source",
            "canonical_name": "Source Scope",
            "source_url": self.source_url,
            "scope_urls": [self.source_url],
        }]
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, scope in enumerate(scopes):
            category_id = str(scope.get("category_id") or f"scope_{index}")
            raw_urls = scope.get("scope_urls") if isinstance(scope.get("scope_urls"), list) else []
            values = [str(scope.get("source_url") or "").strip(), *[str(value).strip() for value in raw_urls]]
            for value in values:
                if not value:
                    continue
                url = _canonical_url(value)
                key = f"{category_id}|{url}"
                if key in seen:
                    continue
                seen.add(key)
                entries.append({
                    "scope_key": key,
                    "category_id": category_id,
                    "canonical_name": str(scope.get("canonical_name") or scope.get("native_name") or "Source Scope"),
                    "native_name": str(scope.get("native_name") or ""),
                    "source_url": url,
                })
        return entries

    @staticmethod
    def _cursor_key(entry: dict[str, Any]) -> str:
        return str(entry.get("scope_key") or entry.get("source_url") or "scope_source")

    def _ensure_scope_cursors(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        cursors = payload.setdefault("scope_cursors", {})
        if not isinstance(cursors, dict):
            cursors = {}
            payload["scope_cursors"] = cursors
        for entry in self._scope_entries():
            key = self._cursor_key(entry)
            cursor = cursors.get(key)
            if not isinstance(cursor, dict):
                cursor = {
                    "scope_key": key,
                    "category_id": entry["category_id"],
                    "canonical_name": entry["canonical_name"],
                    "source_url": entry["source_url"],
                    "next_url": entry["source_url"],
                    "seen_page_urls": [],
                    "pages_fetched": 0,
                    "visited": False,
                    "exhausted": False,
                    "pagination_status": "NOT_STARTED",
                    "strategy": "UNRESOLVED",
                }
                cursors[key] = cursor
            else:
                cursor.setdefault("scope_key", key)
                cursor.setdefault("category_id", entry["category_id"])
                cursor.setdefault("canonical_name", entry["canonical_name"])
                cursor.setdefault("source_url", entry["source_url"])
                cursor.setdefault("next_url", entry["source_url"] if not cursor.get("visited") else None)
                cursor.setdefault("seen_page_urls", [])
                cursor.setdefault("pages_fetched", 0)
                cursor.setdefault("visited", False)
                cursor.setdefault("exhausted", False)
                cursor.setdefault("pagination_status", "NOT_STARTED")
                cursor.setdefault("strategy", "UNRESOLVED")
        return cursors

    def _read(self) -> dict[str, Any]:
        if not self.checkpoint_path.is_file():
            return {
                "schema_version": "product-acquisition.v2",
                "source_url": self.source_url,
                "fetched_scopes": [],
                "visited_scopes": [],
                "exhausted_scopes": [],
                "scope_cursors": {},
                "products": {},
                "emitted": [],
            }
        payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        version = str(payload.get("schema_version") or "")
        if version == "product-acquisition.v1":
            # v1 recorded only ``fetched_scopes``.  Those entries are legacy
            # visits, not proof that the public scope was exhausted.
            payload["schema_version"] = "product-acquisition.v2"
            payload.setdefault("visited_scopes", list(payload.get("fetched_scopes") or []))
            payload.setdefault("exhausted_scopes", [])
            payload.setdefault("scope_cursors", {})
        if payload.get("schema_version") != "product-acquisition.v2":
            raise ProductAcquisitionError("unsupported acquisition checkpoint")
        payload.setdefault("fetched_scopes", [])
        payload.setdefault("visited_scopes", [])
        payload.setdefault("exhausted_scopes", [])
        payload.setdefault("scope_cursors", {})
        payload.setdefault("products", {})
        payload.setdefault("emitted", [])
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        temporary = self.checkpoint_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.checkpoint_path)

    def _get_html(self, url: str) -> tuple[str, str]:
        try:
            return self.client.get_html(url), "L0_L1_HTTP"
        except AccessControlDetected:
            return NativeBrowserCollector(self.browser_session_dir).get_html(url), "L2_BROWSER"
        except RobotsDenied:
            # A declared robots denial is a source-policy boundary, not a
            # browser challenge.  Do not upgrade it to a stronger fetch mode.
            raise
        except HttpStatusError as error:
            # 反爬站点对机器人 UA 常直接回 403/429/5xx（而不是正常验证页），
            # 这类访问控制状态升级到可见浏览器（真实指纹 + 人工验证），而不是直接判失败。
            if error.status_code in {401, 403, 405, 429, 430} or error.retryable:
                return NativeBrowserCollector(self.browser_session_dir).get_html(url), "L2_BROWSER"
            raise
        except (NetworkPolicyError, RequestBudgetExceeded):
            raise

    def discover(self, needed: int) -> list[AcquiredProduct]:
        if needed <= 0:
            return []
        if self.source_type == "MARKETPLACE" and not self.categories:
            raise ProductAcquisitionError("MARKETPLACE_SCOPE_REQUIRED")
        payload = self._read()
        emitted = {str(value) for value in payload.get("emitted") or []}
        products = payload.setdefault("products", {})
        available = [value for key, value in products.items() if key not in emitted]
        if len(available) < needed:
            # ``limit`` is a discovery budget, not a claim about the total
            # supply.  Grow it from the current checkpoint so a later refill
            # can advance its cursor after earlier products were emitted.
            self._discover_more(payload, limit=len(products) + max(needed * 3, 12))
        products = payload["products"]
        available = [value for key, value in products.items() if key not in emitted]
        selected = available[:needed]
        if len(selected) < needed and str(payload.get("discovery_status") or "").upper() == "PAGINATION_UNVERIFIED":
            # Do not hand a partial batch to the Exact-N engine and let it
            # misreport the missing tail as genuine supply shortage.  The
            # checkpoint still contains any already captured products, while
            # the caller receives a resumable discovery blocker.
            raise ProductAcquisitionError("PAGINATION_UNVERIFIED")
        if not selected:
            raise ProductSupplyExhausted("selected scopes have no remaining unique public products")
        for item in selected:
            emitted.add(str(item["identity_key"]))
        payload["emitted"] = sorted(emitted)
        self._write(payload)
        return [AcquiredProduct(**{key: value for key, value in item.items() if key != "identity_key"}) for item in selected]

    @staticmethod
    def _pagination_cursor(base_url: str, current_url: str, page_html: str) -> tuple[str | None, bool]:
        """Return a trustworthy next-page URL and an explicit-end signal.

        The generic path only follows explicit rel/label/data attributes or a
        same-path numeric page link.  It never invents a search URL or follows
        arbitrary navigation links.  A disabled next/load-more control is an
        explicit terminal signal; a visible pagination label without a
        machine-readable cursor is not proof of exhaustion.
        """

        host = (urlsplit(base_url).hostname or "").casefold()
        current = urlsplit(current_url)
        current_page = 1
        current_query = dict(parse_qsl(current.query, keep_blank_values=True))
        for key in ("page", "p", "page_num", "pageNumber"):
            value = current_query.get(key)
            if value and str(value).isdigit():
                current_page = int(value)
                break

        def attrs(raw: str) -> dict[str, str]:
            return {
                key.casefold(): html_lib.unescape(value or "")
                for key, value in re.findall(r"([\w:-]+)\s*=\s*['\"]([^'\"]*)['\"]", raw, re.I)
            }

        pagination_hint = bool(re.search(r"pagination|pager|page-number|load[- ]?more|next|下一页|更多", page_html, re.I))
        explicit_end = False
        numeric_page_seen = False
        numeric_candidates: list[tuple[int, str]] = []
        # ``<link rel="next" href="…">`` is self-closing and therefore is
        # not covered by the anchor-content expression below.
        for raw in re.findall(r"<(?:link|a|button)\b[^>]*>", page_html, re.I):
            attributes = attrs(raw)
            rel = attributes.get("rel", "").casefold()
            href = attributes.get("href") or attributes.get("data-next-url")
            if href and "next" in rel:
                absolute = _canonical_url(urljoin(base_url, href))
                if (urlsplit(absolute).hostname or "").casefold() == host:
                    return absolute, False
        for match in re.finditer(r"<(?:a|link|button)\b([^>]*)>(.*?)</(?:a|link|button)>", page_html, re.I | re.S):
            attributes = attrs(match.group(1))
            label = " ".join(re.sub(r"<[^>]+>", " ", match.group(2)).split())
            href = attributes.get("href") or attributes.get("data-href") or attributes.get("data-next-url")
            rel = attributes.get("rel", "").casefold()
            signal = " ".join((rel, attributes.get("aria-label", ""), attributes.get("title", ""), label)).casefold()
            is_next_control = bool(re.search(r"\bnext\b|next page|下一页|更多|load more", signal, re.I))
            if is_next_control and (
                "disabled" in attributes
                or attributes.get("aria-disabled", "").casefold() == "true"
                or attributes.get("data-disabled", "").casefold() == "true"
            ):
                explicit_end = True
            if href:
                absolute = _canonical_url(urljoin(base_url, href))
                if (urlsplit(absolute).hostname or "").casefold() != host:
                    continue
                if "next" in rel or is_next_control:
                    return absolute, False
                parsed = urlsplit(absolute)
                query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                for key in ("page", "p", "page_num", "pageNumber"):
                    value = query.get(key)
                    if value and str(value).isdigit():
                        numeric_page_seen = True
                        if int(value) > current_page and parsed.path.rstrip("/") == current.path.rstrip("/"):
                            numeric_candidates.append((int(value), absolute))
                        break
            if attributes.get("data-next-page", "").isdigit():
                next_page = int(attributes["data-next-page"])
                if next_page > current_page:
                    query = list(parse_qsl(current.query, keep_blank_values=True))
                    replaced = False
                    for index, (key, value) in enumerate(query):
                        if key in {"page", "p", "page_num", "pageNumber"}:
                            query[index] = (key, str(next_page))
                            replaced = True
                    if not replaced:
                        query.append(("page", str(next_page)))
                    return urlunsplit((current.scheme, current.netloc, current.path, urlencode(query), "")), False
        if numeric_candidates:
            return min(numeric_candidates, key=lambda item: item[0])[1], False
        if pagination_hint and numeric_page_seen:
            explicit_end = True
        return None, explicit_end

    def _store_product(self, payload: dict[str, Any], product: AcquiredProduct) -> None:
        serialized = asdict(product)
        serialized["identity_key"] = product.identity_key
        payload.setdefault("products", {}).setdefault(product.identity_key, serialized)
        capture_path = self.capture_root / f"{product.capture_sha256}.json"
        if not capture_path.exists():
            capture_path.write_text(json.dumps(serialized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _discover_more(self, payload: dict[str, Any], *, limit: int) -> None:
        cursors = self._ensure_scope_cursors(payload)
        products = payload.setdefault("products", {})
        payload.setdefault("visited_scopes", [])
        payload.setdefault("exhausted_scopes", [])
        while len(products) < limit:
            candidates = [
                cursor for cursor in cursors.values()
                if isinstance(cursor, dict)
                and not bool(cursor.get("exhausted"))
                and ((str(cursor.get("strategy") or "") == "MAGENTO_GRAPHQL" and cursor.get("next_page"))
                     or (str(cursor.get("strategy") or "") != "MAGENTO_GRAPHQL" and cursor.get("next_url")))
                and int(cursor.get("pages_fetched") or 0) < MAX_ACQUISITION_PAGES
            ]
            if not candidates:
                break
            cursor = candidates[0]
            scope_url = _canonical_url(str(cursor.get("source_url") or self.source_url))
            scope = {
                "category_id": str(cursor.get("category_id") or "scope_source"),
                "canonical_name": str(cursor.get("canonical_name") or "Source Scope"),
                "native_name": str(cursor.get("native_name") or ""),
                "source_url": scope_url,
            }
            strategy = str(cursor.get("strategy") or "UNRESOLVED")
            current_page_url = scope_url
            if strategy == "MAGENTO_GRAPHQL":
                current_page = int(cursor.get("next_page") or 1)
                try:
                    metadata = self._discover_magento_products(
                        payload, scope, scope_url, limit=limit,
                        current_page=current_page,
                        page_size=int(cursor.get("page_size") or min(max(limit, 1), 48)),
                    )
                except (HttpStatusError, NetworkPolicyError, RequestBudgetExceeded, RobotsDenied) as error:
                    raise ProductAcquisitionError(
                        f"MAGENTO_GRAPHQL_DISCOVERY_FAILED:{type(error).__name__}"
                    ) from error
                item_count = int(metadata.get("item_count") or 0)
                page_size = int(metadata.get("page_size") or cursor.get("page_size") or 1)
                total_count = metadata.get("total_count")
                total_pages = metadata.get("total_pages")
                cursor["page_size"] = page_size
                cursor["total_count"] = total_count
                cursor["total_pages"] = total_pages
                cursor["next_page"] = current_page + 1
                if item_count == 0 or (isinstance(total_pages, int) and current_page >= total_pages) or (total_count is None and item_count < page_size):
                    cursor["exhausted"] = True
                    cursor["pagination_status"] = "EXPLICIT_END"
                    cursor["next_page"] = None
                cursor["strategy"] = "MAGENTO_GRAPHQL"
                cursor["pages_fetched"] = int(cursor.get("pages_fetched") or 0) + 1
                cursor["visited"] = True
                cursor.setdefault("seen_page_urls", []).append(f"{scope_url}#graphql-page-{current_page}")
            else:
                current_page_url = _canonical_url(str(cursor.get("next_url") or scope_url))
                seen_pages = [str(value) for value in cursor.get("seen_page_urls") or []]
                if current_page_url in seen_pages:
                    cursor["next_url"] = None
                    cursor["exhausted"] = True
                    cursor["pagination_status"] = "REPEATED_CURSOR"
                    continue
                html, acquisition = self._get_html(current_page_url)
                if _is_magento_pwa_shell(html):
                    cursor["strategy"] = "MAGENTO_GRAPHQL"
                    try:
                        metadata = self._discover_magento_products(
                            payload, scope, scope_url, limit=limit,
                            current_page=1,
                            page_size=min(max(limit, 1), 48),
                        )
                    except (HttpStatusError, NetworkPolicyError, RequestBudgetExceeded, RobotsDenied) as error:
                        raise ProductAcquisitionError(
                            f"MAGENTO_GRAPHQL_DISCOVERY_FAILED:{type(error).__name__}"
                        ) from error
                    item_count = int(metadata.get("item_count") or 0)
                    page_size = int(metadata.get("page_size") or min(max(limit, 1), 48))
                    total_count = metadata.get("total_count")
                    total_pages = metadata.get("total_pages")
                    cursor.update({"page_size": page_size, "total_count": total_count, "total_pages": total_pages, "next_page": 2})
                    if item_count == 0 or (isinstance(total_pages, int) and total_pages <= 1) or (total_count is None and item_count < page_size):
                        cursor["exhausted"] = True
                        cursor["pagination_status"] = "EXPLICIT_END"
                        cursor["next_page"] = None
                    cursor["pages_fetched"] = int(cursor.get("pages_fetched") or 0) + 1
                    cursor["visited"] = True
                    cursor.setdefault("seen_page_urls", []).append(f"{scope_url}#graphql-page-1")
                else:
                    links = self._product_links(current_page_url, html)
                    if not links and not PRODUCT_PATH.search(urlsplit(current_page_url).path):
                        # A JS shell is the one bounded reason to use the same
                        # persistent Website L2 browser session.
                        html = NativeBrowserCollector(self.browser_session_dir).get_html(current_page_url)
                        acquisition = "L2_BROWSER"
                        links = self._product_links(current_page_url, html)
                    if not links:
                        page_product = self._product_from_html(current_page_url, html, scope, acquisition)
                        if page_product is not None and self._is_product_detail_url(current_page_url, page_product):
                            links = [current_page_url]
                    for product_url in links[:max(limit, 1)]:
                        try:
                            product_html, detail_mode = (html, acquisition) if product_url == current_page_url else self._get_html(product_url)
                        except (HttpStatusError, NetworkPolicyError, RequestBudgetExceeded):
                            continue
                        product = self._product_from_html(product_url, product_html, scope, detail_mode)
                        if product is None or not self._is_product_detail_url(product_url, product):
                            continue
                        self._store_product(payload, product)
                    next_url, explicit_end = self._pagination_cursor(current_page_url, current_page_url, html)
                    cursor["strategy"] = "HTML_LINK_OR_QUERY"
                    cursor["next_url"] = next_url
                    cursor["pagination_status"] = "NEXT_CURSOR" if next_url else "EXPLICIT_END" if not links or explicit_end else "VISITED_NO_CURSOR"
                    # A page with no products is an explicit end.  A page with
                    # products but no verifiable continuation remains visited
                    # and resumable in state, rather than being mislabeled as
                    # exhausted (which used to cause false TARGET_SHORTAGE).
                    cursor["exhausted"] = not links or bool(next_url) is False and bool(explicit_end)
                    cursor["pages_fetched"] = int(cursor.get("pages_fetched") or 0) + 1
                    cursor["visited"] = True
                    cursor.setdefault("seen_page_urls", []).append(current_page_url)
            visited = {str(value) for value in payload.get("visited_scopes") or []}
            visited.add(scope_url)
            payload["visited_scopes"] = sorted(visited)
            payload["fetched_scopes"] = sorted(visited)  # v1-compatible read model
            exhausted = {str(value) for value in payload.get("exhausted_scopes") or []}
            if cursor.get("exhausted"):
                exhausted.add(scope_url)
            else:
                exhausted.discard(scope_url)
            payload["exhausted_scopes"] = sorted(exhausted)
            self._write(payload)

        # Preserve the distinction in the checkpoint for callers and the UI;
        # a visited/no-cursor scope is deliberately not put in exhausted_scopes.
        if len(products) < limit and any(
            isinstance(cursor, dict) and cursor.get("visited") and not cursor.get("exhausted")
            for cursor in cursors.values()
        ):
            payload["discovery_status"] = "PAGINATION_UNVERIFIED"
        elif not any(isinstance(cursor, dict) and not cursor.get("exhausted") for cursor in cursors.values()):
            payload["discovery_status"] = "EXHAUSTED"
        self._write(payload)

    def _discover_magento_products(
        self,
        payload: dict[str, Any],
        scope: dict[str, Any],
        scope_url: str,
        *,
        limit: int,
        current_page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """Read products for one selected Magento category through public GraphQL.

        Magento PWA category pages render a small client shell and do not expose
        product links in the initial HTML.  The GraphQL calls remain bounded to
        the persisted scope URL and use read-only ``urlResolver``/``products``
        queries; no search expansion or mutation is allowed here.
        """

        endpoint = urljoin(scope_url, "/graphql")
        parsed = urlsplit(endpoint)
        candidates = [endpoint]
        if parsed.hostname and not parsed.hostname.casefold().startswith("www."):
            candidates.append(urlunsplit((parsed.scheme, f"www.{parsed.netloc}", parsed.path, "", "")))
        active_endpoint: str | None = None

        def post(payload_body: dict[str, object]) -> dict[str, Any]:
            nonlocal active_endpoint
            last_error: Exception | None = None
            for candidate in ([active_endpoint] if active_endpoint else candidates):
                if not candidate:
                    continue
                try:
                    result = self.client.post_json(candidate, payload_body)
                    active_endpoint = candidate
                    return result
                except (HttpStatusError, NetworkPolicyError, RequestBudgetExceeded, RobotsDenied) as error:
                    last_error = error
            if last_error is not None:
                raise last_error
            raise NetworkPolicyError("Magento GraphQL endpoint was not available")

        category_path = urlsplit(scope_url).path.strip("/")
        resolved = post({
            "query": "query ResolveScope($url: String!) { urlResolver(url: $url) { id type relative_url } }",
            "variables": {"url": category_path},
        })
        data = resolved.get("data") if isinstance(resolved, dict) else None
        route = data.get("urlResolver") if isinstance(data, dict) else None
        if not isinstance(route, dict) or str(route.get("type") or "").upper() != "CATEGORY":
            return {"item_count": 0, "total_count": None, "total_pages": 0, "page_size": page_size or 1}
        category_id = str(route.get("id") or "").strip()
        if not category_id.isdigit():
            return {"item_count": 0, "total_count": None, "total_pages": 0, "page_size": page_size or 1}

        page_size = max(1, min(int(page_size or limit), 48))
        current_page = max(1, int(current_page))
        products_response = post({
            "query": (
                "query ProductsByCategory($id: String!, $pageSize: Int!, $currentPage: Int!) { "
                "products(filter: {category_id: {eq: $id}}, pageSize: $pageSize, currentPage: $currentPage) { "
                "items { uid sku name url_key url_suffix image { url } small_image { url } "
                "url_rewrites { url } description { html } short_description { html } } "
                "total_count page_info { current_page page_size total_pages } } }"
            ),
            "variables": {"id": category_id, "pageSize": page_size, "currentPage": current_page},
        })
        product_data = products_response.get("data") if isinstance(products_response, dict) else None
        products = product_data.get("products") if isinstance(product_data, dict) else None
        items = products.get("items") if isinstance(products, dict) else None
        item_values = items if isinstance(items, list) else []
        for item in item_values:
            if not isinstance(item, dict):
                continue
            product = self._product_from_magento_item(
                item, scope=scope, scope_url=scope_url, category_id=category_id,
                graphql_endpoint=active_endpoint or endpoint,
            )
            if product is None:
                continue
            self._store_product(payload, product)
        total_count = products.get("total_count") if isinstance(products, dict) else None
        if not isinstance(total_count, int) or total_count < 0:
            total_count = None
        page_info = products.get("page_info") if isinstance(products, dict) else None
        total_pages = page_info.get("total_pages") if isinstance(page_info, dict) else None
        if not isinstance(total_pages, int) or total_pages < 0:
            total_pages = ((total_count + page_size - 1) // page_size) if total_count is not None else None
        return {
            "item_count": len(item_values),
            "total_count": total_count,
            "total_pages": total_pages,
            "page_size": page_size,
            "current_page": current_page,
        }

    @staticmethod
    def _magento_product_url(item: dict[str, Any], scope_url: str) -> str:
        scope_path = urlsplit(scope_url).path.strip("/")
        url_key = str(item.get("url_key") or "").strip("/")
        slug = url_key.rsplit("/", 1)[-1] if url_key else ""
        rewrites = item.get("url_rewrites")
        paths = [
            str(value.get("url") or "").strip("/")
            for value in rewrites if isinstance(value, dict) and value.get("url")
        ] if isinstance(rewrites, list) else []
        scoped = [path for path in paths if scope_path and path.startswith(f"{scope_path}/") and (not slug or path.endswith(f"/{slug}"))]
        chosen = max(scoped, key=len) if scoped else url_key
        if not chosen:
            return ""
        suffix = str(item.get("url_suffix") or "").strip()
        if suffix and not chosen.endswith(suffix):
            chosen = f"{chosen}{suffix}"
        return _canonical_url(urljoin(scope_url, f"/{chosen}"))

    def _product_from_magento_item(
        self,
        item: dict[str, Any],
        *,
        scope: dict[str, Any],
        scope_url: str,
        category_id: str,
        graphql_endpoint: str,
    ) -> AcquiredProduct | None:
        name = " ".join(str(item.get("name") or "").split()).strip()
        image = _magento_product_image(item)
        canonical = self._magento_product_url(item, scope_url)
        sku = " ".join(str(item.get("sku") or "").split()).strip()
        uid = " ".join(str(item.get("uid") or "").split()).strip()
        if not name or not image or not canonical:
            return None
        source_product_id = sku or uid or canonical.rsplit("/", 1)[-1]
        description = _magento_product_description(item)
        dimensions, unit = _parse_dimension_text(description)
        identity_fields = normalize_identity_fields({
            "canonical_url": canonical,
            "jsonld_sku": sku,
            "product_family_name": name,
            "configuration_key": sku or uid,
            "image_url": image,
        })
        binding_reasons = ["magento_graphql_product_image", "magento_graphql_identity_bound"]
        evidence = {
            "schema_version": "website-magento-graphql-capture.v1",
            "source_url": canonical,
            "graphql_endpoint": graphql_endpoint,
            "graphql_category_id": category_id,
            "graphql_product": True,
            "product_identity_match": True,
            "graphql_image_bound": True,
            "image_source": "magento_graphql_product",
            "image_role": "MAIN_PRODUCT",
            "layered_scene7": "layer=" in image.casefold(),
            "configuration_bound": bool(sku or uid),
            "configuration_binding_source": "magento_graphql_sku",
            "dimension_source": "graphql_description" if dimensions else "missing",
            "identity_fields": identity_fields,
            "media_asset_identity": identity_fields.get("asset_identity") or media_asset_identity(image),
            "media_binding_status": "COMPATIBLE",
            "media_binding_confidence": 0.9,
            "media_binding_reasons": binding_reasons,
            "scope_status": "PASS",
            "scope_reasons": [],
            "description_text_present": bool(description),
        }
        capture_payload = {
            "url": canonical,
            "source_product_id": source_product_id,
            "name": name,
            "image": image,
            "dimensions": dimensions,
            "category_id": str(scope.get("category_id") or "scope_source"),
            "identity_fields": identity_fields,
            "media_binding_status": "COMPATIBLE",
            "scope_status": "PASS",
        }
        capture_sha = hashlib.sha256(
            json.dumps(capture_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return AcquiredProduct(
            source_product_id=source_product_id,
            canonical_url=canonical,
            source_name=name,
            source_brand="",
            category_id=str(scope.get("category_id") or "scope_source"),
            category_group=str(scope.get("canonical_name") or scope.get("native_name") or "Source Scope"),
            image_url=image,
            dimensions=dimensions,
            dimension_unit=unit,
            source_type=self.source_type,
            capture_sha256=capture_sha,
            acquisition="MAGENTO_GRAPHQL",
            evidence=evidence,
            identity_fields=identity_fields,
            media_binding_status="COMPATIBLE",
            media_binding_confidence=0.9,
            scope_status="PASS",
        )

    @staticmethod
    def _product_links(base_url: str, page_html: str) -> list[str]:
        links: list[str] = []
        for item in _json_ld(page_html):
            item_type = str(item.get("@type") or "").casefold()
            if item_type == "product" and item.get("url"):
                links.append(urljoin(base_url, str(item["url"])))
            values = item.get("itemListElement")
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, dict):
                        target = value.get("url") or (value.get("item") or {}).get("url") if isinstance(value.get("item"), dict) else value.get("url")
                        if target:
                            links.append(urljoin(base_url, str(target)))
        for href in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"']", page_html, re.I):
            absolute = urljoin(base_url, html_lib.unescape(href))
            if PRODUCT_PATH.search(urlsplit(absolute).path):
                links.append(absolute)
        host = (urlsplit(base_url).hostname or "").casefold()
        unique: list[str] = []
        seen: set[str] = set()
        for value in links:
            canonical = _canonical_url(value)
            if (urlsplit(canonical).hostname or "").casefold() != host or canonical in seen:
                continue
            seen.add(canonical)
            unique.append(canonical)
        return unique

    @staticmethod
    def _is_product_detail_url(url: str, product: AcquiredProduct) -> bool:
        """Reject collection/category metadata that only looks product-like.

        Some storefronts expose an Open Graph title and image on collection
        pages. That is useful category evidence, but it is not enough to
        create a sellable candidate. A detail URL or explicit Product JSON-LD
        is required before the URL can enter the production candidate pool.
        """

        path = urlsplit(url).path
        evidence = product.evidence if isinstance(product.evidence, dict) else {}
        return bool(
            PRODUCT_PATH.search(path)
            or evidence.get("json_ld_product")
            or evidence.get("jsonld_product_selected")
        )

    def _product_from_html(
        self,
        url: str,
        page_html: str,
        scope: dict[str, Any],
        acquisition: str,
    ) -> AcquiredProduct | None:
        product_nodes = [
            item for item in _json_ld(page_html)
            if str(item.get("@type") or "").casefold() == "product"
        ]
        product_json, product_identity_match, selection = _select_product_json(page_html, url, product_nodes)
        page_tail_id = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        page_item_number = selection.get("page_item_number") or ""
        jsonld_sku = str(product_json.get("sku") or product_json.get("productID") or product_json.get("mpn") or "").strip()
        configuration_key, variant_key, explicit_configuration = _product_configuration(product_json)
        name = str(product_json.get("name") or "").strip() or _first_text(page_html, (
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
            r"<h1[^>]*>(.*?)</h1>",
            r"<title[^>]*>(.*?)</title>",
        ))
        image = _image_value(product_json.get("image")) or _first_text(page_html, (
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        ))
        if not name or not image:
            return None
        image = urljoin(url, image)
        source_product_id = jsonld_sku
        if not source_product_id:
            source_product_id = page_tail_id or hashlib.sha256(url.encode()).hexdigest()[:20]
        brand = _brand_value(product_json.get("brand") or product_json.get("manufacturer"))
        visible = html_lib.unescape(re.sub(r"<[^>]+>", " ", page_html))
        dimensions, unit = _parse_dimension_text(visible)
        dimension_source = "explicit_page_text" if dimensions else "missing"
        likely_product_detail = bool(product_nodes or PRODUCT_PATH.search(urlsplit(url).path))
        if not dimensions and likely_product_detail:
            # 部分反爬站点把尺寸放在可折叠标签里，普通 HTML 抓不到；
            # 此时用同一 L2 可见会话打开详情页，展开 Dimensions 标签再解析一次。
            try:
                browser_dimensions, browser_unit = NativeBrowserCollector(self.browser_session_dir).extract_dimensions(url)
            except (BrowserHumanRequired, BrowserTemporaryFailure, BrowserAccessDenied, BrowserRuntimeMissing):
                browser_dimensions, browser_unit = {}, ""
            if browser_dimensions:
                dimensions, unit = browser_dimensions, browser_unit
                dimension_source = "l2_browser_dimensions_tab"
        product_url = _canonical_url(str(product_json.get("url") or url))
        canonical = product_url if product_url == _canonical_url(url) else _canonical_url(url)
        product_family = ""
        if isinstance(product_json.get("isVariantOf"), dict):
            product_family = str(product_json["isVariantOf"].get("name") or product_json["isVariantOf"].get("@id") or "")
        identity_evidence = {
            "canonical_url": canonical,
            "url_tail_id": page_tail_id,
            "page_item_number": page_item_number,
            "jsonld_sku": jsonld_sku,
            "jsonld_sku_role": (
                "secondary_internal"
                if page_item_number and jsonld_sku and page_item_number.casefold() != jsonld_sku.casefold()
                and product_identity_match and bool(product_json.get("image"))
                else ""
            ),
            "product_family_name": product_family or name,
            "configuration_key": configuration_key,
            "variant_key": variant_key,
            "image_url": image,
        }
        identity_fields = normalize_identity_fields(identity_evidence)
        # A selected Product JSON-LD node whose URL/SKU/name matches the page
        # is also explicit configuration evidence for a layered Scene7 asset.
        # The sellable ID and the asset ID remain separate fields.
        configuration_bound = bool(explicit_configuration or (product_identity_match and (jsonld_sku or product_url == canonical)))
        binding_evidence = {
            **identity_fields,
            "product_identity_match": product_identity_match,
            "jsonld_image_bound": bool(product_json.get("image")) and product_identity_match,
            "configuration_bound": configuration_bound,
            "bound_to_product": product_identity_match,
            "media_url": image,
        }
        binding_status, binding_confidence, binding_reasons = media_binding_status(
            binding_evidence,
            media_url=image,
            source="json_ld_image" if product_json.get("image") else "open_graph",
            role="MAIN_PRODUCT",
        )
        scope_state, scope_reasons = scope_status(
            name,
            selection.get("page_h1"),
            scope.get("canonical_name"),
            scope.get("path"),
        )
        layered = "layer=" in image.casefold()
        evidence = {
            "schema_version": "website-product-capture.v1",
            "source_url": url,
            "json_ld_product": bool(product_json),
            "jsonld_product_selected": bool(product_json),
            "jsonld_product_count": len(product_nodes),
            "jsonld_product_ambiguous": selection.get("jsonld_product_ambiguous") == "true",
            "product_identity_match": product_identity_match,
            "name_source": "json_ld_product" if product_json.get("name") else "page_metadata",
            "image_source": "json_ld_product" if product_json.get("image") else "open_graph",
            "jsonld_image_bound": bool(product_json.get("image")) and product_identity_match,
            "image_role": "MAIN_PRODUCT",
            "layered_scene7": layered,
            "configuration_bound": configuration_bound,
            "configuration_binding_source": "explicit_product_configuration" if explicit_configuration else "product_jsonld_identity" if configuration_bound else "unknown",
            "dimension_source": dimension_source,
            "identity_fields": identity_fields,
            "media_asset_identity": identity_fields.get("asset_identity") or media_asset_identity(image),
            "media_binding_status": binding_status,
            "media_binding_confidence": binding_confidence,
            "media_binding_reasons": binding_reasons,
            "scope_status": scope_state,
            "scope_reasons": scope_reasons,
            "selection": selection,
        }
        capture_payload = {
            "url": canonical,
            "source_product_id": source_product_id,
            "name": name,
            "brand": brand,
            "image": image,
            "dimensions": dimensions,
            "category_id": str(scope.get("category_id") or "scope_source"),
            "identity_fields": identity_fields,
            "media_binding_status": binding_status,
            "scope_status": scope_state,
        }
        capture_sha = hashlib.sha256(
            json.dumps(capture_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return AcquiredProduct(
            source_product_id=source_product_id,
            canonical_url=canonical,
            source_name=" ".join(name.split())[:500],
            source_brand=" ".join(brand.split())[:250],
            category_id=str(scope.get("category_id") or "scope_source"),
            category_group=str(scope.get("canonical_name") or scope.get("native_name") or "Source Scope"),
            image_url=image,
            dimensions=dimensions,
            dimension_unit=unit,
            source_type=self.source_type,
            capture_sha256=capture_sha,
            acquisition=acquisition,
            evidence=evidence,
            identity_fields=identity_fields,
            media_binding_status=binding_status,
            media_binding_confidence=binding_confidence,
            scope_status=scope_state,
        )


__all__ = [
    "AcquiredProduct",
    "BrowserAccessDenied", "BrowserHumanRequired", "BrowserTemporaryFailure",
    "BrowserRuntimeMissing",
    "NativeBrowserCollector",
    "ProductAcquisitionEngine",
    "ProductAcquisitionError",
    "ProductSupplyExhausted",
    "SOURCE_TYPES",
    "classify_source_type",
]
