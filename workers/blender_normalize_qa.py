"""Headless Blender boundary for product import, pose, scale and re-import QA.

This file is executed by a real Blender binary.  It intentionally owns the
scene-side measurement instead of asking Python GLB parsing to stand in for
Blender's evaluated dependency graph.  The caller passes a reviewed proper
rotation matrix; Blender applies one transform around one product pivot and
then exports a new GLB.  A clean second scene imports that output before the
report can be marked PASS.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import sys
from typing import Any

import bpy
from mathutils import Matrix, Vector


def _args() -> dict[str, str]:
    values = list(sys.argv)
    if "--" in values:
        values = values[values.index("--") + 1 :]
    result: dict[str, str] = {}
    index = 0
    while index + 1 < len(values):
        key = values[index]
        value = values[index + 1]
        if key.startswith("--"):
            result[key[2:]] = value
            index += 2
        else:
            index += 1
    # Blender may not append arguments placed after ``--python`` to the
    # executed script's sys.argv on Windows.  The adapter also supplies these
    # explicit environment keys, so the real CLI boundary remains portable and
    # does not require shell-specific quoting or eval.
    env_keys = {
        "src": "BLENDER_QA_SRC",
        "dst": "BLENDER_QA_DST",
        "report": "BLENDER_QA_REPORT",
        "render_dir": "BLENDER_QA_RENDER_DIR",
        "scale": "BLENDER_QA_SCALE",
        "orientation": "BLENDER_QA_ORIENTATION",
    }
    for key, env_key in env_keys.items():
        if not result.get(key) and os.getenv(env_key):
            result[key] = os.environ[env_key]
    return result


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _product_objects() -> list[bpy.types.Object]:
    return [
        obj
        for obj in bpy.context.scene.objects
        if obj.type == "MESH" and not obj.hide_get() and not obj.hide_render
    ]


def _bbox(objects: list[bpy.types.Object]) -> dict[str, Any] | None:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points: list[Vector] = []
    object_names: list[str] = []
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        try:
            if not mesh.vertices:
                continue
            object_names.append(obj.name)
            world = evaluated.matrix_world
            points.extend(world @ vertex.co for vertex in mesh.vertices)
        finally:
            evaluated.to_mesh_clear()
    if not points:
        return None
    minimum = Vector((min(point.x for point in points), min(point.y for point in points), min(point.z for point in points)))
    maximum = Vector((max(point.x for point in points), max(point.y for point in points), max(point.z for point in points)))
    size = maximum - minimum
    return {
        "min": {"x": float(minimum.x), "y": float(minimum.y), "z": float(minimum.z)},
        "max": {"x": float(maximum.x), "y": float(maximum.y), "z": float(maximum.z)},
        "size": {"width": float(size.x), "depth": float(size.y), "height": float(size.z)},
        "mesh_count": len(object_names),
        "mesh_names": object_names,
    }


def _component_geometry(objects: list[bpy.types.Object]) -> list[dict[str, Any]]:
    """Capture per-mesh world geometry for parent/child and material QA."""

    depsgraph = bpy.context.evaluated_depsgraph_get()
    result: list[dict[str, Any]] = []
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        try:
            if not mesh.vertices:
                continue
            points = [evaluated.matrix_world @ vertex.co for vertex in mesh.vertices]
            minimum = Vector((min(point.x for point in points), min(point.y for point in points), min(point.z for point in points)))
            maximum = Vector((max(point.x for point in points), max(point.y for point in points), max(point.z for point in points)))
            center = (minimum + maximum) / 2
            result.append({
                "name": obj.name,
                "parent": obj.parent.name if obj.parent else None,
                "center": {"x": float(center.x), "y": float(center.y), "z": float(center.z)},
                "size": {"width": float(maximum.x - minimum.x), "depth": float(maximum.y - minimum.y), "height": float(maximum.z - minimum.z)},
                "vertex_count": len(mesh.vertices),
            })
        finally:
            evaluated.to_mesh_clear()
    return result


def _matrix3(payload: str) -> Matrix:
    raw = json.loads(payload) if payload else [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    if not isinstance(raw, list) or len(raw) != 3 or any(not isinstance(row, list) or len(row) != 3 for row in raw):
        raise ValueError("rotation_matrix must be 3x3")
    try:
        matrix = Matrix([[float(value) for value in row] for row in raw])
    except (TypeError, ValueError):
        raise ValueError("rotation_matrix must contain numeric values") from None
    if any(not math.isfinite(float(matrix[row][column])) for row in range(3) for column in range(3)):
        raise ValueError("rotation_matrix must contain finite values")
    orthogonality_error = max(
        abs(sum(float(matrix[row][index]) * float(matrix[column][index]) for index in range(3)) - (1.0 if row == column else 0.0))
        for row in range(3)
        for column in range(3)
    )
    if orthogonality_error > 1e-6:
        raise ValueError(f"rotation_matrix must be orthogonal, error={orthogonality_error}")
    determinant = matrix.determinant()
    if not math.isfinite(determinant) or abs(determinant - 1.0) > 1e-6:
        raise ValueError(f"rotation_matrix must be a proper rotation, determinant={determinant}")
    return matrix


def _transform_objects(objects: list[bpy.types.Object], transform: Matrix) -> None:
    # Snapshot every original world matrix first.  Assigning a parent before
    # its child otherwise changes the child's inherited world transform and a
    # second assignment applies the pose twice.
    original_world = [(obj, obj.matrix_world.copy()) for obj in objects]
    for obj, world_matrix in original_world:
        obj.matrix_world = transform @ world_matrix
    bpy.context.view_layer.update()


def _look_at(camera: bpy.types.Object, target: Vector) -> None:
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()


def _render_views(objects: list[bpy.types.Object], bbox: dict[str, Any] | None, directory: pathlib.Path, prefix: str) -> list[str]:
    if not bbox:
        return []
    directory.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = 640
    scene.render.resolution_y = 640
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    camera_data = bpy.data.cameras.new(f"QA_CAMERA_{prefix}")
    camera = bpy.data.objects.new(f"QA_CAMERA_{prefix}", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    center = Vector((
        (bbox["min"]["x"] + bbox["max"]["x"]) / 2,
        (bbox["min"]["y"] + bbox["max"]["y"]) / 2,
        (bbox["min"]["z"] + bbox["max"]["z"]) / 2,
    ))
    extent = max(float(bbox["size"][axis]) for axis in ("width", "depth", "height"))
    distance = max(extent * 3.0, 1.0)
    views = {
        "front": Vector((center.x, center.y - distance, center.z)),
        "top": Vector((center.x, center.y, center.z + distance)),
        "right": Vector((center.x + distance, center.y, center.z)),
        "iso": Vector((center.x + distance, center.y - distance, center.z + distance * 0.7)),
    }
    paths: list[str] = []
    try:
        camera_data.type = "ORTHO"
        camera_data.ortho_scale = max(extent * 1.35, 1.0)
        for name, location in views.items():
            camera.location = location
            _look_at(camera, center)
            target = directory / f"{prefix}_{name}.png"
            scene.render.filepath = str(target)
            bpy.ops.render.render(write_still=True)
            if target.is_file():
                paths.append(str(target))
    finally:
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.cameras.remove(camera_data)
    return paths


def _scene_metadata(objects: list[bpy.types.Object]) -> dict[str, int]:
    materials = {material.name for obj in objects for material in obj.data.materials if material}
    images = {
        image.name
        for material_name in materials
        for material in [bpy.data.materials.get(material_name)]
        if material
        for node in material.node_tree.nodes if material.use_nodes
        if node.type == "TEX_IMAGE" and node.image
        for image in [node.image]
    }
    return {"mesh_count": len(objects), "material_count": len(materials), "image_count": len(images)}


def _import(path: pathlib.Path) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(path))


def main() -> int:
    args = _args()
    source = pathlib.Path(args["src"]).resolve()
    destination = pathlib.Path(args["dst"]).resolve()
    report_path = pathlib.Path(args["report"]).resolve()
    render_dir = pathlib.Path(args["render_dir"]).resolve() if args.get("render_dir") else None
    orientation = json.loads(args.get("orientation", "{}"))
    scale_factor = float(args.get("scale", "1"))
    report: dict[str, Any] = {
        "schema_version": "blender-normalization-qa.v2",
        "blender_version": bpy.app.version_string,
        "source": str(source),
        "destination": str(destination),
        "source_sha256": _sha256(source),
        "scale_factor": scale_factor,
        "orientation": orientation,
        "status": "FAILED",
    }
    try:
        _import(source)
        objects = _product_objects()
        report["raw_scene"] = _scene_metadata(objects)
        report["raw_bbox"] = _bbox(objects)
        report["raw_components"] = _component_geometry(objects)
        if not report["raw_bbox"]:
            report["status"] = "NO_VALID_MESH"
            raise RuntimeError("no evaluated mesh geometry in imported GLB")
        if render_dir:
            report["renders"] = {"raw": _render_views(objects, report["raw_bbox"], render_dir, "raw")}
        rotation = _matrix3(json.dumps(orientation.get("rotation_matrix") or [[1, 0, 0], [0, 1, 0], [0, 0, 1]]))
        transform = rotation.to_4x4()
        _transform_objects(objects, transform)
        oriented_bbox = _bbox(objects)
        report["oriented_bbox"] = oriented_bbox
        report["oriented_components"] = _component_geometry(objects)
        if not oriented_bbox:
            raise RuntimeError("orientation removed all mesh geometry")
        pivot = Vector((
            (oriented_bbox["min"]["x"] + oriented_bbox["max"]["x"]) / 2,
            (oriented_bbox["min"]["y"] + oriented_bbox["max"]["y"]) / 2,
            oriented_bbox["min"]["z"],
        ))
        scale_transform = Matrix.Translation(pivot) @ Matrix.Diagonal((scale_factor, scale_factor, scale_factor, 1.0)) @ Matrix.Translation(-pivot)
        _transform_objects(objects, scale_transform)
        scaled_bbox = _bbox(objects)
        report["scaled_bbox"] = scaled_bbox
        if not scaled_bbox:
            raise RuntimeError("uniform scaling removed all mesh geometry")
        ground_delta = -float(scaled_bbox["min"]["z"])
        if abs(ground_delta) > 1e-9:
            _transform_objects(objects, Matrix.Translation((0.0, 0.0, ground_delta)))
        final_bbox = _bbox(objects)
        report["final_bbox"] = final_bbox
        report["final_components"] = _component_geometry(objects)
        report["pivot"] = {"x": float(pivot.x), "y": float(pivot.y), "z": float(pivot.z)}
        report["ground_delta"] = ground_delta
        if render_dir:
            report.setdefault("renders", {})["final"] = _render_views(objects, final_bbox, render_dir, "final")
        bpy.ops.object.select_all(action="DESELECT")
        for obj in objects:
            obj.select_set(True)
        if objects:
            bpy.context.view_layer.objects.active = objects[0]
        destination.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.export_scene.gltf(filepath=str(destination), export_format="GLB", use_selection=True, export_apply=False)
        report["destination_sha256"] = _sha256(destination)
        _import(destination)
        reimport_objects = _product_objects()
        report["reimport_scene"] = _scene_metadata(reimport_objects)
        report["reimport_bbox"] = _bbox(reimport_objects)
        report["reimport_components"] = _component_geometry(reimport_objects)
        if not report["reimport_bbox"]:
            raise RuntimeError("re-imported GLB has no evaluated mesh geometry")
        report["reimport_match"] = {
            axis: abs(float(report["reimport_bbox"]["size"][axis]) - float(final_bbox["size"][axis])) <= max(1e-6, abs(float(final_bbox["size"][axis])) * 1e-5)
            for axis in ("width", "depth", "height")
        }
        # Component geometry is compared in deterministic size/center order;
        # Blender may add a suffix to duplicate object names during export.
        final_components = sorted(report.get("final_components") or [], key=lambda item: (str(item.get("name") or ""), item.get("vertex_count") or 0))
        reimport_components = sorted(report.get("reimport_components") or [], key=lambda item: (str(item.get("name") or ""), item.get("vertex_count") or 0))
        component_match = len(final_components) == len(reimport_components)
        if component_match:
            for left, right in zip(final_components, reimport_components):
                for axis in ("x", "y", "z"):
                    component_match = component_match and abs(float(left["center"][axis]) - float(right["center"][axis])) <= 1e-5
                for axis in ("width", "depth", "height"):
                    component_match = component_match and abs(float(left["size"][axis]) - float(right["size"][axis])) <= max(1e-6, abs(float(left["size"][axis])) * 1e-5)
        report["component_geometry_match"] = component_match
        report["status"] = "PASS" if all(report["reimport_match"].values()) and component_match else "REIMPORT_MISMATCH"
    except Exception as error:
        report["error"] = f"{type(error).__name__}:{error}"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
