from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, TypeVar

import requests
from pydantic import BaseModel, ValidationError

from app.services.native_contracts import BrainAccessDecision, BrainProductDecision, BrainSourceDecision, BrainTaxonomyResponse


MODEL_MODES = frozenset({"LOCAL_AGENT", "MULTIMODAL_SINGLE_MODEL", "TEXT_BRAIN_PLUS_VISION"})


class BrainError(RuntimeError):
    code = "BRAIN_ERROR"


class BrainNotConfigured(BrainError):
    code = "BRAIN_NOT_CONFIGURED"


class BrainRateLimited(BrainError):
    code = "BRAIN_RATE_LIMITED"


class BrainTimeout(BrainError):
    code = "BRAIN_TIMEOUT"


class BrainInvalidSchema(BrainError):
    code = "BRAIN_INVALID_SCHEMA"


class BrainRequestFailed(BrainError):
    code = "BRAIN_REQUEST_FAILED"


@dataclass(frozen=True, slots=True)
class BrainSettings:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    timeout_seconds: float = 25.0
    max_retries: int = 2
    rpm_limit: int = 30
    model_mode: str = "TEXT_BRAIN_PLUS_VISION"
    local_agent_override: bool = False
    mode_source: str = "explicit"

    @classmethod
    def from_environment(cls) -> "BrainSettings":
        # This namespace is intentionally independent from Vision and legacy
        # Qwen/OpenAI variables. Do not add fallback aliases here.
        requested_mode = (os.getenv("LOCAL_REVIEW_MODE", "").strip().casefold() == "agent")
        raw_mode = os.getenv("WEBSITE_MODEL_MODE", "").strip() or os.getenv("LUNAMAX_MODEL_MODE", "").strip()
        credentials_present = all(os.getenv(name, "").strip() for name in (
            "WEBSITE_BRAIN_API_KEY", "WEBSITE_BRAIN_BASE_URL", "WEBSITE_BRAIN_MODEL",
        ))
        # A company deployment can explicitly choose TEXT_BRAIN_PLUS_VISION or
        # MULTIMODAL_SINGLE_MODEL.  For a local checkout with no Brain
        # credentials, however, the useful and truthful default is the local
        # reviewer: the UI can keep working without pretending a remote model
        # is configured.  An explicit mode always wins over this convenience.
        mode_source = "explicit" if raw_mode else "configured_default" if credentials_present else "local_default"
        normalized_mode = raw_mode.upper().replace("-", "_") if raw_mode else (
            "TEXT_BRAIN_PLUS_VISION" if credentials_present else "LOCAL_AGENT"
        )
        if requested_mode:
            normalized_mode = "LOCAL_AGENT"
            mode_source = "local_review_override"
        if normalized_mode not in MODEL_MODES:
            normalized_mode = "TEXT_BRAIN_PLUS_VISION"
        return cls(
            api_key=os.getenv("WEBSITE_BRAIN_API_KEY", "").strip(),
            base_url=os.getenv("WEBSITE_BRAIN_BASE_URL", "").strip().rstrip("/"),
            model=os.getenv("WEBSITE_BRAIN_MODEL", "").strip(),
            timeout_seconds=max(1.0, min(float(os.getenv("WEBSITE_BRAIN_TIMEOUT_SECONDS", "25")), 120.0)),
            max_retries=max(0, min(int(os.getenv("WEBSITE_BRAIN_MAX_RETRIES", "2")), 3)),
            rpm_limit=max(1, min(int(os.getenv("WEBSITE_BRAIN_RPM_LIMIT", "30")), 600)),
            model_mode=normalized_mode,
            local_agent_override=requested_mode,
            mode_source=mode_source,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    @property
    def local_agent_mode(self) -> bool:
        return self.model_mode == "LOCAL_AGENT"


T = TypeVar("T", bound=BaseModel)


class WebsiteBrainProvider:
    """A strict, receipt-friendly, OpenAI-compatible Website Brain client."""

    def __init__(self, settings: BrainSettings | None = None, *, session: requests.Session | None = None) -> None:
        self.settings = settings or BrainSettings.from_environment()
        self.session = session or requests.Session()
        self.post_count = 0
        self._last_post_at = 0.0

    def health(self) -> dict[str, object]:
        return {
            "status": "LOCAL_AGENT_READY" if self.settings.local_agent_mode else "READY" if self.settings.configured else BrainNotConfigured.code,
            "configured": self.settings.configured,
            "model": self.settings.model if self.settings.configured else None,
            "namespace": "WEBSITE_BRAIN_*",
            "model_mode": self.settings.model_mode,
            "review_provider": self.review_provider,
            "provider_posts": self.post_count,
            "local_agent_override": self.settings.local_agent_override,
            "mode_source": self.settings.mode_source,
            "override_reason": (
                "LOCAL_REVIEW_MODE=agent"
                if self.settings.local_agent_override
                else "未配置 WEBSITE_BRAIN_*，本地开发默认使用 LOCAL_AGENT"
                if self.settings.mode_source == "local_default"
                else "显式 Website Brain 模式"
            ),
        }

    @property
    def review_provider(self) -> str:
        if self.settings.local_agent_mode:
            return "LOCAL_AGENT"
        if self.settings.model_mode == "MULTIMODAL_SINGLE_MODEL":
            return "MULTIMODAL_BRAIN"
        return "TEXT_BRAIN_PLUS_VISION"

    def reason_taxonomy(self, *, source_url: str, evidence: dict[str, object]) -> tuple[BrainTaxonomyResponse, dict[str, object]]:
        prompt = (
            "Classify ambiguous public e-commerce navigation into a conservative furniture taxonomy. "
            "Return JSON only. Never invent counts: use UNKNOWN unless the supplied evidence contains a direct count. "
            "Preserve source URLs and explain each merge."
        )
        response, metadata = self.reason(
            prompt=prompt,
            input_payload={"source_url": source_url, "evidence": evidence},
            schema=BrainTaxonomyResponse,
        )
        return response, metadata

    def reason_source(self, *, source_url: str, evidence: dict[str, object]) -> tuple[BrainSourceDecision, dict[str, object]]:
        return self.reason(
            prompt=(
                "Classify the supplied public commerce source using only evidence. Return JSON only with source_type, "
                "brand_display_name, scope_kind, confidence, and short reason_codes. Use UNKNOWN when evidence is insufficient."
            ),
            input_payload={"source_url": source_url, "evidence": evidence},
            schema=BrainSourceDecision,
        )

    def reason_access(self, *, source_url: str, evidence: dict[str, object]) -> tuple[BrainAccessDecision, dict[str, object]]:
        return self.reason(
            prompt=(
                "Classify bounded public-page access evidence. Return JSON only. A plain HTTP 401/403/429, bot-like "
                "response, or generic marker at L0/L1 must escalate to the same visible L2 session; it is not proof that "
                "a human challenge exists. HUMAN_REQUIRED is allowed only for an explicit visible CAPTCHA/verification "
                "control. Temporary technical/server/navigation failures must retry the same session. Never recommend "
                "stealth, proxy rotation, fingerprint spoofing, cookie copying, or CAPTCHA solving."
            ),
            input_payload={"source_url": source_url, "evidence": evidence},
            schema=BrainAccessDecision,
        )

    def reason_product(self, *, source_url: str, evidence: dict[str, object]) -> tuple[BrainProductDecision, dict[str, object]]:
        return self.reason(
            prompt=(
                "Evaluate one furniture product candidate from explicit Website evidence. Return JSON ONLY and match the "
                "exact schema below; do not add or rename fields and do not wrap in markdown.\n"
                "Required JSON object fields:\n"
                "- \"eligible\": boolean (true if the evidence is a real, single furniture product)\n"
                "- \"single_product\": boolean (true if the image/URL depicts exactly one product)\n"
                "- \"background_ok\": boolean (true if the product photo is on a clean/simple background)\n"
                "- \"image_to_3d_suitable\": boolean (true if the photo is suitable as a 3D-modeling reference)\n"
                "- \"category_group\": string (e.g. Chairs, Sofas, Tables, Storage)\n"
                "- \"style\": string (leave empty unless the evidence states it)\n"
                "- \"color\": string (leave empty unless the evidence states it)\n"
                "- \"material\": string (leave empty unless the evidence states it)\n"
                "- \"product_type\": string (e.g. accent chair, lounge chair, sofa)\n"
                "- \"width\": number or null (from the provided dimensions only; never invent)\n"
                "- \"depth\": number or null\n"
                "- \"height\": number or null\n"
                "- \"dimension_unit\": string (in/cm/mm; empty if unknown)\n"
                "- \"dimension_source\": one of OFFICIAL_STRUCTURED, OFFICIAL_PAGE, AI_ESTIMATED, UNKNOWN; use AI_ESTIMATED when the Website did not provide official dimensions\n"
                "- \"confidence\": number 0..1\n"
                "- \"reason_codes\": array of short uppercase codes\n"
                "Use the source_name, source_brand, category_group, and source_dimensions in the evidence as authoritative "
                "facts. reason_codes must be short codes, not private reasoning."
            ),
            input_payload={"source_url": source_url, "evidence": evidence},
            schema=BrainProductDecision,
        )

    def reason(self, *, prompt: str, input_payload: dict[str, object], schema: type[T]) -> tuple[T, dict[str, object]]:
        if self.settings.local_agent_mode:
            local_payload = self._local_payload(input_payload, schema)
            if local_payload is None:
                raise BrainNotConfigured("LOCAL_AGENT_REVIEW_REQUIRED: provide explicit local_agent_review evidence")
            try:
                validated = schema.model_validate(local_payload)
            except ValidationError as error:
                raise BrainInvalidSchema("LOCAL_AGENT review did not match the required JSON schema") from error
            return validated, {
                "status": "LOCAL_AGENT_REVIEW",
                "review_provider": "LOCAL_AGENT",
                "model_mode": "LOCAL_AGENT",
                "input_hash": hashlib.sha256(json.dumps(input_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
                "provider_posts": 0,
            }
        if not self.settings.configured:
            raise BrainNotConfigured("WEBSITE_BRAIN_API_KEY/BASE_URL/MODEL are required")
        request_body = {
            "model": self.settings.model,
            "temperature": 0,
            "messages": self._messages(prompt, input_payload),
            "response_format": {"type": "json_object"},
        }
        endpoint = self.settings.base_url
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            self._respect_rate_limit()
            try:
                self.post_count += 1
                response = self.session.post(endpoint, json=request_body, headers=headers, timeout=self.settings.timeout_seconds)
            except requests.Timeout as error:
                last_error = BrainTimeout("Website Brain request timed out")
                if attempt >= self.settings.max_retries:
                    raise last_error from error
                continue
            except requests.RequestException as error:
                last_error = BrainRequestFailed(f"Website Brain request failed: {type(error).__name__}")
                if attempt >= self.settings.max_retries:
                    raise last_error from error
                continue
            if response.status_code == 429:
                last_error = BrainRateLimited("Website Brain returned HTTP 429")
                if attempt >= self.settings.max_retries:
                    raise last_error
                self._backoff(attempt, response)
                continue
            if response.status_code >= 500:
                last_error = BrainRequestFailed(f"Website Brain returned HTTP {response.status_code}")
                if attempt >= self.settings.max_retries:
                    raise last_error
                self._backoff(attempt, response)
                continue
            if response.status_code >= 400:
                raise BrainRequestFailed(f"Website Brain returned HTTP {response.status_code}")
            try:
                payload = response.json()
                content = payload["choices"][0]["message"]["content"]
                parsed = self._parse_json(content)
                validated = schema.model_validate(parsed)
            except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError, ValidationError) as error:
                raise BrainInvalidSchema("Website Brain response did not match the required JSON schema") from error
            return validated, {
                "status": "READY",
                "review_provider": self.review_provider,
                "model_mode": self.settings.model_mode,
                "model": self.settings.model,
                "attempt": attempt + 1,
                "input_hash": hashlib.sha256(json.dumps(input_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
                "provider_posts": self.post_count,
            }
        raise last_error or BrainRequestFailed("Website Brain request failed")

    @staticmethod
    def _evidence(input_payload: dict[str, object]) -> dict[str, object]:
        raw = input_payload.get("evidence")
        return raw if isinstance(raw, dict) else {}

    def _local_payload(self, input_payload: dict[str, object], schema: type[T]) -> object | None:
        evidence = self._evidence(input_payload)
        candidates: list[object] = []
        if schema is BrainProductDecision:
            candidates.extend(evidence.get(key) for key in ("local_agent_review", "agent_review", "local_review", "qualification"))
        elif schema is BrainTaxonomyResponse:
            candidates.extend(evidence.get(key) for key in ("local_taxonomy", "local_agent_review", "agent_review"))
            if isinstance(evidence.get("categories"), list):
                candidates.append({"categories": evidence["categories"], "reasoning": "LOCAL_AGENT supplied category review"})
        elif schema is BrainSourceDecision:
            candidates.extend(evidence.get(key) for key in ("local_source_decision", "local_agent_review", "agent_review"))
        elif schema is BrainAccessDecision:
            candidates.extend(evidence.get(key) for key in ("local_access_decision", "local_agent_review", "agent_review"))
            if not any(isinstance(candidate, dict) for candidate in candidates):
                return self._local_access_decision(evidence)
        for candidate in candidates:
            if isinstance(candidate, dict):
                return candidate
        return None

    @staticmethod
    def _local_access_decision(evidence: dict[str, object]) -> dict[str, object]:
        stage = str(evidence.get("stage") or "L1").upper()
        code = str(evidence.get("reason_code") or "").upper()
        explicit_control = bool(evidence.get("explicit_challenge_control"))
        visible_challenge = bool(evidence.get("visible_challenge_text"))
        if explicit_control and visible_challenge:
            return {"access_state": "HUMAN_REQUIRED", "next_action": "WAIT_FOR_HUMAN", "confidence": 0.98,
                    "reason_codes": [code or "VISIBLE_CHALLENGE"], "summary": "页面存在可见且可操作的人机验证控件"}
        if code in {"HTTP_500", "HTTP_502", "HTTP_503", "HTTP_504", "HTTP_522", "HTTP_524"}:
            return {"access_state": "ESCALATE_L2", "next_action": "ESCALATE_L2", "confidence": 0.92,
                    "reason_codes": [code], "summary": "可重试的服务器响应保留同一会话并升级 L2"}
        if code in {"TEMPORARY_PAGE_FAILURE", "BROWSER_NAVIGATION_FAILED"}:
            return {"access_state": "TEMPORARY_FAILURE", "next_action": "RETRY_SAME_SESSION", "confidence": 0.92,
                    "reason_codes": [code], "summary": "临时页面或导航故障，不等同于人机验证"}
        if stage in {"L0", "L1", "HTTP", "PREFLIGHT"}:
            return {"access_state": "ESCALATE_L2", "next_action": "ESCALATE_L2", "confidence": 0.95,
                    "reason_codes": [code or "HTTP_ACCESS_SIGNAL"], "summary": "HTTP 证据不足，升级同一持久可见浏览器会话"}
        if code in {"ACCESS_DENIED", "REQUEST_BLOCKED"}:
            return {"access_state": "ACCESS_CHANGE_REQUIRED", "next_action": "REQUIRE_ACCESS_CHANGE", "confidence": 0.86,
                    "reason_codes": [code], "summary": "可见浏览器仍被拒绝，但没有人工验证控件"}
        return {"access_state": "TEMPORARY_FAILURE", "next_action": "RETRY_SAME_SESSION", "confidence": 0.7,
                "reason_codes": [code or "INCONCLUSIVE_ACCESS"], "summary": "访问证据不充分，保留会话后重试"}

    def _messages(self, prompt: str, input_payload: dict[str, object]) -> list[dict[str, object]]:
        user_text = json.dumps(input_payload, ensure_ascii=False, sort_keys=True)
        if self.settings.model_mode == "MULTIMODAL_SINGLE_MODEL":
            evidence = self._evidence(input_payload)
            image_url = str(evidence.get("image_url") or evidence.get("selected_media_url") or "").strip()
            if image_url:
                return [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ]},
                ]
        return [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_text},
        ]

    def _respect_rate_limit(self) -> None:
        spacing = 60.0 / max(1, self.settings.rpm_limit)
        remaining = spacing - (time.monotonic() - self._last_post_at)
        if remaining > 0:
            time.sleep(min(remaining, 2.0))
        self._last_post_at = time.monotonic()

    @staticmethod
    def _backoff(attempt: int, response: requests.Response) -> None:
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 0.25 * (2**attempt)
        except ValueError:
            delay = 0.25 * (2**attempt)
        time.sleep(min(max(delay, 0.05), 3.0))

    @staticmethod
    def _parse_json(content: object) -> object:
        text = str(content or "").strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = min((index for index in (text.find("{"), text.find("[")) if index >= 0), default=-1)
            if start < 0:
                raise
            return json.loads(text[start:])


__all__ = [
    "MODEL_MODES",
    "BrainAccessDecision", "BrainError", "BrainInvalidSchema", "BrainNotConfigured", "BrainRateLimited",
    "BrainRequestFailed", "BrainSettings", "BrainTimeout", "WebsiteBrainProvider",
]
