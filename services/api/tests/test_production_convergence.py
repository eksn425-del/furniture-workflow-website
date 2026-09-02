from __future__ import annotations

import json
import inspect
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.routes.control_plane import _job_categories, approve_job, get_candidate_for_review, get_control_site, list_control_sites, record_local_agent_review
from app.database import Database
from app.models import (
    ProductionJob,
    ProductionJobEvent,
    ProductionProviderTask,
    ProductionRun,
    RuntimeEvent,
    SiteCategory,
    SiteCategorySnapshot,
    SiteEntryURL,
    SiteRegistryRecord,
    SiteScanRun,
    SiteTaxonomySnapshot,
    utc_now,
)
from app.schemas import ControlJobApproval, ControlJobStart, LocalAgentProductReviewRequest
from app.services.brain_provider import BrainSettings, WebsiteBrainProvider
from app.services.product_acquisition import (
    AcquiredProduct,
    BrowserHumanRequired,
    ProductAcquisitionEngine,
    ProductAcquisitionError,
    ProductSupplyExhausted,
    classify_source_type,
)
from app.services.production_runtime import ProductionRuntimeService
from app.services import production_runtime as production_runtime_module
from app.services.site_scan_runtime import SiteScanRuntimeService
from packages.workflow_core.statuses import ItemState
from furniture_workflow_engine import StageDecision
from packages.workflow_core.candidate_pool import CandidatePoolStore, CandidateRecord
from workers.blender_adapter import validate_glb
from workers.production_pipeline import ProductionPipeline, WebsiteStageAdapter


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + (b"production-fixture" * 128)


class FakeMediaResponse:
    content = PNG_BYTES
    content_type = "image/png"


class FakeMediaClient:
    def __init__(self, **_: object) -> None:
        pass

    def get_media(self, _: str) -> FakeMediaResponse:
        return FakeMediaResponse()


class FakeAcquisition:
    def __init__(self, products: list[AcquiredProduct]) -> None:
        self.products = list(products)
        self.cursor = 0

    def discover(self, needed: int) -> list[AcquiredProduct]:
        result = self.products[self.cursor : self.cursor + needed]
        self.cursor += len(result)
        if not result:
            raise ProductSupplyExhausted("fixture scopes exhausted")
        return result


class FakeProvider:
    def __init__(self, *, poll_timeout: bool = False, capacity_rejection: bool = False) -> None:
        self.create_calls = 0
        self.poll_calls = 0
        self.download_calls = 0
        self.poll_timeout = poll_timeout
        self.capacity_rejection = capacity_rejection

    def create_task(self, image_path: Path, *, idempotency_key: str | None = None):
        assert image_path.is_file()
        assert idempotency_key
        self.create_calls += 1
        if self.capacity_rejection:
            return None, "Provider capacity is full (429)"
        return f"provider-task-{idempotency_key[:12]}", None

    def poll_task(self, provider_task_id: str):
        assert provider_task_id.startswith("provider-task-")
        self.poll_calls += 1
        if self.poll_timeout:
            return None, "timeout while Provider task is still active"
        return {"status": "completed", "model_url": "fixture://model.glb"}, None

    def download_glb(self, result: dict, provider_task_id: str, target: Path) -> bool:
        assert result.get("status") == "completed"
        assert provider_task_id.startswith("provider-task-")
        self.download_calls += 1
        payload = json.dumps(
            {"asset": {"version": "2.0"}, "extras": {"task": provider_task_id}},
            separators=(",", ":"),
        ).encode("utf-8")
        payload += b" " * ((4 - len(payload) % 4) % 4)
        raw = (
            b"glTF"
            + (2).to_bytes(4, "little")
            + (12 + 8 + len(payload)).to_bytes(4, "little")
            + len(payload).to_bytes(4, "little")
            + b"JSON"
            + payload
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return True


def _product(index: int, *, name: str | None = None, brand: str = "Maker") -> AcquiredProduct:
    qualification = {
        "eligible": True,
        "single_product": True,
        "background_ok": True,
        "image_to_3d_suitable": True,
        "category_group": "Chairs",
        "style": "Modern",
        "color": "Walnut",
        "material": "Wood",
        "product_type": "Chair",
        "width": 24,
        "depth": 26,
        "height": 31,
        "confidence": 0.99,
        "reason_codes": ["TEST_FIXTURE_ACCEPTED"],
    }
    return AcquiredProduct(
        source_product_id=f"sku-{index}",
        canonical_url=f"https://example.test/products/chair-{index}",
        source_name=name or f"Lounge Chair {index}",
        source_brand=brand,
        category_id="cat_chairs",
        category_group="Chairs",
        image_url=f"https://example.test/media/chair-{index}.png",
        dimensions={"width": 24.0, "depth": 26.0, "height": 31.0},
        dimension_unit="in",
        source_type="DIRECT_BRAND",
        capture_sha256=f"{index + 1:064x}",
        acquisition="TEST_FIXTURE",
        evidence={"qualification": qualification},
    )


def _contract(tmp_path: Path, *, job_id: str, target: int, provider: str = "OFF", source_type: str = "DIRECT_BRAND") -> dict:
    return {
        "schema_version": "job-contract.v3",
        "job_id": job_id,
        "source_url": "https://example.test/collections/chairs",
        "site_key": "example.test",
        "site_display_name": "Acme",
        "title": "Production convergence fixture",
        "goal": "Exact-N validated furniture models",
        "target_mode": "EXACT_N",
        "target_value": target,
        "category_ids": ["cat_chairs"],
        "categories": [{
            "category_id": "cat_chairs",
            "canonical_name": "Chairs",
            "native_name": "Chairs",
            "path": "Chairs",
            "source_url": "https://example.test/collections/chairs",
            "parent_category_id": None,
            "level": 1,
            "scope_kind": "CATEGORY",
            "selected": True,
        }],
        "scope": "NEW_ONLY",
        "category_allocation": "TOTAL_ACROSS_SELECTED",
        "allocation_strategy": "SEQUENTIAL",
        "spillover": "STOP",
        "source_type": source_type,
        "provider": provider,
        "authorization": {"approve_paid_generation": provider.casefold() != "off"},
        "browser_session": {"user_data_dir": str(tmp_path / "browser-session")},
        "workspace": str(tmp_path / job_id),
        "database_path": str(tmp_path / "system" / "control.sqlite3"),
        "approved_plan": {"approved_cost_ceiling_minor": 1000 if provider.casefold() != "off" else 0},
    }


def _run_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    contract: dict,
    products: list[AcquiredProduct],
    provider: FakeProvider | None = None,
):
    monkeypatch.setenv("FURNITURE_WORKFLOW_TEST_FIXTURES", "true")
    monkeypatch.setattr("workers.production_pipeline.SafeHttpClient", FakeMediaClient)
    acquisition = FakeAcquisition(products)
    events: list[dict] = []

    def emit(event_type, stage, message, done, total, payload):
        events.append({
            "type": event_type,
            "stage": stage,
            "message": message,
            "done": done,
            "total": total,
            "payload": payload or {},
        })

    pipeline = ProductionPipeline(
        contract=contract,
        workspace=Path(contract["workspace"]),
        emit=emit,
        acquisition_factory=lambda **_: acquisition,
        provider_client=provider,
    )
    return pipeline, events


def test_dimension_browser_challenge_does_not_fall_back_to_ai_estimate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An official-dimension access failure stays resumable, never becomes a guess."""

    contract = _contract(tmp_path, job_id="job-dimension-challenge", target=1)
    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    pool = CandidatePoolStore(
        tmp_path / "pool.json",
        order_id=contract["job_id"],
        job_id=contract["job_id"],
    )
    candidate = CandidateRecord(
        candidate_id="candidate-dimension-challenge",
        order_id=contract["job_id"],
        job_id=contract["job_id"],
        record_id="record-dimension-challenge",
        source="example.test",
        source_product_id="sku-dimension-challenge",
        canonical_url="https://example.test/products/chair-1",
        preview_id="chair-1",
        preview_url="https://example.test/media/chair-1.png",
        capture_sha256="capture-dimension-challenge",
        image_sha256="image-dimension-challenge",
        category_group="Chairs",
        lineage={
            "source_dimensions": {},
            "brain_product_decision": {
                "height": 31,
                "dimension_source": "AI_ESTIMATED",
            },
        },
    )

    def raise_challenge(*_: object, **__: object) -> tuple[dict[str, float], str]:
        raise BrowserHumanRequired(
            "official dimensions are temporarily unavailable",
            url=candidate.canonical_url,
            session_dir=tmp_path / "browser",
            reason_code="TEMPORARY_PAGE_FAILURE",
        )

    monkeypatch.setattr(
        "workers.production_pipeline.NativeBrowserCollector.extract_dimensions",
        raise_challenge,
    )
    adapter = WebsiteStageAdapter(
        contract=contract,
        database=database,
        pool=pool,
        acquisition=SimpleNamespace(browser_session_dir=tmp_path / "browser"),
        workspace=tmp_path / "workspace",
        brain=SimpleNamespace(review_provider="LOCAL_AGENT"),
        provider_client=None,
        blender_adapter=None,
        media_client_factory=None,
        emit=lambda *_: None,
    )
    try:
        outcome = adapter._stage_dimension(candidate)
        assert outcome.decision is StageDecision.PENDING
        assert outcome.reason == "DIMENSIONS_BROWSER_HUMAN_REQUIRED"
        assert outcome.evidence["dimension_access_reason"] == "TEMPORARY_PAGE_FAILURE"
        assert candidate.lineage.get("dimension_estimation") is not True
    finally:
        database.dispose()


def test_category_quota_strategies_use_stable_scope_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    contract = _contract(tmp_path, job_id="job-quota-strategies", target=4)
    contract["category_ids"] = ["cat-a", "cat-b"]
    contract["categories"] = [
        {"category_id": "cat-a", "canonical_name": "Chairs", "count_value": 9, "count_kind": "EXACT"},
        {"category_id": "cat-b", "canonical_name": "Tables", "count_value": 3, "count_kind": "EXACT"},
    ]
    contract["allocation_strategy"] = "PROPORTIONAL"
    pipeline, _ = _run_pipeline(monkeypatch, tmp_path, contract=contract, products=[])
    target, quotas, quota_mode = pipeline._target_and_quotas()
    assert (target, quotas, quota_mode) == (4, {"cat-a": 3, "cat-b": 1}, "REQUIRED")

    contract["allocation_strategy"] = "CUSTOM"
    contract["category_quotas"] = {"cat-a": 1, "cat-b": 3}
    assert pipeline._target_and_quotas() == (4, {"cat-a": 1, "cat-b": 3}, "REQUIRED")
    contract["category_quotas"] = {"Chairs": 4}
    with pytest.raises(ProductAcquisitionError, match="CUSTOM_CATEGORY_QUOTAS_REQUIRED_BY_SCOPE_ID"):
        pipeline._target_and_quotas()


def _job(job_id: str, *, provider: str = "OFF", policy: dict | None = None) -> ProductionJob:
    return ProductionJob(
        job_id=job_id,
        source_url="https://example.test/collections/chairs",
        site_key="example.test",
        title="Fixture Job",
        goal="Exact-N production",
        target_mode="EXACT_N",
        target_value=1,
        requested_count=1,
        provider=provider,
        policy_json=json.dumps(policy or {"category_ids": ["cat_chairs"]}),
    )


def test_selected_category_drives_acquisition(tmp_path: Path) -> None:
    category_html = '<a href="/products/chair-one">Chair</a>'
    product_html = """
    <script type="application/ld+json">
    {"@type":"Product","name":"Chair One","sku":"one","image":"/media/one.png","url":"/products/chair-one","brand":{"name":"Acme"}}
    </script><p>24 W x 26 D x 31 H in</p>
    """

    class Client:
        calls: list[str] = []

        def __init__(self, **_: object) -> None:
            pass

        def get_html(self, url: str) -> str:
            self.calls.append(url)
            return product_html if "/products/" in url else category_html

    engine = ProductAcquisitionEngine(
        source_url="https://example.test/",
        site_key="example.test",
        source_type="DIRECT_BRAND",
        categories=[{
            "category_id": "cat_chairs",
            "canonical_name": "Chairs",
            "source_url": "https://example.test/collections/chairs",
            "selected": True,
        }],
        workspace=tmp_path / "acquisition",
        browser_session_dir=tmp_path / "browser",
        client_factory=Client,
    )
    products = engine.discover(1)
    assert [item.category_id for item in products] == ["cat_chairs"]
    assert Client.calls[0].endswith("/collections/chairs")
    assert all("tables" not in url for url in Client.calls)


def test_product_acquisition_selects_page_bound_jsonld_and_accepts_layered_scene7(tmp_path: Path) -> None:
    detail_html = """
    <h1>Wyatt Bed</h1><p>Item Number: 575954</p>
    <script type="application/ld+json">
    [{"@type":"Product","name":"Related Chair","sku":"126154","image":"/media/chair.png"},
     {"@type":"Product","name":"Wyatt Bed","sku":"575954","url":"/catalog/575954",
      "image":"https://scene7.example/126154.jpg?layer=bed-queen","brand":{"name":"Room & Board"}}]
    </script><p>Overall: 80"w 84"d 45"h</p>
    """

    class Client:
        def __init__(self, **_: object) -> None:
            pass

        def get_html(self, _: str) -> str:
            return detail_html

    engine = ProductAcquisitionEngine(
        source_url="https://www.roomandboard.com/",
        site_key="roomandboard.com",
        source_type="DIRECT_BRAND",
        categories=[{
            "category_id": "cat_beds",
            "canonical_name": "Beds",
            "path": "Bedroom / Beds",
            "source_url": "https://www.roomandboard.com/catalog/575954",
            "selected": True,
        }],
        workspace=tmp_path / "r-and-b",
        browser_session_dir=tmp_path / "browser",
        client_factory=Client,
    )
    products = engine.discover(1)
    assert len(products) == 1
    product = products[0]
    assert product.source_name == "Wyatt Bed"
    assert product.source_product_id == "575954"
    assert product.identity_fields["url_tail_id"] == "575954"
    assert product.identity_fields["jsonld_sku"] == "575954"
    assert product.evidence["layered_scene7"] is True
    assert product.media_binding_status == "COMPATIBLE"
    assert product.media_binding_confidence == 0.9


def test_marketplace_requires_scope_before_network(tmp_path: Path) -> None:
    class NoNetwork:
        def __init__(self, **_: object) -> None:
            pass

        def get_html(self, _: str) -> str:
            raise AssertionError("marketplace without a selected scope must not fetch")

    engine = ProductAcquisitionEngine(
        source_url="https://market.example/",
        site_key="market.example",
        source_type="MARKETPLACE",
        categories=[],
        workspace=tmp_path / "market",
        browser_session_dir=tmp_path / "browser",
        client_factory=NoNetwork,
    )
    with pytest.raises(ProductAcquisitionError, match="MARKETPLACE_SCOPE_REQUIRED"):
        engine.discover(1)


def test_parent_child_category_narrows_to_child(tmp_path: Path) -> None:
    engine = ProductAcquisitionEngine(
        source_url="https://example.test/",
        site_key="example.test",
        source_type="MULTI_BRAND_RETAILER",
        categories=[
            {"category_id": "parent", "source_url": "https://example.test/chairs", "selected": True},
            {"category_id": "child", "parent_category_id": "parent", "source_url": "https://example.test/chairs/dining", "selected": True},
        ],
        workspace=tmp_path / "compact",
        browser_session_dir=tmp_path / "browser",
        client_factory=lambda **_: SimpleNamespace(),
    )
    # 子级收窄父级：父级和子级同时勾选时只保留子级范围。
    assert [item["category_id"] for item in engine.categories] == ["child"]


def test_no_browser_evidence_dir_required_and_selected_scope_contract(tmp_path: Path) -> None:
    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    session = database.session_factory()
    try:
        session.add(SiteRegistryRecord(site_key="example.test", domain="example.test", display_name="Acme", source_kind="DIRECT_BRAND"))
        session.add_all([
            SiteCategory(category_id="cat_chairs", site_key="example.test", path="Chairs", native_name="Chairs", canonical_name="Chairs", source_url="https://example.test/chairs"),
            SiteCategory(category_id="cat_tables", site_key="example.test", path="Tables", native_name="Tables", canonical_name="Tables", source_url="https://example.test/tables"),
        ])
        job = _job("job-contract")
        session.add(job)
        session.commit()
        runtime = ProductionRuntimeService(database, tmp_path / "output")
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)
        _, _, contract = runtime._contract(session, job, workspace)
        assert contract["category_ids"] == ["cat_chairs"]
        assert [item["category_id"] for item in contract["categories"]] == ["cat_chairs"]
        def keys(value: object):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield str(key)
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        assert "browser_evidence_dir" not in set(keys(contract))
        assert "browser_evidence_dir" not in ControlJobStart.model_fields
    finally:
        session.close()
        database.dispose()


def test_site_scan_persists_across_navigation_and_service_restart(tmp_path: Path) -> None:
    class Analyzer:
        @staticmethod
        def analyze(source_url: str, *, live: bool, output_dir: Path) -> dict:
            assert live is True
            output_dir.mkdir(parents=True, exist_ok=True)
            return {
                "status": "READY",
                "verified": True,
                "taxonomy_level": "L1",
                "profile_version": "fixture-v1",
                "source_type": "DIRECT_BRAND",
                "source_scope": "CATEGORY",
                "brain": {"status": "NOT_NEEDED", "provider_posts": 0},
                "categories": [{
                    "category_id": "cat_chairs",
                    "path": "Furniture / Chairs",
                    "native_name": "Chairs",
                    "canonical_name": "Chairs",
                    "source_url": source_url,
                    "count_value": 12,
                    "count_kind": "EXACT",
                    "confidence": 1.0,
                    "level": 1,
                    "evidence": ["fixture"],
                }],
            }

    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    session = database.session_factory()
    try:
        session.add(SiteRegistryRecord(site_key="example.test", domain="example.test", display_name="Acme"))
        session.commit()
        session.add(SiteEntryURL(entry_url_id="entry-1", site_key="example.test", url="https://example.test/"))
        session.commit()
    finally:
        session.close()
    first = SiteScanRuntimeService(database, tmp_path / "output", Analyzer())
    started = first.start(site_key="example.test", source_url="https://example.test/", job_id=None, live=True)
    deadline = time.monotonic() + 3
    current = started
    while current["status"] in {"QUEUED", "ANALYZING", "L2_BROWSER"} and time.monotonic() < deadline:
        time.sleep(0.01)
        current = first.status(started["scan_id"]) or current
    assert current["status"] == "READY"
    first.shutdown()
    second = SiteScanRuntimeService(database, tmp_path / "output", Analyzer())
    try:
        restored = second.status(started["scan_id"])
        assert restored and restored["status"] == "READY"
        session = database.session_factory()
        try:
            category = session.scalar(select(SiteCategory).where(SiteCategory.category_id == "cat_chairs"))
            assert category and category.count_value == 12
        finally:
            session.close()
    finally:
        second.shutdown()
        database.dispose()


def test_site_scan_rekeys_path_only_category_ids_per_site(tmp_path: Path) -> None:
    """A shared path hash from another retailer must not abort a new scan."""

    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    session = database.session_factory()
    try:
        session.add_all([
            SiteRegistryRecord(site_key="first.test", domain="first.test", display_name="First"),
            SiteRegistryRecord(site_key="second.test", domain="second.test", display_name="Second"),
            SiteCategory(
                category_id="cat_shared",
                site_key="first.test",
                path="/accessories",
                native_name="Accessories",
                canonical_name="Accessories",
                source_url="https://first.test/accessories",
            ),
            SiteScanRun(
                scan_id="scan-second",
                site_key="second.test",
                source_url="https://second.test/",
                status="ANALYZING",
                live=True,
            ),
        ])
        session.commit()
    finally:
        session.close()

    runtime = SiteScanRuntimeService(database, tmp_path / "output", object())
    try:
        runtime._persist("scan-second", {
            "status": "READY",
            "verified": True,
            "taxonomy_level": "L1",
            "source_type": "DIRECT_BRAND",
            "source_scope": "SITE",
            "categories": [{
                "category_id": "cat_shared",
                "path": "/accessories",
                "native_name": "Accessories",
                "canonical_name": "Accessories",
                "source_url": "https://second.test/accessories",
                "count_value": 56,
                "count_kind": "ESTIMATED",
                "confidence": 0.7,
                "level": 1,
                "evidence": [{"role": "navigation"}],
            }],
        }, tmp_path / "analysis")
        session = database.session_factory()
        try:
            first = session.scalar(select(SiteCategory).where(SiteCategory.site_key == "first.test"))
            second = session.scalar(select(SiteCategory).where(SiteCategory.site_key == "second.test"))
            assert first and first.category_id == "cat_shared"
            assert second and second.category_id != "cat_shared"
            assert second.path == "/accessories"
            assert second.count_value == 56
        finally:
            session.close()
    finally:
        runtime.shutdown()
        database.dispose()


def test_partial_unknown_scan_escalates_to_browser_l2(tmp_path: Path) -> None:
    class Analyzer:
        def __init__(self) -> None:
            self.browser_calls = 0

        @staticmethod
        def analyze(source_url: str, *, live: bool, output_dir: Path) -> dict:
            assert live is True
            output_dir.mkdir(parents=True, exist_ok=True)
            return {
                "status": "PARTIAL",
                "verified": False,
                "taxonomy_level": "L1",
                "profile_version": "fixture-partial-v1",
                "source_type": "DIRECT_BRAND",
                "source_scope": "SITE",
                "brain": {"status": "NOT_NEEDED", "provider_posts": 0},
                "categories": [{
                    "category_id": "cat_chairs",
                    "path": "/chairs",
                    "native_name": "Chairs",
                    "canonical_name": "Chairs",
                    "source_url": source_url,
                    "count_value": None,
                    "count_kind": "UNKNOWN",
                    "confidence": 0.62,
                    "level": 1,
                    "evidence": [{"role": "sitemap"}],
                }],
            }

        def analyze_browser(self, source_url: str, *, output_dir: Path, session_dir: Path) -> dict:
            self.browser_calls += 1
            return {
                "status": "READY",
                "verified": True,
                "taxonomy_level": "L1",
                "profile_version": "fixture-l2-v1",
                "source_type": "DIRECT_BRAND",
                "source_scope": "SITE",
                "brain": {"status": "NOT_NEEDED", "provider_posts": 0},
                "categories": [{
                    "category_id": "cat_chairs",
                    "path": "/chairs",
                    "native_name": "Chairs",
                    "canonical_name": "Chairs",
                    "source_url": source_url,
                    "count_value": 9,
                    "count_kind": "EXACT",
                    "confidence": 0.95,
                    "level": 1,
                    "evidence": [{"role": "visible_count", "value": 9}],
                }],
            }

    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    session = database.session_factory()
    try:
        session.add(SiteRegistryRecord(site_key="example.test", domain="example.test", display_name="Acme"))
        session.commit()
    finally:
        session.close()

    analyzer = Analyzer()
    runtime = SiteScanRuntimeService(database, tmp_path / "output", analyzer)
    started = runtime.start(site_key="example.test", source_url="https://example.test/", job_id=None, live=True)
    deadline = time.monotonic() + 3
    current = started
    while current["status"] in {"QUEUED", "ANALYZING", "L2_BROWSER"} and time.monotonic() < deadline:
        time.sleep(0.01)
        current = runtime.status(started["scan_id"]) or current
    try:
        assert current["status"] == "READY"
        assert analyzer.browser_calls == 1
        session = database.session_factory()
        try:
            category = session.scalar(select(SiteCategory).where(SiteCategory.category_id == "cat_chairs"))
            assert category and category.count_value == 9 and category.count_kind == "EXACT"
        finally:
            session.close()
    finally:
        runtime.shutdown()
        database.dispose()


def test_blocked_latest_snapshot_does_not_expose_retained_categories(tmp_path: Path) -> None:
    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    session = database.session_factory()
    try:
        session.add(SiteRegistryRecord(site_key="example.test", domain="example.test", display_name="Acme"))
        session.add(SiteCategory(
            category_id="cat-old",
            site_key="example.test",
            snapshot_id="old-snapshot",
            path="/chairs",
            native_name="Chairs",
            canonical_name="Chairs",
            source_url="https://example.test/chairs",
            count_kind="UNKNOWN",
        ))
        session.add(SiteTaxonomySnapshot(
            snapshot_id="blocked-snapshot",
            site_key="example.test",
            source_url="https://example.test/",
            status="HUMAN_REQUIRED",
        ))
        session.add(SiteScanRun(
            scan_id="scan-blocked",
            site_key="example.test",
            source_url="https://example.test/",
            status="HUMAN_REQUIRED",
            live=True,
            finished_at=utc_now(),
        ))
        session.commit()
    finally:
        session.close()

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=database)))
    try:
        result = list_control_sites(request)
        item = result["items"][0]
        assert item["category_count"] is None
        assert item["unknown_count_categories"] is None
        assert item["taxonomy_available"] is False
        assert item["taxonomy_state"] == "HUMAN_REQUIRED"
        assert item["count_state"] == "UNKNOWN"
        assert item["latest_scan_status"] == "HUMAN_REQUIRED"

        detail = get_control_site("example.test", request)
        assert detail["categories"] == []
        assert detail["taxonomy_available"] is False
        assert detail["taxonomy_state"] == "HUMAN_REQUIRED"
        assert detail["count_state"] == "UNKNOWN"
        assert detail["snapshots"][0]["status"] == "HUMAN_REQUIRED"
    finally:
        database.dispose()


def test_job_snapshot_binding_never_falls_back_to_newer_taxonomy(tmp_path: Path) -> None:
    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    session = database.session_factory()
    try:
        session.add(SiteRegistryRecord(site_key="example.test", domain="example.test", display_name="Acme"))
        session.add_all([
            SiteCategory(
                category_id="old-chairs",
                site_key="example.test",
                snapshot_id="old-snapshot",
                path="/chairs",
                native_name="Chairs",
                canonical_name="Chairs",
                source_url="https://example.test/chairs",
            ),
            SiteCategory(
                category_id="new-tables",
                site_key="example.test",
                snapshot_id="new-snapshot",
                path="/tables",
                native_name="Tables",
                canonical_name="Tables",
                source_url="https://example.test/tables",
            ),
        ])
        job = _job("job-snapshot-bound", policy={
            "category_ids": ["old-chairs"],
            "category_snapshot_id": "missing-snapshot",
        })
        session.add(job)
        session.commit()

        assert _job_categories(session, job) == []
    finally:
        session.close()
        database.dispose()


def test_job_snapshot_binding_survives_site_rescan(tmp_path: Path) -> None:
    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    session = database.session_factory()
    try:
        session.add(SiteRegistryRecord(site_key="example.test", domain="example.test", display_name="Acme"))
        job = _job("job-snapshot-rescan", policy={"category_ids": ["cat-chairs"]})
        session.add(job)
        session.add(SiteScanRun(scan_id="scan-old", site_key="example.test", source_url="https://example.test/", status="ANALYZING", live=True, job_id=job.job_id))
        session.commit()
    finally:
        session.close()

    runtime = SiteScanRuntimeService(database, tmp_path / "output", object())
    old_receipt = {
        "status": "READY", "verified": True, "taxonomy_level": "L1", "source_type": "DIRECT_BRAND", "source_scope": "SITE",
        "categories": [{
            "category_id": "cat-chairs", "path": "/chairs", "native_name": "Chairs", "canonical_name": "Chairs",
            "source_url": "https://example.test/chairs", "count_value": 12, "count_kind": "EXACT", "confidence": 0.95,
            "level": 1, "evidence": [{"role": "visible_count", "value": 12}],
        }],
    }
    new_receipt = {
        "status": "READY", "verified": True, "taxonomy_level": "L1", "source_type": "DIRECT_BRAND", "source_scope": "SITE",
        "categories": [{
            "category_id": "cat-chairs", "path": "/chairs", "native_name": "Chairs", "canonical_name": "Chairs",
            "source_url": "https://example.test/chairs", "count_value": 99, "count_kind": "EXACT", "confidence": 0.95,
            "level": 1, "evidence": [{"role": "visible_count", "value": 99}],
        }],
    }
    try:
        runtime._persist("scan-old", old_receipt, tmp_path / "old")
        session = database.session_factory()
        try:
            job = session.get(ProductionJob, "job-snapshot-rescan")
            assert job is not None
            old_categories = _job_categories(session, job)
            assert old_categories and old_categories[0].count_value == 12
            snapshot_id = json.loads(job.policy_json)["category_snapshot_id"]
            assert session.scalar(select(SiteCategorySnapshot).where(SiteCategorySnapshot.snapshot_id == snapshot_id)) is not None
            session.add(SiteScanRun(scan_id="scan-new", site_key="example.test", source_url="https://example.test/", status="ANALYZING", live=True))
            session.commit()
        finally:
            session.close()
        runtime._persist("scan-new", new_receipt, tmp_path / "new")
        session = database.session_factory()
        try:
            job = session.get(ProductionJob, "job-snapshot-rescan")
            assert job is not None
            assert _job_categories(session, job)[0].count_value == 12
            current = session.scalar(select(SiteCategory).where(SiteCategory.site_key == "example.test", SiteCategory.path == "/chairs"))
            assert current is not None and current.count_value == 99
            contract_root = tmp_path / "contract"
            contract_root.mkdir(parents=True, exist_ok=True)
            contract = ProductionRuntimeService(database, tmp_path / "output")._contract(session, job, contract_root)[2]
            assert contract["categories"][0]["count_value"] == 12
        finally:
            session.close()
    finally:
        runtime.shutdown()
        database.dispose()


def test_glb_qa_rejects_truncated_chunk(tmp_path: Path) -> None:
    path = tmp_path / "truncated.glb"
    payload = b'{"asset":{"version":"2.0"}}' + b" " * 2
    declared = 12 + 8 + len(payload)
    path.write_bytes(
        b"glTF"
        + (2).to_bytes(4, "little")
        + declared.to_bytes(4, "little")
        + (len(payload) + 4).to_bytes(4, "little")
        + b"JSON"
        + payload
    )

    valid, reason = validate_glb(path)

    assert valid is False
    assert reason == "invalid_chunk_length"


def test_site_list_exposes_active_scan_for_frontend_resume(tmp_path: Path) -> None:
    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    session = database.session_factory()
    try:
        session.add(SiteRegistryRecord(site_key="example.test", domain="example.test", display_name="Acme"))
        session.add(SiteScanRun(
            scan_id="scan-active",
            site_key="example.test",
            source_url="https://example.test/",
            status="ANALYZING",
            live=True,
        ))
        session.commit()
    finally:
        session.close()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=database)))
    try:
        result = list_control_sites(request)
        item = result["items"][0]
        assert item["category_count"] is None
        assert item["taxonomy_available"] is False
        assert item["taxonomy_state"] == "SCANNING"
        assert item["count_state"] == "UNKNOWN"
        assert item["latest_scan_id"] == "scan-active"
        assert item["latest_scan_status"] == "ANALYZING"
        assert item["latest_scan_finished_at"] is None
    finally:
        database.dispose()


def test_provider_ready_has_real_qualification_path_without_post(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LUX3D_API_KEY", "fixture-key-never-sent")
    monkeypatch.setenv("LUX3D_BASE_URL", "http://provider.test")
    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    session = database.session_factory()
    try:
        session.add(_job("job-provider-ready", provider="lux3d"))
        session.commit()
    finally:
        session.close()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=database)))
    result = approve_job(
        "job-provider-ready",
        ControlJobApproval(confirm=True, approved_cost_ceiling_minor=2500, actor="test-operator"),
        request,
    )
    assert result["status"] == "PRODUCTION_READY"
    assert result["provider_calls"] == 0
    database.dispose()


def test_exact_n_reaches_ready_pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    contract = _contract(tmp_path, job_id="job-ready-three", target=3)
    pipeline, events = _run_pipeline(monkeypatch, tmp_path, contract=contract, products=[_product(index) for index in range(3)])
    assert pipeline.run() == 2
    assert [item.state for item in pipeline.pool.records()] == [ItemState.MODEL_INPUT_LOCKED] * 3
    assert any(item["type"] == "READY_POOL_COMPLETED" and item["done"] == 3 for item in events)
    assert sum(int(item["payload"].get("provider_calls", 0)) for item in events) == 0


def test_local_agent_review_mode_reaches_ready_pool_without_brain_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FURNITURE_WORKFLOW_TEST_FIXTURES", raising=False)
    monkeypatch.setenv("LOCAL_REVIEW_MODE", "agent")
    monkeypatch.setattr("workers.production_pipeline.SafeHttpClient", FakeMediaClient)
    contract = _contract(tmp_path, job_id="job-local-agent", target=1)
    acquisition = FakeAcquisition([_product(0)])
    events: list[dict] = []

    def emit(event_type, stage, message, done, total, payload):
        events.append({"type": event_type, "payload": payload or {}})

    pipeline = ProductionPipeline(
        contract=contract,
        workspace=Path(contract["workspace"]),
        emit=emit,
        acquisition_factory=lambda **_: acquisition,
        provider_client=None,
    )
    assert pipeline.run() == 2
    record = pipeline.pool.records()[0]
    assert record.state is ItemState.MODEL_INPUT_LOCKED
    assert record.lineage["review_provider"] == "LOCAL_AGENT"
    assert record.lineage["brain_receipt"]["status"] == "LOCAL_AGENT_REVIEW"
    assert record.lineage["brain_receipt"]["provider_posts"] == 0


def test_strict_source_l2_pending_emits_human_required_instead_of_bounded_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    contract = _contract(tmp_path, job_id="job-l2-human-required", target=1, source_type="DIRECT_BRAND")
    contract["source_url"] = "https://francfranc.com/collections/living_storage"
    contract["site_key"] = "francfranc.com"
    product = _product(0)
    product.acquisition = "NATIVE"
    pipeline, events = _run_pipeline(
        monkeypatch,
        tmp_path,
        contract=contract,
        products=[product],
    )

    assert pipeline.run() == 2
    human = next(item for item in events if item["type"] == "HUMAN_REQUIRED")
    assert human["stage"] == "L2_BROWSER"
    assert human["payload"]["reason_code"] == "L2_BROWSER_REQUIRED"
    assert human["payload"]["candidate_id"]
    assert human["payload"]["url"].startswith("https://example.test/products/")
    assert not any(item["payload"].get("blocker") == "BOUNDED_TICK_LIMIT" for item in events)


def test_local_agent_without_review_evidence_reports_actionable_blocker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FURNITURE_WORKFLOW_TEST_FIXTURES", raising=False)
    monkeypatch.setenv("LOCAL_REVIEW_MODE", "agent")
    monkeypatch.setattr("workers.production_pipeline.SafeHttpClient", FakeMediaClient)
    contract = _contract(tmp_path, job_id="job-local-agent-awaiting-review", target=1)
    product = _product(0)
    product.acquisition = "NATIVE"
    product.evidence = {}
    pipeline, events = _run_pipeline(monkeypatch, tmp_path, contract=contract, products=[product])
    monkeypatch.delenv("FURNITURE_WORKFLOW_TEST_FIXTURES", raising=False)

    assert pipeline.run() == 2
    blocked = next(item for item in events if item["type"] == "JOB_BLOCKED" and item["stage"] == "BRAIN_DECISION")
    assert blocked["payload"]["blocker"] == "LOCAL_AGENT_REVIEW_REQUIRED"
    assert "显式复核证据" in blocked["message"]


def test_local_agent_review_endpoint_persists_same_image_evidence_for_resume(tmp_path: Path) -> None:
    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    job = _job("job-local-review-endpoint")
    job.status = "BLOCKED"
    job.current_stage = "BRAIN_DECISION"
    pool_path = tmp_path / "candidate_pool.json"
    job.candidate_pool_path = str(pool_path)
    session = database.session_factory()
    try:
        session.add(job)
        session.commit()
    finally:
        session.close()
    digest = "a" * 64
    pool = CandidatePoolStore(pool_path, order_id=job.job_id, job_id=job.job_id)
    pool.add_candidates([CandidateRecord(
        candidate_id="candidate-local-review",
        order_id=job.job_id,
        job_id=job.job_id,
        record_id="record-local-review",
        source="example.test",
        source_product_id="sku-local-review",
        canonical_url="https://example.test/products/chair",
        preview_id="preview-local-review",
        preview_url="https://example.test/media/chair.png",
        capture_sha256=digest,
        image_sha256=digest,
        category_group="Chairs",
        state=ItemState.VISUAL_PENDING,
        rejection_reason="BRAIN_NOT_CONFIGURED",
        media_status="READY",
        lineage={
            "source_name": "Lounge Chair",
            "media_sha256": digest,
            "media_binding_status": "COMPATIBLE",
            "capture_evidence": {"product_identity_match": True},
        },
    )])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        database=database,
        website_brain=WebsiteBrainProvider(BrainSettings(model_mode="LOCAL_AGENT")),
    )))
    review = {
        "eligible": True,
        "single_product": True,
        "background_ok": True,
        "image_to_3d_suitable": True,
        "category_group": "Chairs",
        "product_type": "Lounge Chair",
        "confidence": 0.95,
        "reason_codes": ["LOCAL_AGENT_TEST"],
        "source_image_vision_consistent": True,
        "reviewed_media_sha256": digest,
    }
    try:
        detail = get_candidate_for_review(job.job_id, "candidate-local-review", request)
        assert detail["candidate"]["media_sha256"] == digest
        assert detail["local_agent_enabled"] is True
        result = record_local_agent_review(
            job.job_id,
            "candidate-local-review",
            LocalAgentProductReviewRequest(review=review, actor="test-local-agent"),
            request,
        )
        assert result["status"] == "RECORDED"
        assert result["resume_safe"] is True
        record = pool.records()[0]
        assert record.lineage["capture_evidence"]["local_agent_review"]["reviewed_media_sha256"] == digest
        session = database.session_factory()
        try:
            refreshed = session.get(ProductionJob, job.job_id)
            assert refreshed and refreshed.status == "REVIEW_RESOLVED"
        finally:
            session.close()
    finally:
        database.dispose()


def test_up_to_n_accepts_smaller_ready_pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    contract = _contract(tmp_path, job_id="job-up-to-three", target=3)
    contract["target_mode"] = "UP_TO_N"
    pipeline, events = _run_pipeline(
        monkeypatch,
        tmp_path,
        contract=contract,
        products=[_product(0), _product(1)],
    )
    assert pipeline.run() == 2
    assert [item.state for item in pipeline.pool.records()] == [ItemState.MODEL_INPUT_LOCKED] * 2
    assert any(item["type"] == "READY_POOL_COMPLETED" and item["done"] == 2 and item["total"] == 3 for item in events), events
    assert not any(item["type"] == "TARGET_SHORTAGE" for item in events)


@pytest.mark.parametrize("target", [1, 3])
def test_fake_provider_completes_exact_n_delivery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: int) -> None:
    contract = _contract(tmp_path, job_id=f"job-delivery-{target}", target=target, provider="lux3d")
    provider = FakeProvider()
    pipeline, events = _run_pipeline(
        monkeypatch,
        tmp_path,
        contract=contract,
        products=[_product(index, name="Shared Product Name") for index in range(target)],
        provider=provider,
    )
    assert pipeline.run() == 0, events
    assert provider.create_calls == target
    assert provider.download_calls == target
    manifest = json.loads((Path(contract["workspace"]) / "05_delivery" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["delivered"] == target
    assert len({item["filename"] for item in manifest["items"]}) == target
    assert any(item["type"] == "JOB_COMPLETED" and item["done"] == target for item in events)


def test_provider_capacity_wait_preserves_attempt_ledger_and_resumes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    contract = _contract(tmp_path, job_id="job-provider-capacity", target=1, provider="lux3d")
    waiting_provider = FakeProvider(capacity_rejection=True)
    first, events = _run_pipeline(monkeypatch, tmp_path, contract=contract, products=[_product(1)], provider=waiting_provider)
    assert first.run() == 2
    assert waiting_provider.create_calls == 1
    assert any(item["payload"].get("blocker") == "PROVIDER_CAPACITY" for item in events)

    finishing_provider = FakeProvider()
    resumed, _ = _run_pipeline(monkeypatch, tmp_path, contract=contract, products=[], provider=finishing_provider)
    assert resumed.run() == 0
    assert finishing_provider.create_calls == 1
    session = resumed.database.session_factory()
    try:
        ledger = session.scalar(select(ProductionProviderTask).where(ProductionProviderTask.job_id == contract["job_id"]))
        assert ledger and ledger.post_attempts == 2 and ledger.status == "DELIVERED"
    finally:
        session.close()


def test_provider_off_ready_pool_can_resume_after_approval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    off_contract = _contract(tmp_path, job_id="job-off-to-live", target=1)
    first, _ = _run_pipeline(monkeypatch, tmp_path, contract=off_contract, products=[_product(1)])
    assert first.run() == 2
    old_hash = first.pool.records()[0].model_input_hash

    live_contract = dict(off_contract)
    live_contract["provider"] = "lux3d"
    live_contract["authorization"] = {"approve_paid_generation": True}
    provider = FakeProvider()
    resumed, _ = _run_pipeline(monkeypatch, tmp_path, contract=live_contract, products=[], provider=provider)
    assert resumed.run() == 0
    record = resumed.pool.records()[0]
    assert provider.create_calls == 1
    assert record.model_input_hash != old_hash
    assert record.lineage["model_input_policy_migrated"] is True


def test_resume_does_not_duplicate_provider_post(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    contract = _contract(tmp_path, job_id="job-provider-resume", target=1, provider="lux3d")
    waiting_provider = FakeProvider(poll_timeout=True)
    first, _ = _run_pipeline(monkeypatch, tmp_path, contract=contract, products=[_product(1)], provider=waiting_provider)
    assert first.run() == 2
    assert waiting_provider.create_calls == 1

    finishing_provider = FakeProvider()
    resumed, _ = _run_pipeline(monkeypatch, tmp_path, contract=contract, products=[], provider=finishing_provider)
    assert resumed.run() == 0
    assert finishing_provider.create_calls == 0
    session = resumed.database.session_factory()
    try:
        ledger = session.scalar(select(ProductionProviderTask).where(ProductionProviderTask.job_id == contract["job_id"]))
        assert ledger and ledger.post_attempts == 1 and ledger.status == "DELIVERED"
    finally:
        session.close()


def test_source_name_first_rules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cases = [
        ("DIRECT_BRAND", "Lounge Chair", "Maker", "Acme Lounge Chair"),
        ("MULTI_BRAND_RETAILER", "Aeron Chair", "Herman Miller", "Herman Miller Aeron Chair"),
        ("MARKETPLACE", "Vintage Chair", "Marketplace Platform", "Vintage Chair"),
    ]
    for index, (source_type, source_name, source_brand, expected) in enumerate(cases):
        case_root = tmp_path / str(index)
        contract = _contract(case_root, job_id=f"job-name-{index}", target=1, source_type=source_type)
        pipeline, _ = _run_pipeline(
            monkeypatch,
            case_root,
            contract=contract,
            products=[_product(index, name=source_name, brand=source_brand)],
        )
        assert pipeline.run() == 2
        record = pipeline.pool.records()[0]
        assert record.product_name == expected
        assert record.lineage["naming_decision_source"] == "SOURCE_NAME_FIRST"
        if source_type == "MARKETPLACE":
            assert record.lineage["platform_brand_excluded"] is True


def test_anthropologie_source_policy_preserves_multi_category_kind() -> None:
    assert classify_source_type(
        "https://www.anthropologie.com/anthrohome/collection-shop-all-kitchen",
        "<html><body>1196 products</body></html>",
    ) == "MULTI_CATEGORY_RETAILER"


def test_production_runtime_uses_workflow_engine() -> None:
    source = inspect.getsource(ProductionPipeline.run)
    assert "ProductionWorkflowEngine(" in source
    assert "engine.tick()" in source
    assert "CandidatePoolStore" in inspect.getsource(ProductionPipeline.__init__)
    launcher_source = inspect.getsource(ProductionRuntimeService._launch)
    assert 'packages" / "workflow-engine" / "src' in launcher_source


def test_production_runtime_keeps_new_resume_alive_during_worker_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    job = _job("job-startup-grace")
    session = database.session_factory()
    try:
        session.add(job)
        session.commit()
        claimed_at = utc_now()
        session.add(RuntimeEvent(
            runtime_event_id="job-startup-grace:previous",
            job_id=job.job_id,
            sequence=1,
            event_type="JOB_BLOCKED",
            status="BLOCKED",
            stage="BRAIN_DECISION",
            message="previous attempt",
            created_at=claimed_at - timedelta(seconds=10),
        ))
        run = ProductionRun(
            run_id="run-startup-grace",
            job_id=job.job_id,
            status="RUNNING",
            stage="PREFLIGHT",
            workspace=str(tmp_path / "workspace"),
            command_json=json.dumps([sys.executable]),
            pid=999999,
            started_at=claimed_at,
            claimed_at=claimed_at,
        )
        session.add(run)
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(production_runtime_module, "_process_alive", lambda *_: False)
    monkeypatch.setattr(production_runtime_module, "RUNTIME_EVENT_FLUSH_SECONDS", 0)
    runtime = ProductionRuntimeService(database, tmp_path / "output")
    try:
        current = runtime.reconcile_run("run-startup-grace")
        assert current and current["status"] == "RUNNING"
        assert current["stage"] == "PREFLIGHT"
    finally:
        database.dispose()


def test_production_runtime_retries_empty_worker_launch_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    job = _job("job-empty-launch-retry")
    session = database.session_factory()
    try:
        session.add(job)
        session.commit()
        started_at = utc_now()
        session.add(ProductionRun(
            run_id="run-empty-launch-retry",
            job_id=job.job_id,
            status="RUNNING",
            stage="PREFLIGHT",
            workspace=str(tmp_path / "workspace"),
            command_json=json.dumps([sys.executable]),
            pid=999999,
            launch_attempts=1,
            started_at=started_at,
            claimed_at=started_at,
        ))
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(production_runtime_module, "_process_alive", lambda *_: False)
    monkeypatch.setattr(production_runtime_module, "RUNTIME_STARTUP_GRACE_SECONDS", 0)
    monkeypatch.setattr(production_runtime_module, "RUNTIME_EVENT_FLUSH_SECONDS", 0)
    launches: list[bool] = []
    runtime = ProductionRuntimeService(database, tmp_path / "output")

    def fake_launch(session, job, run, *, resume: bool = False) -> None:
        launches.append(resume)
        run.status = "RUNNING"
        run.stage = "PREFLIGHT"
        run.pid = 123456
        run.launch_attempts += 1

    monkeypatch.setattr(runtime, "_launch", fake_launch)
    try:
        current = runtime.reconcile_run("run-empty-launch-retry")
        assert current and current["status"] == "RUNNING"
        assert current["launch_attempts"] == 2
        assert launches == [False]
    finally:
        database.dispose()


def test_production_runtime_event_ingestion_is_idempotent_across_read_models(tmp_path: Path) -> None:
    database = Database(tmp_path / "system" / "control.sqlite3")
    database.create_schema()
    job = _job("job-event-idempotent")
    session = database.session_factory()
    try:
        session.add(job)
        session.commit()
        runtime = ProductionRuntimeService(database, tmp_path / "output")
        event = {
            "schema_version": "workflow-event.v2",
            "event_id": "job-event-idempotent:1",
            "job_id": job.job_id,
            "sequence": 1,
            "type": "JOB_STARTED",
            "status": "RUNNING",
            "stage": "PREFLIGHT",
            "message": "started",
        }
        assert runtime.ingest_event(session, event) is True
        session.commit()
        # A second browser poll must be a no-op even though both read models
        # carry the same runtime identity.
        assert runtime.ingest_event(session, event) is False
        session.commit()
        assert session.scalar(
            select(RuntimeEvent).where(RuntimeEvent.runtime_event_id == event["event_id"])
        ) is not None
        assert session.scalar(
            select(ProductionJobEvent).where(ProductionJobEvent.runtime_event_id == event["event_id"])
        ) is not None
    finally:
        session.close()
        database.dispose()


def test_database_migration_is_additive_and_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "system" / "control.sqlite3"
    first = Database(database_path)
    first.create_schema()
    session = first.session_factory()
    try:
        session.add(SiteRegistryRecord(site_key="history.test", domain="history.test", display_name="Historical Site"))
        session.commit()
    finally:
        session.close()
        first.dispose()

    resumed = Database(database_path)
    resumed.create_schema()
    resumed.create_schema()
    session = resumed.session_factory()
    try:
        site = session.get(SiteRegistryRecord, "history.test")
        assert site and site.display_name == "Historical Site"
        assert resumed.SCHEMA_VERSION == "workflow-schema.v8-production-launch-retry"
    finally:
        session.close()
        resumed.dispose()
