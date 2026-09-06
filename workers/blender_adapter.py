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
    dimension_anchor_axis: str | None = None
    non_anchor_dimension_error: dict[str, float] | None = None
    dimension_anchor_policy: str = "FULL_ONLY"
    orientation_status: str = "UNKNOWN"
    orientation: dict[str, object] | None = None
    raw_sha256: str = ""
    reimport_bbox: dict[str, object] | None = None
    evidence_path: str = ""

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
            "dimension_anchor_axis": self.dimension_anchor_axis,
            "non_anchor_dimension_error": self.non_anchor_dimension_error,
            "dimension_anchor_policy": self.dimension_anchor_policy,
            "orientation_status": self.orientation_status,
            "orientation": self.orientation,
            "raw_sha256": self.raw_sha256,
            "reimport_bbox": self.reimport_bbox,
            "evidence_path": self.evidence_path,
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


def _known_unit_factor(unit: object) -> float | None:
    normalized = str(unit or "").strip().casefold()
    if not normalized or normalized in {"source_unit", "unknown", ""}:
        return None
    return _UNIT_TO_MODEL.get(normalized)


def _geometry_tolerance() -> float:
    """Final measured-size tolerance, separate from aspect-ratio safety."""

    try:
        value = float(os.getenv("BLENDER_DIMENSION_TOLERANCE", "0.05"))
    except (TypeError, ValueError):
        value = 0.05
    return min(0.15, max(0.001, value))


def _aspect_ratio_limit() -> float:
    try:
        value = float(os.getenv("BLENDER_ASPECT_RATIO_LIMIT", "1.25"))
    except (TypeError, ValueError):
        value = 1.25
    return max(1.0, value)


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


def _mat4_identity() -> tuple[tuple[float, ...], ...]:
    return (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def _mat4_mul(left: tuple[tuple[float, ...], ...], right: tuple[tuple[float, ...], ...]) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(sum(left[row][k] * right[k][column] for k in range(4)) for column in range(4))
        for row in range(4)
    )


def _mat4_point(matrix: tuple[tuple[float, ...], ...], point: tuple[float, float, float]) -> tuple[float, float, float]:
    values = (
        matrix[0][0] * point[0] + matrix[0][1] * point[1] + matrix[0][2] * point[2] + matrix[0][3],
        matrix[1][0] * point[0] + matrix[1][1] * point[1] + matrix[1][2] * point[2] + matrix[1][3],
        matrix[2][0] * point[0] + matrix[2][1] * point[1] + matrix[2][2] * point[2] + matrix[2][3],
    )
    w = matrix[3][0] * point[0] + matrix[3][1] * point[1] + matrix[3][2] * point[2] + matrix[3][3]
    if not all(math.isfinite(value) for value in values) or not math.isfinite(w):
        raise BlenderAdapterError("glb_geometry_read_failed:non_finite_transform")
    if abs(w) > 1e-12 and abs(w - 1.0) > 1e-12:
        return tuple(value / w for value in values)
    return values


def _translation_matrix(values: object) -> tuple[tuple[float, ...], ...]:
    values = values if isinstance(values, list) and len(values) >= 3 else [0.0, 0.0, 0.0]
    return (
        (1.0, 0.0, 0.0, float(values[0])),
        (0.0, 1.0, 0.0, float(values[1])),
        (0.0, 0.0, 1.0, float(values[2])),
        (0.0, 0.0, 0.0, 1.0),
    )


def _scale_matrix(values: object) -> tuple[tuple[float, ...], ...]:
    values = values if isinstance(values, list) and len(values) >= 3 else [1.0, 1.0, 1.0]
    return (
        (float(values[0]), 0.0, 0.0, 0.0),
        (0.0, float(values[1]), 0.0, 0.0),
        (0.0, 0.0, float(values[2]), 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def _rotation_matrix(values: object) -> tuple[tuple[float, ...], ...]:
    values = values if isinstance(values, list) and len(values) >= 4 else [0.0, 0.0, 0.0, 1.0]
    x, y, z, w = (float(values[index]) for index in range(4))
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length <= 1e-12 or not math.isfinite(length):
        raise BlenderAdapterError("glb_geometry_read_failed:invalid_quaternion")
    x, y, z, w = x / length, y / length, z / length, w / length
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0.0),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0.0),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def _node_matrix(node: Mapping[str, object]) -> tuple[tuple[float, ...], ...]:
    raw_matrix = node.get("matrix")
    if isinstance(raw_matrix, list) and len(raw_matrix) >= 16:
        # glTF stores matrices column-major; the rest of this module uses rows.
        return tuple(tuple(float(raw_matrix[column * 4 + row]) for column in range(4)) for row in range(4))
    return _mat4_mul(
        _mat4_mul(_translation_matrix(node.get("translation")), _rotation_matrix(node.get("rotation"))),
        _scale_matrix(node.get("scale")),
    )


def _accessor_points(
    accessor: Mapping[str, object],
    buffer_views: list[object],
    binary: bytes,
) -> tuple[list[tuple[float, float, float]], str]:
    component = _COMPONENT_FORMATS.get(int(accessor.get("componentType") or 0))
    if component is None or str(accessor.get("type") or "") != "VEC3":
        return [], "UNSUPPORTED_POSITION_ACCESSOR"
    view_index = accessor.get("bufferView")
    if not isinstance(view_index, int) or not (0 <= view_index < len(buffer_views)):
        minimum = accessor.get("min")
        maximum = accessor.get("max")
        if isinstance(minimum, list) and isinstance(maximum, list) and len(minimum) >= 3 and len(maximum) >= 3:
            low = tuple(float(minimum[index]) for index in range(3))
            high = tuple(float(maximum[index]) for index in range(3))
            if all(math.isfinite(value) for value in (*low, *high)):
                return [
                    (x, y, z)
                    for x in (low[0], high[0])
                    for y in (low[1], high[1])
                    for z in (low[2], high[2])
                ], "ACCESSOR_BOUNDS_FALLBACK"
        return [], "UNSUPPORTED_POSITION_ACCESSOR"
    view = buffer_views[view_index]
    if not isinstance(view, dict):
        return [], "UNSUPPORTED_POSITION_ACCESSOR"
    fmt, component_size = component
    count = int(accessor.get("count") or 0)
    element_size = component_size * 3
    stride = int(view.get("byteStride") or element_size)
    start = int(view.get("byteOffset") or 0) + int(accessor.get("byteOffset") or 0)
    result: list[tuple[float, float, float]] = []
    for item_index in range(max(0, count)):
        item_start = start + item_index * stride
        item_end = item_start + element_size
        if item_end > len(binary):
            return result, "TRUNCATED_POSITION_ACCESSOR"
        values = struct.unpack_from("<" + fmt * 3, binary, item_start)
        if not all(math.isfinite(float(value)) for value in values):
            return result, "NON_FINITE_POSITION"
        result.append((float(values[0]), float(values[1]), float(values[2])))
    return result, "POSITION_BYTES"


def _bbox_from_points(points: list[tuple[float, float, float]], *, method: str = "WORLD_GEOMETRY") -> dict[str, object] | None:
    if not points:
        return None
    minimum = tuple(min(point[index] for point in points) for index in range(3))
    maximum = tuple(max(point[index] for point in points) for index in range(3))
    return {
        "min": {"x": minimum[0], "y": minimum[1], "z": minimum[2]},
        "max": {"x": maximum[0], "y": maximum[1], "z": maximum[2]},
        # Raw glTF is Y-up; the Website contract maps X/Z/Y to W/D/H.
        "size": {
            "width": maximum[0] - minimum[0],
            "depth": maximum[2] - minimum[2],
            "height": maximum[1] - minimum[1],
        },
        "method": method,
        "point_count": len(points),
    }


def extract_glb_bbox(path: Path) -> dict[str, object] | None:
    """Measure referenced POSITION geometry in glTF world space.

    This parser is a bounded, dependency-free cross-check for the real Blender
    measurement path.  It walks scene nodes, applies parent TRS/matrix values,
    preserves shared-mesh instances, and decodes POSITION bytes when present.
    Accessor min/max is used only as an explicit fallback for synthetic or
    malformed-but-bounded fixtures; it is labelled in the returned evidence.
    """

    document, binary = _read_glb_json_and_bin(path)
    accessors = document.get("accessors") if isinstance(document.get("accessors"), list) else []
    buffer_views = document.get("bufferViews") if isinstance(document.get("bufferViews"), list) else []
    meshes = document.get("meshes") if isinstance(document.get("meshes"), list) else []
    nodes = document.get("nodes") if isinstance(document.get("nodes"), list) else []
    mesh_points: dict[int, tuple[list[tuple[float, float, float]], str]] = {}
    for mesh_index, mesh in enumerate(meshes):
        if not isinstance(mesh, dict):
            continue
        mesh_points[mesh_index] = ([], "POSITION_BYTES")
        primitives = mesh.get("primitives") if isinstance(mesh.get("primitives"), list) else []
        methods: list[str] = []
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
            points, method = _accessor_points(accessor, buffer_views, binary)
            mesh_points[mesh_index][0].extend(points)
            methods.append(method)
        mesh_points[mesh_index] = (mesh_points[mesh_index][0], "+".join(sorted(set(methods))) or "NO_POSITION")
    if not nodes:
        roots = [(index, _mat4_identity()) for index, node in enumerate(meshes) if isinstance(node, dict)]
    else:
        scenes = document.get("scenes") if isinstance(document.get("scenes"), list) else []
        active_scene_index = document.get("scene") if isinstance(document.get("scene"), int) else 0
        scene_nodes = []
        if scenes and 0 <= active_scene_index < len(scenes) and isinstance(scenes[active_scene_index], dict):
            scene_nodes = [value for value in scenes[active_scene_index].get("nodes", []) if isinstance(value, int)]
        if scene_nodes:
            roots = [(index, _mat4_identity()) for index in scene_nodes]
        else:
            referenced: set[int] = set()
            for node in nodes:
                if isinstance(node, dict):
                    referenced.update(value for value in node.get("children", []) if isinstance(value, int))
            roots = [(index, _mat4_identity()) for index in range(len(nodes)) if index not in referenced]
    points: list[tuple[float, float, float]] = []
    methods: list[str] = []
    instance_count = 0

    def visit(node_index: int, parent_matrix: tuple[tuple[float, ...], ...], active: set[int]) -> None:
        nonlocal instance_count
        if not (0 <= node_index < len(nodes)) or node_index in active:
            return
        node = nodes[node_index]
        if not isinstance(node, dict):
            return
        world = _mat4_mul(parent_matrix, _node_matrix(node))
        mesh_index = node.get("mesh")
        if isinstance(mesh_index, int) and mesh_index in mesh_points:
            local_points, method = mesh_points[mesh_index]
            if local_points:
                instance_count += 1
                points.extend(_mat4_point(world, point) for point in local_points)
                methods.append(method)
        next_active = set(active)
        next_active.add(node_index)
        for child in node.get("children", []) if isinstance(node.get("children"), list) else []:
            if isinstance(child, int):
                visit(child, world, next_active)

    if nodes:
        for node_index, parent in roots:
            visit(node_index, parent, set())
    else:
        for mesh_index, (local_points, method) in mesh_points.items():
            if local_points:
                instance_count += 1
                points.extend(local_points)
                methods.append(method)
    result = _bbox_from_points(points, method="GLTF_NODE_WORLD:" + "+".join(sorted(set(methods))))
    if result is not None:
        result["mesh_count"] = len({node.get("mesh") for node in nodes if isinstance(node, dict) and isinstance(node.get("mesh"), int)}) if nodes else len([value for value in mesh_points.values() if value[0]])
        result["instance_count"] = instance_count
        result["scene_node_count"] = len(nodes)
    return result


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
    *,
    dimension_anchor_axis: str | None = None,
    allow_non_anchor_dimension_error: bool = False,
) -> dict[str, object]:
    """Plan safe uniform normalization and reject obvious aspect-ratio conflicts."""

    target = _dimension_values(target_dimensions)
    factor_unit = _known_unit_factor(dimension_unit)
    has_partial_target = isinstance(target_dimensions, Mapping) and any(
        value not in (None, "") for value in target_dimensions.values()
    )
    if (target is not None or has_partial_target) and factor_unit is None:
        return {
            "target_dimensions": target or dict(target_dimensions or {}),
            "target_dimensions_model": None,
            "scale_factor": None,
            "dimension_status": "UNKNOWN_UNIT",
            "reason": "dimension_unit must be explicitly verified before geometry scaling",
        }
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
            target_partial = {axis: value for axis, value in anchors}
            target_model_partial = {axis: value * float(factor_unit or 1.0) for axis, value in anchors}
            ratios = [target_model_partial[axis] / raw_size[axis] for axis, _ in anchors]
            spread = max(ratios) / min(ratios)
            if spread > _aspect_ratio_limit():
                raise ModelDimensionConflict(
                    "partial target dimensions require non-uniform deformation "
                    f"(ratio spread {spread:.3f} > {_aspect_ratio_limit():.3f})"
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
                "dimension_status": "PASS" if max(errors.values()) <= _geometry_tolerance() else "MODEL_DIMENSION_CONFLICT",
                "planned_final_dimensions": final_size,
                "dimension_error": errors,
                "partial_axes_anchored": [axis for axis, _ in anchors],
                "single_axis_anchored": anchors[0][0] if len(anchors) == 1 else None,
                "height_anchored": len(anchors) == 1 and anchors[0][0] == "height",
                "geometry_tolerance": _geometry_tolerance(),
                "aspect_ratio_limit": _aspect_ratio_limit(),
            }
    if target is None:
        return {
            "target_dimensions": None,
            "target_dimensions_model": None,
            "scale_factor": 1.0,
            "dimension_status": "MEASURED_NO_TARGET",
        }
    target_model = {axis: value * float(factor_unit or 1.0) for axis, value in target.items()}
    anchor_axis = str(dimension_anchor_axis or "").strip().casefold()
    if anchor_axis and anchor_axis not in {"width", "depth", "height"}:
        return {
            "target_dimensions": target,
            "target_dimensions_model": target_model,
            "scale_factor": None,
            "dimension_status": "INVALID_ANCHOR_AXIS",
            "reason": "dimension_anchor_axis must be width, depth, or height",
        }
    # An explicit anchor is used for products whose official catalog gives a
    # trusted primary span but whose depth/height may represent a different
    # configuration or a provider reconstruction.  The geometry is still
    # transformed by one uniform factor; only the anchor is a delivery gate.
    # This deliberately does not weaken the default FULL_ONLY policy.
    if anchor_axis and allow_non_anchor_dimension_error:
        if anchor_axis not in target_model or anchor_axis not in raw_size:
            return {
                "target_dimensions": target,
                "target_dimensions_model": target_model,
                "scale_factor": None,
                "dimension_status": "ANCHOR_MISSING",
                "reason": "explicit dimension anchor must exist in target and measured geometry",
            }
        factor = target_model[anchor_axis] / raw_size[anchor_axis]
        final_size = {axis: raw_size[axis] * factor for axis in raw_size}
        errors = {
            axis: abs(final_size[axis] - target_model[axis]) / target_model[axis]
            for axis in ("width", "depth", "height")
            if axis in target_model and axis in final_size
        }
        anchor_error = errors.get(anchor_axis, float("inf"))
        non_anchor_error = {axis: value for axis, value in errors.items() if axis != anchor_axis}
        return {
            "target_dimensions": target,
            "target_dimensions_model": target_model,
            "scale_factor": factor,
            "dimension_status": "PASS" if anchor_error <= _geometry_tolerance() else "MODEL_DIMENSION_CONFLICT",
            "planned_final_dimensions": final_size,
            "dimension_error": errors,
            "non_anchor_dimension_error": non_anchor_error,
            "dimension_anchor_axis": anchor_axis,
            "dimension_anchor_policy": "EXPLICIT_ANCHOR",
            "geometry_tolerance": _geometry_tolerance(),
            "aspect_ratio_limit": _aspect_ratio_limit(),
        }
    ratios = [target_model[axis] / raw_size[axis] for axis in ("width", "depth", "height")]
    spread = max(ratios) / min(ratios)
    if spread > _aspect_ratio_limit():
        raise ModelDimensionConflict(
            "target dimensions require non-uniform deformation "
            f"(ratio spread {spread:.3f} > {_aspect_ratio_limit():.3f})"
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
        "dimension_status": "PASS" if max(errors.values()) <= _geometry_tolerance() else "MODEL_DIMENSION_CONFLICT",
        "planned_final_dimensions": final_size,
        "dimension_error": errors,
        "geometry_tolerance": _geometry_tolerance(),
        "aspect_ratio_limit": _aspect_ratio_limit(),
    }


_AXIS_VECTORS: dict[str, tuple[float, float, float]] = {
    "+X": (1.0, 0.0, 0.0), "-X": (-1.0, 0.0, 0.0),
    "+Y": (0.0, 1.0, 0.0), "-Y": (0.0, -1.0, 0.0),
    "+Z": (0.0, 0.0, 1.0), "-Z": (0.0, 0.0, -1.0),
}


def _vec_dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum(left[index] * right[index] for index in range(3))


def _vec_cross(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _mat3_det(matrix: tuple[tuple[float, ...], ...]) -> float:
    return (
        matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
        - matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
        + matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0])
    )


def _validate_rotation_matrix(value: object, *, error_prefix: str = "ORIENTATION_INVALID_MATRIX") -> tuple[tuple[float, ...], ...]:
    """Validate a true proper rotation, not merely a determinant.

    A matrix with determinant +1 can still contain shear or non-uniform scale.
    The Website contract accepts only finite 3x3 orthonormal matrices with a
    positive handedness.  Keeping this check in the Python preflight mirrors
    the check in the real Blender script and prevents a bad payload from
    reaching a paid-model delivery path.
    """

    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise BlenderAdapterError(error_prefix)
    try:
        matrix = tuple(tuple(float(item) for item in row) for row in value)
    except (TypeError, ValueError):
        raise BlenderAdapterError(error_prefix) from None
    if any(len(row) != 3 for row in matrix):
        raise BlenderAdapterError(error_prefix)
    if not all(math.isfinite(item) for row in matrix for item in row):
        raise BlenderAdapterError("ORIENTATION_MATRIX_NON_FINITE")
    orthogonality_error = max(
        abs(sum(matrix[row][index] * matrix[column][index] for index in range(3)) - (1.0 if row == column else 0.0))
        for row in range(3)
        for column in range(3)
    )
    if orthogonality_error > 1e-6:
        raise BlenderAdapterError("ORIENTATION_MATRIX_NOT_ORTHOGONAL")
    determinant = _mat3_det(matrix)
    if not math.isfinite(determinant) or abs(determinant - 1.0) > 1e-6:
        raise BlenderAdapterError("ORIENTATION_WOULD_MIRROR_PRODUCT")
    return matrix


def _oriented_bbox_for_plan(
    raw_bbox: Mapping[str, object] | None,
    rotation_matrix: object,
) -> dict[str, object] | None:
    """Rotate the parser cross-check bbox into Blender contract axes.

    ``extract_glb_bbox`` reports glTF X/Z/Y as Website width/depth/height
    (glTF is Y-up).  The real Blender helper applies the reviewed matrix in
    Blender X/Y/Z, so planning the scale from the unrotated width/depth would
    incorrectly reject a legitimate 90-degree pose as non-uniform.  Use the
    eight raw bbox corners only for the preflight scale estimate; the final
    truth still comes from Blender's evaluated world geometry.
    """

    if not isinstance(raw_bbox, Mapping) or not isinstance(raw_bbox.get("min"), Mapping) or not isinstance(raw_bbox.get("max"), Mapping):
        return None
    if not isinstance(rotation_matrix, list) or len(rotation_matrix) != 3:
        return raw_bbox if isinstance(raw_bbox, dict) else None
    try:
        matrix = tuple(tuple(float(value) for value in row) for row in rotation_matrix)
        if any(len(row) != 3 for row in matrix):
            return raw_bbox if isinstance(raw_bbox, dict) else None
        raw_min = raw_bbox["min"]
        raw_max = raw_bbox["max"]
        # raw x/y/z are glTF x/y/z; Blender import presents x/z/y.
        corners = [
            (float(x), float(z), float(y))
            for x in (raw_min.get("x"), raw_max.get("x"))
            for y in (raw_min.get("y"), raw_max.get("y"))
            for z in (raw_min.get("z"), raw_max.get("z"))
        ]
        transformed = [
            tuple(sum(matrix[row][column] * point[column] for column in range(3)) for row in range(3))
            for point in corners
        ]
        minimum = tuple(min(point[index] for point in transformed) for index in range(3))
        maximum = tuple(max(point[index] for point in transformed) for index in range(3))
        return {
            "min": {"x": minimum[0], "y": minimum[1], "z": minimum[2]},
            "max": {"x": maximum[0], "y": maximum[1], "z": maximum[2]},
            "size": {
                "width": maximum[0] - minimum[0],
                "depth": maximum[1] - minimum[1],
                "height": maximum[2] - minimum[2],
            },
            "method": "GLTF_BBOX_ORIENTATION_PREFLIGHT",
            "source_method": raw_bbox.get("method"),
        }
    except (TypeError, ValueError, KeyError, IndexError):
        return raw_bbox if isinstance(raw_bbox, dict) else None


def orientation_rotation(
    *,
    front_axis: object,
    top_axis: object,
    target_front_axis: str = "-Y",
    target_top_axis: str = "+Z",
) -> dict[str, object]:
    """Build a legal proper rotation from a reviewed source pose.

    Axis strings describe the current Blender-world directions identified by
    the Website Brain/operator.  The returned matrix maps those directions to
    the product contract (front -Y, top +Z); it can never silently mirror a
    left/right asymmetric product.
    """

    source_front_key = str(front_axis or "").strip().upper()
    source_top_key = str(top_axis or "").strip().upper()
    target_front_key = str(target_front_axis or "-Y").strip().upper()
    target_top_key = str(target_top_axis or "+Z").strip().upper()
    source_front = _AXIS_VECTORS.get(source_front_key)
    source_top = _AXIS_VECTORS.get(source_top_key)
    target_front = _AXIS_VECTORS.get(target_front_key)
    target_top = _AXIS_VECTORS.get(target_top_key)
    if not source_front or not source_top or not target_front or not target_top:
        raise BlenderAdapterError("ORIENTATION_UNKNOWN_AXIS")
    if abs(_vec_dot(source_front, source_top)) > 1e-9 or abs(_vec_dot(target_front, target_top)) > 1e-9:
        raise BlenderAdapterError("ORIENTATION_AXES_NOT_ORTHOGONAL")
    source_right = _vec_cross(source_top, source_front)
    target_right = _vec_cross(target_top, target_front)
    # R = B_target * B_source^T, with columns [right, front, top].
    source_basis = (source_right, source_front, source_top)
    target_basis = (target_right, target_front, target_top)
    matrix = tuple(
        tuple(sum(target_basis[basis][row] * source_basis[basis][column] for basis in range(3)) for column in range(3))
        for row in range(3)
    )
    matrix = _validate_rotation_matrix([list(row) for row in matrix], error_prefix="ORIENTATION_INVALID_MATRIX")
    determinant = _mat3_det(matrix)
    return {
        "source_front_axis": source_front_key,
        "source_top_axis": source_top_key,
        "target_front_axis": target_front_key,
        "target_top_axis": target_top_key,
        "rotation_matrix": [list(row) for row in matrix],
        "determinant": determinant,
        "status": "CONFIRMED",
    }


class FakeBlenderAdapter:
    """Deterministic local adapter used only by an explicitly marked E2E run."""

    name = "FAKE_LOCAL_BLENDER"

    def render_orientation_views(self, raw_path: Path, render_dir: Path) -> dict[str, object]:
        # Explicitly marked fixture seam.  Production closure evidence never
        # treats this as real visual inspection.
        return {"status": "FIXTURE_ONLY", "source": str(raw_path), "views": []}

    def normalize_and_qa(
        self,
        raw_path: Path,
        output_path: Path,
        *,
        target_dimensions: Mapping[str, object] | None = None,
        dimension_unit: str = "source_unit",
        orientation: Mapping[str, object] | None = None,
        dimension_anchor_axis: str | None = None,
        allow_non_anchor_dimension_error: bool = False,
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
        plan = plan_dimension_normalization(
            raw_bbox,
            target_dimensions,
            dimension_unit,
            dimension_anchor_axis=dimension_anchor_axis,
            allow_non_anchor_dimension_error=allow_non_anchor_dimension_error,
        )
        if plan.get("dimension_status") in {"UNKNOWN_UNIT", "INVALID_ANCHOR_AXIS", "ANCHOR_MISSING", "MODEL_DIMENSION_CONFLICT"}:
            raise ModelDimensionConflict(str(plan.get("reason") or "target dimensions cannot be normalized safely"))
        return BlenderQAResult(
            status="FIXTURE_ONLY",
            adapter=self.name,
            normalized_path=str(output_path),
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            reason="deterministic local fixture pass-through; real Blender geometry scaling and re-import are not claimed",
            raw_bbox=raw_bbox,
            target_dimensions=plan.get("target_dimensions"),
            target_dimensions_model=plan.get("target_dimensions_model"),
            dimension_unit=dimension_unit,
            scale_factor=plan.get("scale_factor"),
            final_bbox=raw_bbox,
            dimension_error=plan.get("dimension_error"),
            dimension_status=("NOT_MEASURED_NO_MESH" if raw_bbox is None else "FIXTURE_NOT_APPLIED"),
            dimension_anchor_axis=plan.get("dimension_anchor_axis"),
            non_anchor_dimension_error=plan.get("non_anchor_dimension_error"),
            dimension_anchor_policy=str(plan.get("dimension_anchor_policy") or "FULL_ONLY"),
            orientation_status="NOT_VERIFIED",
            raw_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        )


class BlenderCLIAdapter:
    """Run Blender headlessly to import/export one GLB and then validate it."""

    name = "BLENDER_CLI"

    def __init__(self, executable: str, *, timeout_seconds: int = 300) -> None:
        self.executable = executable
        self.timeout_seconds = max(30, min(int(timeout_seconds), 1800))

    def render_orientation_views(self, raw_path: Path, render_dir: Path) -> dict[str, object]:
        """Import the raw GLB and render four unmodified orientation views."""

        if not raw_path.is_file():
            raise BlenderAdapterError("raw_glb_missing")
        render_dir.mkdir(parents=True, exist_ok=True)
        report_path = render_dir / "orientation-views.json"
        helper = Path(__file__).with_name("blender_render_views.py")
        blender_env = os.environ.copy()
        blender_env.update({
            "BLENDER_VIEWS_SRC": str(raw_path),
            "BLENDER_VIEWS_OUT": str(render_dir),
            "BLENDER_VIEWS_REPORT": str(report_path),
        })
        try:
            result = subprocess.run(
                [
                    self.executable, "--background", "--factory-startup", "--python", str(helper), "--",
                    "--src", str(raw_path), "--out", str(render_dir), "--report", str(report_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                env=blender_env,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BlenderAdapterError(f"blender_orientation_views_failed:{type(error).__name__}") from error
        if not report_path.is_file():
            raise BlenderAdapterError(f"blender_orientation_views_failed:exit_{result.returncode}")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise BlenderAdapterError("blender_orientation_views_report_invalid") from error
        if result.returncode != 0 or not isinstance(report, dict) or report.get("status") != "PASS":
            detail = report.get("error") or report.get("status") or f"exit_{result.returncode}" if isinstance(report, dict) else f"exit_{result.returncode}"
            raise BlenderAdapterError(f"blender_orientation_views_failed:{detail}")
        return report

    def normalize_and_qa(
        self,
        raw_path: Path,
        output_path: Path,
        *,
        target_dimensions: Mapping[str, object] | None = None,
        dimension_unit: str = "source_unit",
        orientation: Mapping[str, object] | None = None,
        dimension_anchor_axis: str | None = None,
        allow_non_anchor_dimension_error: bool = False,
    ) -> BlenderQAResult:
        if not raw_path.is_file():
            raise BlenderAdapterError("raw_glb_missing")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_bbox = extract_glb_bbox(raw_path)
        orientation_missing = orientation is None
        # Use identity only as an execution fallback so a legacy subprocess
        # seam can still surface a final-dimension conflict.  A real report
        # without an explicit reviewed pose is rejected below; identity is
        # never presented as a normal R01 PASS.
        orientation_payload = dict(orientation or {})
        if orientation_missing:
            orientation_payload = orientation_rotation(front_axis="-Y", top_axis="+Z")
        if not orientation_payload.get("rotation_matrix"):
            orientation_payload = orientation_rotation(
                front_axis=orientation_payload.get("front_axis") or orientation_payload.get("source_front_axis"),
                top_axis=orientation_payload.get("top_axis") or orientation_payload.get("source_top_axis"),
                target_front_axis=str(orientation_payload.get("target_front_axis") or "-Y"),
                target_top_axis=str(orientation_payload.get("target_top_axis") or "+Z"),
            )
        else:
            matrix = orientation_payload.get("rotation_matrix")
            if not isinstance(matrix, list) or len(matrix) != 3 or any(not isinstance(row, list) or len(row) != 3 for row in matrix):
                raise BlenderAdapterError("ORIENTATION_INVALID_MATRIX")
            numeric = _validate_rotation_matrix(matrix)
            orientation_payload["status"] = "CONFIRMED"
        oriented_preflight_bbox = _oriented_bbox_for_plan(raw_bbox, orientation_payload.get("rotation_matrix"))
        plan = plan_dimension_normalization(
            oriented_preflight_bbox,
            target_dimensions,
            dimension_unit,
            dimension_anchor_axis=dimension_anchor_axis,
            allow_non_anchor_dimension_error=allow_non_anchor_dimension_error,
        )
        if plan.get("dimension_status") == "UNKNOWN_UNIT":
            raise BlenderAdapterError("UNKNOWN_DIMENSION_UNIT")
        if plan.get("dimension_status") in {"MODEL_DIMENSION_CONFLICT", "INVALID_ANCHOR_AXIS", "ANCHOR_MISSING"}:
            raise ModelDimensionConflict("target dimensions exceed uniform normalization tolerance")
        report_path = output_path.with_suffix(".blender-qa.json")
        render_dir = output_path.parent / f"{output_path.stem}.views"
        helper = Path(__file__).with_name("blender_normalize_qa.py")
        blender_env = os.environ.copy()
        blender_env.update({
            "BLENDER_QA_SRC": str(raw_path),
            "BLENDER_QA_DST": str(output_path),
            "BLENDER_QA_REPORT": str(report_path),
            "BLENDER_QA_RENDER_DIR": str(render_dir),
            "BLENDER_QA_SCALE": str(plan.get("scale_factor") or 1.0),
            "BLENDER_QA_ORIENTATION": json.dumps(orientation_payload, ensure_ascii=False),
        })
        try:
            result = subprocess.run(
                [
                    self.executable, "--background", "--factory-startup", "--python", str(helper), "--",
                    "--src", str(raw_path), "--dst", str(output_path), "--report", str(report_path),
                    "--render-dir", str(render_dir), "--scale", str(plan.get("scale_factor") or 1.0),
                    "--orientation", json.dumps(orientation_payload, ensure_ascii=False),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                env=blender_env,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BlenderAdapterError(f"blender_cli_failed:{type(error).__name__}") from error
        if output_path.is_file():
            valid, reason = validate_glb(output_path)
            if not valid:
                raise BlenderAdapterError(f"normalized_glb_qa_failed:{reason}")
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise BlenderAdapterError("blender_qa_report_invalid") from error
        else:
            report = {}
        if not report and result.returncode == 0 and output_path.is_file():
            # Keep the old safe-domain test seam: a subprocess stub without a
            # Blender-side report may still prove a final dimension conflict,
            # but it can never be accepted as a real Blender run.
            fallback_bbox = extract_glb_bbox(output_path)
            fallback_size = fallback_bbox.get("size") if isinstance(fallback_bbox, dict) else None
            target_model = plan.get("target_dimensions_model")
            if isinstance(fallback_size, dict) and isinstance(target_model, dict):
                fallback_error = {
                    axis: abs(float(fallback_size[axis]) - float(target_model[axis])) / float(target_model[axis])
                    for axis in ("width", "depth", "height")
                    if axis in fallback_size and axis in target_model
                }
                fallback_gate = (
                    fallback_error.get(str(dimension_anchor_axis or "").casefold(), float("inf"))
                    if dimension_anchor_axis and allow_non_anchor_dimension_error
                    else max(fallback_error.values()) if fallback_error else float("inf")
                )
                if fallback_error and fallback_gate > _geometry_tolerance():
                    raise ModelDimensionConflict("final dimensions exceed configured geometry tolerance after uniform normalization")
        if orientation_missing:
            raise BlenderAdapterError("ORIENTATION_REVIEW_REQUIRED")
        if result.returncode != 0 or report.get("status") != "PASS":
            detail = report.get("error") or report.get("status") or f"exit_{result.returncode}"
            raise BlenderAdapterError(f"blender_cli_failed:{detail}")
        valid, reason = validate_glb(output_path)
        if not valid:
            raise BlenderAdapterError(f"normalized_glb_qa_failed:{reason}")
        raw = output_path.read_bytes()
        final_bbox = report.get("reimport_bbox") if isinstance(report.get("reimport_bbox"), dict) else extract_glb_bbox(output_path)
        dimension_error = plan.get("dimension_error")
        final_size = final_bbox.get("size") if isinstance(final_bbox, dict) else None
        target_model = plan.get("target_dimensions_model")
        if isinstance(final_size, dict) and isinstance(target_model, dict):
            dimension_error = {
                axis: abs(float(final_size[axis]) - float(target_model[axis])) / float(target_model[axis])
                for axis in ("width", "depth", "height")
                if axis in final_size and axis in target_model
            }
        gate_error = (
            dimension_error.get(str(dimension_anchor_axis or "").casefold(), float("inf"))
            if dimension_anchor_axis and allow_non_anchor_dimension_error and dimension_error
            else max(dimension_error.values()) if dimension_error else 0.0
        )
        if dimension_error and gate_error > _geometry_tolerance():
            raise ModelDimensionConflict("final dimensions exceed configured geometry tolerance after uniform normalization")
        return BlenderQAResult(
            status="PASS",
            adapter=self.name,
            normalized_path=str(output_path),
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            reason="Blender CLI import/export, evaluated world bbox, uniform normalization and clean re-import QA passed",
            raw_bbox=report.get("raw_bbox") if isinstance(report.get("raw_bbox"), dict) else raw_bbox,
            target_dimensions=plan.get("target_dimensions"),
            target_dimensions_model=plan.get("target_dimensions_model"),
            dimension_unit=dimension_unit,
            scale_factor=plan.get("scale_factor"),
            final_bbox=report.get("final_bbox") if isinstance(report.get("final_bbox"), dict) else final_bbox,
            dimension_error=dimension_error,
            dimension_status="PASS" if final_bbox is not None else "NOT_MEASURED_NO_MESH",
            dimension_anchor_axis=plan.get("dimension_anchor_axis"),
            non_anchor_dimension_error=plan.get("non_anchor_dimension_error") or {
                axis: value for axis, value in (dimension_error or {}).items()
                if axis != str(dimension_anchor_axis or "").casefold()
            } if dimension_anchor_axis and allow_non_anchor_dimension_error else None,
            dimension_anchor_policy=str(plan.get("dimension_anchor_policy") or "FULL_ONLY"),
            orientation_status=str((report.get("orientation") or orientation_payload).get("status") or "CONFIRMED"),
            orientation=report.get("orientation") if isinstance(report.get("orientation"), dict) else orientation_payload,
            raw_sha256=str(report.get("source_sha256") or hashlib.sha256(raw_path.read_bytes()).hexdigest()),
            reimport_bbox=final_bbox,
            evidence_path=str(report_path),
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
    "orientation_rotation",
    "validate_glb",
]
