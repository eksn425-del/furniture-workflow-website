"""Create reproducible synthetic GLB structures for the real Blender gate.

This script is executed *inside an installed Blender binary*.  The resulting
files are synthetic structural fixtures, not vendor products; they are useful
for measuring truth (TRS, parents, instances, orientation and uniform scale)
without claiming historical production coverage.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

import bpy
from mathutils import Euler, Vector


BASE_DIMENSIONS = {"width": 1.2, "depth": 0.8, "height": 1.2}


def reset() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def cube(name: str, dimensions: tuple[float, float, float], location: tuple[float, float, float], *, parent=None):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = dimensions
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if parent is not None:
        obj.parent = parent
    return obj


def semantic_chair(*, parent=None, shared_instances: bool = False):
    """A simple chair whose semantic front is -Y and top is +Z."""

    parts = [
        ("seat", (1.1, 0.7, 0.2), (0.0, -0.02, 0.58)),
        ("back", (1.1, 0.15, 0.72), (0.0, 0.30, 0.86)),
        ("left_arm", (0.10, 0.75, 0.50), (-0.55, 0.0, 0.76)),
        ("right_arm", (0.10, 0.75, 0.50), (0.55, 0.0, 0.76)),
        ("leg_front_left", (0.08, 0.08, 0.40), (-0.48, -0.28, 0.20)),
        ("leg_front_right", (0.08, 0.08, 0.40), (0.48, -0.28, 0.20)),
        ("leg_back_left", (0.08, 0.08, 0.40), (-0.48, 0.28, 0.20)),
        ("leg_back_right", (0.08, 0.08, 0.40), (0.48, 0.28, 0.20)),
    ]
    objects = [cube(name, dims, loc, parent=parent) for name, dims, loc in parts]
    if shared_instances:
        # Two additional legs deliberately share one mesh datablock while
        # retaining separate object transforms.
        source = objects[-1]
        for name, x in (("shared_leg_a", -0.40), ("shared_leg_b", 0.40)):
            instance = source.copy()
            instance.data = source.data
            instance.name = name
            instance.location = (x, 0.30, 0.20)
            if parent is not None:
                instance.parent = parent
            bpy.context.collection.objects.link(instance)
            objects.append(instance)
    return objects


def symmetric_table(*, parent=None):
    bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=0.55, depth=0.12, location=(0, 0, 0.95))
    top = bpy.context.object
    top.name = "round_table_top"
    if parent is not None:
        top.parent = parent
    bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=0.10, depth=0.95, location=(0, 0, 0.475))
    stem = bpy.context.object
    stem.name = "round_table_stem"
    if parent is not None:
        stem.parent = parent
    return [top, stem]


def export_case(path: Path, *, variant: str) -> dict[str, object]:
    reset()
    root = bpy.data.objects.new("PRODUCT_ROOT", None)
    bpy.context.collection.objects.link(root)
    root.location = (0.0, 0.0, 0.0)
    objects = semantic_chair(parent=root, shared_instances=variant == "shared_mesh_instances")
    target = dict(BASE_DIMENSIONS)
    orientation = {"front_axis": "-Y", "top_axis": "+Z", "semantic": "chair_front"}

    if variant == "depth_greater_than_width":
        root.scale = (2.0 / 3.0, 2.0, 5.0 / 6.0)
        target = {"width": 0.8, "depth": 1.6, "height": 1.0}
    elif variant == "width_approximately_depth":
        root.scale = (0.9, 1.35, 1.0)
        target = {"width": 1.08, "depth": 1.08, "height": 1.2}
    elif variant == "source_front_plus_x":
        root.rotation_euler = Euler((0.0, 0.0, math.radians(90.0)), "XYZ")
        orientation = {"front_axis": "+X", "top_axis": "+Z", "semantic": "chair_front_rotated_90"}
    elif variant == "source_front_plus_y":
        root.rotation_euler = Euler((0.0, 0.0, math.radians(180.0)), "XYZ")
        orientation = {"front_axis": "+Y", "top_axis": "+Z", "semantic": "chair_front_rotated_180"}
    elif variant == "source_top_minus_z":
        root.rotation_euler = Euler((0.0, math.radians(180.0), 0.0), "XYZ")
        orientation = {"front_axis": "-Y", "top_axis": "-Z", "semantic": "chair_inverted"}
    elif variant == "parent_scale":
        root.scale = (1.25, 0.75, 1.1)
        target = {"width": 1.5, "depth": 0.6, "height": 1.32}
    elif variant == "nested_parent_rotation":
        outer = bpy.data.objects.new("OUTER_PARENT", None)
        bpy.context.collection.objects.link(outer)
        root.parent = outer
        outer.rotation_euler = Euler((0.0, 0.0, math.radians(90.0)), "XYZ")
        orientation = {"front_axis": "+X", "top_axis": "+Z", "semantic": "nested_parent_rotated"}
    elif variant == "multi_mesh_separated":
        cube("detached_side_table", (0.22, 0.22, 0.5), (0.95, 0.0, 0.25), parent=root)
        # The detached part is intentionally part of this synthetic product;
        # its published target therefore covers the complete assembled span.
        target = {"width": 1.66, "depth": 0.8, "height": 1.2}
    elif variant == "symmetric_no_unique_front":
        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        objects = symmetric_table(parent=root)
        target = {"width": 1.1, "depth": 1.1, "height": 1.0}
        orientation = {"front_axis": "-Y", "top_axis": "+Z", "semantic": "symmetric_equivalent", "equivalent_directions": True}
    elif variant == "tilted_unsupported":
        root.rotation_euler = Euler((math.radians(15.0), 0.0, 0.0), "XYZ")
        orientation = {"front_axis": "UNKNOWN_TILTED", "top_axis": "UNKNOWN_TILTED", "semantic": "requires_review"}
    elif variant == "wider_product":
        root.scale = (1.5, 1.0, 1.0)
        target = {"width": 1.8, "depth": 0.8, "height": 1.2}

    bpy.ops.object.select_all(action="DESELECT")
    for obj in list(bpy.context.scene.objects):
        if obj.type in {"MESH", "EMPTY"}:
            obj.select_set(True)
    bpy.context.view_layer.objects.active = next((obj for obj in bpy.context.scene.objects if obj.type == "MESH"), None)
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=True, export_apply=False)
    return {"variant": variant, "source": str(path), "target_dimensions_m": target, "orientation_review": orientation}


def main() -> int:
    values = list(__import__("sys").argv)
    if "--" in values:
        values = values[values.index("--") + 1 :]
    output_arg = ""
    if "--output-dir" in values:
        index = values.index("--output-dir")
        if index + 1 < len(values):
            output_arg = values[index + 1]
    output = Path(output_arg or os.getenv("BLENDER_FIXTURE_OUTPUT_DIR", "")).resolve()
    if not str(output) or str(output) == ".":
        raise SystemExit("--output-dir or BLENDER_FIXTURE_OUTPUT_DIR is required")
    variants = [
        "width_greater_than_depth", "depth_greater_than_width", "width_approximately_depth",
        "source_front_plus_x", "source_front_plus_y", "source_top_minus_z",
        "parent_scale", "nested_parent_rotation", "shared_mesh_instances", "multi_mesh_separated",
        "symmetric_no_unique_front", "tilted_unsupported", "wider_product",
    ]
    records = []
    for variant in variants:
        path = output / f"{variant}.glb"
        records.append(export_case(path, variant=variant))
    (output / "fixture_manifest.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"count": len(records), "output_dir": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
