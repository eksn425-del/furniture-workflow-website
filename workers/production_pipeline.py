"""Converged Website production pipeline backed by the shared workflow engine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import select

from app.database import Database
from app.models import ProductionProviderTask, ProductionRegistryEntry, utc_now
from app.services.brain_provider import BrainError, BrainNotConfigured, WebsiteBrainProvider
from app.services.product_acquisition import (
    AcquiredProduct,
    BrowserHumanRequired,
    BrowserRuntimeMissing,
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
from packages.workflow_core.candidate_pool import CandidatePoolStore, CandidateRecord
from packages.workflow_core.dimensions import govern_dimensions
from packages.workflow_core.locks import make_order_policy_lock, provider_idempotency_key, stable_hash
from packages.workflow_core.naming import (
    NamingReviewRequired,
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
from workers.scrape.http_client import NetworkPolicyError, SafeHttpClient


Emit = Callable[[str, str, str, int | None, int | None, dict[str, Any] | None], None]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_name(value: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip(" .")[:140]


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
        emit: Emit,
    ) -> None:
        self.contract = contract
        self.database = database
        self.pool = pool
        self.acquisition = acquisition
        self.workspace = Path(workspace).resolve()
        self.brain = brain
        self.provider_client = provider_client
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
            result = SafeHttpClient(source_url=url, request_budget=8, timeout=30, request_delay=0).get_media(url)
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
            "review_mode": str((receipt or {}).get("model_mode") or ("LOCAL_AGENT" if review_provider == "LOCAL_AGENT" else "TEXT_BRAIN_PLUS_VISION")),
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
        group = str(decision.get("category_group") or candidate.category_group or "").strip()
        if not group:
            return StageOutcome(StageDecision.REJECTED, "CATEGORY_UNRESOLVED")
        self.pool.enrich_candidate(candidate.candidate_id, category_group=group, lineage={
            "brain_category_group": group,
            "category_authority": self.source_policy.category_authority,
        })
        return StageOutcome(StageDecision.ACCEPTED, "CATEGORY_ACCEPTED", {"category_group": group})

    def _stage_date(self, candidate: CandidateRecord) -> StageOutcome:
        return StageOutcome(StageDecision.ACCEPTED, "DATE_POLICY_NOT_RESTRICTED", {"date_policy": "SOURCE_CURRENT_PUBLIC_CATALOG"})

    def _stage_dimension(self, candidate: CandidateRecord) -> StageOutcome:
        values = candidate.lineage.get("source_dimensions") or {}
        source = "SOURCE_EXPLICIT"
        if not all(values.get(axis) for axis in ("width", "depth", "height")):
            decision = candidate.lineage.get("brain_product_decision") or {}
            values = {axis: decision.get(axis) for axis in ("width", "depth", "height")}
            source = "WEBSITE_BRAIN"
        if not all(values.get(axis) for axis in ("width", "depth", "height")):
            # 页面/Brain 都没有尺寸：用同一 L2 可见会话打开详情页，
            # 展开 Dimensions 标签自动抓官方尺寸，避免人工手动注入。
            browser_dims: dict[str, float] = {}
            browser_unit = ""
            try:
                session_dir = Path(str(self.acquisition.browser_session_dir))
                browser_dims, browser_unit = NativeBrowserCollector(session_dir).extract_dimensions(str(candidate.canonical_url))
            except BrowserHumanRequired:
                return StageOutcome(StageDecision.PENDING, "DIMENSIONS_BROWSER_HUMAN_REQUIRED")
            except BrowserRuntimeMissing:
                browser_dims, browser_unit = {}, ""
            if all(browser_dims.get(axis) for axis in ("width", "depth", "height")):
                values = {axis: browser_dims[axis] for axis in ("width", "depth", "height")}
                source = "L2_BROWSER_DIMENSIONS"
                candidate.lineage.setdefault("dimension_unit", browser_unit or "source_unit")
        try:
            governed = govern_dimensions(values)
        except ValueError:
            return StageOutcome(StageDecision.REJECTED, "DIMENSIONS_MISSING_OR_INVALID")
        evidence = {"dimensions": governed, "dimension_source": source, "dimension_unit": candidate.lineage.get("dimension_unit") or "source_unit"}
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

        # CGTrader is visual-first: route/category only records discovery
        # scope and can never replace the reviewed product type.
        if self.source_policy.source_host == "cgtrader.com":
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
            raw = target.read_bytes()
            valid = len(raw) >= 20 and raw[:4] == b"glTF" and int.from_bytes(raw[8:12], "little") == len(raw)
            digest = hashlib.sha256(raw).hexdigest()
            ledger.status = ledger.checkpoint_state = "DELIVERED" if valid else "RAW_GLB_INVALID"
            ledger.finished_at = utc_now()
            session.commit()
            return StageOutcome(
                StageDecision.ACCEPTED if valid else StageDecision.REJECTED,
                "RAW_GLB_VALIDATED" if valid else "RAW_GLB_INVALID",
                raw_glb_path=str(target),
                raw_glb_sha256=digest,
                raw_glb_valid=valid,
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
        if str(self.contract.get("category_allocation") or "TOTAL_ACROSS_SELECTED") == "PER_CATEGORY":
            groups = [str(item.get("canonical_name") or item.get("category_id")) for item in categories]
            quotas = {group: base for group in groups}
            return max(1, base * len(groups)), quotas, "REQUIRED"
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
        policy = RuntimePolicy(
            order_id=str(self.contract["job_id"]),
            job_id=str(self.contract["job_id"]),
            target_count=target,
            progressive_gates=gates,
            provider=str(self.contract.get("provider") or "OFF"),
            order_policy=policy_lock,
            max_provider_slots=1,
            max_steps_per_tick=20,
            max_refill_rounds=100,
        )
        adapter = WebsiteStageAdapter(
            contract=self.contract,
            database=self.database,
            pool=self.pool,
            acquisition=self.acquisition,
            workspace=self.workspace,
            brain=self.brain,
            provider_client=self.provider_client,
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
        try:
            for _ in range(max(80, target * 20)):
                records = self.pool.records()
                ready = sum(item.state is ItemState.MODEL_INPUT_LOCKED for item in records)
                provider_off = str(self.contract.get("provider") or "OFF").casefold() == "off"
                if provider_off and ready and ready == len(records) and ready < target:
                    try:
                        refill = adapter.refill(needed=target - ready, pool=self.pool)
                    except SupplyExhaustedError:
                        if str(self.contract.get("target_mode") or "EXACT_N") == "UP_TO_N":
                            self.pool.set_job_status("READY_POOL", reason=f"UP_TO_N supply exhausted at {ready}/{target}")
                            self.emit("READY_POOL_COMPLETED", "READY_POOL", f"Up-To {target} Ready Pool 已形成 {ready} 个；所选范围已耗尽且 Provider OFF", ready, target, {"ready_count": ready, "eligible_count": ready, "provider_calls": adapter.provider_posts, "target_mode": "UP_TO_N", "candidate_pool_path": str(self.pool.path)})
                            self.emit("JOB_BLOCKED", "PROVIDER_SAFETY", "Ready Pool 已保存；选择并审批 Provider 后恢复同一 Job", ready, target, {"blocker": "PROVIDER_REQUIRED", "ready_count": ready, "provider_calls": adapter.provider_posts, "target_mode": "UP_TO_N"})
                            return 2
                        self.emit("TARGET_SHORTAGE", "EXACT_N", f"所选范围已耗尽：Ready Pool {ready}/{target}", ready, target, {"shortage": target - ready, "provider_calls": adapter.provider_posts})
                        return 2
                    added = self.pool.add_candidates(refill)["added"]
                    self.emit("REFILL_COMPLETED", "DISCOVERY", f"Ready Pool 补货新增 {added} 个唯一候选", ready + added, target, {"refill_added": added, "provider_calls": adapter.provider_posts})
                    continue
                if provider_off and ready >= target:
                    target_mode = str(self.contract.get("target_mode") or "EXACT_N")
                    label = f"Up-To {target}" if target_mode == "UP_TO_N" else f"Exact {target}"
                    self.emit("READY_POOL_COMPLETED", "READY_POOL", f"{label} Ready Pool 已形成；Provider OFF，未发起外部调用", ready, target, {"ready_count": ready, "eligible_count": ready, "provider_calls": adapter.provider_posts, "target_mode": target_mode, "candidate_pool_path": str(self.pool.path)})
                    self.emit("JOB_BLOCKED", "PROVIDER_SAFETY", "Ready Pool 已保存；选择并审批 Provider 后恢复同一 Job", ready, target, {"blocker": "PROVIDER_REQUIRED", "ready_count": ready, "provider_calls": adapter.provider_posts, "target_mode": target_mode})
                    return 2
                status = engine.tick()
                if status is RuntimeStatus.SUCCEEDED:
                    return self._deliver(target, adapter.provider_posts)
                if status is RuntimeStatus.SUPPLY_EXHAUSTED:
                    count = self.pool.success_count()
                    if str(self.contract.get("target_mode") or "EXACT_N") == "UP_TO_N":
                        if str(self.contract.get("provider") or "OFF").casefold() == "off":
                            ready = sum(item.state is ItemState.MODEL_INPUT_LOCKED for item in self.pool.records())
                            if ready:
                                self.pool.set_job_status("READY_POOL", reason=f"UP_TO_N supply exhausted at {ready}/{target}")
                                self.emit("READY_POOL_COMPLETED", "READY_POOL", f"Up-To {target} Ready Pool 已形成 {ready} 个；所选范围已耗尽且 Provider OFF", ready, target, {"ready_count": ready, "eligible_count": ready, "provider_calls": adapter.provider_posts, "target_mode": "UP_TO_N", "candidate_pool_path": str(self.pool.path)})
                                self.emit("JOB_BLOCKED", "PROVIDER_SAFETY", "Ready Pool 已保存；选择并审批 Provider 后恢复同一 Job", ready, target, {"blocker": "PROVIDER_REQUIRED", "ready_count": ready, "provider_calls": adapter.provider_posts, "target_mode": "UP_TO_N"})
                                return 2
                        elif count:
                            self.pool.set_job_status("SUCCEEDED", reason=f"UP_TO_N delivered {count}/{target} after supply exhaustion")
                            return self._deliver(count, adapter.provider_posts, requested_target=target)
                    self.emit("TARGET_SHORTAGE", "EXACT_N", f"所选范围已耗尽：完成 {count}/{target}", count, target, {"shortage": target - count, "provider_calls": adapter.provider_posts})
                    return 2
                if status is RuntimeStatus.MANUAL_RECONCILIATION:
                    self.emit("JOB_BLOCKED", "PROVIDER_RECONCILIATION", "存在 SUBMISSION_UNKNOWN，禁止自动重提", self.pool.success_count(), target, {"blocker": "SUBMISSION_UNKNOWN", "provider_calls": adapter.provider_posts})
                    return 2
                if adapter.provider_capacity_waiting():
                    self.emit("JOB_BLOCKED", "PROVIDER_CAPACITY", "Provider 当前容量已满；checkpoint 已保存，将从同一 Job 安全恢复", self.pool.success_count(), target, {"blocker": "PROVIDER_CAPACITY", "provider_calls": adapter.provider_posts, "resume_safe": True})
                    return 2
                pending_reasons = {str(item.rejection_reason or "") for item in self.pool.records() if item.state in {ItemState.VISUAL_PENDING, ItemState.DIMENSION_PENDING}}
                if "BRAIN_NOT_CONFIGURED" in pending_reasons:
                    self.emit("JOB_BLOCKED", "BRAIN_DECISION", "WEBSITE_BRAIN_* 未配置；需要 Qwen3.6 决策的候选保持暂停", 0, target, {"blocker": "BRAIN_NOT_CONFIGURED", "provider_calls": adapter.provider_posts})
                    return 2
        except BrowserHumanRequired as error:
            self.emit("HUMAN_REQUIRED", "L2_BROWSER", str(error), self.pool.success_count(), target, {"reason_code": error.reason_code, "url": error.url, "browser_session_dir": str(error.session_dir), "provider_calls": adapter.provider_posts})
            return 2
        except BrowserRuntimeMissing as error:
            self.emit("JOB_BLOCKED", "L2_BROWSER", str(error), self.pool.success_count(), target, {"blocker": error.code, "reason_code": error.code, "provider_calls": adapter.provider_posts})
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

    def _deliver(self, target: int, provider_posts: int, *, requested_target: int | None = None) -> int:
        completed = [item for item in self.pool.records() if item.state is ItemState.COMPLETED and item.raw_glb_path]
        if len(completed) != target:
            self.emit("JOB_BLOCKED", "DELIVERY_QA", "Exact-N 与完成模型数量不一致，交付已阻断", len(completed), target, {"blocker": "DELIVERY_COUNT_MISMATCH", "provider_calls": provider_posts})
            return 2
        delivery = self.workspace / "05_delivery"
        delivery.mkdir(parents=True, exist_ok=True)
        manifest_items = []
        for item in completed:
            source = Path(str(item.raw_glb_path))
            target_path = delivery / source.name
            shutil.copy2(source, target_path)
            manifest_items.append({"record_id": item.record_id, "product_name": item.product_name, "filename": target_path.name, "sha256": item.raw_glb_sha256})
        requested = requested_target or target
        target_mode = str(self.contract.get("target_mode") or "EXACT_N")
        manifest = {"schema_version": "website-delivery-manifest.v2", "job_id": self.contract["job_id"], "target_mode": target_mode, "requested_target": requested, "target": target, "delivered": len(manifest_items), "items": manifest_items}
        (delivery / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        label = f"Up-To {requested}" if target_mode == "UP_TO_N" else f"Exact {target}"
        self.emit("DELIVERY_COMPLETED", "DELIVERY", f"{label}：{target} 个 GLB 已完成校验并交付", target, requested, {"artifact_type": "DELIVERY_FOLDER", "relative_path": "05_delivery", "item_count": target, "manifest_schema": "website-delivery-manifest.v2", "manifest": manifest, "delivered": target, "requested_target": requested, "target_mode": target_mode, "provider_calls": provider_posts})
        self.emit("JOB_COMPLETED", "COMPLETED", f"生产完成：{target}/{requested}", target, requested, {"delivered": target, "requested_target": requested, "target_mode": target_mode, "provider_calls": provider_posts})
        return 0


__all__ = ["ProductionPipeline", "WebsiteStageAdapter"]
