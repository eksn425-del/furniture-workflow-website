"""Safe, operator-facing runtime capability diagnostics.

The control plane reports whether a capability is usable without exposing
credentials, private endpoints, local filesystem paths, browser profiles, or
process commands.  The checks are intentionally bounded and side-effect free:
they inspect installed runtimes and the durable database, but never launch a
browser, call a provider, or submit a model request.
"""

from __future__ import annotations

import os
import shutil
import importlib.util
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from app.models import ProductionRun, SiteScanRun


def _truthy(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _binary_available(value: str, candidates: tuple[Path, ...] = ()) -> bool:
    name = value.strip()
    if not name:
        return False
    if shutil.which(name):
        return True
    try:
        if Path(name).is_file():
            return True
    except OSError:
        return False
    return any(candidate.is_file() for candidate in candidates)


def _browser_candidates(engine: str) -> tuple[Path, ...]:
    local_app_data = os.getenv("LOCALAPPDATA", "")
    program_files = os.getenv("PROGRAMFILES", "")
    program_files_x86 = os.getenv("PROGRAMFILES(X86)", "")
    roots = tuple(Path(value) for value in (local_app_data, program_files, program_files_x86) if value)
    if engine == "msedge":
        return tuple(root / "Microsoft" / "Edge" / "Application" / "msedge.exe" for root in roots)
    if engine == "chrome":
        return tuple(root / "Google" / "Chrome" / "Application" / "chrome.exe" for root in roots)
    return ()


def browser_diagnostic() -> dict[str, object]:
    raw_engine = os.getenv("WEBSITE_L2_BROWSER_ENGINE", "").strip().casefold()
    engine = "chromium" if raw_engine in {"", "chromium"} else "msedge" if raw_engine in {"edge", "msedge"} else "chrome" if raw_engine in {"chrome", "google-chrome"} else raw_engine
    if engine == "chromium":
        ready = _playwright_chromium_available()
        return {
            "status": "READY" if ready else "NOT_INSTALLED",
            "engine": engine,
            "mode": "ISOLATED_PERSISTENT",
            "reason_code": "READY" if ready else "PLAYWRIGHT_CHROMIUM_MISSING",
        }
    if engine in {"msedge", "chrome"}:
        executable = "msedge.exe" if engine == "msedge" else "chrome.exe"
        ready = _binary_available(executable, _browser_candidates(engine))
        return {
            "status": "READY" if ready else "NOT_INSTALLED",
            "engine": engine,
            "mode": "ISOLATED_PERSISTENT",
            "reason_code": "READY" if ready else "SYSTEM_BROWSER_MISSING",
        }
    return {
        "status": "NOT_INSTALLED",
        "engine": engine or "unknown",
        "mode": "ISOLATED_PERSISTENT",
        "reason_code": "UNSUPPORTED_BROWSER_ENGINE",
    }


def _playwright_chromium_available() -> bool:
    """Inspect the Playwright browser cache without starting a driver process."""

    if importlib.util.find_spec("playwright") is None:
        return False
    roots: list[Path] = []
    configured_root = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if configured_root and configured_root != "0":
        roots.append(Path(configured_root))
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        roots.append(Path(local_app_data) / "ms-playwright")
    roots.append(Path.home() / ".cache" / "ms-playwright")
    executable_names = {"chrome.exe", "chrome", "chromium", "Chromium"}
    for root in dict.fromkeys(roots):
        if not root.is_dir():
            continue
        try:
            for candidate in root.glob("chromium-*/*/*"):
                if candidate.is_file() and candidate.name in executable_names:
                    return True
            for candidate in root.glob("chromium-*/*/*/*"):
                if candidate.is_file() and candidate.name in executable_names:
                    return True
        except OSError:
            continue
    return False


def _provider_diagnostic() -> dict[str, object]:
    provider_mode = os.getenv("PROVIDER_MODE", "disabled").strip().casefold()
    key_present = bool(os.getenv("LUX3D_API_KEY", "").strip())
    endpoint_present = bool(os.getenv("LUX3D_BASE_URL", "").strip())
    if provider_mode in {"", "off", "disabled", "disabled_by_default"} and not key_present and not endpoint_present:
        return {"status": "OFF", "reason_code": "PROVIDER_MODE_DISABLED"}
    if key_present and endpoint_present:
        return {"status": "READY", "reason_code": "CREDENTIALS_PRESENT"}
    return {"status": "NOT_CONFIGURED", "reason_code": "LUX3D_CONFIGURATION_INCOMPLETE"}


def _blender_diagnostic() -> dict[str, object]:
    if not _truthy(os.getenv("BLENDER_WORKER_ENABLED")):
        return {"status": "NOT_CONFIGURED", "reason_code": "BLENDER_WORKER_DISABLED"}
    configured = os.getenv("BLENDER_EXECUTABLE", "blender").strip() or "blender"
    return {
        "status": "READY" if _binary_available(configured) else "NOT_CONFIGURED",
        "reason_code": "READY" if _binary_available(configured) else "BLENDER_EXECUTABLE_MISSING",
    }


def _database_diagnostic(database: Any) -> dict[str, object]:
    session = database.session_factory()
    try:
        session.execute(text("SELECT 1"))
        return {"status": "READY", "engine": "sqlite-dev-or-configured"}
    except Exception:
        return {"status": "ERROR", "engine": "sqlite-dev-or-configured"}
    finally:
        session.close()


def _queue_diagnostic(database: Any) -> tuple[dict[str, object], dict[str, object]]:
    session = database.session_factory()
    try:
        active_runs = int(session.scalar(select(ProductionRun.run_id).where(ProductionRun.status == "RUNNING").limit(1)) is not None)
        queued_runs = int(session.scalar(select(ProductionRun.run_id).where(ProductionRun.status == "QUEUED").limit(1)) is not None)
        active_scans = int(session.scalar(select(SiteScanRun.scan_id).where(SiteScanRun.status.in_({"QUEUED", "ANALYZING", "L2_BROWSER"})).limit(1)) is not None)
    except Exception:
        active_runs = queued_runs = active_scans = 0
    finally:
        session.close()
    production = {
        "status": "BUSY" if active_runs or queued_runs else "READY",
        "reason_code": "ACTIVE_OR_QUEUED_RUN" if active_runs or queued_runs else "NO_ACTIVE_RUN",
    }
    scans = {
        "status": "BUSY" if active_scans else "READY",
        "reason_code": "ACTIVE_SCAN" if active_scans else "NO_ACTIVE_SCAN",
    }
    return production, scans


def collect_runtime_diagnostics(database: Any, brain: Any) -> dict[str, object]:
    """Return the non-secret capability matrix consumed by `/control/system`."""

    brain_health = dict(brain.health())
    effective_mode = str(brain_health.get("model_mode") or "TEXT_BRAIN_PLUS_VISION")
    production_worker, site_scan_worker = _queue_diagnostic(database)
    return {
        "api": {"status": "READY", "reason_code": "CONTROL_PLANE_RESPONDED"},
        "database": _database_diagnostic(database),
        "l2_browser": browser_diagnostic(),
        "brain": {
            "status": "LOCAL_AGENT" if effective_mode == "LOCAL_AGENT" else "READY" if brain_health.get("configured") else "NOT_CONFIGURED",
            "effective_mode": effective_mode,
            "configured": bool(brain_health.get("configured")),
            "review_provider": brain_health.get("review_provider"),
        },
        "brain_override": {
            "status": "ON" if brain_health.get("local_agent_override") else "OFF",
            "reason": brain_health.get("override_reason") or "无覆盖",
        },
        "lux3d": _provider_diagnostic(),
        "blender": _blender_diagnostic(),
        "production_worker": production_worker,
        "site_scan_worker": site_scan_worker,
    }


__all__ = ["browser_diagnostic", "collect_runtime_diagnostics"]
