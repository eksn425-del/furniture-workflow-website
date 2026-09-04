from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

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


class VisionProviderNotConfigured(BrainError):
    code = "VISION_PROVIDER_NOT_CONFIGURED"


class VisionInputRequired(BrainError):
    code = "VISION_INPUT_REQUIRED"


@dataclass(frozen=True, slots=True)
class BrainSettings:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    timeout_seconds: float = 25.0
    connect_timeout_seconds: float = 5.0
    max_retries: int = 2
    rpm_limit: int = 30
    model_mode: str = "TEXT_BRAIN_PLUS_VISION"
    local_agent_override: bool = False
    agent_enabled: bool = True
    agent_max_steps: int = 6
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
            connect_timeout_seconds=max(1.0, min(float(os.getenv("WEBSITE_BRAIN_CONNECT_TIMEOUT_SECONDS", "5")), 30.0)),
            max_retries=max(0, min(int(os.getenv("WEBSITE_BRAIN_MAX_RETRIES", "2")), 3)),
            rpm_limit=max(1, min(int(os.getenv("WEBSITE_BRAIN_RPM_LIMIT", "30")), 600)),
            model_mode=normalized_mode,
            local_agent_override=requested_mode,
            agent_enabled=os.getenv("WEBSITE_BRAIN_AGENT_ENABLED", "true").strip().casefold() not in {"0", "false", "off", "no"},
            agent_max_steps=max(1, min(int(os.getenv("WEBSITE_BRAIN_AGENT_MAX_STEPS", "6")), 20)),
            mode_source=mode_source,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    @property
    def local_agent_mode(self) -> bool:
        return self.model_mode == "LOCAL_AGENT"


@dataclass(frozen=True, slots=True)
class VisionSettings:
    """Independent Vision endpoint used only by TEXT_BRAIN_PLUS_VISION.

    Keep this namespace separate from Website Brain.  A text-capable Brain is
    not evidence that a second model inspected the captured product image.
    """

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    timeout_seconds: float = 25.0
    connect_timeout_seconds: float = 5.0
    max_retries: int = 2
    rpm_limit: int = 30

    @classmethod
    def from_environment(cls) -> "VisionSettings":
        return cls(
            api_key=os.getenv("WEBSITE_VISION_API_KEY", "").strip(),
            base_url=os.getenv("WEBSITE_VISION_BASE_URL", "").strip().rstrip("/"),
            model=os.getenv("WEBSITE_VISION_MODEL", "").strip(),
            timeout_seconds=max(1.0, min(float(os.getenv("WEBSITE_VISION_TIMEOUT_SECONDS", "25")), 120.0)),
            connect_timeout_seconds=max(1.0, min(float(os.getenv("WEBSITE_VISION_CONNECT_TIMEOUT_SECONDS", "5")), 30.0)),
            max_retries=max(0, min(int(os.getenv("WEBSITE_VISION_MAX_RETRIES", "2")), 3)),
            rpm_limit=max(1, min(int(os.getenv("WEBSITE_VISION_RPM_LIMIT", "30")), 600)),
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


T = TypeVar("T", bound=BaseModel)


class AgentToolError(RuntimeError):
    """Raised by a tool executor to terminate an agent loop (e.g. budget/access).

    Terminating on this error is intentional: the loop must not keep spending
    Brain turns after a compliance gate (robots/access/budget) is tripped.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AgentLoopOptions:
    max_steps: int = 6
    max_tool_calls: int = 16
    response_schema: type[BaseModel] | None = None
    finish_tool_name: str = "finish"
    temperature: float = 0.0


@dataclass(frozen=True, slots=True)
class AgentLoopResult:
    validated: BaseModel | None
    turns: int
    tool_calls: list[dict[str, object]]
    provider_posts: int
    stopped_reason: str  # FINISH / SCHEMA / MAX_STEPS / BUDGET / TERMINAL_TOOL / BRAIN_NOT_CONFIGURED / BRAIN_ERROR


class WebsiteBrainProvider:
    """A strict, receipt-friendly, OpenAI-compatible Website Brain client."""

    def __init__(
        self,
        settings: BrainSettings | None = None,
        *,
        session: requests.Session | None = None,
        vision_settings: VisionSettings | None = None,
        vision_session: requests.Session | None = None,
    ) -> None:
        self.settings = settings or BrainSettings.from_environment()
        self.session = session or requests.Session()
        self.vision_settings = vision_settings or VisionSettings.from_environment()
        self.vision_session = vision_session or requests.Session()
        self.post_count = 0
        self.vision_post_count = 0
        self._last_post_at = 0.0

    def health(self) -> dict[str, object]:
        vision_required = self.settings.model_mode == "TEXT_BRAIN_PLUS_VISION"
        operational = self.settings.local_agent_mode or (
            self.settings.configured and (not vision_required or self.vision_settings.configured)
        )
        if self.settings.local_agent_mode:
            status = "LOCAL_AGENT_READY"
        elif not self.settings.configured:
            status = BrainNotConfigured.code
        elif vision_required and not self.vision_settings.configured:
            status = VisionProviderNotConfigured.code
        else:
            status = "READY"
        return {
            "status": status,
            "configured": self.settings.configured,
            "operational": operational,
            "model": self.settings.model if self.settings.configured else None,
            "namespace": "WEBSITE_BRAIN_*",
            "model_mode": self.settings.model_mode,
            "review_provider": self.review_provider,
            "provider_posts": self.post_count,
            "vision_configured": self.vision_settings.configured,
            "vision_provider_posts": self.vision_post_count,
            "vision_input": (
                "LOCAL_AGENT_EXPLICIT_REVIEW"
                if self.settings.local_agent_mode
                else "IMAGE_URL_IN_BRAIN_REQUEST"
                if self.settings.model_mode == "MULTIMODAL_SINGLE_MODEL"
                else "INDEPENDENT_VISION_RECEIPT"
            ),
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
            "Classify the supplied public e-commerce navigation into a clean two-level furniture taxonomy "
            "(level=1 departments like Seating/Tables/Beds/Rugs, level=2 their child categories like Chairs/Sofas). "
            "Use evidence.navigation_items (each has path, label, count) as the primary source: infer department->category "
            "relationships even when the site renders them as a flat list, and group flat sibling categories under a "
            "sensible department. When evidence.navigation_tree with explicit nesting is present, treat its top-level "
            "entries as level-1 departments and their children as level-2. A label ending in a number (e.g. \"CHAIRS 33\") "
            "is a direct count of 33; otherwise use UNKNOWN unless evidence.navigation_items gives a direct count. "
            "Never invent counts. Return JSON only with "
            "categories[{path, source_url, native_name, canonical_name, count_value, count_kind, level, parent_path}] "
            "and short reasoning."
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
        prompt = (
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
                "- \"width\": number or null (official width only; null if the Website did not provide it)\n"
                "- \"depth\": number or null (official depth only; null if the Website did not provide it)\n"
                "- \"height\": number or null (official height if the Website provided it; otherwise, if the Website provided NO official dimensions at all, estimate a reasonable height in inches for the product type based on the image)\n"
                "- \"dimension_unit\": string (in/cm/mm; use \"in\" when you estimated the height; empty if unknown)\n"
                "- \"dimension_source\": one of OFFICIAL_STRUCTURED, OFFICIAL_PAGE, AI_ESTIMATED, UNKNOWN; use AI_ESTIMATED when the Website did not provide official dimensions and you estimated the height\n"
                "- \"confidence\": number 0..1\n"
                "- \"reason_codes\": array of short uppercase codes\n"
                "Use the source_name, source_brand, category_group, and source_dimensions in the evidence as authoritative "
                "facts. reason_codes must be short codes, not private reasoning."
        )
        media_sha256 = str(evidence.get("media_sha256") or "").strip().casefold()
        image_url = str(evidence.get("selected_media_url") or evidence.get("image_url") or "").strip()
        if self.settings.model_mode == "TEXT_BRAIN_PLUS_VISION" and not self.settings.local_agent_mode:
            if not self.vision_settings.configured:
                raise VisionProviderNotConfigured(
                    "TEXT_BRAIN_PLUS_VISION requires WEBSITE_VISION_API_KEY/BASE_URL/MODEL and an independent receipt"
                )
            if not image_url or not media_sha256:
                raise VisionInputRequired("A captured image URL and media_sha256 are required for visual review")
            vision_provider = WebsiteBrainProvider(
                BrainSettings(
                    api_key=self.vision_settings.api_key,
                    base_url=self.vision_settings.base_url,
                    model=self.vision_settings.model,
                    timeout_seconds=self.vision_settings.timeout_seconds,
                    connect_timeout_seconds=self.vision_settings.connect_timeout_seconds,
                    max_retries=self.vision_settings.max_retries,
                    rpm_limit=self.vision_settings.rpm_limit,
                    model_mode="MULTIMODAL_SINGLE_MODEL",
                    mode_source="independent_vision_provider",
                ),
                session=self.vision_session,
                vision_settings=self.vision_settings,
                vision_session=self.vision_session,
            )
            vision_decision, vision_receipt = vision_provider.reason(
                prompt=prompt,
                input_payload={"source_url": source_url, "evidence": evidence},
                schema=BrainProductDecision,
            )
            self.vision_post_count += vision_provider.post_count
            augmented = dict(evidence)
            augmented["independent_vision_decision"] = vision_decision.model_dump(mode="json")
            decision, metadata = self.reason(
                prompt=prompt + " Use independent_vision_decision as the authoritative visual evidence; do not claim you saw the image.",
                input_payload={"source_url": source_url, "evidence": augmented},
                schema=BrainProductDecision,
            )
            merged = decision.model_copy(update={
                "eligible": bool(decision.eligible and vision_decision.eligible),
                "single_product": bool(decision.single_product and vision_decision.single_product),
                "background_ok": bool(decision.background_ok and vision_decision.background_ok),
                "image_to_3d_suitable": bool(decision.image_to_3d_suitable and vision_decision.image_to_3d_suitable),
                "confidence": min(decision.confidence, vision_decision.confidence),
                "source_image_vision_consistent": bool(
                    decision.source_image_vision_consistent is not False
                    and vision_decision.source_image_vision_consistent is not False
                ),
                "reviewed_media_sha256": media_sha256,
                "reason_codes": list(dict.fromkeys([*vision_decision.reason_codes, *decision.reason_codes]))[:16],
            })
            metadata.update({
                "review_provider": "TEXT_BRAIN_PLUS_VISION",
                "vision_input": "IMAGE_URL",
                "visual_input_included": True,
                "reviewed_media_sha256": media_sha256,
                "independent_vision_receipt": {
                    "status": vision_receipt.get("status"),
                    "model_mode": vision_receipt.get("model_mode"),
                    "model": vision_receipt.get("model"),
                    "input_hash": vision_receipt.get("input_hash"),
                    "reviewed_media_sha256": media_sha256,
                    "visual_input_included": True,
                },
            })
            return merged, metadata
        decision, metadata = self.reason(
            prompt=prompt,
            input_payload={"source_url": source_url, "evidence": evidence},
            schema=BrainProductDecision,
        )
        if self.settings.model_mode == "MULTIMODAL_SINGLE_MODEL" and not self.settings.local_agent_mode:
            decision = decision.model_copy(update={
                "reviewed_media_sha256": media_sha256,
                "source_image_vision_consistent": decision.source_image_vision_consistent is not False,
            })
        return decision, metadata

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
        evidence = self._evidence(input_payload)
        visual_request = schema is BrainProductDecision and self.settings.model_mode == "MULTIMODAL_SINGLE_MODEL"
        visual_url = str(evidence.get("selected_media_url") or evidence.get("image_url") or "").strip()
        media_sha256 = str(evidence.get("media_sha256") or "").strip().casefold()
        if visual_request and (not visual_url or not media_sha256):
            raise VisionInputRequired("MULTIMODAL_SINGLE_MODEL requires image_url and media_sha256")
        message, metadata = self._chat_once(
            messages=self._messages(prompt, input_payload),
            response_format={"type": "json_object"},
            input_payload=input_payload,
        )
        try:
            content = message.get("content")
            parsed = self._parse_json(content)
            validated = schema.model_validate(parsed)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, ValidationError) as error:
            raise BrainInvalidSchema("Website Brain response did not match the required JSON schema") from error
        if visual_request:
            metadata.update({
                "vision_input": "IMAGE_URL",
                "visual_input_included": True,
                "reviewed_media_sha256": media_sha256,
            })
        return validated, metadata

    def _chat_once(
        self,
        *,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        response_format: dict[str, object] | None = None,
        input_payload: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Single OpenAI-compatible chat completion. Returns (message, metadata).

        Shared by `reason()` (one-shot JSON) and the agent loop (multi-turn
        tool calling). Handles rate limiting, retries, backoff and receipt
        metadata once, so the agent loop does not duplicate that bookkeeping.
        """
        if not self.settings.configured:
            raise BrainNotConfigured("WEBSITE_BRAIN_API_KEY/BASE_URL/MODEL are required")
        request_body: dict[str, object] = {
            "model": self.settings.model,
            "temperature": 0,
            "messages": messages,
        }
        if tools:
            request_body["tools"] = tools
            request_body["tool_choice"] = "auto"
        if response_format:
            request_body["response_format"] = response_format
        endpoint = self.settings.base_url
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            self._respect_rate_limit()
            try:
                self.post_count += 1
                response = self.session.post(endpoint, json=request_body, headers=headers, timeout=(self.settings.connect_timeout_seconds, self.settings.timeout_seconds))
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
                message = payload["choices"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise BrainInvalidSchema("Website Brain response was not a valid chat completion") from error
            metadata = {
                "status": "READY",
                "review_provider": self.review_provider,
                "model_mode": self.settings.model_mode,
                "model": self.settings.model,
                "attempt": attempt + 1,
                "provider_posts": self.post_count,
            }
            if input_payload is not None:
                metadata["input_hash"] = hashlib.sha256(json.dumps(input_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            return message, metadata
        raise last_error or BrainRequestFailed("Website Brain request failed")

    def run_agent_loop(
        self,
        *,
        system_prompt: str,
        input_payload: dict[str, object],
        tools: list[dict[str, object]],
        tool_executor: Callable[[str, dict[str, object]], dict[str, object]],
        options: AgentLoopOptions | None = None,
    ) -> AgentLoopResult:
        """Multi-turn ReAct loop driving the Brain through `tools`.

        The Brain only *decides*; `tool_executor(name, args)` executes each tool
        and must respect the compliance gates (robots/access/budget). The loop
        stops on: a `finish` tool whose payload validates against
        `response_schema`, a content-only response that validates, the step
        budget, an `AgentToolError`, or a Brain failure (fall back to rules).
        """
        options = options or AgentLoopOptions()
        if (
            self.settings.local_agent_mode
            or not self.settings.configured
            or not self.settings.agent_enabled
        ):
            return AgentLoopResult(None, 0, [], self.post_count, "BRAIN_NOT_CONFIGURED")
        messages: list[dict[str, object]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(input_payload, ensure_ascii=False, sort_keys=True)},
        ]
        tool_calls_log: list[dict[str, object]] = []
        turns = 0
        tool_budget = options.max_tool_calls
        while turns < options.max_steps and tool_budget > 0:
            turns += 1
            try:
                message, _ = self._chat_once(messages=messages, tools=tools, input_payload=input_payload)
            except BrainNotConfigured:
                return AgentLoopResult(None, turns, tool_calls_log, self.post_count, "BRAIN_NOT_CONFIGURED")
            except BrainError:
                return AgentLoopResult(None, turns, tool_calls_log, self.post_count, "BRAIN_ERROR")
            calls = message.get("tool_calls") or []
            if calls:
                for tc in calls:
                    fn = tc.get("function") or {}
                    name = str(fn.get("name") or "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                        args = args if isinstance(args, dict) else {}
                    except json.JSONDecodeError:
                        args = {}
                    if name == options.finish_tool_name:
                        validated = self._finish_payload(args, options.response_schema)
                        return AgentLoopResult(validated, turns, tool_calls_log, self.post_count, "FINISH" if validated is not None else "SCHEMA")
                    if tool_budget <= 0:
                        return AgentLoopResult(None, turns, tool_calls_log, self.post_count, "BUDGET")
                    tool_budget -= 1
                    try:
                        result = tool_executor(name, args)
                        result_status = "OK"
                        content = json.dumps(result, ensure_ascii=False, default=str)
                    except AgentToolError as err:
                        return AgentLoopResult(None, turns, tool_calls_log, self.post_count, "TERMINAL_TOOL")
                    except Exception as exc:  # noqa: BLE001 - executor bugs must not kill the scan
                        result_status = "EXEC_ERROR"
                        content = json.dumps({"error": "EXEC_ERROR", "message": str(exc)[:300]}, ensure_ascii=False, default=str)
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": content})
                    tool_calls_log.append({"name": name, "arguments": args, "result_status": result_status})
                continue
            # No tool calls: model answered directly. Validate content as the schema if possible.
            content = message.get("content")
            if options.response_schema is not None and content:
                validated = self._finish_payload(
                    {"_content": content}, options.response_schema, from_content=True
                )
                if validated is not None:
                    return AgentLoopResult(validated, turns, tool_calls_log, self.post_count, "SCHEMA")
            return AgentLoopResult(None, turns, tool_calls_log, self.post_count, "MAX_STEPS" if turns >= options.max_steps else "STOPPED")
        return AgentLoopResult(None, turns, tool_calls_log, self.post_count, "BUDGET" if tool_budget <= 0 else "MAX_STEPS")

    @staticmethod
    def _finish_payload(args: dict[str, object], schema: type[BaseModel] | None, *, from_content: bool = False) -> BaseModel | None:
        """Validate a `finish` payload (or a content-only answer) against schema."""
        if schema is None:
            return None
        candidates: list[object] = []
        if from_content:
            try:
                candidates.append(WebsiteBrainProvider._parse_json(args.get("_content")))
            except (ValueError, json.JSONDecodeError):
                return None
        else:
            candidates.append(args)
            nested = args.get("taxonomy") or args.get("report") or args.get("result")
            if isinstance(nested, dict):
                candidates.append(nested)
        for candidate in candidates:
            if isinstance(candidate, dict):
                try:
                    return schema.model_validate(candidate)
                except ValidationError:
                    continue
        return None

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
    "AgentLoopOptions", "AgentLoopResult", "AgentToolError",
    "BrainAccessDecision", "BrainError", "BrainInvalidSchema", "BrainNotConfigured", "BrainRateLimited",
    "BrainRequestFailed", "BrainSettings", "BrainTimeout", "VisionInputRequired",
    "VisionProviderNotConfigured", "VisionSettings", "WebsiteBrainProvider",
]
