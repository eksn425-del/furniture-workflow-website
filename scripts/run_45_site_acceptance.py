"""Bounded, truthful 45-site layered acceptance runner.

The runner owns the denominator and writes raw evidence outside the repository.
It performs Layer A for every requested URL and optionally Layer B/C for sites
whose A result is not an access-policy blocker.  No result is defaulted to
PASS: every missing layer is explicitly NOT_RUN/NOT_APPLICABLE with a reason.
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.database import Database
from app.models import SiteRegistryRecord, SiteScanRun
from app.services.native_site_analysis import NativeSiteAnalyzer
from app.services.product_acquisition import (
    AcquiredProduct,
    BrowserAccessDenied,
    BrowserHumanRequired,
    BrowserRuntimeMissing,
    BrowserTemporaryFailure,
    ProductAcquisitionEngine,
    ProductAcquisitionError,
    ProductSupplyExhausted,
)
from app.services.site_scan_runtime import SiteScanRuntimeService
from app.services.native_site_analysis import site_key_for
from workers.production_pipeline import ProductionPipeline


SITES = [
    ("Anthropologie", "https://www.anthropologie.com/"),
    ("Arhaus", "https://www.arhaus.com/"),
    ("Article", "https://www.article.com/"),
    ("Castlery", "https://www.castlery.com/"),
    ("Interior Define", "https://www.interiordefine.com/"),
    ("Ligne Roset", "https://www.ligne-roset.com/"),
    ("MacKenzie-Childs", "https://www.mackenzie-childs.com/"),
    ("Nathan James", "https://nathanjames.com/"),
    ("POLYWOOD", "https://www.polywood.com/"),
    ("RH", "https://rh.com/"),
    ("Room & Board", "https://www.roomandboard.com/"),
    ("Rove Concepts", "https://www.roveconcepts.com/"),
    ("Sixpenny", "https://sixpenny.com/"),
    ("Walker Edison", "https://walkeredison.com/"),
    ("India Art n Design", "https://www.indiaartndesign.com/"),
    ("Kayu", "https://kayu.com/"),
    ("Fabuliv", "https://fabuliv.com/"),
    ("Globally Indian", "https://globallyindian.com/"),
    ("Indikasa", "https://indikasa.com/"),
    ("Iris", "https://www.irisus.com/"),
    ("Hometown", "https://www.hometown.in/"),
    ("Featherlite", "https://featherlitefurniture.com/"),
    ("Interio", "https://www.interio.in/"),
    ("Durian", "https://www.durian.in/"),
    ("Indian Nest", "https://indiannest.in/"),
    ("Furnishka", "https://furnishka.com/"),
    ("LoveNspire", "https://www.lovenspire.com/"),
    ("Indian Hub", "https://indianhub.com/"),
    ("DesignConnected", "https://www.designconnected.com/"),
    ("Archive3D", "https://archive3d.net/"),
    ("GrabCAD", "https://grabcad.com/"),
    ("CGTrader", "https://www.cgtrader.com/"),
    ("Free3D", "https://free3d.com/"),
    ("Archibase", "https://archibaseplanet.com/"),
    ("3DExport", "https://3dexport.com/"),
    ("3D Warehouse", "https://3dwarehouse.sketchup.com/"),
    ("Sweet Home 3D", "https://www.sweethome3d.com/"),
    ("NASA 3D", "https://nasa3d.arc.nasa.gov/"),
    ("3DXO", "https://3dxo.com/"),
    ("MyMiniFactory", "https://www.myminifactory.com/"),
    ("Poly Haven", "https://polyhaven.com/"),
    ("Alessi", "https://www.alessi.com/"),
    ("Safavieh", "https://safavieh.com/"),
    ("Driade", "https://www.driade.com/"),
    ("West Elm", "https://www.westelm.com/"),
]


def _status_bucket(status: str, *, layer: str) -> str:
    value = str(status or "").upper()
    if value in {"READY", "PASS", "AGENT_READY", "EXHAUSTED"}:
        return "PASS"
    if value in {"PARTIAL", "PAGINATION_UNVERIFIED", "AGENT_RECOVERED", "REVIEW_REQUIRED", "TARGET_SHORTAGE"}:
        return "PARTIAL"
    if value in {"ROBOTS_DENIED", "BROWSER_REQUIRED", "HUMAN_REQUIRED", "ACCESS_CHANGE_REQUIRED", "BROWSER_RESUME_REQUIRED", "ACCESS_BLOCKED"}:
        return "ACCESS_BLOCKED"
    if value in {"TEMPORARY_FAILURE", "ENVIRONMENT_BLOCKED", "BROWSER_RUNTIME_MISSING", "BROWSER_NAVIGATION_FAILED"}:
        return "ENVIRONMENT_BLOCKED"
    if value in {"BRAIN_NOT_CONFIGURED", "VISION_PROVIDER_NOT_CONFIGURED", "BRAIN_ERROR"}:
        return "UNSUPPORTED_CAPABILITY"
    if value in {"NOT_APPLICABLE", "UNSUPPORTED_CAPABILITY"}:
        return value
    if not value:
        return "NOT_RUN"
    return "CODE_DEFECT" if layer in {"B", "C", "D"} else "FAILED"


def _exception_evidence(error: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {"error_type": type(error).__name__, "message": str(error)[:800]}
    for key in ("reason_code", "code", "url", "session_dir", "evidence"):
        value = getattr(error, key, None)
        if value not in (None, ""):
            payload[key] = str(value) if key != "evidence" else value
    return payload


def _bounded_call(function, *, timeout_seconds: float):
    """Run one site operation with a daemon timeout for campaign isolation."""

    box: dict[str, Any] = {}

    def invoke() -> None:
        try:
            box["result"] = function()
        except BaseException as error:  # propagate into the caller thread
            box["error"] = error

    worker = threading.Thread(target=invoke, daemon=True, name="acceptance-operation")
    worker.start()
    worker.join(max(1.0, float(timeout_seconds)))
    if worker.is_alive():
        raise TimeoutError(f"bounded site operation exceeded {timeout_seconds:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _formal_runtime_taxonomy_inner(
    name: str,
    url: str,
    output_root: Path,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run Layer B through the same durable SiteScanRuntime as employees.

    The campaign runner used to call ``NativeSiteAnalyzer.analyze`` directly,
    which skipped the persisted scan/browser-session boundary and therefore
    could not prove L2 continuation or restart recovery.  This helper creates
    only the normal control-plane rows, queues one scan, waits on its durable
    status, and reads the receipt emitted by that runtime.  It does not make a
    second acquisition engine or fabricate a taxonomy when the runtime stops.
    """

    slug = name.replace("/", "_")
    # Each concurrently accepted site gets an isolated control-plane database.
    # Sharing one SQLite file across the six campaign workers made otherwise
    # independent scans contend on the same write lock and turned a campaign
    # harness defect into false CODE_DEFECT results.  The employee runtime is
    # still the same SiteScanRuntimeService; only its bounded evidence root is
    # isolated per site for this parallel acceptance run.
    runtime_root = output_root / "formal_runtime" / slug
    database = Database(runtime_root / "_system" / "acceptance.sqlite3")
    database.create_schema()
    analyzer = NativeSiteAnalyzer(output_root / "analyzer" / slug)
    runtime = SiteScanRuntimeService(database, runtime_root, analyzer)
    site_key = site_key_for(url)
    domain = (urlsplit(url).hostname or site_key).lower()
    session = database.session_factory()
    try:
        if session.get(SiteRegistryRecord, site_key) is None:
            session.add(SiteRegistryRecord(
                site_key=site_key,
                domain=domain,
                display_name=name,
                source_kind="UNKNOWN",
                source_health="UNKNOWN",
                acquisition_mode="UNKNOWN",
                status="DRAFT",
            ))
            session.commit()
    finally:
        session.close()
    try:
        queued = runtime.start(site_key=site_key, source_url=url, job_id=None, live=True)
        scan_id = str(queued.get("scan_id") or "")
        if not scan_id:
            raise RuntimeError("formal SiteScanRuntime did not return scan_id")
        terminal = {
            "READY", "PARTIAL", "HUMAN_REQUIRED", "ROBOTS_DENIED", "TEMPORARY_FAILURE",
            "ACCESS_CHANGE_REQUIRED", "SESSION_CONTINUITY_BROKEN", "BRAIN_NOT_CONFIGURED",
            "BROWSER_RUNTIME_NOT_INSTALLED", "FAILED", "BROWSER_RESUME_REQUIRED",
            # A live browser session can finish with a durable receipt that
            # still requires the employee brain/user to continue.  This is a
            # real terminal runtime state, not an invitation to spin until
            # the watchdog turns it into a misleading timeout.
            "BROWSER_REQUIRED",
        }
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        status: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status = runtime.status(scan_id)
            if status and str(status.get("status") or "") in terminal:
                break
            time.sleep(0.25)
        if not status or str(status.get("status") or "") not in terminal:
            # Persist the watchdog decision before releasing this isolated
            # runtime.  A timeout is not proof of an external block; it is an
            # unclassified bounded operation and must remain visible for
            # diagnosis rather than being promoted to ACCESS/ENVIRONMENT.
            session = database.session_factory()
            try:
                scan = session.get(SiteScanRun, scan_id)
                if scan is not None:
                    scan.status = "FAILED"
                    scan.error_code = "SCAN_TIMEOUT"
                    scan.error_message = f"formal SiteScanRuntime exceeded {timeout_seconds:.0f}s"
                    scan.finished_at = datetime.now(UTC)
                    session.commit()
            finally:
                session.close()
            raise TimeoutError(f"formal SiteScanRuntime scan exceeded {timeout_seconds:.0f}s")
        session = database.session_factory()
        try:
            scan = session.get(SiteScanRun, scan_id)
            receipt_path = Path(str(scan.receipt_path)) if scan and scan.receipt_path else None
        finally:
            session.close()
        receipt: dict[str, Any] = {}
        if receipt_path and receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not receipt:
            receipt = {
                "status": status.get("status"),
                "url": url,
                "categories": [],
                "blocker": {"code": status.get("error_code"), "message": status.get("error_message")},
            }
        receipt["formal_runtime"] = {
            "service": "SiteScanRuntimeService",
            "scan_id": scan_id,
            "browser_session_id": status.get("browser_session_id"),
            "status": status.get("status"),
            "receipt_path": str(receipt_path) if receipt_path else None,
            "resume_capable": True,
        }
        return receipt
    finally:
        # The scan receipt/status is already durable before this cleanup runs.
        # Do not wait on the executor here: a browser worker can be finishing
        # its Playwright teardown while the control-plane row is terminal, and
        # waiting from the formal child used to deadlock the acceptance harness
        # for the full watchdog window.  The parent observes the result file
        # and terminates this isolated child, so no worker can leak into the
        # next site.  Production runtime keeps its normal non-blocking policy.
        runtime.shutdown(wait=False)


def _formal_runtime_entry(
    name: str,
    url: str,
    output_root: str,
    timeout_seconds: float,
    result_queue,
) -> None:
    """Spawn target for one formal runtime scan.

    The target deliberately serializes only a small result envelope.  The
    durable scan receipt/database remain the source of truth on disk.  A
    parent timeout terminates this process, which is the only reliable way to
    stop arbitrary browser/network work on Windows.
    """

    result_path = Path(output_root) / "formal_runtime" / name.replace("/", "_") / "_control" / "formal_runtime_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = _formal_runtime_taxonomy_inner(
            name, url, Path(output_root), timeout_seconds=timeout_seconds
        )
        result_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Keep the IPC envelope tiny.  A full category receipt can exceed a
        # Windows multiprocessing pipe buffer and otherwise make a completed
        # child appear hung while the parent waits in join().
        result_queue.put({"kind": "result", "path": str(result_path)})
    except BaseException as error:
        error_path = result_path.with_name("formal_runtime_error.json")
        error_path.write_text(json.dumps({
            "kind": "error",
            "type": type(error).__name__,
            "message": str(error)[:1000],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result_queue.put({"kind": "error", "path": str(error_path)})


def _formal_runtime_taxonomy(
    name: str,
    url: str,
    output_root: Path,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run one employee-path scan in a killable process boundary."""

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_formal_runtime_entry,
        args=(name, url, str(output_root), float(timeout_seconds), result_queue),
        name=f"formal-site-runtime-{name.replace('/', '_')}",
    )
    process.start()
    runtime_root = output_root / "formal_runtime" / name.replace("/", "_")
    result_path = runtime_root / "_control" / "formal_runtime_result.json"
    error_path = runtime_root / "_control" / "formal_runtime_error.json"
    # A durable result file is the handoff boundary.  The child may still be
    # unwinding a browser thread; terminate only that isolated process once
    # the file is complete instead of waiting on an executor deadlock.
    deadline = time.monotonic() + max(1.0, float(timeout_seconds)) + 5.0
    while process.is_alive() and time.monotonic() < deadline:
        if result_path.is_file() or error_path.is_file():
            process.terminate()
            process.join(10.0)
            break
        process.join(0.25)
    if process.is_alive():
        process.terminate()
        process.join(10.0)
        # The child may have been killed between queueing and persisting its
        # watchdog state.  Leave an explicit durable timeout receipt so the
        # next operator can distinguish an interrupted scan from missing
        # evidence, without pretending the site was externally blocked.
        timeout_receipt = runtime_root / "_control" / "formal_runtime_timeout.json"
        timeout_receipt.parent.mkdir(parents=True, exist_ok=True)
        timeout_receipt.write_text(
            json.dumps({
                "status": "FAILED",
                "error_code": "SCAN_TIMEOUT",
                "error_message": f"formal SiteScanRuntime process exceeded {timeout_seconds:.0f}s",
                "site": name,
                "url": url,
                "timeout_seconds": timeout_seconds,
                "process_exitcode": process.exitcode,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        raise TimeoutError(f"formal SiteScanRuntime process exceeded {timeout_seconds:.0f}s")
    if result_path.is_file():
        return json.loads(result_path.read_text(encoding="utf-8"))
    if error_path.is_file():
        error_payload = json.loads(error_path.read_text(encoding="utf-8"))
        message = str(error_payload.get("message") or "formal SiteScanRuntime child failed")
        if str(error_payload.get("type") or "") == "TimeoutError":
            raise TimeoutError(message)
        raise RuntimeError(message)
    try:
        envelope = result_queue.get(timeout=2.0)
    except Exception as error:
        raise RuntimeError(
            f"formal SiteScanRuntime exited without a result (exitcode={process.exitcode})"
        ) from error
    if envelope.get("kind") == "result":
        result_path = Path(str(envelope.get("path") or ""))
        try:
            value = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("formal SiteScanRuntime result file is invalid") from error
        if isinstance(value, dict):
            return value
        raise RuntimeError("formal SiteScanRuntime returned a non-object result")
    error_path = Path(str(envelope.get("path") or ""))
    try:
        error_payload = json.loads(error_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        error_payload = {}
    error_type = str(error_payload.get("type") or "RuntimeError")
    message = str(error_payload.get("message") or "formal SiteScanRuntime child failed")
    if error_type == "TimeoutError":
        raise TimeoutError(message)
    raise RuntimeError(f"{error_type}: {message}")


def _product_discovery_entry(
    source_url: str,
    site_key: str,
    source_type: str,
    categories: list[dict[str, Any]],
    workspace: str,
    browser_session_dir: str,
    request_budget: int,
    result_path: str,
) -> None:
    """Killable child for one C-layer discovery attempt."""

    path = Path(result_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Reuse the production factory so the formal C path carries the same
        # pagination Agent-recovery callback as employee production.  The
        # wrapper keeps this campaign's explicit request budget without
        # creating another agent engine or a parallel acquisition contract.
        def acquisition_factory(**kwargs):
            return ProductAcquisitionEngine(request_budget=request_budget, **kwargs)

        contract = {
            "job_id": f"formal_c_{site_key}",
            "database_path": str(Path(workspace) / "production.db"),
            "workspace": str(Path(workspace).resolve()),
            "source_url": source_url,
            "site_key": site_key,
            "source_type": source_type,
            "categories": categories,
            "browser_session": {"user_data_dir": str(Path(browser_session_dir).resolve())},
            "provider": "OFF",
            "scope": "NEW_ONLY",
        }
        pipeline = ProductionPipeline(
            contract=contract,
            workspace=Path(workspace),
            emit=lambda *args: None,
            acquisition_factory=acquisition_factory,
        )
        try:
            products = pipeline.acquisition.discover(3)
        finally:
            pipeline.database.dispose()
        path.write_text(json.dumps({
            "kind": "result",
            "products": [asdict(item) for item in products],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except BaseException as error:
        path.write_text(json.dumps({
            "kind": "error",
            "type": type(error).__name__,
            "message": str(error)[:1000],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _discover_products_bounded(
    *,
    source_url: str,
    site_key: str,
    source_type: str,
    categories: list[dict[str, Any]],
    workspace: Path,
    browser_session_dir: Path,
    request_budget: int,
    timeout_seconds: float,
) -> list[Any]:
    """Run C in a killable process so a timeout cannot leak browser work."""

    context = multiprocessing.get_context("spawn")
    result_path = workspace / "discovery_process_result.json"
    process = context.Process(
        target=_product_discovery_entry,
        args=(source_url, site_key, source_type, categories, str(workspace), str(browser_session_dir), request_budget, str(result_path)),
        name=f"product-discovery-{site_key}",
    )
    process.start()
    process.join(max(1.0, float(timeout_seconds)) + 5.0)
    if process.is_alive():
        process.terminate()
        process.join(10.0)
        result_path.write_text(json.dumps({
            "kind": "timeout",
            "type": "TimeoutError",
            "message": f"bounded product discovery exceeded {timeout_seconds:.0f}s",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise TimeoutError(f"bounded product discovery exceeded {timeout_seconds:.0f}s")
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"product discovery child exited without valid evidence (exitcode={process.exitcode})") from error
    if payload.get("kind") == "result":
        return [AcquiredProduct(**item) for item in payload.get("products") or []]
    error_type = str(payload.get("type") or "ProductAcquisitionError")
    message = str(payload.get("message") or "product discovery child failed")
    if error_type == "ProductSupplyExhausted":
        raise ProductSupplyExhausted(message)
    if error_type == "ProductAcquisitionError":
        raise ProductAcquisitionError(message)
    if error_type == "TimeoutError":
        raise TimeoutError(message)
    raise RuntimeError(f"{error_type}: {message}")


def _is_network_block_message(message: str) -> bool:
    """Recognize transport failures without hiding arbitrary code errors."""

    text = str(message or "").casefold()
    return any(token in text for token in (
        "connectionerror",
        "connection aborted",
        "connection reset",
        "read timed out",
        "connect timeout",
        "sslerror",
        "ssleoferror",
        "max retries exceeded",
        "name or service not known",
        "temporary failure in name resolution",
    ))


def _run_one(
    name: str,
    url: str,
    run_id: str,
    output_root: Path,
    *,
    run_bc: bool,
    run_d: bool,
    operation_timeout_seconds: float = 180.0,
    preflight_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = datetime.now(UTC)
    analyzer = NativeSiteAnalyzer(output_root / "analyzer" / name.replace("/", "_"))
    try:
        receipt = preflight_override if isinstance(preflight_override, dict) else _bounded_call(
            lambda: analyzer.preflight(url, live=True, output_dir=output_root / "preflight" / name.replace("/", "_")),
            timeout_seconds=operation_timeout_seconds,
        )
        status = str(receipt.get("status") or "FAILED")
        if status in {"READY"}:
            a_state = "PASS"
        elif status in {"ROBOTS_DENIED", "BROWSER_REQUIRED", "HUMAN_REQUIRED", "ACCESS_CHANGE_REQUIRED"}:
            a_state = "ACCESS_BLOCKED"
        elif status in {"TEMPORARY_FAILURE"}:
            a_state = "ENVIRONMENT_BLOCKED"
        else:
            a_state = "FAILED" if status in {"FAILED", "INVALID_INPUT"} else "ENVIRONMENT_BLOCKED"
        row = {
            "site": name,
            "url": url,
            "run_id": run_id,
            "attempt_id": f"{run_id}:{name}",
            "time_utc": started.isoformat(),
            "environment": {"python": sys.version.split()[0], "hostname": socket.gethostname()},
            "layer_a": a_state,
            "preflight_status": status,
            "reason": str(receipt.get("next_action") or (receipt.get("blocker") or {}).get("message") or ""),
            "evidence": receipt,
            "source_type": "UNKNOWN",
            "scope_applicability": "UNKNOWN",
            "brain_mode": "CODEX_DEVELOPMENT_BRIDGE" if os.getenv("WEBSITE_MODEL_MODE", "").strip().upper() == "CODEX_DEVELOPMENT_BRIDGE" else "LOCAL_AGENT_OR_REMOTE",
            "layer_b": {"status": "NOT_RUN", "reason": "B not requested; run with --layers-bc."},
            "layer_c": {"status": "NOT_RUN", "reason": "C not requested; run with --layers-bc."},
            "layer_d": {"status": "NOT_RUN", "reason": "D not requested; run with --layers-d."},
            "target_count": 3,
            "found_count": 0,
            "dedup_count": 0,
            "image_pass_count": 0,
            "ready_count": 0,
            "manual_intervention_count": 0,
            "developer_rescue": False,
            "evidence_paths": [],
        }
        row["evidence_paths"].append(str(output_root / "preflight" / name.replace("/", "_")))
        receipt_source_type = str(receipt.get("source_type") or "UNKNOWN")
        row["source_type"] = receipt_source_type
        row["scope_applicability"] = "APPLICABLE" if receipt_source_type != "UNKNOWN" or status == "READY" else "UNKNOWN"
        if not run_bc or row["layer_a"] not in {"PASS", "PARTIAL"}:
            return row

        site_slug = name.replace("/", "_")
        b_root = output_root / "taxonomy" / site_slug
        try:
            # _formal_runtime_taxonomy owns its durable watchdog and persists
            # SCAN_TIMEOUT before returning.  Do not wrap it in a second
            # daemon-thread timeout: that would detach a still-running scan
            # from its SQLite session and make the next site race it.
            taxonomy = _formal_runtime_taxonomy(
                name, url, output_root, timeout_seconds=operation_timeout_seconds
            )
            b_status = str(taxonomy.get("status") or "FAILED")
            row["source_type"] = str(taxonomy.get("source_type") or receipt_source_type or "UNKNOWN")
            row["scope_applicability"] = "APPLICABLE" if taxonomy.get("categories") else "UNKNOWN"
            row["layer_b"] = {
                "status": _status_bucket(b_status, layer="B"),
                "raw_status": b_status,
                "category_count": len(taxonomy.get("categories") or []),
                "taxonomy_level": taxonomy.get("taxonomy_level"),
                "brain": taxonomy.get("brain") or {},
                "evidence": taxonomy.get("evidence") or {},
                "formal_runtime": taxonomy.get("formal_runtime") or {},
                "reason": (taxonomy.get("blocker") or {}).get("message") if isinstance(taxonomy.get("blocker"), dict) else "",
            }
            row["evidence_paths"].append(str(b_root))
        except TimeoutError as error:
            row["layer_b"] = {
                "status": "CODE_DEFECT",
                "raw_status": "UNCLASSIFIED_ERROR",
                "error_classification": "TIMEOUT_REQUIRES_DIAGNOSIS",
                "evidence": _exception_evidence(error),
            }
            return row
        except Exception as error:  # keep denominator row and classify the defect
            row["layer_b"] = {
                "status": "CODE_DEFECT",
                "raw_status": "UNCLASSIFIED_ERROR",
                "error_classification": type(error).__name__.upper(),
                "evidence": _exception_evidence(error),
            }
            return row

        if row["layer_b"]["status"] not in {"PASS", "PARTIAL"}:
            row["layer_c"] = {"status": "NOT_RUN", "reason": "B did not produce a usable taxonomy; downstream product discovery was not claimed."}
            row["layer_d"] = {"status": "NOT_RUN", "reason": "B did not produce a usable taxonomy."}
            return row

        categories = [dict(item) for item in taxonomy.get("categories") or [] if isinstance(item, dict) and item.get("source_url")]
        if not categories:
            row["layer_c"] = {"status": "UNSUPPORTED_CAPABILITY", "reason": "B receipt contained no category scope; no product request was fabricated."}
            row["layer_d"] = {"status": "NOT_RUN", "reason": "No verified category scope."}
            return row
        source_type = str(taxonomy.get("source_type") or receipt_source_type or "UNKNOWN")
        c_root = output_root / "products" / site_slug
        c_root.mkdir(parents=True, exist_ok=True)
        try:
            c_site_key = str(taxonomy.get("site_key") or site_slug.casefold())
            c_browser_root = output_root / "browser" / site_slug
            products = _discover_products_bounded(
                source_url=url,
                site_key=c_site_key,
                source_type=source_type,
                categories=categories,
                workspace=c_root,
                browser_session_dir=c_browser_root,
                request_budget=36,
                timeout_seconds=operation_timeout_seconds,
            )
            checkpoint_path = c_root / "acquisition_checkpoint.json"
            try:
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.is_file() else {}
            except (OSError, ValueError, json.JSONDecodeError):
                checkpoint = {}
            row["found_count"] = len(products)
            row["dedup_count"] = len(checkpoint.get("products") or {})
            row["layer_c"] = {
                "status": "PASS" if len(products) >= 3 else "PARTIAL",
                "raw_status": checkpoint.get("discovery_status") or "DISCOVERED",
                "found_count": len(products),
                "dedup_count": len(checkpoint.get("products") or {}),
                "products": [
                    {
                        "identity_key": item.identity_key,
                        "canonical_url": item.canonical_url,
                        "source_name": item.source_name,
                        "image_url": item.image_url,
                        "dimension_lookup_state": item.dimension_lookup_state,
                        "acquisition": item.acquisition,
                    }
                    for item in products
                ],
                "checkpoint": checkpoint,
            }
            row["evidence_paths"].append(str(c_root))
        except (BrowserHumanRequired, BrowserAccessDenied, BrowserTemporaryFailure, BrowserRuntimeMissing) as error:
            row["manual_intervention_count"] = 1 if isinstance(error, BrowserHumanRequired) else 0
            row["layer_c"] = {"status": "ACCESS_BLOCKED" if not isinstance(error, BrowserTemporaryFailure) else "ENVIRONMENT_BLOCKED", "raw_status": type(error).__name__.upper(), "evidence": _exception_evidence(error)}
            row["layer_d"] = {"status": "NOT_RUN", "reason": "Product discovery was blocked before Ready Pool."}
            return row
        except ProductSupplyExhausted as error:
            row["layer_c"] = {"status": "PARTIAL", "raw_status": "SUPPLY_EXHAUSTED", "evidence": _exception_evidence(error), "reason": "No additional verified product was available; this is not a target=3 PASS."}
            row["layer_d"] = {"status": "NOT_RUN", "reason": "No verified product batch."}
            return row
        except ProductAcquisitionError as error:
            row["layer_c"] = {"status": "PARTIAL" if "PAGINATION" in str(error).upper() else "CODE_DEFECT", "raw_status": str(error), "evidence": _exception_evidence(error)}
            row["layer_d"] = {"status": "NOT_RUN", "reason": "Product discovery did not return a stable batch."}
            return row
        except TimeoutError as error:
            row["layer_c"] = {
                "status": "CODE_DEFECT",
                "raw_status": "UNCLASSIFIED_ERROR",
                "error_classification": "TIMEOUT_REQUIRES_DIAGNOSIS",
                "evidence": _exception_evidence(error),
            }
            row["layer_d"] = {"status": "NOT_RUN", "reason": "Product discovery exceeded its bounded process and was terminated."}
            return row
        except Exception as error:
            message = str(error)
            network_block = _is_network_block_message(message)
            row["layer_c"] = {
                "status": "ENVIRONMENT_BLOCKED" if network_block else "CODE_DEFECT",
                "raw_status": "NETWORK_ERROR" if network_block else "UNCLASSIFIED_ERROR",
                "error_classification": "NETWORK_ERROR" if network_block else type(error).__name__.upper(),
                "evidence": _exception_evidence(error),
            }
            row["layer_d"] = {"status": "NOT_RUN", "reason": "Product discovery raised before Ready Pool."}
            return row

        if not run_d:
            return row
        d_root = output_root / "ready_pool" / site_slug
        d_root.mkdir(parents=True, exist_ok=True)
        categories_for_contract = [dict(item, selected=True) for item in categories]
        events: list[dict[str, Any]] = []
        contract = {
            "job_id": f"acceptance_{run_id}_{site_slug}",
            "database_path": str(d_root / "production.db"),
            "workspace": str(d_root),
            "source_url": url,
            "site_key": str(taxonomy.get("site_key") or site_slug.casefold()),
            "source_type": source_type,
            "categories": categories_for_contract,
            "category_ids": [str(item.get("category_id") or "") for item in categories_for_contract],
            "target_mode": "EXACT_N",
            "target_value": 3,
            "category_allocation": "TOTAL_ACROSS_SELECTED",
            "allocation_strategy": "SEQUENTIAL",
            "provider": "OFF",
            "scope": "NEW_ONLY",
            "browser_session": {"user_data_dir": str(output_root / "browser" / site_slug)},
            "site_profile": taxonomy.get("site_profile"),
        }
        try:
            pipeline = ProductionPipeline(contract=contract, workspace=d_root, emit=lambda *args: events.append({"event": args[0], "stage": args[1], "detail": args[2], "meta": args[5] if len(args) > 5 else {}}))
            exit_code = _bounded_call(pipeline.run, timeout_seconds=operation_timeout_seconds)
            pool_payload: dict[str, Any] = {}
            pool_path = d_root / "candidate_pool.json"
            if pool_path.is_file():
                try:
                    pool_payload = json.loads(pool_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pool_payload = {}
            records = pool_payload.get("records") if isinstance(pool_payload, dict) else []
            records = records if isinstance(records, list) else []
            ready_count = sum(1 for item in records if str(item.get("state") or "") in {"CATALOG_READY", "MODEL_INPUT_LOCKED", "COMPLETED"})
            row["ready_count"] = ready_count
            row["image_pass_count"] = sum(1 for item in records if bool((item.get("lineage") or {}).get("image_decodable")))
            # D is a fixed three-item Ready Pool gate.  A site that exposes
            # only one or two candidates is a truthful PARTIAL/shortage, not
            # a smaller target silently re-labelled PASS.
            row["layer_d"] = {
                "status": "PASS" if ready_count >= 3 else "PARTIAL" if records else "NOT_RUN",
                "exit_code": exit_code,
                "provider": "OFF",
                "target_count": 3,
                "ready_count": ready_count,
                "records": records,
                "events": events[-40:],
            }
            row["evidence_paths"].append(str(d_root))
        except TimeoutError as error:
            # A watchdog timeout is not proof of an external block.  Preserve
            # the complete denominator and leave it as an unclassified defect
            # until the bounded worker/bridge path is diagnosed.
            row["layer_d"] = {"status": "CODE_DEFECT", "raw_status": "UNCLASSIFIED_ERROR", "error_classification": "TIMEOUT_REQUIRES_DIAGNOSIS", "evidence": _exception_evidence(error), "events": events[-40:]}
        except Exception as error:
            row["layer_d"] = {"status": "CODE_DEFECT", "raw_status": type(error).__name__.upper(), "evidence": _exception_evidence(error), "events": events[-40:]}
        return row
    except Exception as error:  # evidence runner must retain every denominator row
        return {
            "site": name, "url": url, "run_id": run_id,
            "attempt_id": f"{run_id}:{name}", "time_utc": started.isoformat(),
            "environment": {"python": sys.version.split()[0], "hostname": socket.gethostname()},
            "layer_a": "ENVIRONMENT_BLOCKED", "preflight_status": type(error).__name__.upper(),
            "reason": str(error)[:500], "evidence": {"error": type(error).__name__, "message": str(error)},
            "source_type": "UNKNOWN",
            "scope_applicability": "UNKNOWN",
            "brain_mode": "CODEX_DEVELOPMENT_BRIDGE" if os.getenv("WEBSITE_MODEL_MODE", "").strip().upper() == "CODEX_DEVELOPMENT_BRIDGE" else "LOCAL_AGENT_OR_REMOTE",
            "layer_b": {"status": "NOT_RUN", "reason": "Layer A raised before taxonomy."},
            "layer_c": {"status": "NOT_RUN", "reason": "Layer A raised before products."},
            "layer_d": {"status": "NOT_RUN", "reason": "Layer A raised before Ready Pool."},
            "target_count": 3,
            "found_count": 0,
            "dedup_count": 0,
            "image_pass_count": 0,
            "ready_count": 0,
            "manual_intervention_count": 0,
            "developer_rescue": False,
            "evidence_paths": [str(output_root / "preflight" / name.replace("/", "_"))],
        }


def _write(rows: list[dict[str, Any]], output_root: Path, run_id: str) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": "website-45-site-acceptance.v1", "run_id": run_id, "site_count": len(rows), "rows": rows}
    (output_root / f"acceptance_{run_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = [
        "site", "url", "run_id", "attempt_id", "time_utc", "layer_a", "preflight_status", "source_type",
        "scope_applicability", "brain_mode", "target_count", "found_count", "dedup_count", "image_pass_count",
        "ready_count", "manual_intervention_count", "developer_rescue", "reason",
    ]
    with (output_root / f"acceptance_{run_id}.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fields} for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default=os.getenv("WEBSITE_ACCEPTANCE_ROOT", "E:/living/website_acceptance_20260905"))
    parser.add_argument("--layers-bc", action="store_true", help="run real bounded taxonomy (B) and product discovery (C) after A")
    parser.add_argument("--layers-d", action="store_true", help="run Provider-OFF Ready Pool (D) for sites that reach C")
    parser.add_argument("--operation-timeout-seconds", type=float, default=180.0, help="per-site bounded timeout for B/C/D operations")
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("WEBSITE_ACCEPTANCE_WORKERS", "1")),
        help="bounded site workers (default 1 because the real browser session is process-global)",
    )
    parser.add_argument("--reuse-a", help="reuse a prior acceptance JSON's real Layer A evidence instead of refetching A")
    parser.add_argument("--sites", help="comma-separated site names to run; default is all 45")
    args = parser.parse_args()
    run_id = f"acceptance_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    root = Path(args.output_root).resolve()
    selected_names = {value.strip() for value in str(args.sites or "").split(",") if value.strip()}
    selected_sites = [(name, url) for name, url in SITES if not selected_names or name in selected_names]
    prior_a: dict[str, dict[str, Any]] = {}
    if args.reuse_a:
        source = json.loads(Path(args.reuse_a).read_text(encoding="utf-8"))
        for item in source.get("rows") or []:
            if isinstance(item, dict) and item.get("site"):
                prior_a[str(item["site"])] = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, int(args.workers)), thread_name_prefix="acceptance") as executor:
        futures = [executor.submit(
            _run_one,
            name,
            url,
            run_id,
            root,
            run_bc=args.layers_bc,
            run_d=args.layers_d,
            operation_timeout_seconds=args.operation_timeout_seconds,
            preflight_override=prior_a.get(name),
        ) for name, url in selected_sites]
        for future in as_completed(futures):
            rows.append(future.result())
    rows.sort(key=lambda row: next(index for index, (name, _) in enumerate(SITES) if name == row["site"]))
    _write(rows, root, run_id)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["layer_a"]] = counts.get(row["layer_a"], 0) + 1
    print(json.dumps({"run_id": run_id, "count": len(rows), "layer_a": counts, "output_root": str(root)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
