"""Deterministic local commerce site and provider boundaries for Website E2E.

The local profile is deliberately opt-in and is only assembled by the E2E
test/runtime when ``test_profile=LOCAL_E2E`` and
``FURNITURE_WORKFLOW_LOCAL_E2E`` are both present.  It never replaces the
normal Website acquisition, Brain, Lux3D, or Blender configuration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.brain_provider import BrainSettings, WebsiteBrainProvider
from app.services.product_acquisition import AcquiredProduct, ProductSupplyExhausted
from workers.blender_adapter import FakeBlenderAdapter


LOCAL_SITE_KEY = "local.mock"
LOCAL_SITE_URL = "https://local.mock/"
LOCAL_CATEGORY_ID = "local-cat-chairs"
LOCAL_CATEGORY_PATH = "/collections/chairs"
LOCAL_PRODUCT_COUNT = 21


def _minimal_glb(seed: str) -> bytes:
    """Build a tiny valid glTF 2.0 container without external assets."""

    payload = json.dumps(
        {"asset": {"version": "2.0", "generator": "Furniture Workflow local E2E"}, "extras": {"seed": seed}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    payload += b" " * ((4 - len(payload) % 4) % 4)
    total_length = 12 + 8 + len(payload)
    return b"glTF" + (2).to_bytes(4, "little") + total_length.to_bytes(4, "little") + len(payload).to_bytes(4, "little") + b"JSON" + payload


LOCAL_IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + (b"local-e2e-product-image" * 128)


@dataclass(frozen=True, slots=True)
class LocalMockMediaResponse:
    content: bytes = LOCAL_IMAGE_BYTES
    content_type: str = "image/png"


class LocalMockMediaClient:
    """Stable media boundary; no network is reachable from this client."""

    def __init__(self, **_: object) -> None:
        pass

    def get_media(self, _: str) -> LocalMockMediaResponse:
        return LocalMockMediaResponse()


class LocalMockAcquisition:
    """A small product pool that behaves like a resumable commerce collector."""

    def __init__(self, *, categories: list[dict[str, Any]], target_count: int, browser_session_dir: Path, **_: object) -> None:
        self.browser_session_dir = Path(browser_session_dir)
        self.browser_session_dir.mkdir(parents=True, exist_ok=True)
        selected = next((item for item in categories if item.get("selected", True)), {})
        self.category_id = str(selected.get("category_id") or LOCAL_CATEGORY_ID)
        self.category_group = str(selected.get("canonical_name") or "Chairs")
        self.products = self._make_products(max(LOCAL_PRODUCT_COUNT, int(target_count or 0)))
        self.cursor = 0

    def _make_products(self, count: int) -> list[AcquiredProduct]:
        products: list[AcquiredProduct] = []
        for index in range(1, count + 1):
            sku = f"LOCAL-{index:03d}"
            canonical_url = f"{LOCAL_SITE_URL.rstrip('/')}{LOCAL_CATEGORY_PATH}/product/{sku.casefold()}"
            image_url = f"{LOCAL_SITE_URL.rstrip('/')}/media/{sku.casefold()}.png"
            dimensions = {"width": 24.0, "depth": 26.0, "height": 31.0}
            review = {
                "eligible": True,
                "single_product": True,
                "background_ok": True,
                "image_to_3d_suitable": True,
                "category_group": self.category_group,
                "style": "Modern",
                "color": "Walnut" if index % 2 else "Oak",
                "material": "Wood",
                "product_type": "Lounge Chair",
                "feature": "Solid frame",
                "width": dimensions["width"],
                "depth": dimensions["depth"],
                "height": dimensions["height"],
                "dimension_unit": "in",
                "confidence": 0.99,
                "reason_codes": ["LOCAL_E2E_ACCEPTED"],
                "source_image_vision_consistent": True,
            }
            identity_fields = {
                "source_product_id": sku,
                "route_id": f"product/{sku.casefold()}",
                "url_tail_id": sku.casefold(),
                "jsonld_sku": sku,
                "configuration_key": f"{sku}:default",
                "variant_key": "default",
                "asset_identity": sku,
                "identity_conflicts": [],
            }
            evidence = {
                "local_agent_review": review,
                "product_identity_match": True,
                "configuration_bound": True,
                "media_binding_status": "COMPATIBLE",
                "media_binding_confidence": 0.99,
                "media_binding_reasons": ["LOCAL_PRIMARY_IMAGE_BOUND_TO_PRODUCT"],
                "scope_status": "PASS",
                "scope_reasons": ["LOCAL_CATEGORY_SCOPE"],
                "image_role": "MAIN_PRODUCT",
                "layered_scene7": False,
                "dimension_source": "OFFICIAL_STRUCTURED",
            }
            products.append(AcquiredProduct(
                source_product_id=sku,
                canonical_url=canonical_url,
                source_name=f"Walnut Lounge Chair {index:02d}",
                source_brand="Local Mock Commerce",
                category_id=self.category_id,
                category_group=self.category_group,
                image_url=image_url,
                dimensions=dimensions,
                dimension_unit="in",
                source_type="DIRECT_BRAND",
                capture_sha256=hashlib.sha256(f"capture:{sku}".encode("utf-8")).hexdigest(),
                acquisition="LOCAL_MOCK_COMMERCE",
                evidence=evidence,
                identity_fields=identity_fields,
                media_binding_status="COMPATIBLE",
                media_binding_confidence=0.99,
                scope_status="PASS",
            ))
        return products

    def discover(self, needed: int) -> list[AcquiredProduct]:
        selected = self.products[self.cursor : self.cursor + max(0, int(needed))]
        self.cursor += len(selected)
        if not selected:
            raise ProductSupplyExhausted("local mock commerce pool exhausted")
        return selected


class LocalMockLux3D:
    """Receipt-compatible Lux3D substitute with deterministic task IDs."""

    def __init__(self) -> None:
        self.create_calls = 0
        self.poll_calls = 0
        self.download_calls = 0
        self._tasks: dict[str, str] = {}

    def create_task(self, image_path: Path, *, idempotency_key: str | None = None):
        if not image_path.is_file() or not idempotency_key:
            return None, "local provider input or idempotency key missing"
        self.create_calls += 1
        task_id = self._tasks.setdefault(idempotency_key, f"local-lux3d-{hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()[:16]}")
        return task_id, None

    def poll_task(self, provider_task_id: str):
        self.poll_calls += 1
        return {"status": "completed", "model_url": f"local://{provider_task_id}.glb"}, None

    def download_glb(self, _: dict[str, Any] | None, provider_task_id: str, out_path: Path) -> bool:
        self.download_calls += 1
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(_minimal_glb(provider_task_id))
        return True


class LocalMockSiteAnalyzer:
    """Deterministic analyzer used to exercise Add Site → taxonomy persistence."""

    def preflight(self, source_url: str, *, live: bool, output_dir: Path) -> dict[str, Any]:
        return {
            "schema_version": "website-site-preflight.v2",
            "url": source_url,
            "domain": LOCAL_SITE_KEY,
            "status": "READY",
            "network_called": False,
            "live": live,
            "source_type": "DIRECT_BRAND",
            "next_action": "可以开始类目发现",
        }

    def _receipt(self, source_url: str, *, live: bool) -> dict[str, Any]:
        return {
            "schema_version": "website-taxonomy-receipt.v3",
            "site_key": LOCAL_SITE_KEY,
            "source_url": source_url,
            "live": live,
            "status": "READY",
            "verified": True,
            "fixture_only": False,
            "taxonomy_level": "L1",
            "source_type": "DIRECT_BRAND",
            "source_scope": "CATEGORY",
            "profile_version": "local-e2e-v1",
            "evidence": {"collector": "LOCAL_MOCK_COMMERCE", "network_called": False},
            "brain": {"status": "LOCAL_AGENT_READY", "provider_posts": 0},
            "categories": [{
                "category_id": LOCAL_CATEGORY_ID,
                "native_name": "Chairs",
                "canonical_name": "Chairs",
                "path": LOCAL_CATEGORY_PATH,
                "source_url": f"{LOCAL_SITE_URL.rstrip('/')}{LOCAL_CATEGORY_PATH}",
                "count_value": LOCAL_PRODUCT_COUNT,
                "count_kind": "EXACT",
                "confidence": 1.0,
                "level": 1,
                "parent_path": None,
                "evidence": [{"kind": "local_catalog_count", "value": LOCAL_PRODUCT_COUNT}],
            }],
        }

    def analyze(self, source_url: str, *, live: bool, output_dir: Path) -> dict[str, Any]:
        receipt = self._receipt(source_url, live=live)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "taxonomy_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return receipt

    def analyze_browser(self, source_url: str, *, output_dir: Path, session_dir: Path) -> dict[str, Any]:
        return self.analyze(source_url, live=True, output_dir=output_dir)


def build_local_e2e_components(contract: dict[str, Any], workspace: Path) -> dict[str, Any]:
    categories = [dict(item) for item in contract.get("categories") or []]
    target_count = int(contract.get("target_value") or LOCAL_PRODUCT_COUNT)
    browser = contract.get("browser_session") or {}
    session_dir = Path(str(browser.get("user_data_dir") or workspace / "browser_session"))
    return {
        "acquisition_factory": lambda **_: LocalMockAcquisition(
            categories=categories,
            target_count=target_count,
            browser_session_dir=session_dir,
        ),
        "brain": WebsiteBrainProvider(BrainSettings(model_mode="LOCAL_AGENT")),
        "provider_client": LocalMockLux3D(),
        "blender_adapter": FakeBlenderAdapter(),
        "media_client_factory": LocalMockMediaClient,
    }


__all__ = [
    "LOCAL_CATEGORY_ID",
    "LOCAL_CATEGORY_PATH",
    "LOCAL_PRODUCT_COUNT",
    "LOCAL_SITE_KEY",
    "LOCAL_SITE_URL",
    "LocalMockAcquisition",
    "LocalMockLux3D",
    "LocalMockMediaClient",
    "LocalMockSiteAnalyzer",
    "build_local_e2e_components",
]
