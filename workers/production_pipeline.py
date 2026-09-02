"""Converged Website production pipeline backed by the shared workflow engine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import select

from app.database import Database
from app.models import ProductionProviderTask, ProductionRegistryEntry, utc_now
from app.services.brain_provider import BrainError, BrainNotConfigured, WebsiteBrainProvider
from app.services.product_acquisition import (
    AcquiredProduct,
    BrowserAccessDenied,
    BrowserHumanRequired,
    BrowserRuntimeMissing,
    BrowserTemporaryFailure,
    NativeBrowserCollector,
    ProductAcquisitionEngine,
    ProductAcquisitionError,
    ProductSupplyExhausted,
)
from furniture_workflow_engine import (
    HardStopError,
    ProductionWorkflowEngine,
    RuntimePolicy,
    RuntimeStatus,
    StageDecision,
    StageOutcome,
    SupplyExhaustedError,
)
from packages.workflow_core.candidate_pool import CandidatePoolStore, CandidateRecord, TERMINAL_ITEM_STATES
from packages.workflow_core.dimensions import govern_dimensions, round_dimension
from packages.workflow_core.locks import make_order_policy_lock, provider_idempotency_key, stable_hash
from packages.workflow_core.naming import (
    NamingReviewRequired,
    compose_brand_official_name,
    compose_official_name,
    compose_product_name,
    shorten_name_to_limit,
)
from packages.workflow_core.production_gate import (
    L2_BROWSER_REQUIRED,
    MEDIA_IDENTITY_MISMATCH,
    READY_FOR_MODELING,
    REVIEW_REQUIRED,
    SCOPE_VISUAL_CONFLICT,
    ProductionGate,
)
from packages.workflow_core.source_identity import normalize_identity_fields
from packages.workflow_core.source_policy import resolve_source_policy
from packages.workflow_core.statuses import ItemState
from workers.modeling_provider import Lux3DClient, is_capacity_rejection
from workers.blender_adapter import BlenderAdapterError, ModelDimensionConflict, resolve_blender_adapter, validate_glb
from workers.scrape.http_client import NetworkPolicyError, RobotsDenied, SafeHttpClient


Emit = Callable[[str, str, str, int | None, int | None, dict[str, Any] | None], None]

_L2_HUMAN_REQUIRED_REASONS = frozenset({
    L2_BROWSER_REQUIRED,
    "DIMENSIONS_BROWSER_HUMAN_REQUIRED",
})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_name(value: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip(" .")[:140]


def _distribute_evenly(total: int, groups: list[str]) -> dict[str, int]:
    """把总数 N 硬性均分到每个已选类目，且各项之和恒等于 N。

    余数按类目顺序逐一分给靠前的类目，保证 sum(quotas) == total。
    """
    total = max(1, int(total))
    if not groups:
        return {}
    share = total // len(groups)
    remainder = total % len(groups)
    return {
        str(group): share + (1 if index < remainder else 0)
        for index, group in enumerate(groups)
    }


def _distribute_proportionally(total: int, weights: dict[str, int]) -> dict[str, int]:
    """Allocate an Exact-N total by reported scope counts with stable ties."""

    total = max(1, int(total))
    positive = {str(key): max(0, int(value)) for key, value in weights.items()}
    weight_sum = sum(positive.values())
    if weight_sum <= 0:
        return _distribute_evenly(total, list(positive))
    raw = {key: total * value / weight_sum for key, value in positive.items()}
    quotas = {key: int(value) for key, value in raw.items()}
    remainder = total - sum(quotas.values())
    ranked = sorted(raw, key=lambda key: (-(raw[key] - quotas[key]), list(positive).index(key)))
    for key in ranked[:remainder]:
        quotas[key] += 1
    return quotas


def _candidate_progress_signature(records: list[CandidateRecord]) -> tuple[tuple[str, str, str, str, str, str], ...]:
    """Capture durable fields that prove one engine tick made progress."""
    return tuple(sorted(
        (
            record.candidate_id,
            record.state.value,
            str(record.rejection_reason or ""),
            str(record.provider_status or ""),
            str(record.product_name or ""),
            str(record.raw_glb_path or ""),
        )
        for record in records
    ))


# 线性流程展示用的细分阶段：把 DISCOVERY 内部逐候选推进的
# 筛选/尺寸命名/入库/建模，映射为前端时间线上的独立阶段，
# 这样流程不会从「产品发现」直接跳到「建模」，而是逐步点亮。
_GRANULAR_STAGE_INDEX = {
    ItemState.MEDIA_READY: 1,
    ItemState.VISUAL_PENDING: 1,
    ItemState.VISUAL_ACCEPTED: 1,
    ItemState.VISUAL_REJECTED: 1,
    ItemState.CATEGORY_ACCEPTED: 1,
    ItemState.CATEGORY_REJECTED: 1,
    ItemState.DATE_ACCEPTED: 1,
    ItemState.DATE_REJECTED: 1,
    ItemState.DIMENSION_PENDING: 2,
    ItemState.DIMENSION_READY: 2,
    ItemState.DIMENSION_REJECTED: 2,
    ItemState.NAMING_READY: 2,
    ItemState.NAMING_REVIEW: 2,
    ItemState.CATALOG_READY: 3,
    ItemState.MODEL_INPUT_LOCKED: 3,
    ItemState.SUBMITTING: 4,
    ItemState.PROVIDER_ACTIVE: 4,
    ItemState.PROVIDER_SUCCESS: 4,
    ItemState.RAW_GLB_READY: 4,
    ItemState.COMPLETED: 4,
}
_GRANULAR_STAGE_LABEL = {1: "MEDIA", 2: "DIMENSION", 3: "READY_POOL", 4: "PROVIDER"}
_GRANULAR_STAGE_DETAIL = {
    1: "候选已通过筛选与复核（媒体/视觉/类目/日期）",
    2: "候选已进入尺寸与命名阶段",
    3: "候选已入库（Catalog / Model Input Lock）",
    4: "候选已进入建模生成阶段",
}


def _max_granular_stage(records: list[CandidateRecord]) -> int:
    return max((_GRANULAR_STAGE_INDEX.get(record.state, 0) for record in records), default=0)


_SUPPORTED_DIMENSION_UNITS = frozenset({
    "m", "meter", "meters",
    "cm", "centimeter", "centimeters",
    "mm", "millimeter", "millimeters",
    "in", "inch", "inches",
    "ft", "foot", "feet",
})


def _candidate_from_product(contract: dict[str, Any], product: AcquiredProduct) -> CandidateRecord:
    identity = hashlib.sha256(product.identity_key.encode("utf-8")).hexdigest()[:24]
    identity_fields = dict(product.identity_fields or {})
    if not identity_fields:
        identity_fields = normalize_identity_fields({
            "canonical_url": product.canonical_url,
            "source_product_id": product.source_product_id,
            "image_url": product.image_url,
        })
    evidence = dict(product.evidence or {})
    scope_state = str(product.scope_status or evidence.get("scope_status") or "UNKNOWN")
    binding_status = str(
        evidence.get("media_binding_status")
        if product.media_binding_status == "UNKNOWN" and evidence.get("media_binding_status")
        else product.media_binding_status or "UNKNOWN"
    )
    try:
        binding_confidence = float(
            evidence.get("media_binding_confidence") or product.media_binding_confidence or 0
        )
    except (TypeError, ValueError):
        binding_confidence = 0.0
    if product.acquisition == "TEST_FIXTURE":
        # Test-only evidence is explicit and never reaches a real provider
        # through native_runtime.py, but it still exercises every gate check.
        if binding_status.upper() == "UNKNOWN":
            binding_status, binding_confidence = "COMPATIBLE", max(binding_confidence, 0.99)
        if scope_state.upper() == "UNKNOWN":
            scope_state = "PASS"
    return CandidateRecord(
        candidate_id=f"candidate_{identity}",
        order_id=str(contract["job_id"]),
        job_id=str(contract["job_id"]),
        record_id=f"record_{identity}",
        source=str(contract.get("site_key") or "unknown"),
        source_product_id=product.source_product_id,
        canonical_url=product.canonical_url,
        preview_id=identity,
        preview_url=product.image_url,
        capture_sha256=product.capture_sha256,
        image_sha256=hashlib.sha256(product.image_url.encode("utf-8")).hexdigest(),
        category_group=product.category_group,
        lineage={
            "source_name": product.source_name,
            "source_brand": product.source_brand,
            "source_type": product.source_type,
            "category_id": product.category_id,
            "category_group": product.category_group,
            "image_url": product.image_url,
            "source_dimensions": product.dimensions,
            "dimension_unit": product.dimension_unit,
            "dimension_source": str(evidence.get("dimension_source") or "UNKNOWN"),
            "capture_evidence": product.evidence,
            "acquisition": product.acquisition,
            "identity_fields": identity_fields,
            "route_id": identity_fields.get("route_id") or "",
            "url_tail_id": identity_fields.get("url_tail_id") or "",
            "page_item_number": identity_fields.get("page_item_number") or "",
            "jsonld_sku": identity_fields.get("jsonld_sku") or "",
            "product_family_name": identity_fields.get("product_family_name") or "",
            "configuration_key": identity_fields.get("configuration_key") or "",
            "variant_key": identity_fields.get("variant_key") or "",
            "asset_identity": identity_fields.get("asset_identity") or "",
            "identity_conflicts": identity_fields.get("identity_conflicts") or [],
            "product_identity_match": bool(evidence.get("product_identity_match")),
            "configuration_bound": bool(evidence.get("configuration_bound")),
            "media_binding_status": binding_status,
            "media_binding_confidence": binding_confidence,
            "media_binding_reasons": evidence.get("media_binding_reasons") or [],
            "scope_status": scope_state,
            "scope_reasons": evidence.get("scope_reasons") or [],
            "image_role": str(evidence.get("image_role") or "MAIN_PRODUCT"),
            "layered_scene7": bool(evidence.get("layered_scene7")),
            "dedup_status": "UNIQUE",
            "claim_status": "CLAIMED",
        },
    )


class WebsiteStageAdapter:
    """All external boundaries for one shared ProductionWorkflowEngine."""

    def __init__(
        self,
        *,
        contract: dict[str, Any],
        database: Database,
        pool: CandidatePoolStore,
        acquisition: ProductAcquisitionEngine,
        workspace: Path,
        brain: WebsiteBrainProvider,
        provider_client: Lux3DClient | None,
        blender_adapter: Any | None,
        media_client_factory: Callable[..., Any] | None,
        emit: Emit,
    ) -> None:
        self.contract = contract
        self.database = database
        self.pool = pool
        self.acquisition = acquisition
        self.workspace = Path(workspace).resolve()
        self.brain = brain
        self.provider_client = provider_client
        self.blender_adapter = blender_adapter
        self.media_client_factory = media_client_factory or SafeHttpClient
        self.emit = emit
        self.source_policy = resolve_source_policy(
            str(contract.get("source_url") or contract.get("site_key") or "")
        )
        self.production_gate = ProductionGate(source_policy=self.source_policy)
        self.selected_ids = {str(value) for value in contract.get("category_ids") or []}
        self.provider_posts = 0
        self.media_root = self.workspace / "02_qualification" / "media"
        self.model_root = self.workspace / "04_provider" / "glb"
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.model_root.mkdir(parents=True, exist_ok=True)

    def refill(self, *, needed: int, pool: CandidatePoolStore) -> list[CandidateRecord]:
        try:
            products = self.acquisition.discover(max(needed * 3, needed))
        except ProductSupplyExhausted as error:
            raise SupplyExhaustedError(str(error)) from error
        return [_candidate_from_product(self.contract, product) for product in products]

    def run(self, *, stage: str, candidate: CandidateRecord) -> StageOutcome:
        handler = getattr(self, f"_stage_{stage}")
        return handler(candidate)

    def _stage_capture(self, candidate: CandidateRecord) -> StageOutcome:
        if self.selected_ids and str(candidate.lineage.get("category_id") or "") not in self.selected_ids:
            return StageOutcome(StageDecision.REJECTED, "CATEGORY_OUTSIDE_SELECTED_SCOPE")
        if str(candidate.lineage.get("scope_status") or "").upper() == "CONFLICT":
            return StageOutcome(StageDecision.REJECTED, SCOPE_VISUAL_CONFLICT)
        if candidate.lineage.get("identity_conflicts") and self.source_policy.source_host == "roomandboard.com":
            return StageOutcome(StageDecision.REJECTED, MEDIA_IDENTITY_MISMATCH)
        if (
            str(candidate.lineage.get("media_binding_status") or "").upper() == "MISMATCH"
            and self.source_policy.media_policy == "STRICT_MAIN_PRODUCT"
        ):
            return StageOutcome(StageDecision.REJECTED, MEDIA_IDENTITY_MISMATCH)
        if str(self.contract.get("scope") or "NEW_ONLY") == "NEW_ONLY":
            session = self.database.session_factory()
            try:
                existing = session.scalar(select(ProductionRegistryEntry).where(
                    ProductionRegistryEntry.site_key == self.contract["site_key"],
                    ProductionRegistryEntry.identity_key == candidate.identity_key,
                    ProductionRegistryEntry.status == "COMPLETED",
                ))
                if existing is not None:
                    return StageOutcome(StageDecision.REJECTED, "PERMANENT_REGISTRY_DUPLICATE")
            finally:
                session.close()
        return StageOutcome(StageDecision.ACCEPTED, "CANONICAL_CAPTURE_READY", {
            "capture_sha256": candidate.capture_sha256,
            "identity_key": candidate.identity_key,
            "source_policy": self.source_policy.source_host,
            "identity_fields": candidate.lineage.get("identity_fields") or {},
            "media_binding_status": candidate.lineage.get("media_binding_status") or "UNKNOWN",
            "scope_status": candidate.lineage.get("scope_status") or "UNKNOWN",
        })

    def _stage_media(self, candidate: CandidateRecord) -> StageOutcome:
        binding_status = str(candidate.lineage.get("media_binding_status") or "UNKNOWN").upper()
        if binding_status == "MISMATCH" and self.source_policy.media_policy == "STRICT_MAIN_PRODUCT":
            return StageOutcome(StageDecision.REJECTED, MEDIA_IDENTITY_MISMATCH)
        if str(candidate.lineage.get("image_role") or "MAIN_PRODUCT").upper() != "MAIN_PRODUCT":
            return StageOutcome(StageDecision.REJECTED, "MEDIA_ROLE_NOT_MAIN_PRODUCT")
        existing = str(candidate.lineage.get("media_path") or "")
        if existing and Path(existing).is_file():
            self.pool.enrich_candidate(candidate.candidate_id, lineage={
                "image_decodable": True,
                "selected_media_url": candidate.lineage.get("image_url") or candidate.preview_url,
            })
            if self.source_policy.media_policy == "STRICT_MAIN_PRODUCT" and binding_status == "UNKNOWN":
                return StageOutcome(StageDecision.PENDING, L2_BROWSER_REQUIRED)
            return StageOutcome(StageDecision.ACCEPTED, "MEDIA_CHECKPOINT_REUSED", {
                "media_path": existing,
                "media_sha256": candidate.lineage.get("media_sha256"),
            })
        url = str(candidate.lineage.get("image_url") or candidate.preview_url or "")
        if not url:
            return StageOutcome(StageDecision.REJECTED, "MEDIA_URL_MISSING")
        try:
            result = self.media_client_factory(source_url=url, request_budget=8, timeout=30, request_delay=0).get_media(url)
        except Exception as error:
            return StageOutcome(StageDecision.REJECTED, f"MEDIA_FETCH_FAILED:{type(error).__name__}")
        raw = result.content
        valid_magic = raw.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8")) or (len(raw) > 12 and raw[8:12] in {b"WEBP", b"avif"})
        if len(raw) < 1024 or not valid_magic:
            return StageOutcome(StageDecision.REJECTED, "MEDIA_INVALID_OR_TOO_SMALL")
        suffix = ".jpg" if "jpeg" in result.content_type else ".png" if "png" in result.content_type else ".webp"
        target = self.media_root / f"{candidate.candidate_id}{suffix}"
        target.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        evidence = {
            "media_path": str(target),
            "media_sha256": digest,
            "media_bytes": len(raw),
            "content_type": result.content_type,
            "image_decodable": True,
            "selected_media_url": url,
            "media_role": "MAIN_PRODUCT",
        }
        self.pool.enrich_candidate(candidate.candidate_id, lineage=evidence)
        if self.source_policy.media_policy == "STRICT_MAIN_PRODUCT" and binding_status == "UNKNOWN":
            return StageOutcome(StageDecision.PENDING, L2_BROWSER_REQUIRED, evidence)
        return StageOutcome(StageDecision.ACCEPTED, "MEDIA_READY", evidence)

    def _product_decision(self, candidate: CandidateRecord) -> tuple[dict[str, Any] | None, str | None]:
        existing = candidate.lineage.get("brain_product_decision")
        if isinstance(existing, dict):
            return existing, None
        fixture = (candidate.lineage.get("capture_evidence") or {}).get("qualification") if isinstance(candidate.lineage.get("capture_evidence"), dict) else None
        if isinstance(fixture, dict) and os.getenv("FURNITURE_WORKFLOW_TEST_FIXTURES", "").strip().lower() in {"1", "true", "yes"}:
            decision = dict(fixture)
            self.pool.enrich_candidate(candidate.candidate_id, lineage={"brain_product_decision": decision, "brain_decision_source": "TEST_FIXTURE"})
            return decision, None
        evidence = {
            "source_name": candidate.lineage.get("source_name"),
            "source_brand": candidate.lineage.get("source_brand"),
            "category_group": candidate.category_group,
            "image_url": candidate.lineage.get("image_url"),
            "media_sha256": candidate.lineage.get("media_sha256"),
            "source_dimensions": candidate.lineage.get("source_dimensions"),
            "selected_media_url": candidate.lineage.get("selected_media_url") or candidate.lineage.get("image_url") or candidate.preview_url,
            "identity_fields": candidate.lineage.get("identity_fields") or {},
            "local_agent_review": (
                ((candidate.lineage.get("capture_evidence") or {}).get("local_agent_review")
                 or (candidate.lineage.get("capture_evidence") or {}).get("qualification"))
                if isinstance(candidate.lineage.get("capture_evidence"), dict)
                else None
            ),
        }
        try:
            decision, metadata = self.brain.reason_product(source_url=candidate.canonical_url, evidence=evidence)
        except BrainNotConfigured:
            return None, "BRAIN_NOT_CONFIGURED"
        except BrainError as error:
            return None, error.code
        payload = decision.model_dump(mode="json")
        self.pool.enrich_candidate(candidate.candidate_id, lineage={
            "brain_product_decision": payload,
            "brain_receipt": metadata,
        })
        return payload, None

    def _stage_visual(self, candidate: CandidateRecord) -> StageOutcome:
        decision, error = self._product_decision(candidate)
        if error:
            return StageOutcome(StageDecision.PENDING, error)
        assert decision is not None
        accepted = all(bool(decision.get(key)) for key in ("eligible", "single_product", "background_ok", "image_to_3d_suitable"))
        if not accepted:
            codes = ",".join(str(value) for value in decision.get("reason_codes") or [])
            self.pool.enrich_candidate(candidate.candidate_id, lineage={
                "visual_review_status": "REJECTED",
                "visual_review_reason_codes": decision.get("reason_codes") or [],
            })
            return StageOutcome(StageDecision.REJECTED, f"VISUAL_REJECTED:{codes or 'SEMANTIC_GATE'}")
        try:
            confidence = float(decision.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        receipt = candidate.lineage.get("brain_receipt") or {}
        if candidate.lineage.get("brain_decision_source") == "TEST_FIXTURE":
            review_provider = "TEST_FIXTURE"
        else:
            review_provider = str(receipt.get("review_provider") or self.brain.review_provider)
        consistency = decision.get("source_image_vision_consistent")
        reviewed_hash = str(decision.get("reviewed_media_sha256") or "")
        media_hash = str(candidate.lineage.get("media_sha256") or "")
        review_mode = str((receipt or {}).get("model_mode") or "")
        if review_mode in {"MULTIMODAL_SINGLE_MODEL", "TEXT_BRAIN_PLUS_VISION"}:
            visual_input_included = bool((receipt or {}).get("visual_input_included"))
            independent_receipt = (receipt or {}).get("independent_vision_receipt")
            if not visual_input_included or not reviewed_hash or reviewed_hash != media_hash:
                return StageOutcome(StageDecision.PENDING, "VISION_RECEIPT_INVALID_OR_MEDIA_HASH_MISMATCH")
            if review_mode == "TEXT_BRAIN_PLUS_VISION" and not isinstance(independent_receipt, dict):
                return StageOutcome(StageDecision.PENDING, "VISION_PROVIDER_NOT_CONFIGURED")
        if consistency is False or (reviewed_hash and media_hash and reviewed_hash != media_hash):
            self.pool.enrich_candidate(candidate.candidate_id, lineage={
                "visual_review_status": "REJECTED",
                "source_image_vision_consistent": False,
                "review_provider": review_provider,
            })
            return StageOutcome(StageDecision.REJECTED, MEDIA_IDENTITY_MISMATCH)
        review_evidence = {
            "visual_review_status": "PASS" if confidence >= 0.85 else "SECOND_REVIEW_REQUIRED",
            "visual_confidence": confidence,
            "visual_reason_codes": decision.get("reason_codes") or [],
            "review_provider": review_provider,
            "review_mode": review_mode or ("LOCAL_AGENT" if review_provider == "LOCAL_AGENT" else "TEXT_BRAIN_PLUS_VISION"),
            "reviewed_media_sha256": reviewed_hash or media_hash,
            "source_image_vision_consistent": True if consistency is None else bool(consistency),
            "visual_style": decision.get("style") or "",
            "visual_color": decision.get("color") or "",
            "visual_material": decision.get("material") or "",
            "visual_product_type": decision.get("product_type") or "",
            "visual_feature": decision.get("feature") or "",
        }
        self.pool.enrich_candidate(candidate.candidate_id, lineage=review_evidence)
        if confidence < 0.65:
            return StageOutcome(StageDecision.REJECTED, "REVIEW_REQUIRED:visual_confidence_below_0_65", review_evidence)
        if confidence < 0.85:
            return StageOutcome(StageDecision.PENDING, REVIEW_REQUIRED, review_evidence)
        return StageOutcome(StageDecision.ACCEPTED, "VISUAL_ACCEPTED", review_evidence)

    def _stage_category(self, candidate: CandidateRecord) -> StageOutcome:
        decision = candidate.lineage.get("brain_product_decision") or {}
        brain_group = str(decision.get("category_group") or "").strip()
        # category_group 保留「所属所选类目」的身份（scope canonical_name），
        # 用于 PER_CATEGORY 配额匹配；brain 的语义分组单独存 brain_category_group，
        # 避免用语义分组覆盖类目配额 key 导致 _category_has_capacity 恒 False、提交被跳过。
        group = str(candidate.category_group or "").strip() or brain_group
        if not group:
            return StageOutcome(StageDecision.REJECTED, "CATEGORY_UNRESOLVED")
        self.pool.enrich_candidate(candidate.candidate_id, category_group=group, lineage={
            "brain_category_group": brain_group or group,
            "category_authority": self.source_policy.category_authority,
        })
        return StageOutcome(StageDecision.ACCEPTED, "CATEGORY_ACCEPTED", {"category_group": group})

    def _stage_date(self, candidate: CandidateRecord) -> StageOutcome:
        return StageOutcome(StageDecision.ACCEPTED, "DATE_POLICY_NOT_RESTRICTED", {"date_policy": "SOURCE_CURRENT_PUBLIC_CATALOG"})

    def _stage_dimension(self, candidate: CandidateRecord) -> StageOutcome:
        axes = ("width", "depth", "height")
        values = candidate.lineage.get("source_dimensions") or {}
        source_hint = str(candidate.lineage.get("dimension_source") or "").strip().upper()
        source_aliases = {
            "EXPLICIT_PAGE_TEXT": "OFFICIAL_PAGE",
            "L2_BROWSER_DIMENSIONS_TAB": "OFFICIAL_PAGE",
            "L2_BROWSER_DIMENSIONS": "OFFICIAL_PAGE",
            "GRAPHQL_DESCRIPTION": "OFFICIAL_PAGE",
            "GRAPHQL_STRUCTURED": "OFFICIAL_STRUCTURED",
            "EXPLICIT_STRUCTURED": "OFFICIAL_STRUCTURED",
            "SOURCE_EXPLICIT": "OFFICIAL_PAGE",
        }
        source = source_aliases.get(source_hint, source_hint if source_hint in {"OFFICIAL_STRUCTURED", "OFFICIAL_PAGE"} else "")
        if all(values.get(axis) for axis in axes):
            source = source or "OFFICIAL_PAGE"
        else:
            # L1/HTML evidence may omit an accordion's Dimensions block.  Give
            # the same bounded L2 session one chance to obtain official values
            # before accepting any Brain estimate. A visible challenge is not
            # evidence that the official dimensions are absent: keep the
            # candidate resumable instead of silently converting an access
            # failure into AI_ESTIMATED dimensions.
            browser_dims: dict[str, float] = {}
            browser_unit = ""
            try:
                session_dir = Path(str(self.acquisition.browser_session_dir))
                browser_dims, browser_unit = NativeBrowserCollector(session_dir).extract_dimensions(str(candidate.canonical_url))
            except BrowserHumanRequired as error:
                return StageOutcome(
                    StageDecision.PENDING,
                    "DIMENSIONS_BROWSER_HUMAN_REQUIRED",
                    {
                        "dimension_access_status": "HUMAN_REQUIRED",
                        "dimension_access_reason": error.reason_code,
                        "dimension_access_url": error.url,
                    },
                )
            except BrowserTemporaryFailure as error:
                return StageOutcome(StageDecision.PENDING, error.reason_code, {
                    "dimension_access_status": "TEMPORARY_FAILURE",
                    "dimension_access_reason": error.reason_code,
                    "dimension_access_url": error.url,
                })
            except BrowserAccessDenied as error:
                return StageOutcome(StageDecision.PENDING, error.code, {
                    "dimension_access_status": "ACCESS_CHANGE_REQUIRED",
                    "dimension_access_reason": error.reason_code,
                    "dimension_access_url": error.url,
                })
            except BrowserRuntimeMissing:
                raise
            if all(browser_dims.get(axis) for axis in axes):
                values = {axis: browser_dims[axis] for axis in axes}
                source = "OFFICIAL_PAGE"
                candidate.lineage["dimension_source_detail"] = "L2_BROWSER_DIMENSIONS_TAB"
                candidate.lineage["dimension_unit"] = browser_unit or candidate.lineage.get("dimension_unit") or "source_unit"

        if not all(values.get(axis) for axis in axes):
            decision = candidate.lineage.get("brain_product_decision") or {}
            est_source = str(decision.get("dimension_source") or "").upper()
            # 官网无官方尺寸时按规则用 AI 预估高度（单轴）+ 后续等比缩放，
            # 不因缺少三轴尺寸而拒绝候选。
            if not any(values.get(axis) for axis in axes) and decision.get("height") and est_source == "AI_ESTIMATED":
                values = {"width": None, "depth": None, "height": float(decision["height"])}
                source = "AI_ESTIMATED"
                candidate.lineage["dimension_source_detail"] = "AI_HEIGHT_ESTIMATE"
                candidate.lineage["dimension_estimation"] = True
                unit = str(decision.get("dimension_unit") or "in").strip().casefold()
                candidate.lineage["dimension_unit"] = unit if unit in _SUPPORTED_DIMENSION_UNITS else "in"
            else:
                estimated_values = {axis: decision.get(axis) for axis in axes}
                if all(estimated_values.get(axis) for axis in axes):
                    values = estimated_values
                    source = est_source if est_source in {"OFFICIAL_STRUCTURED", "OFFICIAL_PAGE"} else "AI_ESTIMATED"
                    candidate.lineage["dimension_source_detail"] = "BRAIN_DECISION_AFTER_OFFICIAL_LOOKUP"
                    if decision.get("dimension_unit"):
                        candidate.lineage["dimension_unit"] = decision.get("dimension_unit")
        governed: dict[str, int] = {}
        try:
            for axis in axes:
                raw = values.get(axis)
                if raw in (None, ""):
                    continue
                governed[axis] = round_dimension(raw)
        except ValueError:
            return StageOutcome(StageDecision.REJECTED, "DIMENSIONS_MISSING_OR_INVALID")
        if not governed:
            return StageOutcome(StageDecision.REJECTED, "DIMENSIONS_MISSING_OR_INVALID")
        target_dimensions = dict(governed)
        evidence = {
            "dimensions": governed,
            "target_dimensions": target_dimensions,
            "dimension_source": source or "UNKNOWN",
            "dimension_unit": candidate.lineage.get("dimension_unit") or "source_unit",
            "dimension_source_detail": candidate.lineage.get("dimension_source_detail") or source,
            "dimension_estimation": bool(candidate.lineage.get("dimension_estimation")),
        }
        self.pool.enrich_candidate(candidate.candidate_id, lineage=evidence)
        return StageOutcome(StageDecision.ACCEPTED, "DIMENSIONS_READY", evidence)

    def _stage_naming(self, candidate: CandidateRecord) -> StageOutcome:
        source_name = _safe_name(str(candidate.lineage.get("source_name") or ""))
        source_brand = _safe_name(str(candidate.lineage.get("source_brand") or ""))
        source_type = str(self.contract.get("source_type") or "UNKNOWN")
        site_brand = _safe_name(str(
            self.source_policy.brand_display_name
            or self.contract.get("site_display_name")
            or self.contract.get("site_key")
            or ""
        ))
        decision = candidate.lineage.get("brain_product_decision") or {}
        reliable = len(source_name) >= 3 and source_name.casefold() not in {"product", "item", "untitled"}
        decision_source = "SOURCE_NAME_FIRST"

        # 品牌库（界面标记 / 配置登记）：品牌前缀 + 官方名 + 四段式（风格/颜色/材质/类型）。
        if bool(self.contract.get("is_brand_library")):
            brand = _safe_name(str(self.contract.get("brand_name") or site_brand or ""))
            try:
                if reliable:
                    governed_name = compose_brand_official_name(
                        brand=brand,
                        official_name=source_name,
                        style=decision.get("style") or candidate.lineage.get("visual_style"),
                        color=decision.get("color") or candidate.lineage.get("visual_color"),
                        material=decision.get("material") or candidate.lineage.get("visual_material"),
                        product_type=decision.get("product_type") or candidate.lineage.get("visual_product_type") or "",
                    )
                    decision_source = "BRAND_OFFICIAL_FOUR_PART"
                else:
                    governed_name = compose_product_name(
                        style=decision.get("style") or candidate.lineage.get("visual_style"),
                        color=decision.get("color") or candidate.lineage.get("visual_color"),
                        material=decision.get("material") or candidate.lineage.get("visual_material"),
                        product_type=decision.get("product_type") or candidate.lineage.get("visual_product_type"),
                        feature=decision.get("feature") or candidate.lineage.get("visual_feature"),
                        brand=brand,
                        brand_prefix_policy="REQUIRED",
                    )
                    decision_source = "BRAND_GOVERNED_FALLBACK"
            except NamingReviewRequired as error:
                return StageOutcome(StageDecision.REJECTED, f"NAMING_REVIEW:{error}")

        # CGTrader is visual-first: route/category only records discovery
        # scope and can never replace the reviewed product type.
        elif self.source_policy.source_host == "cgtrader.com":
            try:
                governed_name = compose_product_name(
                    style=decision.get("style") or candidate.lineage.get("visual_style"),
                    color=decision.get("color") or candidate.lineage.get("visual_color"),
                    material=decision.get("material") or candidate.lineage.get("visual_material"),
                    product_type=decision.get("product_type") or candidate.lineage.get("visual_product_type"),
                    feature=decision.get("feature") or candidate.lineage.get("visual_feature"),
                    brand="",
                    brand_prefix_policy="NONE",
                    contract_version=self.source_policy.naming_contract_version,
                )
                decision_source = "VISION_PRIMARY"
            except NamingReviewRequired as error:
                return StageOutcome(StageDecision.REJECTED, f"NAMING_REVIEW:{error}")
        elif self.source_policy.source_host == "roomandboard.com":
            if not reliable:
                return StageOutcome(StageDecision.REJECTED, "NAMING_REVIEW:official_product_name_missing")
            try:
                governed_name = compose_official_name(
                    source_name=source_name,
                    verified_type=decision.get("product_type") or candidate.lineage.get("visual_product_type") or source_name,
                    brand=site_brand,
                )
                decision_source = "OFFICIAL_NAME_WITH_VISION_VERIFY"
            except NamingReviewRequired as error:
                return StageOutcome(StageDecision.REJECTED, f"NAMING_REVIEW:{error}")
        else:
            governed_name = source_name
            if reliable and source_type == "DIRECT_BRAND" and site_brand and not source_name.casefold().startswith(site_brand.casefold()):
                governed_name = f"{site_brand} {source_name}"
            elif reliable and source_type in {"MULTI_BRAND_RETAILER", "MULTI_CATEGORY_RETAILER"} and source_brand and not source_name.casefold().startswith(source_brand.casefold()):
                governed_name = f"{source_brand} {source_name}"
            if not reliable:
                try:
                    governed_name = compose_product_name(
                        style=decision.get("style"),
                        color=decision.get("color"),
                        material=decision.get("material"),
                        product_type=decision.get("product_type"),
                        feature=decision.get("feature"),
                        brand=site_brand if source_type == "DIRECT_BRAND" else source_brand,
                        brand_prefix_policy="REQUIRED" if source_type == "DIRECT_BRAND" else "OPTIONAL" if source_type in {"MULTI_BRAND_RETAILER", "MULTI_CATEGORY_RETAILER"} else "NONE",
                    )
                    decision_source = "GOVERNED_FALLBACK"
                except NamingReviewRequired as error:
                    return StageOutcome(StageDecision.REJECTED, f"NAMING_REVIEW:{error}")
        try:
            governed_name = shorten_name_to_limit(
                governed_name,
                required_prefix=site_brand if self.source_policy.is_brand_direct else "",
                required_type=decision.get("product_type") or candidate.lineage.get("visual_product_type") or "",
                removable_phrases=("New", "Exclusive", "Collection", "Premium", "Signature", "Limited Edition"),
            )
        except NamingReviewRequired as error:
            return StageOutcome(StageDecision.REJECTED, f"NAMING_REVIEW:{error}")
        if len(governed_name) > 50:
            return StageOutcome(StageDecision.REJECTED, "NAMING_REVIEW:product_name_exceeds_50_characters")
        self.pool.enrich_candidate(candidate.candidate_id, product_name=governed_name, lineage={
            "governed_name": governed_name,
            "naming_decision_source": decision_source,
            "source_name_reliable": reliable,
            "platform_brand_excluded": source_type == "MARKETPLACE",
            "source_policy_host": self.source_policy.source_host,
            "source_title_as_evidence": source_name,
            "route_category_authority": self.source_policy.category_authority,
            "type_authority": self.source_policy.type_authority,
            "final_name_char_count": len(governed_name),
            "final_name_limit": 50,
        })
        return StageOutcome(StageDecision.ACCEPTED, "NAMING_READY", {
            "product_name": governed_name,
            "decision_source": decision_source,
            "final_name_char_count": len(governed_name),
            "final_name_limit": 50,
        })

    def _stage_catalog(self, candidate: CandidateRecord) -> StageOutcome:
        current = next(item for item in self.pool.records() if item.candidate_id == candidate.candidate_id)
        payload = {
            "record_id": current.record_id,
            "identity_key": current.identity_key,
            "canonical_url": current.canonical_url,
            "product_name": current.product_name,
            "category_group": current.category_group,
            "dimensions": current.lineage.get("dimensions"),
            "target_dimensions": current.lineage.get("target_dimensions") or current.lineage.get("dimensions"),
            "dimension_source": current.lineage.get("dimension_source") or "UNKNOWN",
            "dimension_unit": current.lineage.get("dimension_unit") or "source_unit",
            "media_sha256": current.lineage.get("media_sha256"),
            "identity_fields": current.lineage.get("identity_fields") or {},
            "media_binding_status": current.lineage.get("media_binding_status") or "UNKNOWN",
            "visual_review_status": current.lineage.get("visual_review_status") or "MISSING",
            "source_image_vision_consistent": current.lineage.get("source_image_vision_consistent"),
        }
        lock_hash = stable_hash(payload)
        self.pool.enrich_candidate(candidate.candidate_id, lineage={"catalog_lock_hash": lock_hash})
        return StageOutcome(StageDecision.ACCEPTED, "CATALOG_LOCKED", {"catalog_lock_hash": lock_hash})

    def _stage_model_input(self, candidate: CandidateRecord) -> StageOutcome:
        current = next(item for item in self.pool.records() if item.candidate_id == candidate.candidate_id)
        payload = {
            "job_id": current.job_id,
            "candidate_id": current.candidate_id,
            "image_sha256": current.lineage.get("media_sha256"),
            "dimensions": current.lineage.get("dimensions"),
            "target_dimensions": current.lineage.get("target_dimensions") or current.lineage.get("dimensions"),
            "dimension_source": current.lineage.get("dimension_source") or "UNKNOWN",
            "dimension_unit": current.lineage.get("dimension_unit") or "source_unit",
            "product_name": current.product_name,
            "catalog_lock_hash": current.lineage.get("catalog_lock_hash"),
            "provider": self.contract.get("provider"),
            "source_policy": self.source_policy.source_host,
            "media_binding_status": current.lineage.get("media_binding_status") or "UNKNOWN",
            "visual_review_status": current.lineage.get("visual_review_status") or "MISSING",
            "source_image_vision_consistent": current.lineage.get("source_image_vision_consistent"),
        }
        model_hash = stable_hash(payload)
        self.pool.enrich_candidate(candidate.candidate_id, lineage={"model_input_hash": model_hash, "model_input": payload})
        return StageOutcome(StageDecision.ACCEPTED, "MODEL_INPUT_LOCKED", {"model_input_hash": model_hash})

    def _ledger(self, candidate: CandidateRecord) -> ProductionProviderTask | None:
        session = self.database.session_factory()
        try:
            return session.scalar(select(ProductionProviderTask).where(
                ProductionProviderTask.job_id == self.contract["job_id"],
                ProductionProviderTask.candidate_id == candidate.candidate_id,
            ))
        finally:
            session.close()

    def provider_capacity_waiting(self) -> bool:
        session = self.database.session_factory()
        try:
            return session.scalar(
                select(ProductionProviderTask.ledger_id)
                .where(
                    ProductionProviderTask.job_id == self.contract["job_id"],
                    ProductionProviderTask.status == "CAPACITY_WAIT",
                )
                .limit(1)
            ) is not None
        finally:
            session.close()

    def _production_gate_facts(self, current: CandidateRecord, *, idempotency_key: str) -> dict[str, Any]:
        """Assemble immutable facts for the final pre-provider gate."""

        return {
            "identity": current.identity_key,
            "identity_fields": current.lineage.get("identity_fields") or {},
            "dedup_status": current.lineage.get("dedup_status") or "UNIQUE",
            "claim_status": current.lineage.get("claim_status") or "CLAIMED",
            "media_binding_status": current.lineage.get("media_binding_status") or "UNKNOWN",
            "media_binding_confidence": current.lineage.get("media_binding_confidence") or 0,
            "image_decodable": current.lineage.get("image_decodable"),
            "visual_review_status": current.lineage.get("visual_review_status") or "MISSING",
            "visual_confidence": current.lineage.get("visual_confidence"),
            "review_provider": current.lineage.get("review_provider") or "",
            "source_image_vision_consistent": current.lineage.get("source_image_vision_consistent"),
            "scope_status": current.lineage.get("scope_status") or "UNKNOWN",
            "final_name": current.product_name,
            "dimensions": current.lineage.get("dimensions"),
            "provider_idempotency_key": idempotency_key,
        }

    def _approved_provider_call_limit(self) -> int:
        plan = self.contract.get("approved_plan")
        raw = plan.get("approved_provider_call_limit") if isinstance(plan, dict) else None
        if raw in (None, ""):
            # Backward-compatible safe default for pre-hardening Exact-N Jobs.
            raw = self.contract.get("target_value")
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _provider_slot_reserved(ledger: ProductionProviderTask) -> bool:
        return bool(
            ledger.provider_task_id
            or ledger.status in {"CREATE_IN_FLIGHT", "SUBMISSION_UNKNOWN"}
            or ledger.checkpoint_state in {"CREATE_IN_FLIGHT", "SUBMISSION_UNKNOWN"}
        )

    def _stage_submit(self, candidate: CandidateRecord) -> StageOutcome:
        provider = str(self.contract.get("provider") or "OFF").casefold()
        if provider == "off" or self.provider_client is None:
            return StageOutcome(StageDecision.PENDING, "PROVIDER_OFF_OR_NOT_CONFIGURED")
        current = next(item for item in self.pool.records() if item.candidate_id == candidate.candidate_id)
        model_hash = str(current.lineage.get("model_input_hash") or "")
        key = provider_idempotency_key(order_id=current.order_id, record_id=current.record_id, model_input_hash=model_hash, provider=provider)
        gate = self.production_gate.evaluate(self._production_gate_facts(current, idempotency_key=key))
        self.pool.enrich_candidate(candidate.candidate_id, lineage={
            "production_gate": gate.as_dict(),
            "production_gate_status": gate.status,
            "production_gate_reasons": list(gate.reasons),
        })
        if not gate.ready:
            if gate.status == MEDIA_IDENTITY_MISMATCH or gate.status == SCOPE_VISUAL_CONFLICT:
                return StageOutcome(StageDecision.REJECTED, gate.status, gate.as_dict())
            return StageOutcome(StageDecision.PENDING, gate.status, gate.as_dict())
        session = self.database.session_factory()
        try:
            ledger = session.scalar(select(ProductionProviderTask).where(ProductionProviderTask.idempotency_key == key))
            if ledger is not None and ledger.provider_task_id:
                return StageOutcome(StageDecision.ACCEPTED, "KNOWN_PROVIDER_TASK_RESUMED", provider_task_id=ledger.provider_task_id, provider=provider)
            if ledger is not None and (
                ledger.status == "SUBMISSION_UNKNOWN"
                or (ledger.post_attempts > 0 and ledger.status != "CAPACITY_WAIT")
            ):
                ledger.status = ledger.checkpoint_state = "SUBMISSION_UNKNOWN"
                ledger.error_code = "SUBMISSION_UNKNOWN"
                ledger.error_message = "create POST may have reached Provider; manual reconciliation required"
                session.commit()
                return StageOutcome(StageDecision.HARD_STOP, "SUBMISSION_UNKNOWN: existing ambiguous create checkpoint")
            call_limit = self._approved_provider_call_limit()
            ledgers = list(session.scalars(select(ProductionProviderTask).where(
                ProductionProviderTask.job_id == self.contract["job_id"],
            )))
            reserved_calls = sum(self._provider_slot_reserved(item) for item in ledgers)
            imminent = 0 if ledger is not None and self._provider_slot_reserved(ledger) else 1
            if call_limit <= 0 or reserved_calls + imminent > call_limit:
                return StageOutcome(StageDecision.HARD_STOP, "PROVIDER_CALL_LIMIT_REACHED", {
                    "approved_provider_call_limit": call_limit,
                    "reserved_provider_calls": reserved_calls,
                    "imminent_provider_calls": imminent,
                })
            if ledger is None:
                ledger = ProductionProviderTask(
                    ledger_id=f"ledger_{uuid4().hex}",
                    job_id=str(self.contract["job_id"]),
                    candidate_id=current.candidate_id,
                    record_id=current.record_id,
                    provider=provider,
                    idempotency_key=key,
                    model_input_hash=model_hash,
                    request_json=_json({"image_sha256": current.lineage.get("media_sha256"), "model_input_hash": model_hash}),
                    status="PREPARED",
                    checkpoint_state="PREPARED",
                )
                session.add(ledger)
                session.commit()
            image_path = Path(str(current.lineage.get("media_path") or ""))
            ledger.post_attempts += 1
            ledger.status = ledger.checkpoint_state = "CREATE_IN_FLIGHT"
            session.commit()
            self.provider_posts += 1
            task_id, error = self.provider_client.create_task(image_path, idempotency_key=key)
            if task_id:
                ledger.provider_task_id = task_id
                ledger.status = ledger.checkpoint_state = "ACTIVE"
                ledger.submitted_at = utc_now()
                ledger.error_code = ledger.error_message = None
                session.commit()
                return StageOutcome(StageDecision.ACCEPTED, "PROVIDER_TASK_CREATED", provider_task_id=task_id, provider=provider)
            if is_capacity_rejection(str(error or "")):
                ledger.status = ledger.checkpoint_state = "CAPACITY_WAIT"
                ledger.error_code = "PROVIDER_CAPACITY"
                ledger.error_message = str(error or "")[:1000]
                session.commit()
                return StageOutcome(StageDecision.PENDING, "Provider capacity wait; no task ID was created")
            ledger.status = ledger.checkpoint_state = "SUBMISSION_UNKNOWN"
            ledger.error_code = "SUBMISSION_UNKNOWN"
            ledger.error_message = str(error or "create returned no trustworthy task ID")[:1000]
            session.commit()
            return StageOutcome(StageDecision.HARD_STOP, "SUBMISSION_UNKNOWN: Provider create outcome is ambiguous")
        finally:
            session.close()

    def _stage_poll(self, candidate: CandidateRecord) -> StageOutcome:
        if self.provider_client is None or not candidate.provider_task_id:
            return StageOutcome(StageDecision.HARD_STOP, "KNOWN_PROVIDER_TASK_REQUIRED")
        result, error = self.provider_client.poll_task(candidate.provider_task_id)
        session = self.database.session_factory()
        try:
            ledger = session.scalar(select(ProductionProviderTask).where(
                ProductionProviderTask.job_id == self.contract["job_id"],
                ProductionProviderTask.candidate_id == candidate.candidate_id,
            ))
            if ledger is None:
                return StageOutcome(StageDecision.HARD_STOP, "PROVIDER_LEDGER_MISSING")
            ledger.poll_attempts += 1
            if error:
                ledger.error_code = "POLL_PENDING" if "timeout" in error.casefold() else "PROVIDER_FAILED"
                ledger.error_message = error[:1000]
                ledger.status = ledger.checkpoint_state = "ACTIVE" if "timeout" in error.casefold() else "FAILED"
                session.commit()
                return StageOutcome(StageDecision.PENDING if "timeout" in error.casefold() else StageDecision.REJECTED, error)
            ledger.response_json = _json(result or {})
            ledger.status = ledger.checkpoint_state = "PROVIDER_SUCCESS"
            session.commit()
            return StageOutcome(StageDecision.ACCEPTED, "PROVIDER_SUCCESS")
        finally:
            session.close()

    def _stage_download(self, candidate: CandidateRecord) -> StageOutcome:
        if self.provider_client is None or not candidate.provider_task_id:
            return StageOutcome(StageDecision.HARD_STOP, "KNOWN_PROVIDER_TASK_REQUIRED")
        session = self.database.session_factory()
        try:
            ledger = session.scalar(select(ProductionProviderTask).where(
                ProductionProviderTask.job_id == self.contract["job_id"],
                ProductionProviderTask.candidate_id == candidate.candidate_id,
            ))
            if ledger is None:
                return StageOutcome(StageDecision.HARD_STOP, "PROVIDER_LEDGER_MISSING")
            try:
                result = json.loads(ledger.response_json or "{}")
            except json.JSONDecodeError:
                result = {}
            filename = _safe_name(candidate.product_name or candidate.record_id) or candidate.record_id
            target = self.model_root / f"{filename}__{candidate.record_id}.glb"
            if not self.provider_client.download_glb(result, candidate.provider_task_id, target):
                ledger.status = ledger.checkpoint_state = "DOWNLOAD_FAILED"
                ledger.error_code = "DOWNLOAD_FAILED"
                session.commit()
                return StageOutcome(StageDecision.REJECTED, "RAW_GLB_DOWNLOAD_FAILED")
            valid, validation_reason = validate_glb(target)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if not valid:
                ledger.status = ledger.checkpoint_state = "RAW_GLB_INVALID"
                ledger.error_code = "RAW_GLB_INVALID"
                ledger.error_message = validation_reason
                ledger.finished_at = utc_now()
                session.commit()
                return StageOutcome(
                    StageDecision.REJECTED,
                    f"RAW_GLB_INVALID:{validation_reason}",
                    raw_glb_path=str(target),
                    raw_glb_sha256=digest,
                    raw_glb_valid=False,
                )
            if self.blender_adapter is None:
                ledger.status = ledger.checkpoint_state = "BLENDER_NOT_CONFIGURED"
                ledger.error_code = "BLENDER_NOT_CONFIGURED"
                ledger.error_message = "A configured Blender normalization/QA adapter is required before delivery"
                session.commit()
                return StageOutcome(StageDecision.HARD_STOP, "BLENDER_NOT_CONFIGURED")
            normalized_target = self.model_root / "normalized" / target.name
            try:
                current = next(item for item in self.pool.records() if item.candidate_id == candidate.candidate_id)
                qa = self.blender_adapter.normalize_and_qa(
                    target,
                    normalized_target,
                    target_dimensions=current.lineage.get("target_dimensions") or current.lineage.get("dimensions"),
                    dimension_unit=current.lineage.get("dimension_unit") or "source_unit",
                )
            except ModelDimensionConflict as error:
                ledger.status = ledger.checkpoint_state = "MODEL_DIMENSION_CONFLICT"
                ledger.error_code = "MODEL_DIMENSION_CONFLICT"
                ledger.error_message = str(error)[:1000]
                ledger.finished_at = utc_now()
                session.commit()
                return StageOutcome(StageDecision.REJECTED, f"MODEL_DIMENSION_CONFLICT:{error}")
            except BlenderAdapterError as error:
                ledger.status = ledger.checkpoint_state = "BLENDER_QA_FAILED"
                ledger.error_code = "BLENDER_QA_FAILED"
                ledger.error_message = str(error)[:1000]
                ledger.finished_at = utc_now()
                session.commit()
                return StageOutcome(StageDecision.REJECTED, f"BLENDER_QA_FAILED:{error}")
            ledger.status = ledger.checkpoint_state = "DELIVERED"
            ledger.error_code = ledger.error_message = None
            ledger.finished_at = utc_now()
            session.commit()
            qa_payload = qa.as_dict() if hasattr(qa, "as_dict") else dict(qa)
            return StageOutcome(
                StageDecision.ACCEPTED,
                "RAW_GLB_AND_BLENDER_QA_VALIDATED",
                {
                    "blender_qa": qa_payload,
                    "blender_qa_status": str(qa_payload.get("status") or "UNKNOWN"),
                    "normalized_glb_path": str(qa_payload.get("normalized_path") or normalized_target),
                    "normalized_glb_sha256": str(qa_payload.get("sha256") or ""),
                },
                raw_glb_path=str(target),
                raw_glb_sha256=digest,
                raw_glb_valid=True,
            )
        finally:
            session.close()


class ProductionPipeline:
    def __init__(
        self,
        *,
        contract: dict[str, Any],
        workspace: Path,
        emit: Emit,
        acquisition_factory=ProductAcquisitionEngine,
        brain: WebsiteBrainProvider | None = None,
        provider_client: Lux3DClient | None = None,
        blender_adapter: Any | None = None,
        media_client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.contract = contract
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.emit = emit
        self.source_policy = resolve_source_policy(
            str(contract.get("source_url") or contract.get("site_key") or "")
        )
        self.database = Database(Path(str(contract["database_path"])))
        self.database.create_schema()
        self.blender_adapter = blender_adapter if blender_adapter is not None else resolve_blender_adapter(contract)
        self.media_client_factory = media_client_factory
        browser = contract.get("browser_session") or {}
        self.acquisition = acquisition_factory(
            source_url=str(contract["source_url"]),
            site_key=str(contract["site_key"]),
            source_type=str(contract.get("source_type") or "UNKNOWN"),
            categories=list(contract.get("categories") or []),
            workspace=self.workspace,
            browser_session_dir=Path(str(browser.get("user_data_dir") or self.workspace / "browser_session")),
        )
        self.brain = brain or WebsiteBrainProvider()
        self.provider_client = provider_client if provider_client is not None else self._provider_client()
        self.pool = CandidatePoolStore(self.workspace / "candidate_pool.json", order_id=str(contract["job_id"]), job_id=str(contract["job_id"]))

    def _provider_client(self) -> Lux3DClient | None:
        if str(self.contract.get("provider") or "OFF").casefold() != "lux3d":
            return None
        api_key = os.getenv("LUX3D_API_KEY", "").strip()
        base_url = os.getenv("LUX3D_BASE_URL", "").strip()
        if not api_key or not base_url:
            return None
        return Lux3DClient(
            base_url=base_url,
            api_key=api_key,
            version=os.getenv("LUX3D_VERSION", "v3.0-standard"),
            face_count=int(os.getenv("LUX3D_FACE_COUNT", "60000")),
            interval=float(os.getenv("LUX3D_QUERY_INTERVAL", "15")),
            max_attempts=int(os.getenv("LUX3D_QUERY_MAX_ATTEMPTS", "80")),
        )

    def _target_and_quotas(self) -> tuple[int, dict[str, int], str]:
        mode = str(self.contract.get("target_mode") or "EXACT_N")
        base = int(self.contract.get("target_value") or 0)
        categories = list(self.contract.get("categories") or [])
        if mode == "ALL":
            try:
                products = self.acquisition.discover(5000)
            except ProductSupplyExhausted:
                products = []
            if products:
                self.pool.add_candidates([_candidate_from_product(self.contract, item) for item in products])
            base = max(1, len(self.pool.records()))
        allocation = str(self.contract.get("category_allocation") or "TOTAL_ACROSS_SELECTED")
        scope_ids = [str(item.get("category_id") or "").strip() for item in categories]
        scope_ids = [value for value in scope_ids if value]
        if allocation == "PER_CATEGORY":
            # 每个类目分别满足：用户填的数字是「每个类目各自的数量」，总计 = 数量 × 类目数。
            quotas = {scope_id: base for scope_id in scope_ids}
            return max(1, base * len(scope_ids)), quotas, "REQUIRED"
        # 所选类目合计 + 平均分配：把总数 N 硬性均分到每个已选类目（总计仍是 N），
        # 而不是像 PER_CATEGORY 那样把 N 当成每个类目的量导致翻倍。
        strategy = str(self.contract.get("allocation_strategy") or "SEQUENTIAL").upper()
        if strategy == "EVEN" and categories:
            quotas = _distribute_evenly(max(1, base), scope_ids)
            return max(1, base), quotas, "REQUIRED"
        if strategy == "PROPORTIONAL" and categories:
            weights: dict[str, int] = {}
            for item in categories:
                scope_id = str(item.get("category_id") or "").strip()
                value = item.get("count_value")
                kind = str(item.get("count_kind") or "UNKNOWN").upper()
                if not scope_id or kind == "UNKNOWN" or not isinstance(value, int) or value < 0:
                    raise ProductAcquisitionError("CATEGORY_COUNTS_REQUIRED_FOR_PROPORTIONAL")
                weights[scope_id] = value
            return max(1, base), _distribute_proportionally(max(1, base), weights), "REQUIRED"
        if strategy == "CUSTOM" and categories:
            raw = self.contract.get("category_quotas")
            custom = raw if isinstance(raw, dict) else {}
            if set(custom) != set(scope_ids) or any(int(custom.get(scope_id, 0)) < 0 for scope_id in scope_ids):
                raise ProductAcquisitionError("CUSTOM_CATEGORY_QUOTAS_REQUIRED_BY_SCOPE_ID")
            quotas = {scope_id: int(custom[scope_id]) for scope_id in scope_ids}
            if sum(quotas.values()) != max(1, base):
                raise ProductAcquisitionError("CUSTOM_CATEGORY_QUOTAS_MUST_SUM_TO_TARGET")
            return max(1, base), quotas, "REQUIRED"
        return max(1, base), {}, "NONE"

    def _record_completion(self, record: CandidateRecord) -> None:
        session = self.database.session_factory()
        try:
            entry = session.scalar(select(ProductionRegistryEntry).where(
                ProductionRegistryEntry.site_key == self.contract["site_key"],
                ProductionRegistryEntry.identity_key == record.identity_key,
            ))
            if entry is None:
                entry = ProductionRegistryEntry(
                    registry_id=f"registry_{uuid4().hex}",
                    site_key=str(self.contract["site_key"]),
                    identity_key=record.identity_key,
                    source_product_id=record.source_product_id,
                    canonical_url=record.canonical_url,
                    first_job_id=record.job_id,
                )
                session.add(entry)
            entry.source_name = str(record.lineage.get("source_name") or "")
            entry.governed_name = str(record.product_name or "")
            entry.source_brand = str(record.lineage.get("source_brand") or "")
            entry.category_id = str(record.lineage.get("category_id") or "") or None
            entry.category_group = record.category_group
            entry.completed_job_id = record.job_id
            entry.image_sha256 = str(record.lineage.get("media_sha256") or "") or None
            entry.raw_glb_sha256 = record.raw_glb_sha256
            entry.raw_glb_path = record.raw_glb_path
            entry.evidence_json = _json({"model_input_hash": record.model_input_hash, "catalog_lock_hash": record.lineage.get("catalog_lock_hash")})
            entry.status = "COMPLETED"
            session.commit()
        finally:
            session.close()

    def _emit_granular_progress(self, records: list[CandidateRecord], emitted: int) -> int:
        """把 DISCOVERY 内部逐候选推进的阶段逐步点亮，让前端时间线线性前进。

        返回已发射的最高细分阶段编号；调用方把它保存下来，避免重复发射。
        """
        reached = _max_granular_stage(records)
        for index in range(emitted + 1, reached + 1):
            label = _GRANULAR_STAGE_LABEL[index]
            self.emit(
                "PROGRESS",
                label,
                _GRANULAR_STAGE_DETAIL[index],
                None,
                None,
                {"granular_stage": label},
            )
        return max(emitted, reached)

    def run(self) -> int:
        if not self.contract.get("categories"):
            self.emit("JOB_BLOCKED", "TARGET_POLICY", "没有持久化的已选类目，生产不会扩大范围", 0, 0, {"blocker": "CATEGORY_SELECTION_REQUIRED"})
            return 2
        if str(self.contract.get("provider") or "OFF").casefold() != "off" and not bool((self.contract.get("authorization") or {}).get("approve_paid_generation")):
            self.emit("JOB_BLOCKED", "PROVIDER_SAFETY", "Provider qualification/cost authorization 未进入 PRODUCTION_READY", 0, int(self.contract.get("target_value") or 0), {"blocker": "PRODUCTION_READY_REQUIRED", "provider_calls": 0})
            return 2
        try:
            target, quotas, quota_mode = self._target_and_quotas()
        except BrowserHumanRequired as error:
            # target_mode=ALL 会对全站做 broad discover，可能触发反爬验证；
            # 按候选级一致的处理转为可恢复的 HUMAN_REQUIRED，而不是 JOB_FAILED。
            self.emit("HUMAN_REQUIRED", "L2_BROWSER", str(error), 0, int(self.contract.get("target_value") or 0), {"reason_code": error.reason_code, "url": error.url, "browser_session_dir": str(error.session_dir)})
            return 2
        except BrowserRuntimeMissing as error:
            self.emit("JOB_BLOCKED", "L2_BROWSER", str(error), 0, 0, {"blocker": error.code, "reason_code": error.code})
            return 2
        except NetworkPolicyError as error:
            reason = "ROBOTS_DENIED" if isinstance(error, RobotsDenied) else type(error).__name__.upper()
            self.emit("JOB_BLOCKED", "DISCOVERY", str(error), 0, int(self.contract.get("target_value") or 0), {"blocker": reason, "reason_code": reason, "browser_escalation": False})
            return 2
        except ProductAcquisitionError as error:
            reason = str(error).strip() if str(error).strip().isupper() else error.code
            self.emit("JOB_BLOCKED", "DISCOVERY", str(error), 0, 0, {"blocker": reason, "reason_code": reason})
            return 2
        policy_lock = make_order_policy_lock(
            source=str(self.contract.get("site_key") or "Website"),
            categories=quotas,
            exact_n=target,
            provider=str(self.contract.get("provider") or "OFF"),
            ruleset="website-production-20260826",
            image_policy=self.source_policy.media_policy,
            five_year_policy="source-current-public-catalog",
            naming_policy=f"{self.source_policy.naming_contract_version}:{self.source_policy.type_authority}",
            dimension_policy="source-explicit-or-l2-browser-or-review",
            registry_identity="production_registry_entries",
            registry_version="v1",
            authorization_mode="COST_CEILING",
            quality_policy="validated-raw-glb",
            category_quota_mode=quota_mode,
            policy_revision="20260826.1",
            allowed_product_scope=str(self.contract.get("scope") or "NEW_ONLY"),
        )
        gates = tuple(sorted(set(value for value in (1, 3, target) if value <= target)))
        try:
            requested_slots = int(
                self.contract.get("provider_concurrency")
                or os.getenv("WEBSITE_PROVIDER_CONCURRENCY", "1")
            )
        except (TypeError, ValueError):
            requested_slots = 1
        provider_slots = max(1, min(5, requested_slots))
        policy = RuntimePolicy(
            order_id=str(self.contract["job_id"]),
            job_id=str(self.contract["job_id"]),
            target_count=target,
            progressive_gates=gates,
            provider=str(self.contract.get("provider") or "OFF"),
            order_policy=policy_lock,
            max_provider_slots=provider_slots,
            max_steps_per_tick=20,
            max_refill_rounds=100,
            spillover=str(self.contract.get("spillover") or "ASK").upper(),
        )
        adapter = WebsiteStageAdapter(
            contract=self.contract,
            database=self.database,
            pool=self.pool,
            acquisition=self.acquisition,
            workspace=self.workspace,
            brain=self.brain,
            provider_client=self.provider_client,
            blender_adapter=self.blender_adapter,
            media_client_factory=self.media_client_factory,
            emit=self.emit,
        )
        pool_payload = self.pool.read()
        previous_policy_hash = str(pool_payload.get("order_policy_hash") or "").strip()
        if previous_policy_hash and previous_policy_hash != policy_lock.order_policy_hash:
            session = self.database.session_factory()
            try:
                existing_provider_ledger = session.scalar(
                    select(ProductionProviderTask.ledger_id)
                    .where(ProductionProviderTask.job_id == self.contract["job_id"])
                    .limit(1)
                )
            finally:
                session.close()
            if existing_provider_ledger:
                self.emit(
                    "JOB_BLOCKED",
                    "PROVIDER_SAFETY",
                    "已有 Provider 持久账本，禁止自动改变订单策略或重新提交",
                    self.pool.success_count(),
                    target,
                    {"blocker": "ORDER_POLICY_LOCKED_BY_PROVIDER_LEDGER", "provider_calls": 0},
                )
                return 2
            self.pool.migrate_order_policy_hash(
                previous_policy_hash,
                policy_lock.order_policy_hash,
                target_count=target,
                progressive_gates=gates,
                reason="same Job resumed with an approved production policy before any active Provider task",
            )
            provider = str(self.contract.get("provider") or "OFF")
            for record in self.pool.records():
                if record.state is not ItemState.MODEL_INPUT_LOCKED:
                    continue
                model_input = dict(record.lineage.get("model_input") or {})
                if str(model_input.get("provider") or "OFF").casefold() == provider.casefold():
                    continue
                previous_model_input_hash = str(record.lineage.get("model_input_hash") or "")
                model_input["provider"] = provider
                self.pool.enrich_candidate(
                    record.candidate_id,
                    lineage={
                        "model_input": model_input,
                        "model_input_hash": stable_hash(model_input),
                        "previous_model_input_hash": previous_model_input_hash,
                        "model_input_policy_migrated": True,
                    },
                )
        engine = ProductionWorkflowEngine(policy=policy, pool=self.pool, adapter=adapter, completion_recorder=self._record_completion)
        granular_emitted = 0
        if not self.pool.records():
            try:
                initial = adapter.refill(needed=target, pool=self.pool)
            except BrowserHumanRequired as error:
                self.emit("HUMAN_REQUIRED", "L2_BROWSER", str(error), 0, target, {"reason_code": error.reason_code, "url": error.url, "browser_session_dir": str(error.session_dir)})
                return 2
            except BrowserRuntimeMissing as error:
                self.emit("JOB_BLOCKED", "L2_BROWSER", str(error), 0, target, {"blocker": error.code, "reason_code": error.code})
                return 2
            except SupplyExhaustedError as error:
                self.emit("TARGET_SHORTAGE", "DISCOVERY", str(error), 0, target, {"shortage": target, "reason_code": getattr(error, "code", "SUPPLY_EXHAUSTED")})
                return 2
            except ProductAcquisitionError as error:
                reason = str(error).strip() if str(error).strip().isupper() else error.code
                self.emit("JOB_BLOCKED", "DISCOVERY", str(error), 0, target, {"blocker": reason, "reason_code": reason})
                return 2
            added = self.pool.add_candidates(initial)["added"]
            self.emit("DISCOVERY_COMPLETED", "DISCOVERY", f"已在所选范围发现 {added} 个唯一候选", added, target, {"discovered_count": added, "unique_count": added, "provider_calls": 0})
            granular_emitted = self._emit_granular_progress(self.pool.records(), granular_emitted)
        try:
            for _ in range(max(80, target * 20)):
                records = self.pool.records()
                granular_emitted = self._emit_granular_progress(records, granular_emitted)
                ready = sum(item.state is ItemState.MODEL_INPUT_LOCKED for item in records)
                provider_off = str(self.contract.get("provider") or "OFF").casefold() == "off"
                if provider_off and ready and ready == len(records) and ready < target:
                    try:
                        refill = adapter.refill(needed=target - ready, pool=self.pool)
                    except SupplyExhaustedError:
                        mode = str(self.contract.get("target_mode") or "EXACT_N")
                        if mode in ("UP_TO_N", "ALL"):
                            self.pool.set_job_status("READY_POOL", reason=f"{mode} supply exhausted at {ready}/{target}")
                            self.emit("READY_POOL_COMPLETED", "READY_POOL", f"{'Up-To ' if mode == 'UP_TO_N' else '全部 '}{target} Ready Pool 已形成 {ready} 个；所选范围已耗尽且 Provider OFF", ready, target, {"ready_count": ready, "eligible_count": ready, "provider_calls": adapter.provider_posts, "target_mode": mode, "candidate_pool_path": str(self.pool.path)})
                            self.emit("JOB_BLOCKED", "PROVIDER_SAFETY", "Ready Pool 已保存；选择并审批 Provider 后恢复同一 Job", ready, target, {"blocker": "PROVIDER_REQUIRED", "ready_count": ready, "provider_calls": adapter.provider_posts, "target_mode": mode})
                            return 2
                        self.emit("TARGET_SHORTAGE", "EXACT_N", f"所选范围已耗尽：Ready Pool {ready}/{target}", ready, target, {"shortage": target - ready, "provider_calls": adapter.provider_posts})
                        return 2
                    added = self.pool.add_candidates(refill)["added"]
                    self.emit("REFILL_COMPLETED", "DISCOVERY", f"Ready Pool 补货新增 {added} 个唯一候选", ready + added, target, {"refill_added": added, "provider_calls": adapter.provider_posts})
                    continue
                if provider_off and ready >= target:
                    target_mode = str(self.contract.get("target_mode") or "EXACT_N")
                    retired_ready = 0
                    if target_mode == "EXACT_N" and ready > target:
                        # One engine tick may qualify several candidates before
                        # this outer safety check runs. Keep the first Exact-N
                        # locks in creation order and close the surplus as
                        # auditable, never-submitted candidates.
                        ready_records = sorted(
                            (item for item in self.pool.records() if item.state is ItemState.MODEL_INPUT_LOCKED),
                            key=lambda item: (item.created_at, item.candidate_id),
                        )
                        for extra in ready_records[target:]:
                            self.pool.retire_order_complete_not_needed(
                                extra.candidate_id,
                                reason="EXACT_N_TARGET_REACHED",
                            )
                            retired_ready += 1
                        ready = sum(item.state is ItemState.MODEL_INPUT_LOCKED for item in self.pool.records())
                    label = f"Up-To {target}" if target_mode == "UP_TO_N" else f"Exact {target}"
                    self.emit("READY_POOL_COMPLETED", "READY_POOL", f"{label} Ready Pool 已形成；Provider OFF，未发起外部调用", ready, target, {"ready_count": ready, "eligible_count": ready, "retired_ready": retired_ready, "provider_calls": adapter.provider_posts, "target_mode": target_mode, "candidate_pool_path": str(self.pool.path)})
                    self.emit("JOB_BLOCKED", "PROVIDER_SAFETY", "Ready Pool 已保存；选择并审批 Provider 后恢复同一 Job", ready, target, {"blocker": "PROVIDER_REQUIRED", "ready_count": ready, "retired_ready": retired_ready, "provider_calls": adapter.provider_posts, "target_mode": target_mode})
                    return 2
                before_tick = _candidate_progress_signature(self.pool.records())
                status = engine.tick()
                if status is RuntimeStatus.SUCCEEDED:
                    return self._deliver(target, adapter.provider_posts)
                if status is RuntimeStatus.SUPPLY_EXHAUSTED:
                    count = self.pool.success_count()
                    mode = str(self.contract.get("target_mode") or "EXACT_N")
                    if mode in ("UP_TO_N", "ALL"):
                        if str(self.contract.get("provider") or "OFF").casefold() == "off":
                            ready = sum(item.state is ItemState.MODEL_INPUT_LOCKED for item in self.pool.records())
                            if ready:
                                self.pool.set_job_status("READY_POOL", reason=f"{mode} supply exhausted at {ready}/{target}")
                                self.emit("READY_POOL_COMPLETED", "READY_POOL", f"{'Up-To ' if mode == 'UP_TO_N' else '全部 '}{target} Ready Pool 已形成 {ready} 个；所选范围已耗尽且 Provider OFF", ready, target, {"ready_count": ready, "eligible_count": ready, "provider_calls": adapter.provider_posts, "target_mode": mode, "candidate_pool_path": str(self.pool.path)})
                                self.emit("JOB_BLOCKED", "PROVIDER_SAFETY", "Ready Pool 已保存；选择并审批 Provider 后恢复同一 Job", ready, target, {"blocker": "PROVIDER_REQUIRED", "ready_count": ready, "provider_calls": adapter.provider_posts, "target_mode": mode})
                                return 2
                        elif count:
                            self.pool.set_job_status("SUCCEEDED", reason=f"{mode} delivered {count}/{target} after supply exhaustion")
                            return self._deliver(count, adapter.provider_posts, requested_target=target)
                    self.emit("TARGET_SHORTAGE", "EXACT_N", f"所选范围已耗尽：完成 {count}/{target}", count, target, {"shortage": target - count, "provider_calls": adapter.provider_posts})
                    if count and bool((self.contract.get("allow_shortfall_delivery") or False)):
                        # 操作员已明确「接受缺额交付」：把实际完成的 count 个交付，
                        # 不再因未达到 Exact-N 而阻断整批。
                        self.pool.set_job_status("SUCCEEDED", reason=f"EXACT_N delivered {count}/{target} (shortfall accepted)")
                        return self._deliver(count, adapter.provider_posts, requested_target=target, allow_shortfall=True)
                    # 未接受缺额：明确进入人工/AI 决策——可「接受缺额并交付」或「暂停调整」。
                    self.emit(
                        "HUMAN_DECISION_REQUIRED",
                        "SHORTAGE",
                        f"所选范围已耗尽：仅完成 {count}/{target}。请决策：接受缺额交付（完成 {count} 个直接建模交付），或暂停后调整目标/类目再重跑",
                        count,
                        target,
                        {
                            "shortage": target - count,
                            "provider_calls": adapter.provider_posts,
                            "can_accept_shortfall": bool(count),
                            "resume_safe": True,
                        },
                    )
                    return 2
                if status is RuntimeStatus.MANUAL_RECONCILIATION:
                    self.emit("JOB_BLOCKED", "PROVIDER_RECONCILIATION", "存在 SUBMISSION_UNKNOWN，禁止自动重提", self.pool.success_count(), target, {"blocker": "SUBMISSION_UNKNOWN", "provider_calls": adapter.provider_posts})
                    return 2
                if adapter.provider_capacity_waiting():
                    self.emit("JOB_BLOCKED", "PROVIDER_CAPACITY", "Provider 当前容量已满；checkpoint 已保存，将从同一 Job 安全恢复", self.pool.success_count(), target, {"blocker": "PROVIDER_CAPACITY", "provider_calls": adapter.provider_posts, "resume_safe": True})
                    return 2
                # A local-agent review may unblock a candidate while another
                # candidate is still waiting for review. Finish every durable
                # stage that can proceed before stopping at that review
                # boundary; otherwise one resume would advance only one stage
                # and the operator would need to press resume repeatedly for
                # the same already-reviewed product.
                if before_tick != _candidate_progress_signature(self.pool.records()):
                    continue
                active_records = [item for item in self.pool.records() if item.state not in TERMINAL_ITEM_STATES]
                pending_reasons = {str(item.rejection_reason or "") for item in active_records}
                l2_pending = next(
                    (
                        item for item in sorted(active_records, key=lambda value: (value.created_at, value.candidate_id))
                        if str(item.rejection_reason or "") in _L2_HUMAN_REQUIRED_REASONS
                    ),
                    None,
                )
                if l2_pending is not None:
                    reason_code = str(l2_pending.rejection_reason)
                    browser = self.contract.get("browser_session") or {}
                    session_dir = str(browser.get("user_data_dir") or self.workspace / "browser_session")
                    self.emit(
                        "HUMAN_REQUIRED",
                        "L2_BROWSER",
                        f"{reason_code}：候选需要在同一可见浏览器会话中核对主商品图片/尺寸；完成后恢复同一 Job",
                        self.pool.success_count(),
                        target,
                        {
                            "reason_code": reason_code,
                            "candidate_id": l2_pending.candidate_id,
                            "record_id": l2_pending.record_id,
                            "url": l2_pending.canonical_url,
                            "browser_session_dir": session_dir,
                            "provider_calls": adapter.provider_posts,
                            "resume_safe": True,
                        },
                    )
                    return 2
                if "BRAIN_NOT_CONFIGURED" in pending_reasons:
                    local_agent_mode = bool(getattr(getattr(self.brain, "settings", None), "local_agent_mode", False))
                    blocker = "LOCAL_AGENT_REVIEW_REQUIRED" if local_agent_mode else "BRAIN_NOT_CONFIGURED"
                    message = (
                        "LOCAL_AGENT_REVIEW_REQUIRED：本地 Agent 已就绪，但当前候选没有显式复核证据；"
                        "补充复核后恢复同一 Job"
                        if local_agent_mode
                        else "WEBSITE_BRAIN_* 未配置；需要 Qwen3.6 决策的候选保持暂停"
                    )
                    self.emit("JOB_BLOCKED", "BRAIN_DECISION", message, 0, target, {"blocker": blocker, "provider_calls": adapter.provider_posts, "resume_safe": True})
                    return 2
        except BrowserHumanRequired as error:
            self.emit("HUMAN_REQUIRED", "L2_BROWSER", str(error), self.pool.success_count(), target, {"reason_code": error.reason_code, "url": error.url, "browser_session_dir": str(error.session_dir), "provider_calls": adapter.provider_posts})
            return 2
        except BrowserRuntimeMissing as error:
            self.emit("JOB_BLOCKED", "L2_BROWSER", str(error), self.pool.success_count(), target, {"blocker": error.code, "reason_code": error.code, "provider_calls": adapter.provider_posts})
            return 2
        except NetworkPolicyError as error:
            reason = "ROBOTS_DENIED" if isinstance(error, RobotsDenied) else type(error).__name__.upper()
            self.emit("JOB_BLOCKED", "DISCOVERY", str(error), self.pool.success_count(), target, {"blocker": reason, "reason_code": reason, "browser_escalation": False, "provider_calls": adapter.provider_posts})
            return 2
        except ProductAcquisitionError as error:
            reason = str(error).strip() if str(error).strip().isupper() else error.code
            self.emit("JOB_BLOCKED", "DISCOVERY", str(error), self.pool.success_count(), target, {"blocker": reason, "reason_code": reason, "provider_calls": adapter.provider_posts})
            return 2
        except HardStopError as error:
            self.emit("JOB_BLOCKED", "HARD_STOP", str(error), self.pool.success_count(), target, {"blocker": error.code, "candidate_id": error.candidate_id, "provider_calls": adapter.provider_posts})
            return 2
        self.emit("JOB_BLOCKED", "RUNTIME", "达到有界运行步数；checkpoint 已保存，可恢复同一 Job", self.pool.success_count(), target, {"blocker": "BOUNDED_TICK_LIMIT", "provider_calls": adapter.provider_posts})
        return 2

    def _deliver(self, target: int, provider_posts: int, *, requested_target: int | None = None, allow_shortfall: bool = False) -> int:
        completed = [item for item in self.pool.records() if item.state is ItemState.COMPLETED and item.raw_glb_path]
        target_mode = str(self.contract.get("target_mode") or "EXACT_N")
        if len(completed) != target:
            if target_mode == "ALL" or allow_shortfall:
                # 「全部」模式或「接受缺额」：把实际成功建模的部分交付，
                # 而不是因个别失败 / 范围耗尽而阻断整批。
                target = len(completed)
            else:
                self.emit("JOB_BLOCKED", "DELIVERY_QA", "Exact-N 与完成模型数量不一致，交付已阻断", len(completed), target, {"blocker": "DELIVERY_COUNT_MISMATCH", "provider_calls": provider_posts})
                return 2
        if not completed:
            self.emit("JOB_BLOCKED", "DELIVERY_QA", "没有可交付的模型", 0, target, {"blocker": "NO_COMPLETED_MODELS", "provider_calls": provider_posts})
            return 2
        delivery = self.workspace / "05_delivery"
        delivery.mkdir(parents=True, exist_ok=True)
        batch_root = delivery / "batches"
        batch_root.mkdir(parents=True, exist_ok=True)
        manifest_items: list[dict[str, Any]] = []
        for item in completed:
            raw_source = Path(str(item.raw_glb_path))
            normalized_source = Path(str(item.lineage.get("normalized_glb_path") or raw_source))
            if not normalized_source.is_file():
                self.emit(
                    "JOB_BLOCKED",
                    "DELIVERY_QA",
                    "Blender 归一化 GLB 不存在，交付已阻断",
                    len(manifest_items),
                    target,
                    {"blocker": "NORMALIZED_GLB_MISSING", "record_id": item.record_id, "provider_calls": provider_posts},
                )
                return 2
            normalized_digest = str(item.lineage.get("normalized_glb_sha256") or hashlib.sha256(normalized_source.read_bytes()).hexdigest())
            manifest_items.append({
                "record_id": item.record_id,
                "product_name": item.product_name,
                "filename": normalized_source.name,
                "sha256": normalized_digest,
                "raw_glb_path": str(raw_source),
                "raw_glb_sha256": item.raw_glb_sha256,
                "normalized_glb_path": str(normalized_source),
                "target_dimensions": item.lineage.get("target_dimensions") or item.lineage.get("dimensions") or {},
                "dimension_source": item.lineage.get("dimension_source") or "UNKNOWN",
                "dimension_unit": item.lineage.get("dimension_unit") or "source_unit",
                "blender_qa": item.lineage.get("blender_qa") or {},
            })
        requested = requested_target or target
        target_mode = str(self.contract.get("target_mode") or "EXACT_N")
        batch_size = 20
        batches: list[dict[str, Any]] = []
        for offset in range(0, len(manifest_items), batch_size):
            batch_number = offset // batch_size + 1
            batch_name = f"batch_{batch_number:03d}"
            batch_dir = batch_root / batch_name
            batch_dir.mkdir(parents=True, exist_ok=True)
            batch_items = manifest_items[offset : offset + batch_size]
            for manifest_item in batch_items:
                source = Path(str(manifest_item["normalized_glb_path"]))
                target_path = batch_dir / str(manifest_item["filename"])
                shutil.copy2(source, target_path)
                manifest_item["batch"] = batch_name
                manifest_item["delivery_filename"] = f"{batch_name}/{target_path.name}"
            batch_manifest = {
                "schema_version": "website-delivery-batch.v1",
                "job_id": self.contract["job_id"],
                "batch": batch_name,
                "target_mode": target_mode,
                "requested_target": requested,
                "item_count": len(batch_items),
                "items": batch_items,
            }
            (batch_dir / "manifest.json").write_text(json.dumps(batch_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            # 每个任务的 zip 以「任务名称 + job_id」为前缀：既体现用户设置的
            # 任务名（步骤 1 的「任务名称」，可后续编辑），又保证不同任务即使
            # 同名也不会相互覆盖。
            task_label = _safe_name(self.contract.get("title") or "")
            if not task_label:
                task_label = self.contract["job_id"]
            zip_name = f"{task_label}_{self.contract['job_id']}_{batch_name}.zip"
            zip_path = delivery / zip_name
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                # 压缩包只装模型 GLB，不把 manifest.json 等 JSON 混进去；
                # 每批固定最多 20 个模型。
                for file_path in sorted(batch_dir.rglob("*.glb")):
                    if file_path.is_file():
                        archive.write(file_path, arcname=file_path.relative_to(batch_dir).as_posix())
            batches.append({
                "name": zip_path.name,
                "batch": batch_name,
                "item_count": len(batch_items),
                "relative_path": f"05_delivery/{zip_path.name}",
                "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
                "size_bytes": zip_path.stat().st_size,
                "manifest_schema": "website-delivery-batch.v1",
            })
        manifest = {
            "schema_version": "website-delivery-manifest.v3",
            "job_id": self.contract["job_id"],
            "target_mode": target_mode,
            "requested_target": requested,
            "target": target,
            "delivered": len(manifest_items),
            "batch_size": batch_size,
            "batches": batches,
            "blender_adapter": str((manifest_items[0].get("blender_qa") or {}).get("adapter") or "UNKNOWN") if manifest_items else "UNKNOWN",
            "blender_qa_status": "PASS" if all((item.get("blender_qa") or {}).get("status") == "PASS" for item in manifest_items) else "UNKNOWN",
            "items": manifest_items,
        }
        manifest_path = delivery / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if target_mode == "ALL":
            label = f"全部 {target}"
        elif target_mode == "UP_TO_N":
            label = f"Up-To {requested}"
        else:
            label = f"Exact {target}"
        for batch in batches:
            self.emit(
                "ARTIFACT_READY",
                "DELIVERY",
                f"{batch['batch']}：{batch['item_count']} 个归一化 GLB 已打包",
                int(batch["item_count"]),
                requested,
                {
                    "artifact_type": "DELIVERY_BATCH_ZIP",
                    "relative_path": batch["relative_path"],
                    "item_count": batch["item_count"],
                    "manifest_schema": batch["manifest_schema"],
                    "sha256": batch["sha256"],
                    "size_bytes": batch["size_bytes"],
                    "batch": batch["batch"],
                    "provider_calls": provider_posts,
                },
            )
            self.emit(
                "DELIVERY_COMPLETED",
                "DELIVERY",
                f"{batch['batch']}：交付文件已完成哈希校验",
                int(batch["item_count"]),
                requested,
                {
                    "artifact_type": "DELIVERY_BATCH_ZIP",
                    "relative_path": batch["relative_path"],
                    "item_count": batch["item_count"],
                    "manifest_schema": batch["manifest_schema"],
                    "sha256": batch["sha256"],
                    "size_bytes": batch["size_bytes"],
                    "batch": batch["batch"],
                    "provider_calls": provider_posts,
                },
            )
        self.emit(
            "DELIVERY_COMPLETED",
            "DELIVERY",
            f"{label}：{target} 个 GLB 已完成 Blender QA 并按 20 个/批交付",
            target,
            requested,
            {
                "artifact_type": "MANIFEST_JSON",
                "relative_path": "05_delivery/manifest.json",
                "item_count": target,
                "manifest_schema": "website-delivery-manifest.v3",
                "sha256": manifest_sha256,
                "manifest": manifest,
                "delivered": target,
                "requested_target": requested,
                "target_mode": target_mode,
                "batch_count": len(batches),
                "provider_calls": provider_posts,
            },
        )
        self.emit("JOB_COMPLETED", "COMPLETED", f"生产完成：{target}/{requested}", target, requested, {"delivered": target, "requested_target": requested, "target_mode": target_mode, "provider_calls": provider_posts})
        return 0


__all__ = ["ProductionPipeline", "WebsiteStageAdapter"]
