"""Small, explicit Blender normalization and GLB QA boundary.

The Website must not call a paid model provider and then silently ship the
provider's raw file.  This module keeps the post-processing boundary explicit:
local E2E uses a deterministic fake adapter, while a configured Blender CLI is
available for a real deployment.  No adapter attempts to repair a malformed
GLB by guessing; it reports a hard QA failure instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class BlenderAdapterError(RuntimeError):
    """Base error for normalization/QA failures."""


class BlenderNotConfigured(BlenderAdapterError):
    """No approved local Blender adapter is available."""


@dataclass(frozen=True, slots=True)
class BlenderQAResult:
    status: str
    adapter: str
    normalized_path: str
    sha256: str
    size_bytes: int
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "adapter": self.adapter,
            "normalized_path": self.normalized_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "reason": self.reason,
        }


def validate_glb(path: Path) -> tuple[bool, str]:
    """Validate the GLB header, chunk boundaries, and JSON asset chunk."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        return False, f"read_failed:{type(error).__name__}"
    if len(raw) < 20:
        return False, "too_small"
    if raw[:4] != b"glTF":
        return False, "magic_missing"
    if int.from_bytes(raw[4:8], "little") != 2:
        return False, "unsupported_version"
    declared = int.from_bytes(raw[8:12], "little")
    if declared != len(raw):
        return False, "declared_length_mismatch"
    offset = 12
    saw_json = False
    while offset < len(raw):
        if len(raw) - offset < 8:
            return False, "truncated_chunk_header"
        chunk_length = int.from_bytes(raw[offset : offset + 4], "little")
        chunk_type = raw[offset + 4 : offset + 8]
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_length
        if chunk_end > len(raw) or chunk_length % 4:
            return False, "invalid_chunk_length"
        chunk = raw[chunk_start:chunk_end]
        if chunk_type == b"JSON":
            saw_json = True
            try:
                json.loads(chunk.rstrip(b" \t\r\n\x00").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                return False, "invalid_json_chunk"
        elif chunk_type != b"BIN\x00":
            return False, "unsupported_chunk_type"
        offset = chunk_end
    if offset != len(raw):
        return False, "chunk_boundary_mismatch"
    if not saw_json:
        return False, "json_chunk_missing"
    return True, "container_valid"


class FakeBlenderAdapter:
    """Deterministic local adapter used only by an explicitly marked E2E run."""

    name = "FAKE_LOCAL_BLENDER"

    def normalize_and_qa(self, raw_path: Path, output_path: Path) -> BlenderQAResult:
        valid, reason = validate_glb(raw_path)
        if not valid:
            raise BlenderAdapterError(f"raw_glb_qa_failed:{reason}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(raw_path, output_path)
        normalized_valid, normalized_reason = validate_glb(output_path)
        if not normalized_valid:
            raise BlenderAdapterError(f"normalized_glb_qa_failed:{normalized_reason}")
        raw = output_path.read_bytes()
        return BlenderQAResult(
            status="PASS",
            adapter=self.name,
            normalized_path=str(output_path),
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            reason="deterministic local normalization pass-through",
        )


class BlenderCLIAdapter:
    """Run Blender headlessly to import/export one GLB and then validate it."""

    name = "BLENDER_CLI"

    def __init__(self, executable: str, *, timeout_seconds: int = 300) -> None:
        self.executable = executable
        self.timeout_seconds = max(30, min(int(timeout_seconds), 1800))

    def normalize_and_qa(self, raw_path: Path, output_path: Path) -> BlenderQAResult:
        if not raw_path.is_file():
            raise BlenderAdapterError("raw_glb_missing")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # The script receives paths after ``--``; keeping it inline avoids
        # writing a user-controlled script into the production workspace.
        script = (
            "import bpy,sys;"
            "src=sys.argv[-2];dst=sys.argv[-1];"
            "bpy.ops.wm.read_factory_settings(use_empty=True);"
            "bpy.ops.import_scene.gltf(filepath=src);"
            "bpy.ops.export_scene.gltf(filepath=dst,export_format='GLB')"
        )
        try:
            result = subprocess.run(
                [self.executable, "--background", "--python-expr", script, "--", str(raw_path), str(output_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BlenderAdapterError(f"blender_cli_failed:{type(error).__name__}") from error
        if result.returncode != 0:
            raise BlenderAdapterError(f"blender_cli_failed:exit_{result.returncode}")
        valid, reason = validate_glb(output_path)
        if not valid:
            raise BlenderAdapterError(f"normalized_glb_qa_failed:{reason}")
        raw = output_path.read_bytes()
        return BlenderQAResult(
            status="PASS",
            adapter=self.name,
            normalized_path=str(output_path),
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            reason="Blender CLI import/export and container QA passed",
        )


def _truthy(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def resolve_blender_adapter(contract: dict[str, object]) -> FakeBlenderAdapter | BlenderCLIAdapter | None:
    """Resolve only an explicit test adapter or an explicitly configured CLI."""

    profile = str(contract.get("test_profile") or "").strip().upper()
    if profile == "LOCAL_E2E" and _truthy(os.getenv("FURNITURE_WORKFLOW_LOCAL_E2E")):
        return FakeBlenderAdapter()
    if _truthy(os.getenv("FURNITURE_WORKFLOW_TEST_FIXTURES")):
        return FakeBlenderAdapter()
    if not _truthy(os.getenv("BLENDER_WORKER_ENABLED")):
        return None
    executable = str(os.getenv("BLENDER_EXECUTABLE") or "blender").strip()
    resolved = shutil.which(executable) or (executable if Path(executable).is_file() else "")
    return BlenderCLIAdapter(resolved) if resolved else None


__all__ = [
    "BlenderAdapterError",
    "BlenderCLIAdapter",
    "BlenderNotConfigured",
    "BlenderQAResult",
    "FakeBlenderAdapter",
    "resolve_blender_adapter",
    "validate_glb",
]
