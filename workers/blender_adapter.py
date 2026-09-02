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
import math
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class BlenderAdapterError(RuntimeError):
    """Base error for normalization/QA failures."""


class BlenderNotConfigured(BlenderAdapterError):
    """No approved local Blender adapter is available."""


class ModelDimensionConflict(BlenderAdapterError):
    """Target dimensions would require an unsafe non-uniform deformation."""


@dataclass(frozen=True, slots=True)
class BlenderQAResult:
    status: str
    adapter: str
    normalized_path: str
    sha256: str
    size_bytes: int
    reason: str = ""
    raw_bbox: dict[str, object] | None = None
    target_dimensions: dict[str, float] | None = None
    target_dimensions_model: dict[str, float] | None = None
    dimension_unit: str = "source_unit"
    scale_factor: float | None = None
    final_bbox: dict[str, object] | None = None
    dimension_error: dict[str, float] | None = None
    dimension_status: str = "NOT_MEASURED"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "adapter": self.adapter,
            "normalized_path": self.normalized_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "reason": self.reason,
            "raw_bbox": self.raw_bbox,
            "target_dimensions": self.target_dimensions,
            "target_dimensions_model": self.target_dimensions_model,
            "dimension_unit": self.dimension_unit,
            "scale_factor": self.scale_factor,
            "final_bbox": self.final_bbox,
            "dimension_error": self.dimension_error,
            "dimension_status": self.dimension_status,
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


_COMPONENT_FORMATS: dict[int, tuple[str, int]] = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
_TYPE_COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_UNIT_TO_MODEL = {
    "m": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "cm": 0.01,
    "centimeter": 0.01,
    "centimeters": 0.01,
    "mm": 0.001,
    "millimeter": 0.001,
    "millimeters": 0.001,
    "in": 0.0254,
    "inch": 0.0254,
    "inches": 0.0254,
    "ft": 0.3048,
    "foot": 0.3048,
    "feet": 0.3048,
}


def _read_glb_json_and_bin(path: Path) -> tuple[dict[str, object], bytes]:
    raw = path.read_bytes()
    if len(raw) < 20 or raw[:4] != b"glTF" or int.from_bytes(raw[4:8], "little") != 2:
        raise BlenderAdapterError("glb_geometry_read_failed:invalid_header")
    declared = int.from_bytes(raw[8:12], "little")
    if declared != len(raw):
        raise BlenderAdapterError("glb_geometry_read_failed:length_mismatch")
    offset = 12
    document: dict[str, object] | None = None
    binary = b""
    while offset < len(raw):
        length = int.from_bytes(raw[offset : offset + 4], "little")
        chunk_type = raw[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        chunk = raw[start:end]
        if chunk_type == b"JSON":
            parsed = json.loads(chunk.rstrip(b" \t\r\n\x00").decode("utf-8"))
            if isinstance(parsed, dict):
                document = parsed
        elif chunk_type == b"BIN\x00":
            binary = chunk
        offset = end
    if document is None:
        raise BlenderAdapterError("glb_geometry_read_failed:json_missing")
    return document, binary


def _bbox_from_points(points: list[tuple[float, float, float]]) -> dict[str, object] | None:
    if not points:
        return None
    minimum = tuple(min(point[index] for point in points) for index in range(3))
    maximum = tuple(max(point[index] for point in points) for index in range(3))
    return {
        "min": {"x": minimum[0], "y": minimum[1], "z": minimum[2]},
        "max": {"x": maximum[0], "y": maximum[1], "z": maximum[2]},
        # glTF uses X/Z/Y for width/depth/height in the Website contract.
        "size": {
            "width": maximum[0] - minimum[0],
            "depth": maximum[2] - minimum[2],
            "height": maximum[1] - minimum[1],
        },
    }


def extract_glb_bbox(path: Path) -> dict[str, object] | None:
    """Read a conservative mesh bounding box from glTF POSITION accessors.

    Accessor min/max is preferred because it is cheap and lossless.  When a
    producer omitted those fields, the bounded accessor bytes are decoded for
    POSITION only.  A GLB without mesh positions returns ``None`` instead of
    inventing geometry.
    """

    document, binary = _read_glb_json_and_bin(path)
    accessors = document.get("accessors") if isinstance(document.get("accessors"), list) else []
    buffer_views = document.get("bufferViews") if isinstance(document.get("bufferViews"), list) else []
    meshes = document.get("meshes") if isinstance(document.get("meshes"), list) else []
    points: list[tuple[float, float, float]] = []
    for mesh in meshes:
        if not isinstance(mesh, dict):
            continue
        primitives = mesh.get("primitives") if isinstance(mesh.get("primitives"), list) else []
        for primitive in primitives:
            if not isinstance(primitive, dict):
                continue
            attributes = primitive.get("attributes") if isinstance(primitive.get("attributes"), dict) else {}
            accessor_index = attributes.get("POSITION")
            if not isinstance(accessor_index, int) or not (0 <= accessor_index < len(accessors)):
                continue
            accessor = accessors[accessor_index]
            if not isinstance(accessor, dict):
                continue
            minimum = accessor.get("min")
            maximum = accessor.get("max")
            if isinstance(minimum, list) and isinstance(maximum, list) and len(minimum) >= 3 and len(maximum) >= 3:
                points.extend([
                    (float(minimum[0]), float(minimum[1]), float(minimum[2])),
                    (float(maximum[0]), float(maximum[1]), float(maximum[2])),
                ])
                continue
            component = _COMPONENT_FORMATS.get(int(accessor.get("componentType") or 0))
            component_count = _TYPE_COMPONENTS.get(str(accessor.get("type") or ""))
            view_index = accessor.get("bufferView")
            if component is None or component_count != 3 or not isinstance(view_index, int) or not (0 <= view_index < len(buffer_views)):
                continue
            view = buffer_views[view_index]
            if not isinstance(view, dict):
                continue
            fmt, component_size = component
            count = int(accessor.get("count") or 0)
            element_size = component_size * component_count
            stride = int(view.get("byteStride") or element_size)
            start = int(view.get("byteOffset") or 0) + int(accessor.get("byteOffset") or 0)
            for item_index in range(max(0, count)):
                item_start = start + item_index * stride
                item_end = item_start + element_size
                if item_end > len(binary):
                    break
                values = struct.unpack_from("<" + fmt * component_count, binary, item_start)
                points.append((float(values[0]), float(values[1]), float(values[2])))
    return _bbox_from_points(points)


def _dimension_values(value: Mapping[str, object] | None) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        result = {axis: float(value[axis]) for axis in ("width", "depth", "height")}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(number) and number > 0 for number in result.values()):
        return None
    return result


def plan_dimension_normalization(
    raw_bbox: dict[str, object] | None,
    target_dimensions: Mapping[str, object] | None,
    dimension_unit: str = "source_unit",
) -> dict[str, object]:
    """Plan safe uniform normalization and reject obvious aspect-ratio conflicts."""

    target = _dimension_values(target_dimensions)
    raw_size = _dimension_values(raw_bbox.get("size") if isinstance(raw_bbox, dict) else None)
    if raw_size is None:
        return {
            "target_dimensions": target,
            "target_dimensions_model": target,
            "scale_factor": None,
            "dimension_status": "NOT_MEASURED_NO_MESH",
        }
    # 单轴锚定模式：官网只提供一条有效尺寸，或仅有 AI 预估高度时，
    # 等比缩放到该轴，长:宽:高 保持模型自身比例不变（三轴同系数）。
    if target is None and isinstance(target_dimensions, Mapping):
        anchors: list[tuple[str, float]] = []
        for axis in ("width", "depth", "height"):
            try:
                value = float(target_dimensions.get(axis))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0 and raw_size.get(axis):
                anchors.append((axis, value))
        if anchors:
            factor_unit = _UNIT_TO_MODEL.get(str(dimension_unit or "").strip().casefold(), 1.0)
            target_partial = {axis: value for axis, value in anchors}
            target_model_partial = {axis: value * factor_unit for axis, value in anchors}
            ratios = [target_model_partial[axis] / raw_size[axis] for axis, _ in anchors]
            spread = max(ratios) / min(ratios)
            if spread > 1.25:
                raise ModelDimensionConflict(
                    "partial target dimensions require non-uniform deformation "
                    f"(ratio spread {spread:.3f} > 1.25)"
                )
            factor = sum(ratios) / len(ratios)
            final_size = {axis: raw_size[axis] * factor for axis in raw_size}
            errors = {
                axis: abs(final_size[axis] - target_model_partial[axis]) / target_model_partial[axis]
                for axis, _ in anchors
            }
            return {
                "target_dimensions": target_partial,
                "target_dimensions_model": target_model_partial,
                "scale_factor": factor,
                "dimension_status": "PASS" if max(errors.values()) <= 0.15 else "MODEL_DIMENSION_CONFLICT",
                "planned_final_dimensions": final_size,
                "dimension_error": errors,
                "partial_axes_anchored": [axis for axis, _ in anchors],
                "single_axis_anchored": anchors[0][0] if len(anchors) == 1 else None,
                "height_anchored": len(anchors) == 1 and anchors[0][0] == "height",
            }
    if target is None:
        return {
            "target_dimensions": None,
            "target_dimensions_model": None,
            "scale_factor": 1.0,
            "dimension_status": "MEASURED_NO_TARGET",
        }
    factor_unit = _UNIT_TO_MODEL.get(str(dimension_unit or "").strip().casefold(), 1.0)
    target_model = {axis: value * factor_unit for axis, value in target.items()}
    ratios = [target_model[axis] / raw_size[axis] for axis in ("width", "depth", "height")]
    spread = max(ratios) / min(ratios)
    if spread > 1.25:
        raise ModelDimensionConflict(
            "target dimensions require non-uniform deformation "
            f"(ratio spread {spread:.3f} > 1.25)"
        )
    factor = sum(ratios) / len(ratios)
    final_size = {axis: raw_size[axis] * factor for axis in raw_size}
    errors = {
        axis: abs(final_size[axis] - target_model[axis]) / target_model[axis]
        for axis in ("width", "depth", "height")
    }
    return {
        "target_dimensions": target,
        "target_dimensions_model": target_model,
        "scale_factor": factor,
        "dimension_status": "PASS" if max(errors.values()) <= 0.15 else "MODEL_DIMENSION_CONFLICT",
        "planned_final_dimensions": final_size,
        "dimension_error": errors,
    }


class FakeBlenderAdapter:
    """Deterministic local adapter used only by an explicitly marked E2E run."""

    name = "FAKE_LOCAL_BLENDER"

    def normalize_and_qa(
        self,
        raw_path: Path,
        output_path: Path,
        *,
        target_dimensions: Mapping[str, object] | None = None,
        dimension_unit: str = "source_unit",
    ) -> BlenderQAResult:
        valid, reason = validate_glb(raw_path)
        if not valid:
            raise BlenderAdapterError(f"raw_glb_qa_failed:{reason}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(raw_path, output_path)
        normalized_valid, normalized_reason = validate_glb(output_path)
        if not normalized_valid:
            raise BlenderAdapterError(f"normalized_glb_qa_failed:{normalized_reason}")
        raw = output_path.read_bytes()
        raw_bbox = extract_glb_bbox(output_path)
        plan = plan_dimension_normalization(raw_bbox, target_dimensions, dimension_unit)
        return BlenderQAResult(
            status="PASS",
            adapter=self.name,
            normalized_path=str(output_path),
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            reason="deterministic local normalization pass-through; geometry scaling is not claimed",
            raw_bbox=raw_bbox,
            target_dimensions=plan.get("target_dimensions"),
            target_dimensions_model=plan.get("target_dimensions_model"),
            dimension_unit=dimension_unit,
            scale_factor=plan.get("scale_factor"),
            final_bbox=raw_bbox,
            dimension_error=plan.get("dimension_error"),
            dimension_status=("NOT_MEASURED_NO_MESH" if raw_bbox is None else "FAKE_NOT_APPLIED"),
        )


class BlenderCLIAdapter:
    """Run Blender headlessly to import/export one GLB and then validate it."""

    name = "BLENDER_CLI"

    def __init__(self, executable: str, *, timeout_seconds: int = 300) -> None:
        self.executable = executable
        self.timeout_seconds = max(30, min(int(timeout_seconds), 1800))

    def normalize_and_qa(
        self,
        raw_path: Path,
        output_path: Path,
        *,
        target_dimensions: Mapping[str, object] | None = None,
        dimension_unit: str = "source_unit",
    ) -> BlenderQAResult:
        if not raw_path.is_file():
            raise BlenderAdapterError("raw_glb_missing")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_bbox = extract_glb_bbox(raw_path)
        plan = plan_dimension_normalization(raw_bbox, target_dimensions, dimension_unit)
        if plan.get("dimension_status") == "MODEL_DIMENSION_CONFLICT":
            raise ModelDimensionConflict("target dimensions exceed uniform normalization tolerance")
        # The script receives paths after ``--``; keeping it inline avoids
        # writing a user-controlled script into the production workspace.
        script = (
            "import bpy,sys,json;"
            "src=sys.argv[-3];dst=sys.argv[-2];scale=float(sys.argv[-1]);"
            "bpy.ops.wm.read_factory_settings(use_empty=True);"
            "bpy.ops.import_scene.gltf(filepath=src);"
            "[setattr(o,'scale',tuple(float(v)*scale for v in o.scale)) for o in bpy.context.scene.objects if o.type=='MESH'];"
            "bpy.ops.object.select_all(action='SELECT');"
            "[setattr(bpy.context.view_layer.objects,'active',o) for o in bpy.context.scene.objects if o.type=='MESH'];"
            "bpy.ops.object.transform_apply(location=False,rotation=False,scale=True);"
            "bpy.ops.export_scene.gltf(filepath=dst,export_format='GLB')"
        )
        try:
            result = subprocess.run(
                [self.executable, "--background", "--python-expr", script, "--", str(raw_path), str(output_path), str(plan.get("scale_factor") or 1.0)],
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
        final_bbox = extract_glb_bbox(output_path)
        dimension_error = plan.get("dimension_error")
        final_size = final_bbox.get("size") if isinstance(final_bbox, dict) else None
        target_model = plan.get("target_dimensions_model")
        if isinstance(final_size, dict) and isinstance(target_model, dict):
            dimension_error = {
                axis: abs(float(final_size[axis]) - float(target_model[axis])) / float(target_model[axis])
                for axis in ("width", "depth", "height")
                if axis in final_size and axis in target_model
            }
        if dimension_error and max(dimension_error.values()) > 0.15:
            raise ModelDimensionConflict("final dimensions exceed 15% tolerance after uniform normalization")
        return BlenderQAResult(
            status="PASS",
            adapter=self.name,
            normalized_path=str(output_path),
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            reason="Blender CLI import/export, uniform normalization, bbox and dimension QA passed",
            raw_bbox=raw_bbox,
            target_dimensions=plan.get("target_dimensions"),
            target_dimensions_model=plan.get("target_dimensions_model"),
            dimension_unit=dimension_unit,
            scale_factor=plan.get("scale_factor"),
            final_bbox=final_bbox,
            dimension_error=dimension_error,
            dimension_status="PASS" if final_bbox is not None else "NOT_MEASURED_NO_MESH",
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
    "ModelDimensionConflict",
    "BlenderNotConfigured",
    "BlenderQAResult",
    "FakeBlenderAdapter",
    "resolve_blender_adapter",
    "extract_glb_bbox",
    "plan_dimension_normalization",
    "validate_glb",
]
