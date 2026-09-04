from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

from app.services.brain_provider import (
    AgentLoopOptions,
    AgentToolError,
    BrainError,
    BrainNotConfigured,
    WebsiteBrainProvider,
)
from app.services.native_contracts import BrainTaxonomyResponse, TaxonomyCategoryContract, TaxonomyReceipt
from app.services.product_acquisition import (
    BrowserAccessDenied,
    BrowserHumanRequired,
    BrowserRuntimeMissing,
    BrowserTemporaryFailure,
    NativeBrowserCollector,
    classify_source_type,
)

import requests

from workers.scrape.http_client import (
    AccessControlDetected,
    HttpStatusError,
    NetworkPolicyError,
    RequestBudgetExceeded,
    RobotsDenied,
    SafeHttpClient,
)


COUNT_RE = re.compile(r"(?<![\w])([0-9][0-9,]*)\s*(?:items?|products?|results?|件|个)(?![\w])", re.I)
# 导航锚文本里的「类目名 + 空格 + 行尾数字」数量格式（如 sixpenny 的 "CHAIRS 33"、
# "TABLES 38"、"SOFAS 18"）。数字在行尾、无 items/products 后缀，原 COUNT_RE 匹配不到。
TAIL_COUNT_RE = re.compile(r"(?<![\w])([0-9][0-9,]*)\s*$")
PRODUCT_RE = re.compile(r"/(?:products?|product-page|p|item|sku)/[^/?#]+", re.I)
ASSET_RE = re.compile(r"\.(?:css|js|png|jpe?g|gif|svg|webp|ico|pdf|xml|zip)(?:$|[?#])", re.I)
MAX_COUNT_PROBES = 120
BROWSER_ESCALATION_HTTP_STATUSES = frozenset({401, 403, 405, 430})
CATEGORY_WORDS = {
    "furniture", "living", "bedroom", "dining", "office", "outdoor", "seating", "chairs", "sofas",
    "tables", "desks", "beds", "lighting", "rugs", "storage", "decor", "accessories", "casegoods",
    "bath", "bathroom", "kitchen", "entryway", "kids", "nursery",
    "家具", "客厅", "卧室", "餐厅", "办公", "户外", "椅", "沙发", "桌", "床", "灯", "地毯", "收纳", "装饰",
}
BLOCKED_SEGMENTS = {"account", "login", "signin", "cart", "checkout", "search", "blog", "news", "privacy", "terms", "help"}
# 营销/主题页路径段：这些不是真实商品类目，混进类目清单会污染两级结构。
MARKETING_SEGMENTS = {
    "collection", "collections", "sale", "clearance", "new-arrivals", "best-sellers", "featured",
    "gifts", "gift", "gift-card", "gift-cards", "inspiration", "lookbook", "lookbooks", "story",
    "stories", "about", "about-us", "reviews", "journal", "press", "careers", "showroom", "stores",
}
# 站点导航/目录前缀段：真正的部门在它之后（如 /catalog/home-decor），需剥离后才能按深度定层级。
TAXONOMY_ROOTS = {"catalog", "collections", "shop", "products", "product", "categories"}
# 多语言/地区站点在类目路径前的地区前缀（如 ligne-roset 的 /en/c/…、/fr/c/…）。
# 计深度的真实类目在这些前缀之后，需先剥离才不会因路径过深而被丢弃。
LOCALE_SEGMENTS = {
    "en", "fr", "de", "es", "it", "pt", "nl", "pl", "uk", "us", "at", "ie", "cn",
    "ru", "ja", "zh", "se", "dk", "fi", "no", "au", "ca",
    "sg", "hk", "my", "id", "in", "ae", "kr", "tw", "jp", "mx", "br", "za", "nz",
    "en-gb", "en-us", "en-ca", "en-au", "fr-fr", "fr-ch", "fr-ca", "de-de", "de-ch",
    "es-es", "it-it", "pt-br", "nl-nl",
}
# ligne-roset 用 /c/ 作为类目根；一并视作目录前缀剥离。
_CATEGORY_ROOTS = TAXONOMY_ROOTS | {"c"}
# 页脚/工具/非商品页路径段：出现在类目清单里会污染两层结构（如 /contact-us、/terms-of-sale）。
UTILITY_PATH_WORDS = {
    "accessibility", "affirm", "business", "contact", "contact-us", "customer", "customer-care",
    "customer-photos", "customer-service", "design", "digital-catalog", "digital-catalogs",
    "dmca", "faq", "favorites", "free-design", "free-design-services", "hr-privacy", "privacy",
    "product-care", "product-recalls", "recalls", "sustainability", "terms", "terms-of-sale",
    "terms-of-use", "about", "about-us", "clearance", "fabrics", "swatches", "promo", "demo",
    "account", "login", "signin", "cart", "checkout", "search", "blog", "news", "help", "rewards",
    "warranty", "returns", "shipping", "security", "jobs", "sitemap",
    "care", "contract-grade", "data-request", "data-requests", "trade-program", "financing",
    # West Elm 特有的页脚/工具/促销路径（出现在首页导航/横幅里，不是商品类目）
    "ccvalueprop", "my-boards", "registry", "shoppingcart", "void", "store-locator",
    "storelocator", "gift-card", "gift-card-services", "collaborations", "holidays",
    "iconography", "credit-card", "feedback",
    # 选购指南/配置器/联名/活动页（如 castlery 的 Customisation Service、Furniture
    # Configurator Tool、Outdoor/Sofa Buying Guide、The Gift Guide）不是商品类目。
    "buying-guide", "gift-guide", "style-guide", "configurator", "customisation",
    "customization", "our-story", "how-to", "designer", "partnership",
}


def normalize_site_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL must be a public http(s) URL without credentials")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def site_key_for(value: str) -> str:
    host = (urlsplit(value).hostname or "unknown-site").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


_LIST_TAGS = frozenset({"ul", "ol"})
_ITEM_TAGS = frozenset({"li"})
_MAX_ELEMENT_DEPTH = 64


class _NavNode:
    __slots__ = ("label", "href", "count_raw", "rel_depth", "parent_href", "excluded", "children")

    def __init__(self, href: str, rel_depth: int) -> None:
        self.label = ""
        self.href = href
        self.count_raw = ""
        self.rel_depth = rel_depth
        self.parent_href: str | None = None
        self.excluded = False
        self.children: list["_NavNode"] = []


def _href_key(href: str) -> str:
    return href.split("#")[0].rstrip("/").casefold()


def _is_nav_container(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    if tag in {"nav", "menu"}:
        return True
    # 链接/列表项不可能是导航根容器；class 里的 dropdown/menu 是子菜单标记，不作根。
    if tag in {"a", "li"}:
        return False
    attrs_map = {str(k).casefold(): str(v or "").casefold() for k, v in attrs}
    if attrs_map.get("role", "") == "navigation":
        return True
    hay = " ".join([attrs_map.get("class", ""), attrs_map.get("id", "")]).casefold()
    # 子菜单容器（submenu/dropdown/mega）是导航根的子孙，绝不作为导航根。
    if any(word in hay for word in ("submenu", "dropdown", "mega")):
        return False
    return any(word in hay for word in ("nav", "menu"))


def _is_footer_like(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    if tag == "footer":
        return True
    attrs_map = {str(k).casefold(): str(v or "").casefold() for k, v in attrs}
    hay = " ".join([attrs_map.get("class", ""), attrs_map.get("id", "")])
    return any(word in hay for word in ("footer", "foot", "legal"))


class _NavigationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict[str, str]] = []      # 扁平锚（保持向后兼容）
        self.nav_nodes: list[_NavNode] = []          # 带父子指针的导航节点
        self.nav_tree: list[dict] = []               # 嵌套树（feed 后 build_nav_tree 填充）
        self._current: dict[str, str] | None = None
        self._current_node: _NavNode | None = None
        self._stack = 0
        self._element_stack: list[dict] = []         # 元素帧栈，用于推导导航父级
        self._stack_full = False

    def _frame(self, tag: str, attrs: list[tuple[str, str | None]]) -> dict:
        return {
            "tag": tag,
            "is_nav_root": _is_nav_container(tag, attrs) and not (self._element_stack and self._element_stack[-1]["is_nav_root"]),
            "excluded": _is_footer_like(tag, attrs),
            "node": None,
        }

    def _open_nav_node(self, values: dict[str, str]) -> _NavNode:
        node = _NavNode(href=values.get("href", ""), rel_depth=0)
        if self._stack_full:
            self.nav_nodes.append(node)
            return node
        nav_index = None
        for i, frame in enumerate(self._element_stack):
            if frame["is_nav_root"]:
                nav_index = i
        if nav_index is not None:
            rel_depth, crossed_list, parent_href = 0, False, None
            excluded = False
            for frame in reversed(self._element_stack[nav_index + 1:]):
                excluded = excluded or frame["excluded"]
                if frame["tag"] in _LIST_TAGS:
                    crossed_list = True
                elif frame["tag"] in _ITEM_TAGS:
                    # 数全部 <li> 得到真实深度；父锚取最内层已登记节点（不 break）。
                    rel_depth += 1
                    if parent_href is None and frame.get("node") is not None and crossed_list:
                        parent_href = frame["node"].href
            node.rel_depth = rel_depth
            node.parent_href = parent_href
            node.excluded = excluded
            # 把本节点登记到最近的 <li> 帧，供更深层锚查找父级
            for frame in reversed(self._element_stack):
                if frame["tag"] in _ITEM_TAGS:
                    frame["node"] = node
                    break
        self.nav_nodes.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag != "a":
            if self._current is not None:
                self._stack += 1
            if len(self._element_stack) >= _MAX_ELEMENT_DEPTH:
                self._stack_full = True
            if not self._stack_full:
                self._element_stack.append(self._frame(tag, attrs))
            return
        if self._current is not None:
            self.handle_endtag("a")
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        self._current = {"href": values.get("href", ""), "text": "", "count": values.get("data-count", "") or values.get("aria-label", "")}
        self._current_node = self._open_nav_node(values)
        self._stack = 0
        if not self._stack_full:
            self._element_stack.append(self._frame(tag, attrs))

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"] += f" {data}"

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self._current is not None:
            item = {key: " ".join(value.split()) for key, value in self._current.items()}
            if item["href"]:
                self.anchors.append(item)
                if self._current_node is not None:
                    self._current_node.label = item["text"]
                    self._current_node.count_raw = item["count"]
            self._current = None
            self._current_node = None
            self._stack = 0
        elif self._current is not None and self._stack:
            self._stack -= 1
        if not self._stack_full:
            self._pop_until(tag)

    def _pop_until(self, tag: str) -> None:
        for i in range(len(self._element_stack) - 1, -1, -1):
            if self._element_stack[i]["tag"] == tag:
                del self._element_stack[i:]
                return
        if self._element_stack:
            self._element_stack.pop()

    def build_nav_tree(self) -> list[dict]:
        """构建「大标签→小标签」嵌套树。仅当存在至少一个二级(rel_depth==2)节点时输出非空。"""
        nodes = [n for n in self.nav_nodes if not n.excluded and n.href and 1 <= n.rel_depth <= 2]
        if not any(n.rel_depth == 2 for n in nodes):
            self.nav_tree = []
            return self.nav_tree
        by_href: dict[str, _NavNode] = {}
        for n in nodes:
            by_href.setdefault(_href_key(n.href), n)
        children: dict[int, list[_NavNode]] = {}
        for n in nodes:
            if n.parent_href:
                parent = by_href.get(_href_key(n.parent_href))
                if parent is not None:
                    children.setdefault(id(parent), []).append(n)

        def emit(n: _NavNode) -> dict:
            return {
                "label": " ".join(n.label.split()),
                "path": urlsplit(n.href).path,
                "count": _count_from_nav_text(n.label + " " + n.count_raw),
                "children": [emit(child) for child in children.get(id(n), [])],
            }

        roots = [
            emit(n) for n in nodes
            if n.parent_href is None or by_href.get(_href_key(n.parent_href)) is None
        ]
        self.nav_tree = roots
        return roots



def _json_ld_values(page_html: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for raw in re.findall(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", page_html, flags=re.I | re.S):
        try:
            payload = json.loads(html_lib.unescape(raw.strip()))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if isinstance(item, dict):
                values.append(item)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    values.extend(value for value in graph if isinstance(value, dict))
    return values


def _int_or_none(value: str) -> int | None:
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return None


def _count_from_text(value: str) -> int | None:
    match = COUNT_RE.search(value)
    if not match:
        return None
    return _int_or_none(match.group(1))


def _count_from_nav_text(value: str) -> int | None:
    """从导航锚文本提取数量。

    优先「数字 + items/products 后缀」（如 "33 items"），其次兼容
    「类目名 + 空格 + 行尾数字」格式（如 sixpenny 的 "CHAIRS 33"）。
    仅用于导航 label 场景；整页可见数量提取仍走 _count_from_text，避免误判。
    """
    match = COUNT_RE.search(value)
    if match:
        return _int_or_none(match.group(1))
    if re.fullmatch(r".+\s+[0-9][0-9,]*", value.strip()):
        tail = TAIL_COUNT_RE.search(value)
        if tail:
            return _int_or_none(tail.group(1))
    return None


def _canonical_name(native: str, path: str) -> str:
    value = " ".join(native.replace("&amp;", "&").split()).strip("-–—|:")
    if len(value) > 255:
        value = path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title()
    return (value[:255] or path.strip("/").replace("-", " ").replace("_", " ").title() or "Uncategorized")[:255]


def _looks_like_category(url: str, text: str, source_url: str) -> bool:
    parsed = urlsplit(url)
    source = urlsplit(source_url)
    if parsed.hostname and parsed.hostname.casefold() != (source.hostname or "").casefold():
        return False
    path = parsed.path.casefold()
    segments = {part for part in path.split("/") if part}
    if not path or ASSET_RE.search(path) or PRODUCT_RE.search(path) or segments & BLOCKED_SEGMENTS:
        return False
    if urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")) == urlunsplit((source.scheme, source.netloc, source.path or "/", source.query, "")):
        return False
    label_text = " ".join(text.split())
    # Some SPA shells accidentally place serialized state/tracking payloads inside
    # anchors. They are not taxonomy evidence and must not become oversized
    # category contracts or poison an otherwise valid scan.
    if len(label_text) > 180 or len(parsed.path) > 512:
        return False
    if re.search(r"(?:[{}]|=>|\b(?:window|document|function|__next)\b)", label_text, re.I):
        return False
    label = label_text.casefold()
    return bool(segments & CATEGORY_WORDS or any(word in label for word in CATEGORY_WORDS) or len([x for x in path.split("/") if x]) <= 3)


def _taxonomy_segments(url: str, source_url: str) -> list[str]:
    """Return safe one/two-level taxonomy segments for a same-site URL."""

    parsed = urlsplit(url)
    source = urlsplit(source_url)
    if parsed.hostname and parsed.hostname.casefold() != (source.hostname or "").casefold():
        return []
    if ASSET_RE.search(parsed.path):
        return []
    segments = [part.strip() for part in parsed.path.split("/") if part.strip()]
    while segments and segments[0].casefold() in TAXONOMY_ROOTS:
        segments.pop(0)
    if not segments or len(segments) > 2:
        return []
    if any(
        part.casefold() in BLOCKED_SEGMENTS
        or part.casefold() in UTILITY_PATH_WORDS
        or part.casefold() in MARKETING_SEGMENTS
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", part.casefold())
        or part.isdigit()
        for part in segments
    ):
        return []
    return segments


# 二级类目粗分类：把站点里过于细碎的二级（如 sofas-and-loveseats / sofas-with-chaise /
# sectionals）合并成"沙发 / 椅子 / 桌子 / 柜子…"这类粗分类，避免每个部门下二级过多。
# 顺序即优先级，子串匹配整条 slug，先匹配先得。
CATEGORY_COARSE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("custom", ("custom", "made-to-order")),
    ("bedding", ("bedding", "duvet", "blanket", "coverlet", "sheet", "sham", "linen", "towel", "textile", "pillow", "throw")),
    ("sofas", ("sofa", "loveseat", "sectional", "chaise", "daybed", "sleeper", "couch")),
    ("chairs", ("chair", "stool", "bench", "ottoman", "recliner", "banquette", "seating", "cushion")),
    ("beds", ("bed", "mattress", "bunk", "loft", "crib", "nursery", "headboard", "platform")),
    ("bath", ("bath", "vanit", "medicine", "laundry", "wellness", "self-care", "shower", "toilet")),
    ("tabletop", ("tabletop", "kitchen", "cookware", "serveware", "glassware", "clean", "care")),
    ("lighting", ("lamp", "light", "sconce", "chandelier", "pendant", "ceiling", "lantern", "bulb", "fan")),
    ("tables", ("table", "desk", "console", "sideboard", "island", "counter")),
    ("storage", ("cabinet", "bookcase", "shelve", "storage", "dresser", "armoire", "nightstand", "media", "wardrobe", "basket", "bin", "tray", "organ", "hardware", "hook")),
    ("rugs", ("rug", "carpet", "runner", "mat")),
    ("decor", ("decor", "wall-art", "print", "silkscreen", "frame", "photo", "vase", "bowl", "candle", "holder", "clock", "plant", "planter", "pet", "gift", "season", "favorite", "vintage", "accent", "entertain", "novelty", "ledge", "figurine", "sculpt", "mirror", "wall")),
    ("outdoor", ("outdoor", "patio", "garden", "deck", "porch")),
)

CATEGORY_COARSE_LABELS: dict[str, str] = {
    "custom": "Custom",
    "bedding": "Bedding & Linens",
    "sofas": "Sofas",
    "chairs": "Chairs",
    "beds": "Beds",
    "bath": "Bath",
    "tabletop": "Tabletop & Kitchen",
    "lighting": "Lighting",
    "tables": "Tables",
    "storage": "Storage",
    "rugs": "Rugs",
    "decor": "Decor & Accents",
    "outdoor": "Outdoor",
    "other": "Other",
}


def _coarse_group(slug: str) -> str:
    text = slug.replace("_", "-").casefold()
    for name, words in CATEGORY_COARSE_GROUPS:
        if any(word in text for word in words):
            return name
    return "other"


def _has_brain_parent(category: "TaxonomyCategoryContract") -> bool:
    """类目是否由千问大脑明确输出了父子关系（带 brain_child 标记）。

    这类父子是大脑基于导航语义判断的权威结构，_hierarchize 尊重其
    level/parent_path，不被路径段数推断覆盖；无标记的类目仍走路径推断。
    """
    return any(str(item.get("role") or "") == "brain_child" for item in category.evidence)


def _nav_tree_evidence(category: "TaxonomyCategoryContract") -> dict | None:
    """返回类目上的 nav_tree evidence（若有），供 _hierarchize 读取导航层级。"""
    for item in category.evidence:
        if str(item.get("role") or "") == "nav_tree":
            return item
    return None


def _normalize_parent_path(parent_path: str) -> str | None:
    """剥离父路径的地区/目录前缀，与 _hierarchize 大脑分支逻辑一致（纯提取复用）。"""
    parts = [p for p in parent_path.split("/") if p]
    while parts and parts[0].casefold() in LOCALE_SEGMENTS:
        parts = parts[1:]
    while parts and parts[0].casefold() in _CATEGORY_ROOTS:
        parts = parts[1:]
    return ("/" + "/".join(parts)) if parts else None


# 大脑 agent 可调用的工具声明（OpenAI function-calling 格式）。执行仍由
# NativeSiteAnalyzer 注入的 executor 完成，并全部经过 SafeHttpClient 合规门。
_AGENT_TOOLS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "browse",
            "description": "Fetch a public page on the same site and return cleaned visible text plus candidate navigation links.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "same-site page URL to fetch"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_categories",
            "description": "Run the rule-based category extractor on a page and return its candidate category list as a starting hint.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "same-site page URL (defaults to the site root)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_count",
            "description": "Fetch a category page and estimate its product count (EXACT/ESTIMATED). Use for categories whose count is UNKNOWN.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "same-site category page URL"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_browser",
            "description": "Request that this page be re-fetched in a visible persistent browser session (L2). Not a CAPTCHA claim.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "page URL needing a headed browser"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Finalize the two-level furniture taxonomy with counts and stop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "categories": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "native_name": {"type": "string"},
                                "canonical_name": {"type": "string"},
                                "path": {"type": "string"},
                                "source_url": {"type": "string"},
                                "count_value": {"type": ["integer", "null"]},
                                "count_kind": {"type": "string", "enum": ["EXACT", "ESTIMATED", "UNKNOWN"]},
                                "level": {"type": "integer"},
                                "parent_path": {"type": ["string", "null"]},
                            },
                            "required": ["native_name", "canonical_name", "path", "level"],
                        },
                    },
                    "reasoning": {"type": "string"},
                },
                "required": ["categories"],
            },
        },
    },
]


class NativeSiteAnalyzer:
    """Safe, bounded L0/L1 taxonomy analyzer owned by Website."""

    def __init__(self, output_root: Path, brain: WebsiteBrainProvider | None = None, *, client_factory: Callable[..., SafeHttpClient] | None = None) -> None:
        self.output_root = Path(output_root).resolve()
        self.brain = brain or WebsiteBrainProvider()
        self.client_factory = client_factory or SafeHttpClient

    def _access_decision(self, source_url: str, *, stage: str, reason_code: str, evidence: dict[str, object] | None = None) -> tuple[str, dict[str, str], dict[str, object]]:
        payload = {"stage": stage, "reason_code": reason_code, **(evidence or {})}
        try:
            decision, metadata = self.brain.reason_access(source_url=source_url, evidence=payload)
            state = decision.access_state
            mapping = {
                "ACCESSIBLE": "READY",
                "ESCALATE_L2": "BROWSER_REQUIRED",
                "TEMPORARY_FAILURE": "TEMPORARY_FAILURE",
                "HUMAN_REQUIRED": "HUMAN_REQUIRED",
                "ACCESS_CHANGE_REQUIRED": "ACCESS_CHANGE_REQUIRED",
                "SESSION_CONTINUITY_BROKEN": "SESSION_CONTINUITY_BROKEN",
                "STOP": "FAILED",
            }
            status = mapping[state]
            messages = {
                "BROWSER_REQUIRED": "L1 访问证据不足，已升级到同一持久可见浏览器会话；这不是人机验证结论。",
                "TEMPORARY_FAILURE": "页面或导航暂时失败；会话和检查点已保留，可恢复同一扫描重试。",
                "HUMAN_REQUIRED": "检测到可见且可操作的人机验证控件；完成后恢复同一扫描。",
                "ACCESS_CHANGE_REQUIRED": "可见浏览器仍被站点拒绝，但没有人机验证控件；请检查网络或站点访问条件。",
                "SESSION_CONTINUITY_BROKEN": "持久浏览器会话连续性中断，请恢复同一扫描。",
                "FAILED": "访问策略要求停止当前扫描。",
            }
            blocker = {"code": reason_code, "message": messages.get(status, decision.summary or status)}
            metadata = {**metadata, "access_decision": decision.model_dump(mode="json")}
            return status, blocker, metadata
        except BrainError as error:
            # Access routing must remain truthful even when the remote Brain is
            # not configured. This fallback never claims a CAPTCHA from HTTP.
            if stage.upper() in {"L0", "L1", "HTTP", "PREFLIGHT"}:
                status = "BROWSER_REQUIRED"
            elif reason_code in {"TEMPORARY_PAGE_FAILURE", "BROWSER_NAVIGATION_FAILED"}:
                status = "TEMPORARY_FAILURE"
            elif reason_code == "ACCESS_CHALLENGE" and bool((evidence or {}).get("explicit_challenge_control")):
                status = "HUMAN_REQUIRED"
            else:
                status = "ACCESS_CHANGE_REQUIRED"
            return status, {"code": reason_code, "message": self._blocker_message(status)}, {"status": error.code, "provider_posts": self.brain.post_count}

    def preflight(self, url: str, *, live: bool, output_dir: Path | None = None) -> dict[str, object]:
        normalized = normalize_site_url(url)
        result: dict[str, object] = {
            "schema_version": "website-site-preflight.v2",
            "url": normalized,
            "site_key": site_key_for(normalized),
            "status": "NOT_LIVE" if not live else "READY",
            "network_called": False,
            "provider_posts": 0,
            "next_action": "继续扫描真实公开类目" if live else "仅完成 URL 规范化；生产扫描必须显式 live=true",
        }
        if not live:
            return result
        try:
            client = self.client_factory(source_url=normalized, request_budget=8, timeout=15, request_delay=0.0)
            html = client.get_html(normalized)
            result.update({"status": "READY", "network_called": True, "content_type": "text/html", "http": client.telemetry(), "page_bytes": len(html.encode("utf-8"))})
        except RobotsDenied as error:
            result.update({"status": "ROBOTS_DENIED", "network_called": True, "next_action": "robots.txt 拒绝了该入口，需人工确认官方导出或停止", "blocker": {"code": "ROBOTS_DENIED", "message": str(error)}})
        except AccessControlDetected as error:
            status, blocker, brain = self._access_decision(normalized, stage="PREFLIGHT", reason_code=error.kind.upper())
            result.update({"status": status, "network_called": True, "next_action": blocker["message"], "blocker": blocker, "brain": brain})
        except HttpStatusError as error:
            # 522/5xx、429 和显式访问控制不是“网址不存在”。预检必须把
            # 这类信号交给后续同一站点的可见 L2 会话，否则新建任务会在
            # L2 之前被前端当成普通失败而提前截断。
            if error.status_code in BROWSER_ESCALATION_HTTP_STATUSES or error.status_code == 429 or error.retryable:
                status, blocker, brain = self._access_decision(normalized, stage="PREFLIGHT", reason_code=f"HTTP_{error.status_code}")
                result.update({
                    "status": status,
                    "network_called": True,
                    "next_action": blocker["message"],
                    "blocker": blocker,
                    "brain": brain,
                })
            else:
                result.update({"status": "FAILED", "network_called": True, "next_action": "保留证据并检查站点公开可访问性", "blocker": {"code": f"HTTP_{error.status_code}", "message": str(error)}})
        except (NetworkPolicyError, TimeoutError, OSError, requests.RequestException) as error:
            result.update({"status": "FAILED", "network_called": True, "next_action": "保留证据并检查站点公开可访问性", "blocker": {"code": type(error).__name__.upper(), "message": str(error)[:500]}})
        return result

    def analyze(self, url: str, *, live: bool, output_dir: Path | None = None, fixture: Path | None = None) -> dict[str, object]:
        normalized = normalize_site_url(url)
        site_key = site_key_for(normalized)
        root = Path(output_dir or (self.output_root / "_control" / "site_analysis" / site_key / f"scan_{uuid4().hex}")).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if fixture is not None and fixture.is_file() and self._fixture_allowed():
            receipt = self._fixture_receipt(normalized, site_key, fixture)
            self._write_receipt(root, receipt)
            return receipt
        if not live:
            receipt = self._receipt(normalized, site_key, live=False, status="LIVE_SCAN_REQUIRED", categories=[], evidence={"network_called": False}, blocker={"code": "LIVE_SCAN_REQUIRED", "message": "生产类目扫描必须显式 live=true；离线请求不读取 fixture。"})
            self._write_receipt(root, receipt)
            return receipt
        source_type = classify_source_type(normalized)
        try:
            client = self.client_factory(source_url=normalized, request_budget=48, timeout=25, request_delay=0.15)
            html = client.get_html(normalized)
            source_type = classify_source_type(normalized, html)
            robots = client.robots_sitemaps(normalized)
            categories, signals = self._l0_l1(normalized, html)
            source_category = self._source_scope_category(normalized)
            if source_category is not None:
                # A direct category URL is itself the requested scope.  It is
                # not an anchor in its own page, so retain it explicitly and
                # enrich it from the HTML already fetched for L0/L1.
                self._enrich_count(source_category, html)
                categories.insert(0, source_category)
            sitemap_urls = list(robots[:4])
            if not sitemap_urls:
                sitemap_urls = [urljoin(normalized, "/sitemap.xml")]
            sitemap_hits = []
            for sitemap_url in sitemap_urls[:4]:
                try:
                    sitemap = client.get_sitemap(sitemap_url)
                    sitemap_hits.extend(re.findall(r"<loc>\s*(.*?)\s*</loc>", sitemap, flags=re.I | re.S)[:200])
                except Exception:
                    continue
            for candidate in sitemap_hits:
                candidate_url = urljoin(normalized, html_lib.unescape(candidate).strip())
                if _looks_like_category(candidate_url, "", normalized):
                    categories.append(self._category(candidate_url, candidate_url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title(), None, [{"role": "sitemap", "url": candidate_url}], 0.58))
            magento_taxonomy_evidence: dict[str, object] = {"attempted": False, "level1": 0, "level2": 0}
            if signals.get("platform") == "MAGENTO_PWA":
                official_categories, magento_taxonomy_evidence = self._discover_magento_pwa_taxonomy(client, normalized)
                categories = official_categories + categories
            categories = self._dedupe_categories(self._hierarchize(categories))
            magento_count_evidence: dict[str, object] = {"attempted": False, "resolved": 0, "counted": 0}
            if signals.get("platform") == "MAGENTO_PWA":
                magento_count_evidence = self._enrich_magento_pwa_counts(client, categories, normalized)
            # 数量补全：优先一级类目（页面少、信息密度高），再补二级类目；
            # 一次扫描尽量覆盖全部 UNKNOWN，避免要求用户多次续扫。
            ordered = sorted(categories, key=lambda item: (item.level != 1, item.path))
            unknown_targets = [category for category in ordered[:MAX_COUNT_PROBES] if category.count_kind == "UNKNOWN"]
            if unknown_targets:
                # 预算按待补证类目数自适应，而不是固定小预算导致 PARTIAL。
                probe_client = self.client_factory(
                    source_url=normalized,
                    request_budget=len(unknown_targets) + 8,
                    timeout=25,
                    request_delay=0.15,
                )
                for category in unknown_targets:
                    try:
                        category_html = probe_client.get_html(category.source_url)
                        self._enrich_count(category, category_html)
                    except (AccessControlDetected, RobotsDenied, NetworkPolicyError, HttpStatusError):
                        continue
            # 需要大脑介入的情形：规则未达 READY（低置信/数量 UNKNOWN），或站点为
            # 全平铺一级（无任何二级，可能是 sixpenny 这类需要按部门分组的结构）。
            ambiguous = (not categories
                         or any(item.confidence < 0.65 or item.count_kind == "UNKNOWN" for item in categories)
                         or (bool(categories) and not any(item.level == 2 for item in categories)))
            brain_metadata: dict[str, object] = {"status": "NOT_NEEDED", "provider_posts": 0}
            if source_type == "UNKNOWN":
                try:
                    source_decision, source_metadata = self.brain.reason_source(
                        source_url=normalized,
                        evidence={"signals": signals, "category_paths": [item.path for item in categories[:30]]},
                    )
                    if source_decision.confidence >= 0.6:
                        source_type = source_decision.source_type
                    brain_metadata["source_decision"] = source_decision.model_dump(mode="json")
                    brain_metadata["source_status"] = source_metadata
                    brain_metadata["provider_posts"] = self.brain.post_count
                except BrainNotConfigured:
                    brain_metadata["source_status"] = {"status": "BRAIN_NOT_CONFIGURED"}
                except BrainError as error:
                    brain_metadata["source_status"] = {"status": error.code}
            if ambiguous:
                brain_result, brain_metadata = self._brain_agent_taxonomy(normalized, signals, categories, client)
                if brain_result is not None:
                    brain_metadata["output"] = brain_result.model_dump(mode="json")
                    categories = self._merge_brain(categories, brain_result)
            status = "READY" if categories and all(item.count_kind != "UNKNOWN" for item in categories) else "PARTIAL"
            if not categories and brain_metadata.get("status") == "BRAIN_NOT_CONFIGURED":
                status = "BRAIN_NOT_CONFIGURED"
            elif not categories:
                status = "BROWSER_REQUIRED"
            receipt = self._receipt(normalized, site_key, live=True, status=status, categories=categories, evidence={"l0": signals, "http": client.telemetry(), "sitemaps": sitemap_urls, "sitemap_hits": len(sitemap_hits), "magento_graphql_taxonomy": magento_taxonomy_evidence, "magento_graphql_counts": magento_count_evidence}, brain=brain_metadata, blocker=None if status == "READY" else {"code": str(status), "message": self._blocker_message(str(status))}, source_type=source_type)
        except RobotsDenied as error:
            receipt = self._receipt(normalized, site_key, live=True, status="ROBOTS_DENIED", categories=[], evidence={"network_called": True}, blocker={"code": "ROBOTS_DENIED", "message": str(error)})
        except AccessControlDetected as error:
            status, blocker, brain = self._access_decision(normalized, stage="L1", reason_code=error.kind.upper())
            receipt = self._receipt(normalized, site_key, live=True, status=status, categories=[], evidence={"network_called": True, "access_signal": error.kind}, blocker=blocker, brain=brain)
        except RequestBudgetExceeded as error:
            receipt = self._receipt(normalized, site_key, live=True, status="PARTIAL", categories=[], evidence={"network_called": True}, blocker={"code": "REQUEST_BUDGET_EXCEEDED", "message": str(error)})
        except HttpStatusError as error:
            # Some public catalog edges return a retryable 5xx (notably 522)
            # to a plain HTTP client while the same URL is available in a
            # headed browser.  Escalate only retryable server-side responses;
            # do not treat client errors or rate limits as permission to retry.
            status = "BROWSER_REQUIRED" if error.status_code in BROWSER_ESCALATION_HTTP_STATUSES or error.status_code == 429 or (error.retryable and error.status_code >= 500) else "FAILED"
            receipt = self._receipt(
                normalized,
                site_key,
                live=True,
                status=status,
                categories=[],
                evidence={"network_called": True, "http_status": error.status_code},
                blocker={"code": f"HTTP_{error.status_code}", "message": str(error)},
            )
        except (NetworkPolicyError, OSError, TimeoutError, requests.RequestException) as error:
            # 读超时/连接超时通常意味着站点有反爬（Akamai/PerimeterX 等）把普通 HTTP 挡掉，
            # 但 TCP 可达。这类情况应升级到可见浏览器由人工过一次验证，而不是直接判失败。
            text = str(error).casefold()
            is_timeout = (
                isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ConnectionError, TimeoutError))
                or "read timed out" in text
                or "timed out" in text
                or "connect timeout" in text
            )
            status = "BROWSER_REQUIRED" if is_timeout else "FAILED"
            receipt = self._receipt(normalized, site_key, live=True, status=status, categories=[], evidence={"network_called": True}, blocker={"code": type(error).__name__.upper(), "message": str(error)[:500]})
        self._write_receipt(root, receipt)
        return receipt

    def analyze_browser(self, url: str, *, output_dir: Path, session_dir: Path) -> dict[str, object]:
        """Run compliant L2 taxonomy discovery in one visible persistent session."""

        normalized = normalize_site_url(url)
        site_key = site_key_for(normalized)
        root = Path(output_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        try:
            page_html = NativeBrowserCollector(session_dir).get_html(normalized)
            categories, signals = self._l0_l1(normalized, page_html)
            source_category = self._source_scope_category(normalized)
            if source_category is not None:
                self._enrich_count(source_category, page_html)
                categories.insert(0, source_category)
            magento_taxonomy_evidence: dict[str, object] = {"attempted": False, "level1": 0, "level2": 0}
            if signals.get("platform") == "MAGENTO_PWA":
                try:
                    taxonomy_client = self.client_factory(source_url=normalized, request_budget=32, timeout=20, request_delay=0.1)
                    official_categories, magento_taxonomy_evidence = self._discover_magento_pwa_taxonomy(taxonomy_client, normalized)
                    categories = official_categories + categories
                except (NetworkPolicyError, HttpStatusError, RobotsDenied, RequestBudgetExceeded) as error:
                    magento_taxonomy_evidence = {"attempted": True, "level1": 0, "level2": 0, "errors": [type(error).__name__]}
            categories = self._dedupe_categories(self._hierarchize(categories))
            source_type = classify_source_type(normalized, page_html)
            magento_count_evidence: dict[str, object] = {"attempted": False, "resolved": 0, "counted": 0}
            if signals.get("platform") == "MAGENTO_PWA" and categories:
                try:
                    count_client = self.client_factory(source_url=normalized, request_budget=32, timeout=20, request_delay=0.1)
                    magento_count_evidence = self._enrich_magento_pwa_counts(count_client, categories, normalized)
                except (NetworkPolicyError, HttpStatusError, RobotsDenied, RequestBudgetExceeded) as error:
                    magento_count_evidence = {"attempted": True, "resolved": 0, "counted": 0, "errors": [type(error).__name__]}
            count_probe_blocker: dict[str, object] | None = None
            collector = NativeBrowserCollector(session_dir)
            ordered = sorted(categories, key=lambda item: (item.level != 1, item.path))
            # 浏览器补证一次批量覆盖全部 UNKNOWN（上限 MAX_COUNT_PROBES），
            # 不再每轮只补 4 个导致用户需要反复手动续扫。
            l2_probe_limit = max(1, min(MAX_COUNT_PROBES, int(os.getenv("WEBSITE_L2_COUNT_PROBES", "120"))))
            probe_urls = [
                category.source_url
                for category in ordered[:l2_probe_limit]
                if category.count_kind == "UNKNOWN"
                and category.source_url.rstrip("/").casefold() != normalized.rstrip("/").casefold()
                and _taxonomy_segments(category.source_url, normalized)
            ]
            if probe_urls:
                try:
                    probed_pages = collector.get_html_batch(probe_urls)
                    by_url = {url.rstrip("/").casefold(): html for url, html in probed_pages.items()}
                    for category in ordered:
                        html = by_url.get(category.source_url.rstrip("/").casefold())
                        if html is not None and category.count_kind == "UNKNOWN":
                            self._enrich_count(category, html)
                except (BrowserHumanRequired, BrowserTemporaryFailure, BrowserAccessDenied) as error:
                    # Keep the taxonomy already acquired from the accessible
                    # entry page.  The operator can resume the same scan to
                    # complete bounded count probing after the challenge.
                    count_probe_blocker = {
                        "code": error.reason_code,
                        "message": str(error),
                        "url": error.url,
                        "evidence": error.evidence,
                    }
            brain_metadata: dict[str, object] = {"status": "NOT_NEEDED", "provider_posts": 0}
            if not categories or any(item.confidence < 0.65 for item in categories):
                try:
                    response, brain_metadata = self.brain.reason_taxonomy(
                        source_url=normalized,
                        evidence={"acquisition": "L2_BROWSER", "signals": signals, "categories": [item.model_dump() for item in categories]},
                    )
                    categories = self._merge_brain(categories, response)
                    brain_metadata["output"] = response.model_dump(mode="json")
                except BrainNotConfigured:
                    brain_metadata = {"status": "BRAIN_NOT_CONFIGURED", "provider_posts": self.brain.post_count}
                except BrainError as error:
                    brain_metadata = {"status": error.code, "provider_posts": self.brain.post_count}
            status = "READY" if categories and all(item.count_kind != "UNKNOWN" for item in categories) else "BRAIN_NOT_CONFIGURED" if not categories and brain_metadata.get("status") == "BRAIN_NOT_CONFIGURED" else "PARTIAL"
            if count_probe_blocker is not None:
                status, access_blocker, access_brain = self._access_decision(
                    normalized,
                    stage="L2",
                    reason_code=str(count_probe_blocker.get("code") or "ACCESS_CHALLENGE"),
                    evidence=count_probe_blocker.get("evidence") if isinstance(count_probe_blocker.get("evidence"), dict) else None,
                )
                brain_metadata["access"] = access_brain
            scan_evidence: dict[str, object] = {"acquisition": "L2_BROWSER", "browser_session_dir": str(Path(session_dir).resolve()), "signals": signals, "magento_graphql_taxonomy": magento_taxonomy_evidence, "magento_graphql_counts": magento_count_evidence}
            if count_probe_blocker is not None:
                scan_evidence["count_probe_blocker"] = count_probe_blocker
            receipt = self._receipt(
                normalized,
                site_key,
                live=True,
                status=status,
                categories=categories,
                evidence=scan_evidence,
                blocker=access_blocker if count_probe_blocker is not None else (None if status == "READY" else {"code": status, "message": self._blocker_message(status)}),
                brain=brain_metadata,
                source_type=source_type,
            )
        except BrowserHumanRequired as error:
            status, blocker, access_brain = self._access_decision(normalized, stage="L2", reason_code=error.reason_code, evidence=error.evidence)
            receipt = self._receipt(
                normalized,
                site_key,
                live=True,
                status=status,
                categories=[],
                evidence={"acquisition": "L2_BROWSER", "browser_session_dir": str(error.session_dir), "current_url": error.url, "access_evidence": error.evidence},
                blocker=blocker,
                brain=access_brain,
            )
        except BrowserTemporaryFailure as error:
            status, blocker, access_brain = self._access_decision(normalized, stage="L2", reason_code=error.reason_code, evidence=error.evidence)
            receipt = self._receipt(
                normalized,
                site_key,
                live=True,
                status=status,
                categories=[],
                evidence={"acquisition": "L2_BROWSER", "current_url": error.url, "access_evidence": error.evidence},
                blocker=blocker,
                brain=access_brain,
            )
        except BrowserAccessDenied as error:
            status, blocker, access_brain = self._access_decision(normalized, stage="L2", reason_code=error.reason_code, evidence=error.evidence)
            receipt = self._receipt(
                normalized,
                site_key,
                live=True,
                status=status,
                categories=[],
                evidence={"acquisition": "L2_BROWSER", "current_url": error.url, "access_evidence": error.evidence},
                blocker=blocker,
                brain=access_brain,
            )
        except BrowserRuntimeMissing as error:
            receipt = self._receipt(
                normalized,
                site_key,
                live=True,
                status="BROWSER_RUNTIME_NOT_INSTALLED",
                categories=[],
                evidence={"acquisition": "L2_BROWSER", "browser_session_dir": str(Path(session_dir).resolve())},
                blocker={"code": error.code, "message": str(error)},
            )
        self._write_receipt(root, receipt)
        return receipt

    def _l0_l1(self, source_url: str, page_html: str) -> tuple[list[TaxonomyCategoryContract], dict[str, object]]:
        parser = _NavigationParser()
        parser.feed(page_html)
        nav_tree = parser.build_nav_tree()
        # 导航层级索引：绝对化 href → 节点；节点 → 父节点绝对 href；父级 href 集合。
        abs_nodes: dict[str, _NavNode] = {}
        parent_abs: dict[int, str] = {}
        parent_hrefs: set[str] = set()
        for node in parser.nav_nodes:
            if node.excluded or node.rel_depth < 1 or not node.href:
                continue
            abs_nodes.setdefault(_href_key(urljoin(source_url, node.href)), node)
            if node.parent_href:
                p_abs = urljoin(source_url, node.parent_href)
                parent_abs[id(node)] = p_abs
                parent_hrefs.add(_href_key(p_abs))
        categories: list[TaxonomyCategoryContract] = []
        nav_items: list[dict[str, object]] = []
        for anchor in parser.anchors:
            href = urljoin(source_url, anchor["href"])
            if not _looks_like_category(href, anchor["text"], source_url):
                continue
            count = _count_from_nav_text(anchor["text"] + " " + anchor["count"])
            evidence = [{"role": "navigation", "source_url": href, "label": anchor["text"]}]
            if count is not None:
                evidence.append({"role": "visible_count", "value": count, "source_url": source_url})
            # 导航优先：大标签（有子项）→ 一级；其下小标签 → 二级。仅当检测到
            # 嵌套结构（nav_tree 非空）才启用，否则走 URL 路径段数兜底。
            nav_level = nav_parent_path = nav_parent_href = None
            node = abs_nodes.get(_href_key(href))
            if node is not None and nav_tree:
                p_href = parent_abs.get(id(node))
                if p_href is not None and _href_key(p_href) in abs_nodes:
                    nav_level = 2
                    nav_parent_href = p_href
                    nav_parent_path = urlsplit(p_href).path
                elif _href_key(href) in parent_hrefs:
                    nav_level = 1
            categories.append(self._category(href, anchor["text"], count, evidence, 0.78 if count is not None else 0.62, nav_level=nav_level, nav_parent_path=nav_parent_path, nav_parent_href=nav_parent_href))
            nav_items.append({"path": urlsplit(href).path, "label": anchor["text"], "count": count})
        for item in _json_ld_values(page_html):
            if str(item.get("@type", "")).casefold() in {"itemlist", "collectionpage"}:
                count = item.get("numberOfItems")
                if isinstance(count, str) and count.isdigit():
                    count = int(count)
                if isinstance(count, int):
                    name = str(item.get("name") or "Catalog")
                    categories.append(self._category(source_url, name, count, [{"role": "json_ld", "type": item.get("@type"), "numberOfItems": count}], 0.95))
        signals = {
            "platform": self._platform(page_html),
            "navigation_links": len(parser.anchors),
            "navigation_items": nav_items[:40],
            "navigation_tree": nav_tree,
            "navigation_hierarchy_used": bool(nav_tree),
            "json_ld_blocks": len(_json_ld_values(page_html)),
            "html_bytes": len(page_html.encode("utf-8")),
            "page_has_product_signals": bool(PRODUCT_RE.search(page_html)),
        }
        return self._dedupe_categories(self._hierarchize(categories)), signals

    def _enrich_count(self, category: TaxonomyCategoryContract, page_html: str) -> None:
        json_items = _json_ld_values(page_html)
        for item in json_items:
            count = item.get("numberOfItems")
            if isinstance(count, str) and count.isdigit():
                count = int(count)
            if isinstance(count, int) and count >= 0:
                category.count_value = count
                category.count_kind = "EXACT"
                category.evidence.append({"role": "json_ld", "numberOfItems": count, "source_url": category.source_url})
                category.confidence = max(category.confidence, 0.94)
                return
        visible_count = _count_from_text(re.sub(r"<[^>]+>", " ", page_html))
        # 0 通常不是真实类目数量（空状态文案/JS 未渲染的误读），不作为权威 EXACT，
        # 回退到产品 JSON-LD / 产品链接样本估算。
        if visible_count is not None and visible_count > 0:
            category.count_value = visible_count
            category.count_kind = "EXACT"
            category.evidence.append({"role": "visible_count", "value": visible_count, "source_url": category.source_url})
            category.confidence = max(category.confidence, 0.9)
            return
        product_json_ld = self._product_json_ld_count(page_html)
        if product_json_ld:
            category.count_value = product_json_ld
            category.count_kind = "ESTIMATED"
            category.evidence.append({"role": "bounded_product_jsonld_sample", "value": product_json_ld, "source_url": category.source_url})
            category.confidence = max(category.confidence, 0.7)
            return
        product_links = len(set(PRODUCT_RE.findall(page_html)))
        if product_links:
            category.count_value = product_links
            category.count_kind = "ESTIMATED"
            category.evidence.append({"role": "bounded_product_link_sample", "value": product_links, "source_url": category.source_url})
            category.confidence = max(category.confidence, 0.7)

    def _agent_tool_executor(self, client: SafeHttpClient, source_url: str, *, count_results: dict[str, dict[str, object]] | None = None) -> Callable[[str, dict[str, object]], dict[str, object]]:
        def execute(name: str, args: dict[str, object]) -> dict[str, object]:
            try:
                return self._agent_dispatch(name, args, client, source_url, count_results=count_results)
            except RequestBudgetExceeded as error:
                raise AgentToolError("REQUEST_BUDGET_EXCEEDED", str(error)) from error
            except (RobotsDenied, AccessControlDetected, HttpStatusError, NetworkPolicyError) as error:
                raise AgentToolError(type(error).__name__, str(error)[:300]) from error
        return execute

    def _agent_dispatch(self, name: str, args: dict[str, object], client: SafeHttpClient, source_url: str, *, count_results: dict[str, dict[str, object]] | None = None) -> dict[str, object]:
        if name == "browse":
            url = str(args.get("url") or "").strip()
            if not url:
                raise AgentToolError("BAD_ARGS", "browse requires url")
            html = client.get_html(url)
            parser = _NavigationParser()
            parser.feed(html)
            visible = re.sub(r"<[^>]+>", " ", html)
            visible = " ".join(visible.split())
            nav = [
                {"href": urljoin(url, anchor["href"]), "label": anchor["text"]}
                for anchor in parser.anchors[:40]
            ]
            return {
                "url": url,
                "text_snippet": visible[:1500],
                "nav_items": nav,
                "nav_tree": parser.build_nav_tree()[:10],
                "html_bytes": len(html.encode("utf-8")),
            }
        if name == "list_categories":
            url = str(args.get("url") or source_url or "").strip()
            if not url:
                raise AgentToolError("BAD_ARGS", "list_categories requires url")
            html = client.get_html(url)
            cats, _ = self._l0_l1(url, html)
            return {"url": url, "categories": [c.model_dump(mode="json") for c in cats[:30]]}
        if name == "get_count":
            url = str(args.get("url") or "").strip()
            if not url:
                raise AgentToolError("BAD_ARGS", "get_count requires url")
            html = client.get_html(url)
            category = self._category(
                url, url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title() or "Catalog", None,
                [{"role": "agent_get_count", "source_url": url}], 0.5,
            )
            self._enrich_count(category, html)
            outcome = {
                "url": url,
                "count_value": category.count_value,
                "count_kind": category.count_kind,
                "evidence_roles": [str(e.get("role") or "") for e in category.evidence],
            }
            if count_results is not None:
                # 记录 agent 探到的数量，循环后回填到类目，即使模型没有在 finish
                # 里复述 get_count 结果也不丢失（配合 _merge_brain 规则数量回填）。
                count_results[url.rstrip("/").casefold()] = outcome
            return outcome
        if name == "escalate_browser":
            return {"status": "BROWSER_REQUIRED", "url": str(args.get("url") or ""), "reason": "agent requested visible browser escalation; not treated as a CAPTCHA claim"}
        raise AgentToolError("UNKNOWN_TOOL", f"unknown agent tool {name!r}")

    @staticmethod
    def _apply_agent_counts(categories: list[TaxonomyCategoryContract], count_results: dict[str, dict[str, object]]) -> None:
        """Backfill counts the agent discovered via get_count onto rule categories.

        Keeps agent-probed EXACT/ESTIMATED counts even when the model omits them
        from the `finish` payload; `_merge_brain` then keeps them via its rule
        count backfill. UNKNOWN/zero results are not written (no information).
        """
        for category in categories:
            if category.count_kind != "UNKNOWN":
                continue
            outcome = count_results.get(category.source_url.rstrip("/").casefold())
            if not outcome:
                continue
            value = outcome.get("count_value")
            kind = str(outcome.get("count_kind") or "UNKNOWN").upper()
            if isinstance(value, int) and value > 0 and kind in ("EXACT", "ESTIMATED"):
                category.count_value = value
                category.count_kind = kind
                category.evidence.append({"role": "agent_get_count", "value": value, "source_url": category.source_url})
                category.confidence = max(category.confidence, 0.7)

    def _brain_agent_taxonomy(
        self,
        source_url: str,
        signals: dict[str, object],
        categories: list[TaxonomyCategoryContract],
        client: SafeHttpClient,
    ) -> tuple[BrainTaxonomyResponse | None, dict[str, object]]:
        """Run the Brain as an agent to build the taxonomy; fall back to rules."""
        if (
            self.brain.settings.local_agent_mode
            or not self.brain.settings.configured
            or not self.brain.settings.agent_enabled
        ):
            return None, {"status": "BRAIN_NOT_CONFIGURED", "provider_posts": self.brain.post_count}
        remaining_unknown = [item.path for item in categories if item.count_kind == "UNKNOWN"]
        count_results: dict[str, dict[str, object]] = {}
        input_payload: dict[str, object] = {
            "source_url": source_url,
            "task": "Produce the authoritative two-level furniture taxonomy with per-category counts.",
            "signals": signals,
            "navigation_items": signals.get("navigation_items") or [],
            "navigation_tree": signals.get("navigation_tree") or [],
            "categories": [item.model_dump() for item in categories[:30]],
            "remaining_unknown_counts": remaining_unknown[:40],
        }
        system_prompt = (
            "You are an autonomous furniture-site taxonomy analyst for a compliant product-mapping workflow. "
            "Use browse / list_categories / get_count to explore the public site. Produce a clean two-level "
            "furniture taxonomy: level=1 departments (Seating / Tables / Beds / Rugs / Lighting ...) and level=2 "
            "their child categories (Chairs / Sofas ...). Every level=2 category must set parent_path to its "
            "department. Fill count_value / count_kind (EXACT / ESTIMATED) for as many categories as possible, "
            "preferring get_count over guessing. Never invent counts. When finished, call finish with "
            "{categories: [{native_name, canonical_name, path, source_url, count_value, count_kind, level, parent_path}], reasoning}. "
            "When navigation_tree is present it already encodes the site's department->category DOM nesting; "
            "honor that nesting for level/parent_path. Stay on the same site and respect the access guidance in tool errors."
        )
        try:
            result = self.brain.run_agent_loop(
                system_prompt=system_prompt,
                input_payload=input_payload,
                tools=_AGENT_TOOLS,
                tool_executor=self._agent_tool_executor(client, source_url, count_results=count_results),
                options=AgentLoopOptions(
                    response_schema=BrainTaxonomyResponse,
                    finish_tool_name="finish",
                    max_steps=self.brain.settings.agent_max_steps,
                ),
            )
        except BrainError as error:
            return None, {"status": error.code, "provider_posts": self.brain.post_count}
        # 回填 agent 探到的数量到当前类目（即使模型没在 finish 复述也不丢失）。
        self._apply_agent_counts(categories, count_results)
        receipt = {
            "status": "AGENT_READY" if result.validated is not None else result.stopped_reason,
            "stopped_reason": result.stopped_reason,
            "turns": result.turns,
            "tool_calls": [
                {"name": call.get("name"), "arguments": call.get("arguments"), "result_status": call.get("result_status")}
                for call in result.tool_calls
            ],
            "provider_posts": result.provider_posts,
        }
        if result.validated is not None:
            return result.validated, receipt
        # 大脑 agent 未收敛：回退到一次性 reason_taxonomy，再失败则走规则兜底（保证摸底有产出）。
        try:
            response, brain_metadata = self.brain.reason_taxonomy(source_url=source_url, evidence={
                "signals": signals,
                "navigation_items": signals.get("navigation_items") or [],
                "navigation_tree": signals.get("navigation_tree") or [],
                "categories": [item.model_dump() for item in categories[:30]],
            })
            brain_metadata["agent_loop"] = receipt
            return response, brain_metadata
        except BrainError as error:
            return None, {"status": error.code, "provider_posts": self.brain.post_count, "agent_loop": receipt}

    @staticmethod
    def _discover_magento_pwa_taxonomy(client: SafeHttpClient, source_url: str) -> tuple[list[TaxonomyCategoryContract], dict[str, object]]:
        """Read the official two-level Magento category tree through public GraphQL."""
        endpoint = urljoin(source_url, "/graphql")
        parsed = urlsplit(endpoint)
        candidates = [endpoint]
        if parsed.hostname and not parsed.hostname.casefold().startswith("www."):
            candidates.append(urlunsplit((parsed.scheme, f"www.{parsed.netloc}", parsed.path, "", "")))
        active_endpoint: str | None = None

        def post(payload: dict[str, object]) -> dict:
            nonlocal active_endpoint
            last_error: Exception | None = None
            for candidate in ([active_endpoint] if active_endpoint else candidates):
                if not candidate:
                    continue
                try:
                    result = client.post_json(candidate, payload)
                    active_endpoint = candidate
                    return result
                except (NetworkPolicyError, HttpStatusError, RobotsDenied, RequestBudgetExceeded) as error:
                    last_error = error
            if last_error is not None:
                raise last_error
            raise NetworkPolicyError("Magento GraphQL endpoint was not available")

        root_response = post({"query": "query TaxonomyRoot { storeConfig { root_category_id } }", "variables": {}})
        root_data = root_response.get("data") if isinstance(root_response, dict) else None
        store_config = root_data.get("storeConfig") if isinstance(root_data, dict) else None
        root_id = str(store_config.get("root_category_id") or "") if isinstance(store_config, dict) else ""
        if not root_id.isdigit():
            return [], {"attempted": True, "endpoint": active_endpoint or endpoint, "level1": 0, "level2": 0, "errors": ["ROOT_CATEGORY_MISSING"]}

        root_query = (
            "query RootCategories($parent: String!) { categoryList(filters: {parent_id: {eq: $parent}}) "
            "{ id name url_path include_in_menu level children_count } }"
        )
        root_response = post({"query": root_query, "variables": {"parent": root_id}})
        root_data = root_response.get("data") if isinstance(root_response, dict) else None
        raw_roots = root_data.get("categoryList") if isinstance(root_data, dict) else None
        raw_roots = raw_roots if isinstance(raw_roots, list) else []
        roots: list[tuple[str, TaxonomyCategoryContract]] = []
        for item in raw_roots:
            if not isinstance(item, dict):
                continue
            category_id = str(item.get("id") or "").strip()
            path = str(item.get("url_path") or "").strip("/")
            name = str(item.get("name") or "").strip()
            path_words = set(re.split(r"[-_/]", path.casefold()))
            if not category_id.isdigit() or not path or not name or not (path_words & CATEGORY_WORDS):
                continue
            category = NativeSiteAnalyzer._category(
                urljoin(source_url, f"/{path}"), name, None,
                [{"role": "magento_graphql_category_tree", "category_id": category_id, "source_url": active_endpoint or endpoint}],
                0.99,
            )
            category.category_id = f"magento_{category_id}"
            roots.append((category_id, category))

        children: list[TaxonomyCategoryContract] = []
        if roots:
            variables = {f"p{index}": category_id for index, (category_id, _) in enumerate(roots)}
            definitions = ", ".join(f"$p{index}: String!" for index in range(len(roots)))
            fields = " ".join(
                f"c{index}: categoryList(filters: {{parent_id: {{eq: $p{index}}}}}) {{ id name url_path include_in_menu level children_count }}"
                for index in range(len(roots))
            )
            child_response = post({"query": f"query ChildCategories({definitions}) {{ {fields} }}", "variables": variables})
            child_data = child_response.get("data") if isinstance(child_response, dict) else None
            child_data = child_data if isinstance(child_data, dict) else {}
            for index, (_, parent) in enumerate(roots):
                raw_children = child_data.get(f"c{index}")
                for item in raw_children if isinstance(raw_children, list) else []:
                    if not isinstance(item, dict):
                        continue
                    category_id = str(item.get("id") or "").strip()
                    path = str(item.get("url_path") or "").strip("/")
                    name = str(item.get("name") or "").strip()
                    if not category_id.isdigit() or not path or not name:
                        continue
                    category = NativeSiteAnalyzer._category(
                        urljoin(source_url, f"/{path}"), name, None,
                        [{"role": "magento_graphql_category_tree", "category_id": category_id, "parent_category_id": parent.category_id, "source_url": active_endpoint or endpoint}],
                        0.99,
                    )
                    category.category_id = f"magento_{category_id}"
                    children.append(category)
        return [category for _, category in roots] + children, {
            "attempted": True,
            "endpoint": active_endpoint or endpoint,
            "root_category_id": root_id,
            "level1": len(roots),
            "level2": len(children),
            "errors": [],
        }

    @staticmethod
    def _enrich_magento_pwa_counts(client: SafeHttpClient, categories: list[TaxonomyCategoryContract], source_url: str) -> dict[str, object]:
        """Resolve Magento category URLs and authoritative totals in bounded read-only batches."""

        endpoint = urljoin(source_url, "/graphql")
        parsed_endpoint = urlsplit(endpoint)
        endpoint_candidates = [endpoint]
        if parsed_endpoint.hostname and not parsed_endpoint.hostname.casefold().startswith("www."):
            endpoint_candidates.append(urlunsplit((parsed_endpoint.scheme, f"www.{parsed_endpoint.netloc}", parsed_endpoint.path, "", "")))
        active_endpoint: str | None = None

        def post_graphql(payload: dict[str, object]) -> dict:
            nonlocal active_endpoint
            candidates = [active_endpoint] if active_endpoint else endpoint_candidates
            last_error: Exception | None = None
            for candidate in candidates:
                if not candidate:
                    continue
                try:
                    response = client.post_json(candidate, payload)
                    active_endpoint = candidate
                    return response
                except (NetworkPolicyError, HttpStatusError, RobotsDenied, RequestBudgetExceeded) as error:
                    last_error = error
            if last_error is not None:
                raise last_error
            raise NetworkPolicyError("Magento GraphQL endpoint was not available")
        resolved = 0
        counted = 0
        errors: list[str] = []
        rejected: set[int] = set()
        for offset in range(0, len(categories), 20):
            chunk = categories[offset:offset + 20]
            variables = {f"u{index}": urlsplit(category.source_url).path.lstrip("/") for index, category in enumerate(chunk)}
            definitions = ", ".join(f"$u{index}: String!" for index in range(len(chunk)))
            fields = " ".join(f"c{index}: urlResolver(url: $u{index}) {{ id type relative_url }}" for index in range(len(chunk)))
            try:
                resolution = post_graphql({
                    "query": f"query ResolveCategoryUrls({definitions}) {{ {fields} }}",
                    "variables": variables,
                })
            except (NetworkPolicyError, HttpStatusError, RobotsDenied, RequestBudgetExceeded) as error:
                errors.append(type(error).__name__)
                break
            data = resolution.get("data") if isinstance(resolution, dict) else None
            data = data if isinstance(data, dict) else {}
            valid: list[tuple[int, TaxonomyCategoryContract, str]] = []
            for index, category in enumerate(chunk):
                item = data.get(f"c{index}")
                if not isinstance(item, dict) or str(item.get("type") or "").upper() != "CATEGORY":
                    rejected.add(id(category))
                    continue
                category_id = str(item.get("id") or "").strip()
                if not category_id.isdigit():
                    continue
                valid.append((index, category, category_id))
                resolved += 1
            if not valid:
                continue
            count_variables = {f"i{index}": category_id for index, _, category_id in valid}
            count_definitions = ", ".join(f"$i{index}: String!" for index, _, _ in valid)
            count_fields = " ".join(
                f"c{index}: products(filter: {{category_id: {{eq: $i{index}}}}}, pageSize: 1, currentPage: 1) {{ total_count }}"
                for index, _, _ in valid
            )
            try:
                totals = post_graphql({
                    "query": f"query CategoryProductTotals({count_definitions}) {{ {count_fields} }}",
                    "variables": count_variables,
                })
            except (NetworkPolicyError, HttpStatusError, RobotsDenied, RequestBudgetExceeded) as error:
                errors.append(type(error).__name__)
                continue
            total_data = totals.get("data") if isinstance(totals, dict) else None
            total_data = total_data if isinstance(total_data, dict) else {}
            for index, category, category_id in valid:
                item = total_data.get(f"c{index}")
                total = item.get("total_count") if isinstance(item, dict) else None
                if not isinstance(total, int) or total < 0:
                    continue
                category.count_value = total
                category.count_kind = "EXACT"
                category.confidence = max(category.confidence, 0.98)
                category.evidence.append({
                    "role": "magento_graphql_total_count",
                    "category_id": category_id,
                    "value": total,
                    "source_url": active_endpoint or endpoint,
                })
                counted += 1
        if rejected:
            categories[:] = [category for category in categories if id(category) not in rejected]
        return {"attempted": True, "endpoint": active_endpoint or endpoint, "resolved": resolved, "counted": counted, "errors": errors[:8]}

    @staticmethod
    def _product_json_ld_count(page_html: str) -> int:
        """Count distinct Product objects present in the fetched category page.

        A page-local Product list is only a bounded visible sample unless the
        page also exposes an explicit total.  It therefore becomes ESTIMATED,
        never an invented EXACT total.
        """

        identities: set[str] = set()
        anonymous = 0
        for item in _json_ld_values(page_html):
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if not any(str(value).casefold() == "product" for value in types):
                continue
            identity = item.get("@id") or item.get("sku") or item.get("mpn") or item.get("url") or item.get("name")
            if identity:
                identities.add(str(identity).strip().casefold())
            else:
                anonymous += 1
        return len(identities) + anonymous

    @staticmethod
    def _source_scope_category(source_url: str) -> TaxonomyCategoryContract | None:
        segments = _taxonomy_segments(source_url, source_url)
        if not segments:
            return None
        path = "/" + "/".join(segments)
        native = segments[-1].replace("-", " ").replace("_", " ").title()
        return TaxonomyCategoryContract(
            category_id=f"cat_{hashlib.sha256(path.encode('utf-8')).hexdigest()[:16]}",
            native_name=_canonical_name(native, path),
            canonical_name=_canonical_name(native, path),
            path=path,
            source_url=source_url,
            count_value=None,
            count_kind="UNKNOWN",
            evidence=[{"role": "source_scope", "source_url": source_url}],
            confidence=0.82,
            level=2 if len(segments) == 2 else 1,
            parent_path="/" + segments[0] if len(segments) == 2 else None,
        )

    @staticmethod
    def _category(source_url: str, native: str, count: int | None, evidence: list[dict[str, object]], confidence: float, *, nav_level: int | None = None, nav_parent_path: str | None = None, nav_parent_href: str | None = None) -> TaxonomyCategoryContract:
        path = urlsplit(source_url).path or "/"
        segments = [part for part in path.split("/") if part]
        if nav_level is not None:
            level = nav_level
            parent_path = nav_parent_path
            evidence = list(evidence) + [{"role": "nav_tree", "level": level, "parent_path": nav_parent_path, "parent_href": nav_parent_href}]
        else:
            level = 2 if len(segments) >= 2 else 1
            parent_path = "/" + segments[0] if level == 2 else None
        return TaxonomyCategoryContract(category_id=f"cat_{hashlib.sha256(path.encode('utf-8')).hexdigest()[:16]}", native_name=_canonical_name(native, path), canonical_name=_canonical_name(native, path), path=path, source_url=source_url, count_value=count, count_kind="EXACT" if count is not None else "UNKNOWN", evidence=evidence, confidence=confidence, level=level, parent_path=parent_path)

    @staticmethod
    def _hierarchize(categories: list[TaxonomyCategoryContract]) -> list[TaxonomyCategoryContract]:
        """两级类目模型：过滤营销页、丢弃三级及更深的路径，标注 level/parent_path。"""
        # sitemap 弱证据（confidence<0.6，无数量）来源的一级类目需匹配类目词白名单，
        # 否则视为营销/主题页（如 /fall-edit、/nancy-meyers-edit、/shop-the-catalog）丢弃；
        # 导航等强证据来源的一级类目保留原样。
        slug_words = CATEGORY_WORDS | {
            "sleepers", "sleeper", "sectionals", "sectional", "mattresses", "mattress", "bedding",
            "mirrors", "mirror", "pillows", "pillow", "benches", "bench", "ottomans", "ottoman",
            "kids", "nursery", "entryway", "hallway", "kitchen", "bathroom", "accents", "shelving",
            "cabinets", "cabinet", "dressers", "dresser", "nightstands", "nightstand", "consoles",
            "sideboards", "bookcases", "bookcase", "stools", "stool", "desks", "desk", "rugs", "rug",
        }
        output: list[TaxonomyCategoryContract] = []
        output_index: dict[tuple[int, str], int] = {}
        for category in categories:
            segments = [part for part in category.path.split("/") if part]
            # 剥离多语言/地区前缀（/en/c/… 中的 en），再剥离目录前缀
            #（catalog/shop/collections/products/c…），真正的部门在其后。
            while segments and segments[0].casefold() in LOCALE_SEGMENTS:
                segments = segments[1:]
            while segments and segments[0].casefold() in _CATEGORY_ROOTS:
                segments = segments[1:]
            # Shopify 内容路由（/pages/xxx、/blogs/xxx）是营销/推荐页，
            # 不是商品类目；混进两级结构会形成假二级（如"おすすめベッド12選"）。
            if segments and segments[0].casefold() in {"pages", "blogs"}:
                continue
            if not segments or len(segments) > 2:
                # 路径深度超过两级（如 /bedroom/all-beds/mattresses）按两级模型忽略。
                continue
            # 页脚/工具/非商品页（contact-us、terms-of-sale、about-us、clearance、fabrics…）直接排除。
            if any((p := part.strip().casefold()) in UTILITY_PATH_WORDS
                   or any(len(w) >= 5 and w in p for w in UTILITY_PATH_WORDS)
                   for part in segments):
                continue
            # 拒绝 JS 残留/纯数字/特殊符号路径段（如 /void(0)、/O_LC('')、/18889224119）：
            # 合法类目 slug 只含小写字母、数字、连字符、下划线、点，且不能是纯数字。
            if any(not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", part.strip().casefold())
                   or part.strip().isdigit()
                   for part in segments):
                continue
            normalized_path = "/" + "/".join(segments)
            category.path = normalized_path
            last = segments[-1].casefold()
            if last in MARKETING_SEGMENTS or last.endswith("-collection") or last.endswith("-collections"):
                # 营销主题页（如 /alexander-collection）不是商品类目。
                continue
            # 层级判定优先级：大脑(brain_child) > 导航树(nav_tree) > URL 路径段数。
            if category.level == 2 and category.parent_path and _has_brain_parent(category):
                # Brain 明确输出的父子关系被尊重，父路径做前缀剥离与规范化后的父类目对齐。
                category.parent_path = _normalize_parent_path(category.parent_path)
            elif (nav := _nav_tree_evidence(category)) is not None:
                # 导航 DOM 嵌套：大标签(有子项)=一级；其下小标签=二级，父指向大标签。
                category.level = int(nav.get("level") or (2 if nav.get("parent_path") else 1))
                if category.level == 2 and nav.get("parent_path"):
                    category.parent_path = _normalize_parent_path(str(nav["parent_path"]))
                else:
                    category.parent_path = None
            else:
                category.level = 2 if len(segments) == 2 else 1
                category.parent_path = "/" + segments[0] if category.level == 2 else None
            if category.level == 1:
                words = set(re.split(r"[-_]", last))
                is_direct_scope = any(str(item.get("role") or "") == "source_scope" for item in category.evidence)
                has_nav_scope = any(str(item.get("role") or "") == "nav_tree" for item in category.evidence)
                # 导航强制的一级（大标签，如 living/office）视为导航权威证据，免于家具词白名单。
                if not (is_direct_scope or has_nav_scope) and not (words & slug_words):
                    continue
            # 同一规范化类目在多个地区（castlery 的 /sg /ca /au /us）重复时，
            # 保留数量最多者作为代表，并合并证据，避免类目清单里出现重复。
            dedup_key = (category.level, normalized_path.casefold())
            existing_index = output_index.get(dedup_key)
            if existing_index is not None:
                existing = output[existing_index]
                if (category.count_value or 0) > (existing.count_value or 0):
                    if existing.evidence:
                        category.evidence = list(category.evidence or []) + list(existing.evidence)
                    output[existing_index] = category
                else:
                    existing.evidence = list(existing.evidence or []) + list(category.evidence or [])
                continue
            output_index[dedup_key] = len(output)
            output.append(category)
        return NativeSiteAnalyzer._coarsen(output)

    @staticmethod
    def _coarsen(categories: list[TaxonomyCategoryContract]) -> list[TaxonomyCategoryContract]:
        """把每个一级部门下过于细碎的二级合并成粗分类（沙发/椅子/桌子/柜子…）。

        一级类目原样保留；二级按 (父路径, 粗分类) 归组，组内以商品数最多的二级为
        代表（保留其 category_id/path/source_url），canonical_name 改为粗分类标签，
        count_value 取组内之和，证据合并。这样不丢失商品总量，又让二级不细碎。
        合并以 path 的 slug 为据，重复执行是幂等的。
        """
        level1 = [c for c in categories if c.level == 1]

        grouped: dict[tuple[str | None, str], list[TaxonomyCategoryContract]] = {}
        for c in categories:
            if c.level != 2:
                continue
            slug = c.path.rstrip("/").rsplit("/", 1)[-1]
            key = (c.parent_path, _coarse_group(slug))
            grouped.setdefault(key, []).append(c)

        merged: list[TaxonomyCategoryContract] = []
        for (parent, name), members in grouped.items():
            rep = max(members, key=lambda m: m.count_value or 0)
            total = sum(m.count_value or 0 for m in members)
            evidence = list(rep.evidence)
            scope_urls: list[str] = []
            for other in members:
                if other.source_url and other.source_url not in scope_urls:
                    scope_urls.append(other.source_url)
                for entry in other.evidence:
                    if not isinstance(entry, dict) or entry.get("role") != "coarse_scope_members":
                        continue
                    values = entry.get("urls")
                    if isinstance(values, list):
                        for value in values:
                            value = str(value).strip()
                            if value and value not in scope_urls:
                                scope_urls.append(value)
                if other is not rep:
                    evidence.extend(other.evidence)
            evidence.append({
                "role": "coarse_scope_members",
                "parent_path": parent,
                "coarse_group": name,
                "urls": scope_urls,
                "category_ids": [member.category_id for member in members],
            })
            coarse = rep.model_copy(deep=True)
            coarse.canonical_name = CATEGORY_COARSE_LABELS.get(name, name.title())
            # A direct category URL is an operator-selected scope.  Preserve
            # its native label (e.g. "Vanities") instead of collapsing it to
            # the broader coarse group ("Bath").
            if any(any(str(item.get("role") or "") == "source_scope" for item in member.evidence) for member in members):
                coarse.canonical_name = rep.canonical_name
            # A merged child total is authoritative only when every member is
            # counted.  Summing known children while silently omitting an
            # UNKNOWN child turns a partial total into a false exact/estimate.
            if all(m.count_kind == "EXACT" and m.count_value is not None for m in members):
                coarse.count_value = total
                coarse.count_kind = "EXACT"
            elif all(m.count_kind != "UNKNOWN" and m.count_value is not None for m in members):
                coarse.count_value = total
                coarse.count_kind = "ESTIMATED"
            else:
                coarse.count_value = None
                coarse.count_kind = "UNKNOWN"
            coarse.level = 2
            coarse.parent_path = parent
            coarse.evidence = evidence
            merged.append(coarse)

        return sorted(level1 + merged, key=lambda c: (len([p for p in c.path.split("/") if p]), c.path))

    @staticmethod
    def _dedupe_categories(categories: list[TaxonomyCategoryContract]) -> list[TaxonomyCategoryContract]:
        output: list[TaxonomyCategoryContract] = []
        seen: set[str] = set()
        for category in categories:
            key = category.source_url.rstrip("/").casefold()
            if key in seen or not category.native_name.strip():
                continue
            seen.add(key)
            output.append(category)
        # 浅层（一级）优先排序：截断上限触发时优先保留大类目，子类目其次。
        output.sort(key=lambda item: (len([part for part in item.path.split("/") if part]), item.path))
        return output[:100]

    @staticmethod
    def _merge_brain(current: list[TaxonomyCategoryContract], brain: BrainTaxonomyResponse) -> list[TaxonomyCategoryContract]:
        if not brain.categories:
            return NativeSiteAnalyzer._dedupe_categories(NativeSiteAnalyzer._hierarchize(current))
        rules_by_url = {c.source_url.rstrip("/").casefold(): c for c in current}
        merged_by_url: dict[str, TaxonomyCategoryContract] = {}
        # 大脑输出为主：它基于完整导航语义给出了权威的部门→子类目结构。
        for item in brain.categories:
            if item.count_kind == "EXACT" and item.count_value is None:
                item.count_kind = "UNKNOWN"
            # 规则已确定的 EXACT/ESTIMATED 数量回填，避免大脑对精确数字的偏差。
            rule = rules_by_url.get(item.source_url.rstrip("/").casefold())
            if rule is not None and rule.count_kind in ("EXACT", "ESTIMATED") and rule.count_value is not None:
                item.count_value = rule.count_value
                item.count_kind = rule.count_kind
                item.evidence = list(item.evidence) + list(rule.evidence)
            if item.level == 2 and item.parent_path:
                item.evidence = list(item.evidence) + [{"role": "brain_child"}]
            merged_by_url[item.source_url.rstrip("/").casefold()] = item
        # 大脑未覆盖的规则类目补上，避免遗漏。
        for url, rule in rules_by_url.items():
            if url not in merged_by_url:
                merged_by_url[url] = rule
        return NativeSiteAnalyzer._dedupe_categories(NativeSiteAnalyzer._hierarchize(list(merged_by_url.values())))

    @staticmethod
    def _platform(page_html: str) -> str:
        lowered = page_html.casefold()
        if 'id="root"' in lowered and re.search(r"/client\.[a-f0-9]+\.js", lowered) and "data-media-backend" in lowered:
            return "MAGENTO_PWA"
        if "cdn.shopify.com" in lowered or "shopify" in lowered:
            return "SHOPIFY"
        if "woocommerce" in lowered:
            return "WOOCOMMERCE"
        if "__next_data__" in lowered or "_next/" in lowered:
            return "NEXT"
        return "UNKNOWN"

    @staticmethod
    def _blocker_message(status: str) -> str:
        return {
            "BRAIN_NOT_CONFIGURED": "L0/L1 仍有歧义；请配置 WEBSITE_BRAIN_* 后重扫，系统不会回退到旧 Qwen/Skills Vision。",
            "BROWSER_REQUIRED": "页面没有足够的公开服务器证据，需要同一可见浏览器标签页继续取证。",
            "TEMPORARY_FAILURE": "页面暂时不可用或浏览器导航失败；已保留同一会话和检查点，可恢复重试。",
            "ACCESS_CHANGE_REQUIRED": "可见浏览器被拒绝且没有人工验证控件；请检查网络或站点访问条件。",
            "ROBOTS_DENIED": "robots.txt 拒绝了该入口；请人工确认官方导出、授权接口或停止当前扫描。",
            "SESSION_CONTINUITY_BROKEN": "持久可见浏览器会话中断；请恢复同一扫描。",
            "PARTIAL": "已保存可验证的部分证据，但仍有未知数量或未完成类目。",
        }.get(status, "扫描尚未达到可验证状态。")

    def _fixture_allowed(self) -> bool:
        import os
        return os.getenv("CONTROL_PLANE_DEMO_FIXTURES", "").strip().lower() in {"1", "true", "yes", "on"}

    def _fixture_receipt(self, source_url: str, site_key: str, fixture: Path) -> dict[str, object]:
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        raw_categories = payload.get("categories") if isinstance(payload, dict) else []
        categories: list[TaxonomyCategoryContract] = []
        for raw in raw_categories if isinstance(raw_categories, list) else []:
            if not isinstance(raw, dict):
                continue
            count = raw.get("reported_count") if isinstance(raw.get("reported_count"), int) else None
            categories.append(TaxonomyCategoryContract(category_id=str(raw.get("category_id") or f"cat_{uuid4().hex}"), native_name=str(raw.get("native_name") or "Unknown"), canonical_name=str(raw.get("canonical_name") or raw.get("native_name") or "Unknown"), path=str(raw.get("path") or raw.get("native_name") or "/"), source_url=str(raw.get("source_url") or source_url), count_value=count, count_kind="EXACT" if count is not None else "UNKNOWN", evidence=[{"role": "test_fixture", "path": str(fixture)}], confidence=0.0))
        return self._receipt(source_url, site_key, live=False, status="DEMO_FIXTURE_ONLY", categories=categories, evidence={"source": "explicit_test_fixture", "network_called": False}, blocker={"code": "DEMO_FIXTURE_ONLY", "message": "仅测试 fixture；不是线上验证结果。"}, fixture_only=True)

    @staticmethod
    def _receipt(source_url: str, site_key: str, *, live: bool, status: str, categories: list[TaxonomyCategoryContract], evidence: dict[str, object], blocker: dict[str, str] | None = None, brain: dict[str, object] | None = None, fixture_only: bool = False, source_type: str = "UNKNOWN") -> dict[str, object]:
        taxonomy_level = "L2" if any(item.level == 2 for item in categories) else ("L1" if categories else "L0")
        source_scope = "MARKETPLACE_SCOPE" if source_type == "MARKETPLACE" else "SEARCH" if source_type == "SEARCH_RESULT" else "CATEGORY" if source_type == "SCOPED_CATEGORY" else "SITE"
        receipt = TaxonomyReceipt(site_key=site_key, source_url=source_url, live=live, status=status, verified=bool(live and status == "READY"), fixture_only=fixture_only, taxonomy_level=taxonomy_level, source_type=source_type, source_scope=source_scope, categories=categories, evidence=evidence, blocker=blocker, brain=brain or {}, profile_version=f"website-native-{status.casefold()}", captured_at=datetime.now(UTC))
        return receipt.model_dump(mode="json")

    @staticmethod
    def _write_receipt(root: Path, receipt: dict[str, object]) -> None:
        path = root / "taxonomy_receipt.json"
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


__all__ = ["NativeSiteAnalyzer", "normalize_site_url", "site_key_for"]
