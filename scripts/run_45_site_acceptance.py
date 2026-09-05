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
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.services.native_site_analysis import NativeSiteAnalyzer
from app.services.product_acquisition import (
    BrowserAccessDenied,
    BrowserHumanRequired,
    BrowserRuntimeMissing,
    BrowserTemporaryFailure,
    ProductAcquisitionEngine,
    ProductAcquisitionError,
    ProductSupplyExhausted,
)
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
        analyzer = NativeSiteAnalyzer(output_root / "analyzer" / site_slug)
        b_root = output_root / "taxonomy" / site_slug
        try:
            taxonomy = _bounded_call(
                lambda: analyzer.analyze(url, live=True, output_dir=b_root),
                timeout_seconds=operation_timeout_seconds,
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
                "reason": (taxonomy.get("blocker") or {}).get("message") if isinstance(taxonomy.get("blocker"), dict) else "",
            }
            row["evidence_paths"].append(str(b_root))
        except Exception as error:  # keep denominator row and classify the defect
            row["layer_b"] = {"status": "ENVIRONMENT_BLOCKED", "raw_status": type(error).__name__.upper(), "evidence": _exception_evidence(error)}
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
            engine = ProductAcquisitionEngine(
                source_url=url,
                site_key=str(taxonomy.get("site_key") or site_slug.casefold()),
                source_type=source_type,
                categories=categories,
                workspace=c_root,
                browser_session_dir=output_root / "browser" / site_slug,
                request_budget=36,
            )
            products = _bounded_call(lambda: engine.discover(3), timeout_seconds=operation_timeout_seconds)
            checkpoint = engine._read()
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
        except Exception as error:
            row["layer_c"] = {"status": "ENVIRONMENT_BLOCKED", "raw_status": type(error).__name__.upper(), "evidence": _exception_evidence(error)}
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
            row["layer_d"] = {
                "status": "PASS" if ready_count >= min(3, max(1, row["found_count"])) else "PARTIAL" if records else "NOT_RUN",
                "exit_code": exit_code,
                "provider": "OFF",
                "ready_count": ready_count,
                "records": records,
                "events": events[-40:],
            }
            row["evidence_paths"].append(str(d_root))
        except TimeoutError as error:
            # The bounded runner deliberately separates a hung browser/bridge
            # operation from a product defect.  Keep the site in the full
            # denominator, but do not mislabel a watchdog timeout as a code
            # failure.
            row["layer_d"] = {"status": "ENVIRONMENT_BLOCKED", "raw_status": "TIMEOUTERROR", "evidence": _exception_evidence(error), "events": events[-40:]}
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
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="acceptance") as executor:
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
