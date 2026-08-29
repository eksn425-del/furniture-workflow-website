from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests


USER_AGENT = os.getenv(
    "SCRAPE_USER_AGENT",
    "FurnitureWorkflowEvidenceBot/1.0 (+public catalog evidence; operator contact required)",
)
MAX_DOCUMENT_BYTES = 15_000_000
MAX_MEDIA_BYTES = 25_000_000
MAX_REDIRECTS = 5


def _windows_system_proxies() -> dict[str, str]:
    """Mirror the signed-in user's Windows proxy when Python env proxies are absent."""

    if os.name != "nt" or any(os.getenv(name) for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")):
        return {}
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            raw = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
    except (OSError, ValueError, TypeError):
        return {}
    if not enabled or not raw:
        return {}
    if "=" not in raw:
        proxy = raw if "://" in raw else f"http://{raw}"
        return {"http": proxy, "https": proxy}
    proxies: dict[str, str] = {}
    for item in raw.split(";"):
        scheme, separator, address = item.partition("=")
        if separator and scheme in {"http", "https"} and address:
            proxies[scheme] = address if "://" in address else f"http://{address}"
    return proxies


class NetworkPolicyError(ValueError):
    pass


class RobotsDenied(NetworkPolicyError):
    pass


class RequestBudgetExceeded(NetworkPolicyError):
    pass


class AccessControlDetected(NetworkPolicyError):
    def __init__(self, kind: str) -> None:
        super().__init__(f"website access control detected: {kind}")
        self.kind = kind


class HttpStatusError(RuntimeError):
    def __init__(self, status_code: int, *, retryable: bool) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.retryable = retryable


def _normalized_host(host: str) -> str:
    value = host.casefold().rstrip(".")
    return value[4:] if value.startswith("www.") else value


def same_site_host(candidate: str, expected: str) -> bool:
    left = _normalized_host(candidate)
    right = _normalized_host(expected)
    return bool(
        left
        and right
        and (left == right or left.endswith(f".{right}") or right.endswith(f".{left}"))
    )


def validate_public_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise NetworkPolicyError("only http(s) URLs are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise NetworkPolicyError("URL must contain a hostname and no credentials")
    try:
        port = parsed.port
    except ValueError as error:
        raise NetworkPolicyError("URL port is invalid") from error
    if port is not None and port not in {80, 443}:
        raise NetworkPolicyError("only standard web ports are allowed")
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname, port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise NetworkPolicyError("hostname resolution failed") from error
    if not addresses:
        raise NetworkPolicyError("hostname did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise NetworkPolicyError("URL resolves to a private or reserved address")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


@dataclass(slots=True)
class FetchResult:
    url: str
    content: bytes
    content_type: str
    status_code: int


class SafeHttpClient:
    def __init__(
        self,
        *,
        source_url: str,
        request_budget: int,
        timeout: float = 25,
        request_delay: float = 0.35,
        session: requests.Session | None = None,
    ) -> None:
        if not 1 <= request_budget <= 20000:
            raise ValueError("request_budget must be between 1 and 20000")
        self.source_url = validate_public_url(source_url)
        self.source_host = urlsplit(self.source_url).hostname or ""
        self.request_budget = request_budget
        self.timeout = timeout
        self.request_delay = max(0.0, request_delay)
        self.session = session or requests.Session()
        if session is None:
            self.session.proxies.update(_windows_system_proxies())
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})
        self.request_count = 0
        self.cache_hits = 0
        self.redirect_count = 0
        self._cache: dict[tuple[str, str], FetchResult] = {}
        self._robots: dict[str, tuple[RobotFileParser, list[str], float]] = {}
        self._last_request_at: dict[str, float] = {}

    def telemetry(self) -> dict:
        return {
            "request_count": self.request_count,
            "request_budget": self.request_budget,
            "cache_hits": self.cache_hits,
            "redirect_count": self.redirect_count,
        }

    def get_html(self, url: str) -> str:
        target = self._require_same_site(url)
        result = self._fetch(target, role="document", max_bytes=MAX_DOCUMENT_BYTES)
        content_type = result.content_type.casefold()
        if not any(kind in content_type for kind in ("text/html", "application/xhtml", "text/plain")):
            raise NetworkPolicyError(f"expected HTML but received {result.content_type or 'unknown'}")
        text = result.content.decode(self._charset(result.content_type), errors="replace")
        challenge = self._challenge_kind(text)
        if challenge:
            raise AccessControlDetected(challenge)
        return text

    def get_json(self, url: str) -> object:
        target = self._require_same_site(url)
        result = self._fetch(target, role="document", max_bytes=MAX_DOCUMENT_BYTES)
        if "json" not in result.content_type.casefold() and "text/plain" not in result.content_type.casefold():
            raise NetworkPolicyError(f"expected JSON but received {result.content_type or 'unknown'}")
        try:
            return json.loads(result.content.decode(self._charset(result.content_type), errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NetworkPolicyError("invalid JSON response") from error

    def post_json(self, url: str, payload: dict) -> dict:
        """Send one bounded same-site read-only JSON/GraphQL request.

        This method exists for reviewed core adapters. Agent-authored manifests
        cannot supply arbitrary headers or executable request code.
        """

        target = self._require_same_site(url)
        if not isinstance(payload, dict):
            raise NetworkPolicyError("JSON request body must be an object")
        query = payload.get("query")
        if query is not None:
            if not isinstance(query, str) or len(query) > 50_000:
                raise NetworkPolicyError("GraphQL query is missing or exceeds the safety limit")
            without_comments = re.sub(r"#[^\r\n]*", "", query)
            if re.search(r"\bmutation\b", without_comments, flags=re.I):
                raise NetworkPolicyError("GraphQL mutations are not allowed")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 100_000:
            raise NetworkPolicyError("JSON request body exceeds the safety limit")
        cache_key = ("POST", target + "#" + hashlib.sha256(encoded).hexdigest())
        cached = self._cache.get(cache_key)
        if cached is not None:
            self.cache_hits += 1
            result = cached
        else:
            parser, _, delay = self._robots_for(target)
            if not parser.can_fetch(USER_AGENT, target):
                raise RobotsDenied(f"robots.txt disallows URL: {target}")
            self._consume_budget()
            self._respect_delay(f"{urlsplit(target).scheme}://{urlsplit(target).netloc}", max(delay, self.request_delay))
            response = self.session.post(
                target,
                json=payload,
                timeout=self.timeout,
                allow_redirects=False,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                raise NetworkPolicyError("POST redirects are rejected for reviewed data adapters")
            if response.status_code == 429:
                raise HttpStatusError(429, retryable=True)
            if response.status_code >= 500:
                raise HttpStatusError(response.status_code, retryable=True)
            if response.status_code >= 400:
                raise HttpStatusError(response.status_code, retryable=False)
            content = response.content
            if len(content) > MAX_DOCUMENT_BYTES:
                raise NetworkPolicyError("JSON response exceeds the safety limit")
            result = FetchResult(
                url=target,
                content=content,
                content_type=response.headers.get("Content-Type", "").split(";", 1)[0].strip(),
                status_code=response.status_code,
            )
            self._cache[cache_key] = result
        if "json" not in result.content_type.casefold() and "text/plain" not in result.content_type.casefold():
            raise NetworkPolicyError(f"expected JSON but received {result.content_type or 'unknown'}")
        try:
            value = json.loads(result.content.decode(self._charset(result.content_type), errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NetworkPolicyError("invalid JSON response") from error
        if not isinstance(value, dict):
            raise NetworkPolicyError("expected a JSON object response")
        if isinstance(value.get("errors"), list) and not value.get("data"):
            raise NetworkPolicyError("GraphQL response contained errors without data")
        return value

    def get_sitemap(self, url: str) -> str:
        target = self._require_same_site(url)
        result = self._fetch(target, role="sitemap", max_bytes=MAX_DOCUMENT_BYTES)
        content = result.content
        if target.casefold().endswith(".gz") or "gzip" in result.content_type.casefold():
            try:
                content = gzip.decompress(content)
            except OSError as error:
                raise NetworkPolicyError("invalid gzip sitemap") from error
        if len(content) > MAX_DOCUMENT_BYTES:
            raise NetworkPolicyError("decompressed sitemap exceeds safety limit")
        return content.decode("utf-8", errors="replace")

    def get_media(self, url: str) -> FetchResult:
        target = validate_public_url(url)
        result = self._fetch(target, role="media", max_bytes=MAX_MEDIA_BYTES)
        if not result.content_type.casefold().startswith("image/"):
            raise NetworkPolicyError(
                f"expected image media but received {result.content_type or 'unknown'}"
            )
        return result

    def robots_sitemaps(self, url: str | None = None) -> list[str]:
        target = self._require_same_site(url or self.source_url)
        _, sitemaps, _ = self._robots_for(target)
        return list(sitemaps)

    def _require_same_site(self, url: str) -> str:
        target = validate_public_url(urljoin(self.source_url, url))
        host = urlsplit(target).hostname or ""
        if not same_site_host(host, self.source_host):
            raise NetworkPolicyError("navigation URL is outside the requested site")
        return target

    def _fetch(self, url: str, *, role: str, max_bytes: int) -> FetchResult:
        cache_key = ("GET", url)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        parser, _, delay = self._robots_for(url)
        if not parser.can_fetch(USER_AGENT, url):
            raise RobotsDenied(f"robots.txt disallows URL: {url}")
        result = self._send(url, max_bytes=max_bytes, delay=max(delay, self.request_delay))
        self._cache[cache_key] = result
        if len(self._cache) > 128:
            self._cache.pop(next(iter(self._cache)))
        return result

    def _robots_for(self, url: str) -> tuple[RobotFileParser, list[str], float]:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._robots.get(origin)
        if cached is not None:
            return cached
        robots_url = f"{origin}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        sitemaps: list[str] = []
        delay = self.request_delay
        try:
            result = self._send(robots_url, max_bytes=1_000_000, delay=0)
            text = result.content.decode(self._charset(result.content_type), errors="replace")
            parser.parse(text.splitlines())
            for line in text.splitlines():
                if line.casefold().startswith("sitemap:"):
                    candidate = validate_public_url(line.split(":", 1)[1].strip())
                    if same_site_host(urlsplit(candidate).hostname or "", parsed.hostname or ""):
                        sitemaps.append(candidate)
            crawl_delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*")
            if crawl_delay is not None:
                delay = max(delay, min(float(crawl_delay), 10.0))
        except HttpStatusError as error:
            if error.status_code not in {401, 403, 404}:
                raise
            # RFC-style permissive fallback only when robots is unavailable;
            # a 401/403 robots response still denies the site.
            parser.parse(["User-agent: *", "Disallow: /"] if error.status_code in {401, 403} else [])
        except (requests.RequestException, OSError, TimeoutError):
            # robots.txt 网络不可达（连接超时/DNS 失败等）：按 RFC 9309 降级为
            # “无已知限制”继续抓取，而不是让整个站点扫描失败。
            # 401/403 的明确拒绝在上面分支处理，这里只处理拿不到响应的情况。
            pass
        self._robots[origin] = (parser, list(dict.fromkeys(sitemaps)), delay)
        return self._robots[origin]

    def _send(self, url: str, *, max_bytes: int, delay: float) -> FetchResult:
        current = validate_public_url(url)
        for redirect_index in range(MAX_REDIRECTS + 1):
            self._consume_budget()
            origin = f"{urlsplit(current).scheme}://{urlsplit(current).netloc}"
            self._respect_delay(origin, delay)
            response = self.session.get(
                current,
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect_index >= MAX_REDIRECTS:
                    raise NetworkPolicyError("redirect limit exceeded")
                location = response.headers.get("Location")
                if not location:
                    raise NetworkPolicyError("redirect response omitted Location")
                candidate = validate_public_url(urljoin(current, location))
                if not same_site_host(
                    urlsplit(candidate).hostname or "", urlsplit(current).hostname or ""
                ):
                    raise NetworkPolicyError("cross-site redirect rejected")
                current = candidate
                self.redirect_count += 1
                continue
            if response.status_code == 429:
                raise HttpStatusError(429, retryable=True)
            if response.status_code >= 500:
                raise HttpStatusError(response.status_code, retryable=True)
            if response.status_code >= 400:
                raise HttpStatusError(response.status_code, retryable=False)
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise NetworkPolicyError("response exceeds declared size safety limit")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise NetworkPolicyError("response exceeds streamed size safety limit")
                chunks.append(chunk)
            return FetchResult(
                url=current,
                content=b"".join(chunks),
                content_type=response.headers.get("Content-Type", "").split(";", 1)[0].strip(),
                status_code=response.status_code,
            )
        raise NetworkPolicyError("redirect loop")

    def _consume_budget(self) -> None:
        if self.request_count >= self.request_budget:
            raise RequestBudgetExceeded("logical HTTP request budget exhausted")
        self.request_count += 1

    def _respect_delay(self, origin: str, delay: float) -> None:
        previous = self._last_request_at.get(origin)
        if previous is not None:
            remaining = delay - (time.monotonic() - previous)
            if remaining > 0:
                time.sleep(min(remaining, 10.0))
        self._last_request_at[origin] = time.monotonic()

    @staticmethod
    def _charset(content_type: str) -> str:
        match = re.search(r"charset=([\w.-]+)", content_type, flags=re.I)
        return match.group(1) if match else "utf-8"

    @staticmethod
    def _challenge_kind(text: str) -> str:
        raw = text[:250_000].casefold()
        # Product pages often ship CAPTCHA/login phrases inside third-party JS.
        # Only visible document text or strong challenge DOM signatures count.
        visible = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
        visible = re.sub(r"<!--.*?-->|<[^>]+>", " ", visible, flags=re.S)
        visible = re.sub(r"\s+", " ", visible)
        strong_dom = {
            "captcha": ("id=\"px-captcha", "class=\"px-captcha", "data-sitekey=", "hcaptcha.com/1/api", "recaptcha/api"),
            "javascript_challenge": ("id=\"challenge-form", "cf-chl-widget", "challenge-platform"),
        }
        for kind, markers in strong_dom.items():
            if any(marker in raw for marker in markers):
                return kind
        patterns = {
            "captcha": ("captcha", "verify you are human"),
            "javascript_challenge": ("checking your browser", "just a moment...", "cf-chl-"),
            "login_required": ("sign in to continue", "login required"),
            "access_denied": ("access denied", "request blocked"),
        }
        for kind, markers in patterns.items():
            if any(marker in visible for marker in markers):
                return kind
        return ""
