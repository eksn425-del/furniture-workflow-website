"""Render unmodified GLB orientation evidence in a real Blender process."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
from mathutils import Vector
import bpy


def _args() -> dict[str, str]:
    values = list(sys.argv)
    if "--" in values:
        values = values[values.index("--") + 1 :]
    result: dict[str, str] = {}
    index = 0
    while index + 1 < len(values):
        if values[index].startswith("--"):
            result[values[index][2:]] = values[index + 1]
            index += 2
        else:
            index += 1
    for key, env_key in (("src", "BLENDER_VIEWS_SRC"), ("out", "BLENDER_VIEWS_OUT"), ("report", "BLENDER_VIEWS_REPORT")):
        if not result.get(key) and os.getenv(env_key):
            result[key] = os.environ[env_key]
    return result


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _objects() -> list[bpy.types.Object]:
    return [obj for obj in bpy.context.scene.objects if obj.type == "MESH" and not obj.hide_get() and not obj.hide_render]


def _bbox(objects: list[bpy.types.Object]) -> dict[str, object] | None:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        try:
            points.extend(evaluated.matrix_world @ vertex.co for vertex in mesh.vertices)
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
    }


def _look_at(camera: bpy.types.Object, target: Vector) -> None:
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()


def _render(objects: list[bpy.types.Object], bbox: dict[str, object], output: pathlib.Path) -> list[str]:
    output.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = 640
    scene.render.resolution_y = 640
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    center = Vector(((bbox["min"]["x"] + bbox["max"]["x"]) / 2, (bbox["min"]["y"] + bbox["max"]["y"]) / 2, (bbox["min"]["z"] + bbox["max"]["z"]) / 2))
    extent = max(float(bbox["size"][axis]) for axis in ("width", "depth", "height"))
    distance = max(extent * 3.0, 1.0)
    camera_data = bpy.data.cameras.new("ORIENTATION_CAMERA")
    camera = bpy.data.objects.new("ORIENTATION_CAMERA", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = max(extent * 1.35, 1.0)
    views = {
        "front": Vector((center.x, center.y - distance, center.z)),
        "top": Vector((center.x, center.y, center.z + distance)),
        "right": Vector((center.x + distance, center.y, center.z)),
        "iso": Vector((center.x + distance, center.y - distance, center.z + distance * 0.7)),
    }
    paths: list[str] = []
    try:
        for name, location in views.items():
            camera.location = location
            _look_at(camera, center)
            target = output / f"orientation_{name}.png"
            scene.render.filepath = str(target)
            bpy.ops.render.render(write_still=True)
            if target.is_file():
                paths.append(str(target))
    finally:
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.cameras.remove(camera_data)
    return paths


def main() -> int:
    args = _args()
    source = pathlib.Path(args["src"]).resolve()
    output = pathlib.Path(args["out"]).resolve()
    report_path = pathlib.Path(args["report"]).resolve()
    report: dict[str, object] = {
        "schema_version": "blender-orientation-views.v1",
        "blender_version": bpy.app.version_string,
        "source": str(source),
        "source_sha256": _sha256(source),
        "status": "FAILED",
        "views": [],
    }
    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath=str(source))
        objects = _objects()
        bbox = _bbox(objects)
        report["bbox"] = bbox
        if not bbox:
            raise RuntimeError("no evaluated mesh geometry")
        paths = _render(objects, bbox, output)
        report["views"] = paths
        report["status"] = "PASS" if len(paths) == 4 else "INCOMPLETE_VIEWS"
    except Exception as error:
        report["error"] = f"{type(error).__name__}:{error}"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
