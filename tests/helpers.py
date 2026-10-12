"""Fixtures shared by the tests. Everything here runs inside headless Blender (see run.py)."""
import json
import os
import shutil
import struct
import tempfile
import time
import unittest

import numpy as np

import bpy
import combi_export as ce

GN = "GN"
MODIFIER = "<Modifier Interface>"


class BlenderTestCase(unittest.TestCase):
    """Starts every test from an empty factory file. The add-on stays registered across file loads."""

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        ce._clipboard.update(preset=None, collection=None, nodegroup=None)
        for cache in (ce._compose_memo, ce._socket_menu_memo, ce._reported_failures, ce._expr_edits):
            cache.clear()
        self.tmp = tempfile.mkdtemp(prefix="combi_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bpy.context.scene.batch_stl_root_dir = self.tmp


# --- Scene fixtures ----------------------------------------------------------------------------------------------------

def make_gn_group(name=GN):
    """Modifier group with one interface input per supported type (plus an unsupported vector):
    Cube(Size, Vertices X = Count), moved along X by the Mode menu (A: 0, B: 10, C: 20)."""
    ng = bpy.data.node_groups.new(name, "GeometryNodeTree")
    ng.is_modifier = True
    itf = ng.interface
    itf.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    itf.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    itf.new_socket("Size", in_out="INPUT", socket_type="NodeSocketFloat").default_value = 1.0
    itf.new_socket("Count", in_out="INPUT", socket_type="NodeSocketInt").default_value = 2
    itf.new_socket("Flag", in_out="INPUT", socket_type="NodeSocketBool")
    itf.new_socket("Word", in_out="INPUT", socket_type="NodeSocketString").default_value = "a"
    itf.new_socket("Mode", in_out="INPUT", socket_type="NodeSocketMenu")
    itf.new_socket("Offset", in_out="INPUT", socket_type="NodeSocketVector")

    gi, go = ng.nodes.new("NodeGroupInput"), ng.nodes.new("NodeGroupOutput")
    cube = ng.nodes.new("GeometryNodeMeshCube"); cube.name = "Cube"
    switch = ng.nodes.new("GeometryNodeMenuSwitch"); switch.name = "Switch"
    switch.data_type = 'FLOAT'
    switch.enum_items.new("C")
    for item, x in zip("ABC", (0.0, 10.0, 20.0)): switch.inputs[item].default_value = x
    combine = ng.nodes.new("ShaderNodeCombineXYZ"); combine.name = "Combine"
    move = ng.nodes.new("GeometryNodeTransform"); move.name = "Move"
    merge = ng.nodes.new("GeometryNodeMergeByDistance"); merge.name = "Merge"
    links = ng.links
    links.new(gi.outputs["Size"], cube.inputs["Size"])
    links.new(gi.outputs["Count"], cube.inputs["Vertices X"])
    links.new(gi.outputs["Mode"], switch.inputs["Menu"])
    links.new(switch.outputs[0], combine.inputs["X"])
    links.new(combine.outputs[0], move.inputs["Translation"])
    links.new(cube.outputs["Mesh"], move.inputs["Geometry"])
    links.new(move.outputs["Geometry"], merge.inputs["Geometry"])
    links.new(merge.outputs["Geometry"], go.inputs["Geometry"])
    return ng


def make_instancing_group(name="Inst", count=10):
    """Modifier group whose output is only instances: `count` cubes on a mesh line."""
    ng = bpy.data.node_groups.new(name, "GeometryNodeTree")
    ng.is_modifier = True
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    line, cube = ng.nodes.new("GeometryNodeMeshLine"), ng.nodes.new("GeometryNodeMeshCube")
    line.inputs["Count"].default_value = count
    on_points, go = ng.nodes.new("GeometryNodeInstanceOnPoints"), ng.nodes.new("NodeGroupOutput")
    ng.links.new(line.outputs["Mesh"], on_points.inputs["Points"])
    ng.links.new(cube.outputs["Mesh"], on_points.inputs["Instance"])
    ng.links.new(on_points.outputs["Instances"], go.inputs["Geometry"])
    return ng


def get_collection(name, parent=None):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(col)
    return col


def cube_mesh(name="cube", size=1.0):
    h = size / 2
    verts = [(x, y, z) for x in (-h, h) for y in (-h, h) for z in (-h, h)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    return me


def make_object(name="Box", collection="Parts", group=None, mesh=None):
    ob = bpy.data.objects.new(name, mesh if mesh is not None else bpy.data.meshes.new(name))
    get_collection(collection).objects.link(ob)
    if group is not None:
        ob.modifiers.new("GN", "NODES").node_group = group
    return ob


def make_preset(name="P", collections=("Parts",), prefix="", scene=None):
    scene = scene or bpy.context.scene
    preset = scene.batch_stl_presets.add()
    preset.name, preset.preset_prefix = name, prefix
    for col_name in collections:
        entry = preset.collections.add()
        entry.collection_name = col_name
        ce.sync_collection_objects(entry)
    ce.ensure_preset_uids(scene)
    scene.batch_stl_preset_index = len(scene.batch_stl_presets) - 1
    return preset


def add_group(nodegroups, group=GN, naming=""):
    with ce.raw_edits():
        ng = nodegroups.add()
        ng.group_name = group
    ng.naming = naming
    return ng


def add_node(ng, node=MODIFIER, naming=""):
    with ce.raw_edits():
        n = ng.nodes.add()
        n.name = node
    n.naming = naming
    return n


def add_input(node, name, override_type, *exprs):
    """An input whose values are typed into their value fields, like the override table does."""
    with ce.raw_edits():
        inp = node.inputs.add()
        inp.name, inp.override_type = name, override_type
    for text in exprs:
        val = ce.add_value(inp.values)
        val.expr = text
        if ce.value_fields(val, override_type) is None:
            raise AssertionError(f"value field rejected {text!r} ({override_type})")
    return inp


def override(nodegroups, input_name, override_type, *exprs, group=GN, node=MODIFIER, group_naming="", node_naming=""):
    """One node group > node > input block with values, in one call."""
    ng = next((g for g in nodegroups if g.group_name == group), None) or add_group(nodegroups, group, group_naming)
    n = next((n for n in ng.nodes if n.name == node), None) or add_node(ng, node, node_naming)
    return add_input(n, input_name, override_type, *exprs)


# --- Export planning and output ----------------------------------------------------------------------------------------

def planned_paths(preset=None, scene=None):
    """'folder/.../file.stl' of every file the preset writes, in export order (duplicates kept)."""
    scene = scene or bpy.context.scene
    preset = preset or ce.get_active_preset(scene)
    for c in preset.collections: ce.sync_collection_objects(c)
    live = ce.live_collection_names(bpy.context.view_layer)
    paths = []
    with ce.cached_menu_items():
        for ovrs, items in ce.group_by_signature(ce.iter_export_objects(scene, preset, live)).values():
            for _combo, tags, dirs in ce.generate_named_combinations(ovrs):
                for c, obj_prop, bl_obj in items:
                    parts, filename = ce.export_file_parts(preset, c, obj_prop, bl_obj, tags, dirs)
                    paths.append("/".join(parts + [filename]))
    return paths


def written_files(root):
    """Sorted 'folder/.../file.stl' of the files below `root`."""
    out = []
    for folder, _dirs, files in os.walk(root):
        rel = os.path.relpath(folder, root)
        out += [f if rel == "." else f"{rel}/{f}".replace(os.sep, "/") for f in files]
    return sorted(out)


def read_stl(path):
    """(header, records) of a binary STL; checks the size matches the triangle count."""
    with open(path, "rb") as f: data = f.read()
    count = struct.unpack("<I", data[80:84])[0]
    assert len(data) == 84 + 50 * count, f"{path}: {len(data)} bytes for {count} triangles"
    return data[:80], np.frombuffer(data[84:], dtype=ce.STL_DTYPE)


def stl_bounds(path):
    _header, recs = read_stl(path)
    pts = np.concatenate([recs["v0"], recs["v1"], recs["v2"]])
    return pts.min(axis=0), pts.max(axis=0)


class ContextProxy:
    """bpy.context with a window manager whose modal plumbing (timers, modal handlers) is faked: background Blender
    cannot invoke operators or run modal loops, but the export operator's own logic runs unchanged."""

    def __init__(self):
        self.window_manager = _WindowManagerProxy(bpy.context.window_manager)

    def __getattr__(self, key): return getattr(bpy.context, key)


class _WindowManagerProxy:
    def __init__(self, wm): object.__setattr__(self, "_wm", wm)
    def __getattr__(self, key): return getattr(self._wm, key)
    def __setattr__(self, key, value): setattr(self._wm, key, value)
    def event_timer_add(self, *_args, **_kwargs): return object()
    def event_timer_remove(self, _timer): pass
    def modal_handler_add(self, _op): return True


class ExportRun:
    """Runs EXPORT_OT_batch_stl_multi's invoke() and modal() (worker included) outside Blender's operator system."""
    _op = ce.EXPORT_OT_batch_stl_multi
    invoke, modal, cleanup, cancel = _op.invoke, _op.modal, _op.cleanup, _op.cancel
    _preset, _job, _drain_output = _op._preset, _op._job, _op._drain_output

    def __init__(self, preset_index=-1):
        self.preset_index = preset_index
        self.reports = []

    def report(self, level, message): self.reports.append((next(iter(level)), message))

    def __call__(self, timeout=180.0):
        context = ContextProxy()
        result = self.invoke(context, None)
        deadline = time.monotonic() + timeout
        timer = type("Event", (), {"type": 'TIMER'})()
        while result == {'RUNNING_MODAL'} or result == {'PASS_THROUGH'}:
            if time.monotonic() > deadline:
                self.cleanup(context)
                raise AssertionError("export worker timed out")
            time.sleep(0.05)
            result = self.modal(context, timer)
        return result

    def log(self):
        job = self._job()
        return [line.text for line in job.console_logs] if job else []


def run_export(preset_index=-1, timeout=180.0):
    run = ExportRun(preset_index)
    result = run(timeout)
    return result, run


def write_job(path, preset, root, preset_index=0):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"preset_index": preset_index, "preset_uid": preset.uid, "root_dir": root, "start_time": time.time(),
                   "skip_direct": False}, f)
