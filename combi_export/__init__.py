"""
Combi Export (formerly Fast Batch STL Exporter)
Architecture: Single-File Monolithic (Optimized for Agentic Environments)
Data Hierarchy: Global > Preset > Collection > Object > NodeGroup > Node > Input > Value
"""

# ==============================================================================
# === MODULE IMPORTS ===
# ==============================================================================
import os
import json
import time
import contextlib
import functools
import itertools
import math
import re
import uuid
import threading
import queue
import subprocess
import tempfile
import sys
import struct
import shutil
import traceback
import numpy as np

import bpy
from bpy_extras.io_utils import ExportHelper, ImportHelper
from bpy.app.handlers import persistent

# ==============================================================================
# === [ 1. GLOBALS & STATE ] ===
# ==============================================================================

ICONS = {
    'PRESET': 'PRESET', 'COLLECTION': 'OUTLINER_COLLECTION', 'OBJECT': 'OBJECT_DATA',
    'SWEEP': 'CON_ROTLIMIT', 'GLOBAL': 'WORLD', 'DIR': 'FILE_FOLDER', 'TAG': 'TAG',
    'ADD': 'ADD', 'DEL': 'TRASH', 'UP': 'TRIA_UP', 'DOWN': 'TRIA_DOWN',
    'COPY': 'COPYDOWN', 'PASTE': 'PASTEDOWN', 'CANCEL': 'CANCEL', 'EXPORT': 'EXPORT',
    'IMPORT': 'IMPORT', 'INFO': 'INFO', 'CONSOLE': 'CONSOLE', 'CHECK_ON': 'CHECKBOX_HLT',
    'CHECK_OFF': 'CHECKBOX_DEHLT', 'OVR': 'DECORATE_OVERRIDE', 'NODE': 'NODETREE',
    'TIME': 'TIME', 'MODIFIER': 'MODIFIER', 'TREE': 'OUTLINER_OB_EMPTY', 'ERROR': 'ERROR',
    'RIGHT': 'TRIA_RIGHT', 'BLANK': 'BLANK1', 'FILE': 'FILE_3D',
    'EXPAND_ALL': 'FULLSCREEN_ENTER', 'COLLAPSE_ALL': 'FULLSCREEN_EXIT', 'EXPAND_LAST': 'TRIA_DOWN_BAR',
    'MERGE': 'AUTOMERGE_ON'
}

SUPPORTED_OBJECT_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT"}

STL_DTYPE = np.dtype([
    ('normals', np.float32, (3,)), ('v0', np.float32, (3,)),
    ('v1', np.float32, (3,)), ('v2', np.float32, (3,)), ('attr', np.uint16)
])

STL_HEADER = b'Batch STL Fast Export' + b'\x00' * 59

MAX_SWEEP_STEPS = 1000
# Counting variants by enumeration (only needed when values apply to some branches only) stops above this, and so does
# the tree preview. The export's collision check has no limit.
COUNT_LIMIT = 5000
# Temp folders of a crashed export are removed at startup once they are this old; a younger one may belong to an
# export running in another Blender instance.
STALE_TEMP_AGE = 24 * 3600

_clipboard = {"preset": None, "collection": None, "nodegroup": None}

_ui_cache = {
    "is_dirty": True,
    "live": set(),
    "stats": {"global": {"presets": 0, "cols": 0, "objs": 0, "exp": 0}, "presets": {}, "cols": {}},
    "tree": ({}, set()),
    "preset_metrics": {},
}

def mark_dirty(self=None, context=None):
    _ui_cache["is_dirty"] = True

# Blender gives every UI-edited property an undo step except search fields (prop_search / StringProperty(search=...)):
# those buttons are created without UI_BUT_UNDO, so picking an item from the dropdown leaves no history entry.
# Their update callbacks push the step themselves. Operators record their own step ('UNDO' in bl_options), so
# property writes made while an operator (or an outer update callback) runs must not push a second one.
_undo_suppress_depth = 0

def push_search_undo(label):
    if _undo_suppress_depth: return
    try: bpy.ops.ed.undo_push(message=label)
    except RuntimeError: pass

def search_field_update(label):
    """Update callback for a search field: refresh the UI cache and record an undo step."""
    def update(self, context):
        mark_dirty()
        push_search_undo(label)
    return update

def inside_operator(execute):
    """Decorator for operator execute(): property updates fired by the operator itself do not push undo steps."""
    @functools.wraps(execute)
    def wrapper(self, context):
        global _undo_suppress_depth
        _undo_suppress_depth += 1
        try: return execute(self, context)
        finally: _undo_suppress_depth -= 1
    return wrapper

# Override name/value callbacks react to edits made by the user: they revert duplicates and refill dependent data
# (group -> node -> input -> value). Code that writes already-consistent data (paste, JSON import) runs inside
# raw_edits(), where the callbacks only accept the written value as the new "previous" one and do nothing else.
_raw_edit_depth = 0

@contextlib.contextmanager
def raw_edits():
    global _raw_edit_depth
    _raw_edit_depth += 1
    try: yield
    finally: _raw_edit_depth -= 1

# Returned by an edit callback that removed its own item (field cleared to delete it): `self` is then gone and
# must not be read again.
DELETED = object()

def edit_callback(label, prop, prev_prop):
    """Decorator for the update callback of `prop`: skips the logic during raw_edits(), and records a single undo
    step (when `label` is set) for the outermost callback of a cascade instead of one per nested callback."""
    def decorate(update):
        @functools.wraps(update)
        def wrapper(self, context):
            global _undo_suppress_depth
            if _raw_edit_depth:
                setattr(self, prev_prop, getattr(self, prop))
                mark_dirty()
                return
            old_val = getattr(self, prev_prop)
            _undo_suppress_depth += 1
            try: result = update(self, context)
            finally: _undo_suppress_depth -= 1
            mark_dirty()
            if not label: return
            if result is DELETED:
                push_search_undo(label)
                return
            try: new_val = getattr(self, prop)
            except ReferenceError: return
            if new_val != old_val: push_search_undo(label)
        return wrapper
    return decorate

_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9"}

def sanitize_name(text):
    """Make text safe to use inside a single file or folder name on every OS."""
    s = _INVALID_NAME_CHARS.sub("_", str(text))
    if s.split('.')[0].lower() in _WINDOWS_RESERVED:
        s = f"_{s}"
    return s

def split_path_parts(path_text):
    """Split a user-typed sub-path into safe folder names; empty, '.' and '..' parts are dropped."""
    if not path_text: return []
    parts = (sanitize_name(p).strip(" .") for p in re.split(r"[\\/]", path_text))
    return [p for p in parts if p]

def clash_key(path):
    """Key for output-path collision checks; Windows and macOS file systems are case-insensitive by default."""
    norm = os.path.normpath(path)
    return norm.casefold() if sys.platform in ("win32", "darwin") else norm

def redraw_sidebars(context=None):
    """Redraw only the 3D viewport sidebars (where this add-on lives), not the whole viewports."""
    wm = getattr(context or bpy.context, "window_manager", None)
    if not wm: return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'UI': region.tag_redraw()

# ==============================================================================
# === [ 2. CORE LOGIC & ENGINE ] ===
# ==============================================================================

def clean_node_name(name):
    if not name: return ""
    return name.split(" [")[0].strip()

def is_group_used_by_object(group, obj, override_target):
    if not group: return False
    for mod in obj.modifiers:
        if mod.type == 'NODES' and mod.node_group:
            if override_target == 'MODIFIER':
                if mod.node_group == group: return True
            else:
                def check_tree(tree, seen=None):
                    if not tree: return False
                    if seen is None: seen = set()
                    if tree in seen: return False
                    seen.add(tree)
                    if tree == group: return True
                    for node in tree.nodes:
                        if getattr(node, "node_tree", None):
                            if check_tree(node.node_tree, seen): return True
                    return False
                if check_tree(mod.node_group): return True
    return False

def filter_overrides_for_object(overrides, bl_obj):
    return [o for o in overrides if is_group_used_by_object(o.parent_group_ptr, bl_obj, getattr(o, "override_target", "NODE"))]

def live_collection_names(view_layer):
    """Names of the collections Blender evaluates in `view_layer`: not excluded and not disabled in viewports, and
    neither are their parents. A collection linked in several places is live if one occurrence is. Objects outside
    them are not evaluated, so exporting them would write the mesh without its modifiers."""
    live = set()
    def traverse(layer_collection):
        if layer_collection.exclude or layer_collection.collection.hide_viewport: return
        live.add(layer_collection.collection.name)
        for child in layer_collection.children: traverse(child)
    if view_layer: traverse(view_layer.layer_collection)
    return live

def is_object_live(obj, live):
    """Whether Blender evaluates `obj` (see live_collection_names); hiding with the eye icon still evaluates."""
    return not obj.hide_viewport and any(c.name in live for c in obj.users_collection)

def find_interface_input(node_group, socket_name):
    """The node group's interface input socket named `socket_name` (older API: node_group.inputs), or None."""
    if not node_group: return None
    if hasattr(node_group, "interface"):
        for item in node_group.interface.items_tree:
            if getattr(item, "item_type", "") == 'SOCKET' and getattr(item, "in_out", "INPUT") == 'INPUT' and item.name == socket_name:
                return item
    elif hasattr(node_group, "inputs"):
        return node_group.inputs.get(socket_name)
    return None

def get_modifier_socket_identifier(node_group, socket_name):
    item = find_interface_input(node_group, socket_name)
    return item.identifier if item else None

def get_modifier_socket_default(node_group, socket_name):
    return getattr(find_interface_input(node_group, socket_name), "default_value", None)

# Menu items come from scanning node trees, which the panels would otherwise repeat several times per menu value on
# every redraw. Inside cached_menu_items() the results are reused; node trees cannot change during that scope.
_menu_items_cache = None

@contextlib.contextmanager
def cached_menu_items():
    global _menu_items_cache
    if _menu_items_cache is not None:  # nested: the outer scope owns the cache
        yield
        return
    _menu_items_cache = {}
    try: yield
    finally: _menu_items_cache = None

def get_menu_switch_items(node_group, node_name, input_name):
    """Item names of the menu behind a menu socket. The returned list may be shared: do not modify it."""
    if _menu_items_cache is None: return _scan_menu_switch_items(node_group, node_name, input_name)
    key = (getattr(node_group, "name_full", None), node_name, input_name)
    items = _menu_items_cache.get(key)
    if items is None: items = _menu_items_cache[key] = _scan_menu_switch_items(node_group, node_name, input_name)
    return items

def _scan_menu_switch_items(node_group, node_name, input_name):
    if not node_group: return []
    if not node_name or node_name == "<Modifier Interface>":
        items = modifier_menu_items(node_group, input_name)
        return items if items is not None else _linked_menu_switch_items(node_group.nodes, input_name)
    target_n = node_group.nodes.get(clean_node_name(node_name))
    if not target_n: return []
    if target_n.type == 'MENU_SWITCH' and hasattr(target_n, 'enum_items'):
        return [getattr(item, 'identifier', getattr(item, 'name', '')) for item in target_n.enum_items]
    sock = target_n.inputs.get(input_name)
    items = socket_menu_items(target_n, sock) if sock else None
    if items is None and target_n.type == 'GROUP' and getattr(target_n, 'node_tree', None):
        items = _linked_menu_switch_items(target_n.node_tree.nodes, input_name)
    return items or []

def _linked_menu_switch_items(nodes, input_name):
    """Items of a Menu Switch fed directly by the group input `input_name` (no reroutes, no nested groups)."""
    for node in nodes:
        if node.type == 'MENU_SWITCH' and hasattr(node, 'enum_items'):
            for sock in node.inputs:
                for link in sock.links:
                    if link.from_node.type == 'GROUP_INPUT' and link.from_socket.name == input_name:
                        return [getattr(item, 'identifier', getattr(item, 'name', '')) for item in node.enum_items]
    return []

def modifier_menu_items(node_group, input_name):
    """Items of a menu on the modifier interface, read from a modifier that uses the group: the interface socket does
    not list them, the modifier input does (also through reroutes and nested groups). None when no object uses it."""
    ident = get_modifier_socket_identifier(node_group, input_name)
    if not ident: return None
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            if mod.type == 'NODES' and mod.node_group == node_group:
                prop_input = modifier_input(mod, ident)
                if prop_input is not None:
                    return [e.identifier for e in prop_input.bl_rna.properties["value"].enum_items]
    return None

# Last items found per node menu socket, for draw code (see socket_menu_items).
_socket_menu_memo = {}

def socket_menu_items(node, sock):
    """Items of a node's menu socket (built-in nodes, group nodes). Blender has no API that lists them, but assigning
    an unknown name is rejected, without changing anything, by an error that lists them. Draw code may not write
    data, so it gets the last list found (the UI cache rebuild refreshes it), or None."""
    if sock.type != 'MENU': return None
    key = (node.id_data.name_full, node.name, sock.identifier)
    try: sock.default_value = "\x01"
    except TypeError as e:
        listed = re.search(r"not found in \((.*)\)\s*$", str(e))
        if listed:
            items = _socket_menu_memo[key] = re.findall(r"'([^']*)'", listed.group(1))
            return items
    except (AttributeError, RuntimeError): pass  # drawing, or linked (read-only) data
    return _socket_menu_memo.get(key)

def modifier_input(mod, ident):
    """The input struct of a Geometry Nodes modifier for a group socket identifier (`.value` holds the value)."""
    inputs = getattr(getattr(mod, "properties", None), "inputs", None)
    return getattr(inputs, ident, None) if inputs is not None else None

def get_modifier_input(mod, ident):
    prop_input = modifier_input(mod, ident)
    return prop_input.value if prop_input is not None else None

def set_modifier_input(mod, ident, value):
    prop_input = modifier_input(mod, ident)
    if prop_input is not None: prop_input.value = value

def get_input_value(inp):
    if inp.override_type == 'BOOLEAN': return inp.value_menu.lower() == 'true'
    elif inp.override_type == 'INT':
        try: return int(inp.value_string)
        except ValueError: return 0
    elif inp.override_type == 'FLOAT':
        try: return float(inp.value_string)
        except ValueError: return 0.0
    elif inp.override_type == 'STRING': return inp.value_string
    elif inp.override_type == 'MENU': return inp.value_menu
    return None

# More specific levels win: an Object override replaces the Collection/Preset/Global value of the same socket.
LEVEL_RANK = {"NONE": -1, "GLOBAL": 0, "PRESET": 1, "COLLECTION": 2, "OBJECT": 3}

def override_param_key(ovr, input_name):
    """Identity of one overridden socket, independent of the hierarchy level that defines it."""
    pg_name = ovr.parent_group_ptr.name if ovr.parent_group_ptr else ""
    node = clean_node_name(ovr.node_name) if ovr.override_target == 'NODE' else ""
    return (ovr.override_target, pg_name, node, input_name)

def resolve_overrides(overrides):
    """Drop inherited values of a socket whenever a more specific level also defines that socket."""
    best_rank = {}
    for ovr in overrides:
        rank = LEVEL_RANK.get(ovr.level, -1)
        for inp in ovr.inputs:
            key = override_param_key(ovr, inp.input_name)
            best_rank[key] = max(best_rank.get(key, rank), rank)

    resolved = []
    for ovr in overrides:
        rank = LEVEL_RANK.get(ovr.level, -1)
        kept = [inp for inp in ovr.inputs if best_rank[override_param_key(ovr, inp.input_name)] == rank]
        if kept or not ovr.inputs: resolved.append(MockOverride(ovr.override_target, ovr.parent_group_ptr, ovr.node_name, kept, ovr.level, getattr(ovr, "ng_sub_path", ""), getattr(ovr, "node_sub_path", ""), getattr(ovr, "ng_tag", ""), getattr(ovr, "node_tag", "")))
    return resolved

def get_override_signature(overrides):
    """Hashable fingerprint of everything that decides which variants an object gets and how they are named."""
    sig = []
    for ovr in overrides:
        inputs_sig = tuple(
            (inp.input_name, inp.override_type, get_input_value(inp), inp.use_sweep, inp.sweep_range,
             inp.sweep_start, inp.sweep_step, inp.sweep_count,
             inp.use_tag, inp.tag, inp.use_dir, inp.dir_tag)
            for inp in ovr.inputs
        )
        block_sig = tuple(getattr(ovr, k, "") for k in ("ng_sub_path", "node_sub_path", "ng_tag", "node_tag"))
        sig.append((ovr.level, override_param_key(ovr, ""), block_sig, inputs_sig))
    return tuple(sig)

class MockInput:
    def __init__(self, base_inp, override_val, is_temp=False):
        self.input_name = getattr(base_inp, 'input_name', getattr(base_inp, 'name', ''))
        self.name = self.input_name
        self.override_type = base_inp.override_type
        source = override_val if is_temp else base_inp
        self.use_tag = getattr(source, "use_tag", False)
        self.tag = getattr(source, "tag", "")
        self.use_dir = getattr(source, "use_dir", False)
        self.dir_tag = getattr(source, "dir_tag", "")
        self.use_sweep = getattr(source, "use_sweep", False)
        self.sweep_range = getattr(source, "sweep_range", "")
        self.sweep_start = getattr(source, "sweep_start", "0")
        self.sweep_step = getattr(source, "sweep_step", "1")
        self.sweep_count = getattr(source, "sweep_count", "2")
        self._val = override_val
        self._is_temp = is_temp

    @property
    def value_string(self): return self._val.value_string if self._is_temp else str(self._val)
    @property
    def value_menu(self): return self._val.value_menu if self._is_temp else str(self._val)

class MockOverride:
    def __init__(self, target, ptr, node_name, inputs, level="NONE", ng_sub_path="", node_sub_path="", ng_tag="", node_tag=""):
        self.override_target = target
        self.parent_group_ptr = ptr
        self.node_name = node_name
        self.inputs = inputs
        self.level = level
        self.ng_sub_path = ng_sub_path
        self.node_sub_path = node_sub_path
        self.ng_tag = ng_tag
        self.node_tag = node_tag

def get_sorted_values(ng_ptr, node_obj, inp, values):
    menu_order = {}
    if inp and getattr(inp, "override_type", "") == 'MENU':
        items = get_menu_switch_items(ng_ptr, getattr(node_obj, "name", ""), getattr(inp, "name", "")) if ng_ptr else []
        menu_order = {val: idx for idx, val in enumerate(items)}

    def value_sort_key(x):
        idx, val = x
        if val is None:
            return (float('-inf'), idx)
        if getattr(val, "use_sweep", False):
            if inp.override_type in ('FLOAT', 'INT'):
                try: return (float(val.sweep_start), idx)
                except ValueError: return (float('-inf'), idx)
            return ("", idx) if getattr(inp, "override_type", "") == 'STRING' else (0, idx)
        else:
            if inp.override_type in ('FLOAT', 'INT'):
                try:
                    return (float(val.value_string), idx)
                except ValueError:
                    return (float('-inf'), idx)
            if inp.override_type == 'STRING': return (val.value_string, idx)
            if inp.override_type in ('MENU', 'BOOLEAN'): return (menu_order.get(val.value_menu, float('inf')), idx)
            return (0, idx)

    indexed_values = list(enumerate(values))
    indexed_values.sort(key=value_sort_key)
    return indexed_values

def get_flat_overrides(nodegroups, level="NONE"):
    overrides = []
    if not nodegroups: return overrides
    for ng in nodegroups:
        ng_ptr = bpy.data.node_groups.get(ng.group_name)
        if not ng_ptr: continue
        for node in ng.nodes:
            target = 'MODIFIER' if not node.name or node.name == "<Modifier Interface>" else 'NODE'

            temp_inputs = []
            for inp in node.inputs:
                for _, val in get_sorted_values(ng_ptr, node, inp, inp.values):
                    temp_inputs.append(MockInput(inp, val, is_temp=True))

            block_fields = (getattr(ng, "sub_path", ""), getattr(node, "sub_path", ""), getattr(ng, "tag", ""), getattr(node, "tag", ""))
            if temp_inputs or any(block_fields):
                overrides.append(MockOverride(target, ng_ptr, node.name, temp_inputs, level, *block_fields))
    return overrides

def parse_sweep_values(ovr, inp):
    if inp.override_type == 'BOOLEAN':
        return [True, False]
    elif inp.override_type == 'STRING':
        if not inp.sweep_range:
            return [""]
        res = [s.strip() for s in inp.sweep_range.split(',') if s.strip()]
        return res if res else [""]
    elif inp.override_type in ['INT', 'FLOAT']:
        vals = []
        is_float = (inp.override_type == 'FLOAT')
        start_str = getattr(inp, "sweep_start", "0")
        step_str = getattr(inp, "sweep_step", "1")
        count_str = getattr(inp, "sweep_count", "2")

        try: start = float(start_str) if is_float else int(start_str)
        except ValueError: start = 0.0 if is_float else 0

        try: step = float(step_str) if is_float else int(step_str)
        except ValueError: step = 1.0 if is_float else 1

        try: count = int(count_str)
        except ValueError: count = 2
        count = min(count, MAX_SWEEP_STEPS)  # validation flags larger counts; this only guards imported data

        if count <= 0:
            vals.append(round(start, 8) if is_float else int(start))
        else:
            for i in range(count):
                v = round(start + i * step, 8) if is_float else int(start + i * step)
                vals.append(v)
        return vals
    elif inp.override_type == 'MENU':
        items = get_menu_switch_items(ovr.parent_group_ptr, ovr.node_name, inp.input_name)
        return items if items else [""]
    return []

def _build_override_pools(overrides):
    grouped_inputs = {}
    for ovr in overrides:
        for inp in ovr.inputs:
            grouped_inputs.setdefault(override_param_key(ovr, inp.input_name), []).append((ovr, inp))

    pools = []
    for pairs in grouped_inputs.values():
        value_groups = {}
        for ovr, inp in pairs:
            vals_to_process = parse_sweep_values(ovr, inp) if getattr(inp, "use_sweep", False) else [get_input_value(inp)]
            for val in vals_to_process:
                value_groups.setdefault(val, []).append((ovr, MockInput(inp, val)))
        if value_groups: pools.append(list(value_groups.values()))
    return pools

def count_override_combinations(overrides, cache=None, limit=COUNT_LIMIT):
    """Number of variants. When no value can fall outside a branch, every combination of values is a variant and the
    count is the product of the parameter pool sizes. Otherwise the variants are enumerated, which stops above `limit`
    (the result is then limit + 1; None counts them all). With `cache` (a dict), objects with the same overrides are
    counted once."""
    sig = None
    if cache is not None:
        sig = get_override_signature(overrides)
        if sig in cache: return cache[sig]
    known_before = _known_branch_dirs(overrides)
    if _has_branch_anchors(overrides, known_before):
        count = 0
        for _ in generate_named_combinations(overrides, known_before):
            count += 1
            if limit is not None and count > limit: break
    else:
        count = math.prod(len(pool) for pool in _build_override_pools(overrides))
    if cache is not None: cache[sig] = count
    return count

def _has_branch_anchors(overrides, known_before):
    """Whether a block or an explicitly named value names a branch name created above it. Only then can a value be
    inactive in some branches (see walk_override_branch)."""
    for ovr in overrides:
        known = known_before.get(id(ovr), frozenset())
        block_dirs, block_tags = _block_name_parts(ovr)
        if any(("dir", n) in known for n in block_dirs) or any(("tag", n) in known for n in block_tags): return True
        for inp in ovr.inputs:
            known_inp = known_before.get((id(ovr), inp.input_name), known)
            for kind, anchors in value_field_anchors(inp).items():
                if kind == "tag": anchors = segment_tags(anchors, lambda n: ("tag", n) in known_inp)
                if any((kind, n) in known_inp for n in anchors): return True
    return False

# The walker runs once or more per variant, so what it derives from an override or a pool value (which never change
# during a generation) is cached on the MockOverride / MockInput.
def _cached(obj, attr, compute):
    value = obj.__dict__.get(attr)
    if value is None: value = obj.__dict__[attr] = compute()
    return value

def _block_name_parts(ovr):
    """(folder names, filename tokens) of an override's node group and node sub-folders and tags."""
    return _cached(ovr, "_block_parts", lambda: (
        split_path_parts(ovr.ng_sub_path) + split_path_parts(ovr.node_sub_path),
        split_tag_parts(ovr.ng_tag) + split_tag_parts(ovr.node_tag)))

def _input_names(ovr):
    return _cached(ovr, "_input_names", lambda: list(dict.fromkeys(i.input_name for i in ovr.inputs)))

def _param_key(ovr, input_name):
    keys = _cached(ovr, "_param_keys", dict)
    key = keys.get(input_name)
    if key is None: key = keys[input_name] = override_param_key(ovr, input_name)
    return key

def _value_name_parts(inp):
    """{kind: (anchors, names)} one pool value contributes (see value_name_parts)."""
    return _cached(inp, "_name_parts", lambda: value_parts(inp, get_input_value(inp)))

def value_name_parts(val, field, kind):
    """(anchors, names) the folder field (kind "dir") or filename-tag field (kind "tag") of a value gives.

    - Blank: the value ('15').
    - 'name' replaces the value.
    - Folder 'name\\' (or 'name/') and tag 'name_': `name`, then the value ('name/15', 'Box_name_15').
    '/' separates several names: 'C/2\\' gives 'C/2/15', 'C/2_' gives 'Box_C_2_15'. Only the last character is a mark,
    so a tag name may contain '_' itself ('x__' is the token 'x_', then the value).
    `anchors` are the names typed in the field: one that is already in the branch merges into it (see
    _place_dir_parts). `names` come from the value and are always new: two independent inputs that happen to share a
    value (1, True, a menu item) must multiply, not merge into each other."""
    mark = "\\/" if kind == "dir" else "_"
    then_value = not field or field[-1] in mark
    typed = field[:-1] if field and then_value else field
    anchors = split_path_parts(typed) if kind == "dir" else split_value_tag_parts(typed)
    if not then_value: return anchors, []
    val_str = f"{val:.10g}" if isinstance(val, float) else str(val).replace(" ", "_")
    name = sanitize_name(val_str)
    if kind == "dir": name = name.strip(" .")
    return anchors, [name] if name else []

def split_value_tag_parts(text):
    """Split a value tag field into filename tokens: '/' separates tokens. Unlike split_tag_parts, '_' at the edges is
    kept, so a token like 'x_' (made by the value 'x_') can be named."""
    if not text: return []
    parts = (sanitize_name(p).strip(" ") for p in re.split(r"[\\/]", text))
    return [p for p in parts if p]

def segment_tag(name, is_known):
    """Split a typed token at '_' into a chain of tokens: 'B_2_X' -> ['B', '2', 'X'], so a tag merges into branch B,
    then into its branch 2, then adds X. Longest known tokens win: with a token 'a_b' upstream, 'a_b_X' is
    ['a_b', 'X'], and 'x_' (field 'x__') stays one token when it is known."""
    pieces = name.split("_")
    out, i = [], 0
    while i < len(pieces):
        j = len(pieces)
        while j > i + 1 and not is_known("_".join(pieces[i:j])): j -= 1
        token = "_".join(pieces[i:j])
        if token: out.append(token)
        i = j
    return out

def segment_tags(parts, is_known):
    return [t for p in parts for t in segment_tag(p, is_known)]

def value_parts(inp, val):
    """{kind: (anchors, names)} of one value step, honouring the folder / tag toggles."""
    return {kind: value_name_parts(val, getattr(inp, field), kind) if getattr(inp, use) else ([], [])
            for kind, use, field in (("dir", "use_dir", "dir_tag"), ("tag", "use_tag", "tag"))}

def value_field_anchors(inp):
    """{kind: anchors} of a value. Anchors only depend on the fields, never on the value itself."""
    return {kind: anchors for kind, (anchors, _names) in value_parts(inp, "").items()}

def value_branch_names(ovr, inp, kind, known=frozenset()):
    """Folder names (kind "dir") or filename tokens (kind "tag") one override value can generate (every sweep step
    included), anchors first, in order. Tag chains are split against the names `known` before the value."""
    vals = parse_sweep_values(ovr, inp) if getattr(inp, "use_sweep", False) else [get_input_value(inp)]
    names = []
    for val in vals:
        anchors, new = value_parts(inp, val)[kind]
        if kind == "tag": anchors = segment_tags(anchors, lambda n: (kind, n) in known)
        names.extend(n for n in anchors + new if n not in names)
    return names

def split_tag_parts(text):
    """Split a literal tag field into filename tokens: '/' separates tokens, edge spaces and '_' are dropped."""
    if not text: return []
    parts = (sanitize_name(p).strip(" _") for p in re.split(r"[\\/]", text))
    return [p for p in parts if p]

def _known_branch_dirs(overrides):
    """Branch names that values *earlier* in the stack can generate, as ("dir", folder) / ("tag", token) keys.

    Keyed by id(ovr) for the override's own sub-paths and tags and by (id(ovr), input_name) for its values.
    A sub-path, tag or value naming one of these is an anchor into an existing branch, not a new folder / token."""
    known_before = {}
    known = set()
    for ovr in overrides:
        known_before[id(ovr)] = frozenset(known)
        for input_name in dict.fromkeys(getattr(i, "input_name", "") for i in ovr.inputs):
            known_inp = known_before[(id(ovr), input_name)] = frozenset(known)
            for inp in ovr.inputs:
                if getattr(inp, "input_name", "") == input_name:
                    known.update((kind, n) for kind in SCOPE_KINDS for n in value_branch_names(ovr, inp, kind, known_inp))
    return known_before

def _place_dir_parts(parts, branch, known, kind="dir"):
    """Resolve requested folder names (kind "dir") or filename tokens (kind "tag") against the ones this
    permutation branch already has.

    - A name already in the branch (any ancestor / any earlier token, not only the last one) merges into it;
      several anchors ("C/2") must appear in that order.
    - A token is a chain split at '_' (segment_tag): 'B_2' merges into B, then into 2; tokens already in the branch
      (else known upstream) keep their '_'.
    - A branch name (generated by an earlier value) missing from this branch means the override belongs to another
      branch: returns None (inactive here).
    - Any other name is new: a folder below the branch's deepest folder, or a token at the end of the filename.
    Returns the list of new names to append."""
    if kind == "tag":
        # Read the chain the way this branch spells it; only a name it cannot place falls back to the known names
        # (which then decide whether the override belongs to another branch).
        local = segment_tags(parts, lambda n: n in branch)
        parts = local if all(n in branch for n in local) else segment_tags(parts, lambda n: n in branch or (kind, n) in known)
    new_parts = []
    search_from = 0
    for p in parts:
        try:
            search_from = branch.index(p, search_from) + 1
            continue
        except ValueError:
            pass
        if (kind, p) in known: return None
        new_parts.append(p)
    return new_parts

def walk_override_branch(overrides, combo, known_before=None):
    """Walk overrides top-to-bottom for one (possibly partial) combo, tracking the branch's folders and filename
    tokens.

    Returns (active_input_ids, tags_by_level, paths_by_level). Both combination generation and export naming use this
    walker, so they always agree on which overrides apply to which branch."""
    if known_before is None: known_before = _known_branch_dirs(overrides)
    chosen = {}
    for ovr, inp in combo:
        chosen.setdefault(id(ovr), {})[getattr(inp, "input_name", "")] = inp

    dir_branch, tag_branch = [], []
    paths_by_level = {"GLOBAL": [], "PRESET": [], "COLLECTION": [], "OBJECT": [], "NONE": []}
    tags_by_level = {"GLOBAL": [], "PRESET": [], "COLLECTION": [], "OBJECT": [], "NONE": []}
    active = set()
    processed_params = set()

    for ovr in overrides:
        level = getattr(ovr, "level", "NONE")
        known = known_before.get(id(ovr), frozenset())

        block_dirs, block_tags = _block_name_parts(ovr)
        new_dirs = _place_dir_parts(block_dirs, dir_branch, known, "dir")
        new_tags = _place_dir_parts(block_tags, tag_branch, known, "tag")
        if new_dirs is None or new_tags is None: continue
        dir_branch.extend(new_dirs); paths_by_level[level].extend(new_dirs)
        tag_branch.extend(new_tags); tags_by_level[level].extend(new_tags)

        ovr_chosen = chosen.get(id(ovr))
        if not ovr_chosen: continue
        for input_name in _input_names(ovr):
            inp = ovr_chosen.get(input_name)
            if inp is None: continue
            known_inp = known_before.get((id(ovr), input_name), known)
            parts = _value_name_parts(inp)
            # Only the anchors typed in a field are placed; names derived from the value are always new
            new_dirs = _place_dir_parts(parts["dir"][0], dir_branch, known_inp, "dir")
            new_tags = _place_dir_parts(parts["tag"][0], tag_branch, known_inp, "tag")
            if new_dirs is None or new_tags is None: continue
            new_dirs, new_tags = new_dirs + parts["dir"][1], new_tags + parts["tag"][1]
            active.add(id(inp))

            param_key = _param_key(ovr, input_name)
            if param_key in processed_params: continue
            processed_params.add(param_key)
            dir_branch.extend(new_dirs); paths_by_level[level].extend(new_dirs)
            tag_branch.extend(new_tags); tags_by_level[level].extend(new_tags)

    return active, tags_by_level, paths_by_level

def generate_named_combinations(overrides, known_before=None):
    """Yield (combo, tags_by_level, paths_by_level) for every variant. The naming comes from the same walk that decides
    which values are active, so it costs nothing extra."""
    if known_before is None: known_before = _known_branch_dirs(overrides)
    pools = _build_override_pools(overrides)
    if not pools:
        _, tags_by_level, paths_by_level = walk_override_branch(overrides, [], known_before)
        yield [], tags_by_level, paths_by_level
        return
    # Without anchors every value applies to every branch: no pruning walks, and every combination is unique.
    anchors = _has_branch_anchors(overrides, known_before)
    seen = set()

    def recurse(pool_idx, combo):
        if pool_idx == len(pools):
            active, tags_by_level, paths_by_level = walk_override_branch(overrides, combo, known_before)
            if not anchors:
                yield combo, tags_by_level, paths_by_level
                return
            final = [(o, i) for o, i in combo if id(i) in active]
            key = tuple((id(o), getattr(i, "input_name", ""), repr(get_input_value(i))) for o, i in final)
            if key not in seen:
                seen.add(key)
                yield final, tags_by_level, paths_by_level
            return

        any_active = False
        for variation in pools[pool_idx]:
            trial = combo + variation
            if not anchors or any(id(i) in walk_override_branch(overrides, trial, known_before)[0] for _, i in variation):
                any_active = True
                yield from recurse(pool_idx + 1, trial)
        if not any_active:
            # No value of this parameter belongs to the current branch: leave it at its default.
            yield from recurse(pool_idx + 1, combo)

    yield from recurse(0, [])

def reconstruct_overrides_for_combo(combo):
    grouped = {}
    for ovr, inp in combo:
        target_key = (ovr.override_target, ovr.parent_group_ptr, ovr.node_name, getattr(ovr, "level", "NONE"))
        grouped.setdefault(target_key, []).append(inp)
    return [MockOverride(tgt, ptr, name, inputs, lvl) for (tgt, ptr, name, lvl), inputs in grouped.items()]

def capture_baseline_states(overrides, target_objects):
    global_states, mod_states = [], []
    processed_node_sockets, processed_mod_sockets = set(), set()

    for ovr in overrides:
        if ovr.override_target == 'NODE' and ovr.parent_group_ptr and ovr.node_name:
            parent_tree = ovr.parent_group_ptr
            target_node = parent_tree.nodes.get(clean_node_name(ovr.node_name))
            if not target_node: continue
            for inp in ovr.inputs:
                socket = target_node.inputs.get(inp.input_name)
                if not socket: continue
                key = (parent_tree.name, target_node.name, inp.input_name)
                if key not in processed_node_sockets:
                    link_from = socket.links[0].from_socket if socket.is_linked else None
                    default_v = socket.default_value.copy() if hasattr(socket.default_value, "copy") else socket.default_value
                    global_states.append(('SOCKET', socket, default_v, link_from, parent_tree))
                    processed_node_sockets.add(key)

        elif ovr.override_target == 'MODIFIER' and ovr.parent_group_ptr:
            for inp in ovr.inputs:
                ident = get_modifier_socket_identifier(ovr.parent_group_ptr, inp.input_name)
                if not ident: continue
                for obj in target_objects:
                    for mod in obj.modifiers:
                        if mod.type == 'NODES' and mod.node_group == ovr.parent_group_ptr:
                            key = (obj.name, mod.name, ident)
                            orig_val = get_modifier_input(mod, ident)
                            if key not in processed_mod_sockets and orig_val is not None:
                                mod_states.append((mod, ident, orig_val))
                                processed_mod_sockets.add(key)
    return global_states, mod_states

_reported_failures = set()

def report_apply_failure(target, input_name, value, error):
    """Log a value Blender rejects (e.g. a menu item that no longer exists) once instead of exporting silently."""
    msg = f"WARNING: could not set {target} > {input_name} to {value!r} ({error}); the file keeps its current value"
    if msg not in _reported_failures:
        _reported_failures.add(msg)
        print(msg, flush=True)

def apply_overrides(overrides, target_objects):
    trees_to_update, objects_to_update = set(), set()
    for override in overrides:
        if override.override_target == 'NODE' and override.parent_group_ptr and override.node_name:
            parent_tree = override.parent_group_ptr
            target_node = parent_tree.nodes.get(clean_node_name(override.node_name))
            if not target_node: continue
            for inp in override.inputs:
                socket = target_node.inputs.get(inp.input_name)
                if not socket: continue
                if socket.is_linked: parent_tree.links.remove(socket.links[0])
                val = get_input_value(inp)
                if val is not None:
                    try:
                        if socket.default_value != val:
                            socket.default_value = val
                            trees_to_update.add(parent_tree)
                    except (TypeError, ValueError) as e: report_apply_failure(f"{parent_tree.name} > {target_node.name}", inp.input_name, val, e)

        elif override.override_target == 'MODIFIER' and override.parent_group_ptr:
            for inp in override.inputs:
                ident = get_modifier_socket_identifier(override.parent_group_ptr, inp.input_name)
                val = get_input_value(inp)
                if not ident or val is None: continue
                for obj in target_objects:
                    for mod in obj.modifiers:
                        if mod.type == 'NODES' and mod.node_group == override.parent_group_ptr and get_modifier_input(mod, ident) != val:
                            try: set_modifier_input(mod, ident, val)
                            except (TypeError, ValueError) as e:
                                report_apply_failure(f"{obj.name} > {mod.name}", inp.input_name, val, e)
                                continue
                            objects_to_update.add(obj)
    for tree in trees_to_update: tree.update_tag()
    # Setting modifier inputs from Python does not reliably re-evaluate the object, so tag it explicitly.
    for obj in objects_to_update: obj.update_tag()

def override_targets(overrides):
    """Keys of the sockets an override list writes, matching the baseline states revert_overrides() restores."""
    keys = set()
    for ovr in overrides:
        if not ovr.parent_group_ptr: continue
        for inp in ovr.inputs:
            if ovr.override_target == 'NODE':
                keys.add(('NODE', ovr.parent_group_ptr.name, clean_node_name(ovr.node_name), inp.input_name))
            else:
                keys.add(('MODIFIER', ovr.parent_group_ptr.name, get_modifier_socket_identifier(ovr.parent_group_ptr, inp.input_name)))
    return keys

def revert_overrides(global_states, mod_states, target_objects, skip=frozenset()):
    """Restore the baseline values and links; sockets whose override_targets() key is in `skip` are left alone."""
    objects_to_update = set()
    for mod, ident, orig_val in mod_states:
        try:
            if skip and ('MODIFIER', mod.node_group.name, ident) in skip: continue
            if get_modifier_input(mod, ident) != orig_val:
                set_modifier_input(mod, ident, orig_val)
                objects_to_update.add(mod.id_data)
        except Exception: pass
    for obj in objects_to_update:
        try: obj.update_tag()
        except Exception: pass

    trees_to_update = set()
    for state in global_states:
        if state[0] == 'SOCKET':
            _, socket, original_val, link_from, parent_tree = state
            try:
                if skip and ('NODE', parent_tree.name, socket.node.name, socket.name) in skip: continue
                is_diff = (socket.default_value != original_val)
                if hasattr(is_diff, "__iter__"): is_diff = any(is_diff)
                if is_diff:
                    socket.default_value = original_val
                    trees_to_update.add(parent_tree)
                if link_from and not any(l.from_socket == link_from for l in socket.links):
                    parent_tree.links.new(link_from, socket)
                    trees_to_update.add(parent_tree)
            except Exception: pass

    for tree in trees_to_update:
        try: tree.update_tag()
        except Exception: pass

def get_active_preset(scene):
    presets = scene.batch_stl_presets
    idx = scene.batch_stl_preset_index
    return presets[idx] if presets and 0 <= idx < len(presets) else None

def get_active_collection(preset):
    return preset.collections[preset.collection_index] if preset and preset.collections and 0 <= preset.collection_index < len(preset.collections) else None

def get_active_object(collection):
    return collection.objects[collection.object_index] if collection and collection.objects and 0 <= collection.object_index < len(collection.objects) else None

# Export jobs are keyed by the preset's uid and its scene's session_uid: unlike indices, pointers and names, both
# survive undo, reordering, renaming and switching scenes while an export runs.
def ensure_preset_uids(scene):
    """Give every preset of `scene` a unique uid (new presets, older files, duplicated scenes or presets)."""
    seen = set()
    for preset in scene.batch_stl_presets:
        if not preset.uid or preset.uid in seen: preset.uid = uuid.uuid4().hex
        seen.add(preset.uid)

def find_preset(scene_uid, preset_uid):
    for scene in bpy.data.scenes:
        if scene.session_uid == scene_uid:
            return next((p for p in scene.batch_stl_presets if p.uid == preset_uid), None)
    return None

def find_job(scene_uid, preset_uid):
    if not preset_uid: return None
    for job in bpy.context.window_manager.batch_stl_jobs:
        if job.preset_uid == preset_uid and job.scene_uid == scene_uid: return job
    return None

def get_job(preset, create=False):
    """Runtime export state of a preset. Creating requires an operator context, not a draw call."""
    if preset is None or not preset.uid: return None
    scene_uid = preset.id_data.session_uid
    job = find_job(scene_uid, preset.uid)
    if job or not create: return job
    job = bpy.context.window_manager.batch_stl_jobs.add()
    job.preset_uid, job.scene_uid = preset.uid, scene_uid
    return job

def remove_job(scene_uid, preset_uid):
    jobs = bpy.context.window_manager.batch_stl_jobs
    for i, job in enumerate(jobs):
        if job.preset_uid == preset_uid and job.scene_uid == scene_uid:
            jobs.remove(i)
            return

def is_any_exporting():
    return any(job.is_exporting for job in bpy.context.window_manager.batch_stl_jobs)

def log_to_console(job, text):
    if job:
        # UI labels cannot show line breaks: one log entry per line, like the worker output.
        for line in text.split("\n"):
            job.console_logs.add().text = line
        while len(job.console_logs) > 300: job.console_logs.remove(0)
        job.console_index = len(job.console_logs) - 1

def join_name_segments(segments):
    """Concatenate (names, is_fixed) segments of folders or filename tokens. Fixed segments come from plain fields
    (preset prefix, collection / object sub-folder or tag) that the override walker does not see, so a fixed name equal
    to its neighbour collapses into it. Two neighbouring override names stay: the walker already merged what merges,
    and A=1, B=1 must give 1/1, not 1."""
    out, out_fixed = [], []
    for names, fixed in segments:
        for n in names:
            if out and out[-1] == n and (fixed or out_fixed[-1]): continue
            out.append(n); out_fixed.append(fixed)
    return out

def format_export_filename(bl_obj_name, obj_tag, col_use_tag, col_tag, tags_by_level=None):
    """Object name followed by every filename token in hierarchy order:
    Global overrides, Preset overrides, Collection tag, Collection overrides, Object tag, Object overrides."""
    if tags_by_level is None: tags_by_level = {}
    tokens = join_name_segments([
        (tags_by_level.get("GLOBAL", []), False),
        (tags_by_level.get("PRESET", []), False),
        (split_tag_parts(col_tag) if col_use_tag else [], True),
        (tags_by_level.get("COLLECTION", []), False),
        (split_tag_parts(obj_tag), True),
        (tags_by_level.get("OBJECT", []), False),
        (tags_by_level.get("NONE", []), False),
    ])
    safe_name = sanitize_name(bl_obj_name).strip(" .")
    return safe_name + "".join(f"_{t}" for t in tokens) + ".stl"

def build_export_dir_parts(preset_prefix, col_sub_path, obj_sub_path, paths_by_level=None):
    if paths_by_level is None: paths_by_level = {}
    return join_name_segments([
        (paths_by_level.get("GLOBAL", []), False),
        (split_path_parts(preset_prefix), True),
        (paths_by_level.get("PRESET", []), False),
        (split_path_parts(col_sub_path), True),
        (paths_by_level.get("COLLECTION", []), False),
        (split_path_parts(obj_sub_path), True),
        (paths_by_level.get("OBJECT", []), False),
        (paths_by_level.get("NONE", []), False),
    ])

def sync_collection_objects(col_prop, col_ptr=None):
    """Match the object entries of a mapped collection to its objects: drop entries of objects that left, add entries
    for new ones (in the collection's order) and follow renames, so a renamed object keeps its settings."""
    if not col_ptr: col_ptr = bpy.data.collections.get(col_prop.collection_name)
    if not col_ptr: return

    # A list (not a set) keeps new objects in the collection's own order.
    actual_objs = [obj for obj in col_ptr.all_objects if obj.type in SUPPORTED_OBJECT_TYPES]
    by_name = {obj.name: obj for obj in actual_objs}
    by_uid = {obj.session_uid: obj for obj in actual_objs}
    entry_names = {entry.name for entry in col_prop.objects}

    # Entries remember their object's session_uid; an ID pointer would add a user to the object, so deleting the
    # object would only unlink it. An entry whose name is gone follows the uid to the renamed object. Otherwise the name
    # wins: uids change when a file is loaded (restamp_object_uids() refreshes them) and can be stale in appended data.
    orphans = []
    for entry in col_prop.objects:
        obj = by_name.get(entry.name)
        if obj is None:
            obj = by_uid.get(entry.obj_uid) if entry.obj_uid else None
            if obj is None or obj.name in entry_names:
                orphans.append(entry)
                continue
            entry.name = obj.name
            entry_names.add(obj.name)
        if entry.obj_uid != obj.session_uid: entry.obj_uid = obj.session_uid

    # A rename the uid cannot follow (made while the add-on was off): one entry lost its object, which no longer exists,
    # and one object has no entry.
    new_objs = [obj for obj in actual_objs if obj.name not in entry_names]
    if len(orphans) == 1 and len(new_objs) == 1:
        entry, obj = orphans[0], new_objs[0]
        if not entry.obj_uid or not any(o.session_uid == entry.obj_uid for o in bpy.data.objects):
            entry.name, entry.obj_uid = obj.name, obj.session_uid
            entry_names.add(obj.name)

    for i in reversed(range(len(col_prop.objects))):
        if col_prop.objects[i].name not in by_name: col_prop.objects.remove(i)

    for obj in actual_objs:
        if obj.name not in entry_names:
            entry = col_prop.objects.add()
            entry.name = obj.name  # `export` defaults to True; setting it would mark the cache dirty again
            entry.obj_uid = obj.session_uid

def restamp_object_uids(scene):
    """Session uids change when a file is loaded: point the object entries at their objects again, by name."""
    for preset in scene.batch_stl_presets:
        for col in preset.collections:
            col_ptr = bpy.data.collections.get(col.collection_name)
            for entry in col.objects:
                obj = col_ptr.all_objects.get(entry.name) if col_ptr else None
                uid = obj.session_uid if obj else 0
                if entry.obj_uid != uid: entry.obj_uid = uid

# --- EXPORT TARGETS ---
def export_file_parts(preset, c, obj_prop, bl_obj, tags_by_level=None, paths_by_level=None):
    """(folder names below the root, filename) of one exported file."""
    return (build_export_dir_parts(preset.preset_prefix, c.sub_path, obj_prop.sub_path, paths_by_level),
            format_export_filename(obj_prop.export_name, obj_prop.tag, c.use_tag, c.tag, tags_by_level))

def iter_export_objects(scene, preset, live, global_ovrs=None):
    """(collection index, collection entry, object entry, object, resolved overrides) of every object the preset
    exports: in a mapped collection that Blender evaluates (`live`, see live_collection_names), marked for export, and
    evaluated itself. Call sync_collection_objects() first."""
    if global_ovrs is None: global_ovrs = get_flat_overrides(scene.batch_stl_global_nodegroups, "GLOBAL")
    preset_ovrs = global_ovrs + get_flat_overrides(preset.nodegroups, "PRESET")
    for c_idx, c in enumerate(preset.collections):
        c_ptr = bpy.data.collections.get(c.collection_name)
        if not c_ptr or c_ptr.name not in live: continue
        col_ovrs = preset_ovrs + get_flat_overrides(c.nodegroups, "COLLECTION")
        for obj_prop in c.objects:
            if not obj_prop.export: continue
            bl_obj = c_ptr.all_objects.get(obj_prop.name)
            if not bl_obj or bl_obj.type not in SUPPORTED_OBJECT_TYPES or not is_object_live(bl_obj, live): continue
            ovrs = resolve_overrides(col_ovrs + get_flat_overrides(obj_prop.nodegroups, "OBJECT"))
            yield c_idx, c, obj_prop, bl_obj, filter_overrides_for_object(ovrs, bl_obj)

def group_by_signature(export_objects):
    """{override signature: (overrides, [(collection entry, object entry, object)])}: objects with the same overrides
    share their variants, which are generated once (and evaluated in one depsgraph pass in the worker)."""
    batches = {}
    for _c_idx, c, obj_prop, bl_obj, ovrs in export_objects:
        batches.setdefault(get_override_signature(ovrs), (ovrs, []))[1].append((c, obj_prop, bl_obj))
    return batches

def find_export_clashes(scene, preset, live, root):
    """{clash key: path} of the output paths that more than one file of the preset would be written to. No limit:
    every collision overwrites a file."""
    seen, clashes = set(), {}
    for ovrs, items in group_by_signature(iter_export_objects(scene, preset, live)).values():
        for _combo, tags_by_level, paths_by_level in generate_named_combinations(ovrs):
            for c, obj_prop, bl_obj in items:
                dir_parts, filename = export_file_parts(preset, c, obj_prop, bl_obj, tags_by_level, paths_by_level)
                path = os.path.join(root, *dir_parts, filename)
                key = clash_key(path)
                if key in seen: clashes.setdefault(key, path)
                else: seen.add(key)
    return clashes

# --- BINARY STL WRITER ---
def mesh_to_stl_array(mesh, matrix_world):
    """Return the mesh as a world-space STL record array, or None when it has no triangles."""
    mesh.calc_loop_triangles()
    num_tris = len(mesh.loop_triangles)
    if num_tris == 0 or len(mesh.vertices) == 0: return None

    verts = np.empty((len(mesh.vertices), 3), dtype=np.float32)
    mesh.vertices.foreach_get("co", verts.ravel())

    mat_3x3 = np.array(matrix_world.to_3x3(), dtype=np.float32)
    trans = np.array(matrix_world.translation, dtype=np.float32)
    verts = np.dot(verts, mat_3x3.T) + trans

    tri_verts = np.empty((num_tris, 3), dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", tri_verts.ravel())

    tri_normals = np.empty((num_tris, 3), dtype=np.float32)
    mesh.loop_triangles.foreach_get("normal", tri_normals.ravel())

    mat_inv = np.array(matrix_world.to_3x3().inverted_safe(), dtype=np.float32)
    tri_normals = np.dot(tri_normals, mat_inv)

    norms = np.sqrt(np.sum(tri_normals**2, axis=1, keepdims=True))
    norms[norms == 0] = 1.0
    tri_normals /= norms

    data = np.zeros(num_tris, dtype=STL_DTYPE)
    data['normals'] = tri_normals
    data['v0'] = verts[tri_verts[:, 0]]

    if matrix_world.determinant() < 0.0:
        data['v1'] = verts[tri_verts[:, 2]]
        data['v2'] = verts[tri_verts[:, 1]]
    else:
        data['v1'] = verts[tri_verts[:, 1]]
        data['v2'] = verts[tri_verts[:, 2]]
    return data

def collect_instance_arrays(depsgraph, target_objects):
    """One pass over the depsgraph instances (e.g. unrealized Geometry Nodes instances) generated by target_objects.
    Returns {object name_full: [STL arrays]}; instance data is only valid while iterating, so it is converted immediately."""
    wanted = {obj.name_full for obj in target_objects}
    result = {}
    if not wanted: return result
    for inst in depsgraph.object_instances:
        if not inst.is_instance or not inst.parent: continue
        parent_name = inst.parent.original.name_full
        if parent_name not in wanted: continue
        inst_obj = inst.object
        try: mesh = inst_obj.to_mesh()
        except RuntimeError: continue
        if not mesh: continue
        try:
            arr = mesh_to_stl_array(mesh, inst.matrix_world)
        finally:
            inst_obj.to_mesh_clear()
        if arr is not None: result.setdefault(parent_name, []).append(arr)
    return result

def write_object_stl(filepath, bl_obj, depsgraph, instance_arrays=()):
    """Write the evaluated object (plus its pre-collected instances) as one binary STL. Returns the triangle count, or
    None when the depsgraph does not evaluate the object (e.g. its collection is disabled in viewports): its mesh would
    lack every modifier and override."""
    arrays = []
    obj_eval = bl_obj.evaluated_get(depsgraph)
    if not obj_eval.is_evaluated: return None
    try: mesh = obj_eval.to_mesh()
    except RuntimeError: mesh = None
    if mesh:
        try:
            arr = mesh_to_stl_array(mesh, obj_eval.matrix_world)
        finally:
            obj_eval.to_mesh_clear()
        if arr is not None: arrays.append(arr)
    arrays.extend(instance_arrays)

    num_tris = sum(len(a) for a in arrays)
    if num_tris == 0: return 0
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, 'wb') as f:
        f.write(STL_HEADER)
        f.write(struct.pack('<I', num_tris))
        for a in arrays: a.tofile(f)
    return num_tris

def export_object_stl(filepath, bl_obj, depsgraph, instance_arrays=()):
    """write_object_stl() for the export loops: returns the log text, so one unwritable file (e.g. locked by a
    slicer) is reported instead of aborting the whole export."""
    try: num_tris = write_object_stl(filepath, bl_obj, depsgraph, instance_arrays)
    except OSError as e: return f"FAILED ({e.strerror or e}): {filepath}"
    if num_tris is None: return f"SKIPPED (not evaluated, disabled in viewports): {filepath}"
    return filepath if num_tris else f"SKIPPED (no geometry): {filepath}"

# --- JSON UTILS ---
def copy_val_to_dict(v):
    return {
        "value_string": getattr(v, "value_string", ""), "value_menu": getattr(v, "value_menu", ""),
        "use_tag": getattr(v, "use_tag", False), "tag": getattr(v, "tag", ""),
        "use_dir": getattr(v, "use_dir", False), "dir_tag": getattr(v, "dir_tag", ""), "use_sweep": getattr(v, "use_sweep", False),
        "sweep_range": getattr(v, "sweep_range", ""),
        "sweep_start": getattr(v, "sweep_start", "0"),
        "sweep_step": getattr(v, "sweep_step", "1"),
        "sweep_count": getattr(v, "sweep_count", "2")
    }

def copy_input_to_dict(i):
    return {"name": i.name, "override_type": i.override_type, "values": [copy_val_to_dict(v) for v in i.values]}

def copy_node_to_dict(n):
    return {"name": n.name, "sub_path": getattr(n, "sub_path", ""), "tag": getattr(n, "tag", ""), "inputs": [copy_input_to_dict(i) for i in n.inputs]}

def copy_ng_to_dict(ng):
    return {"group": ng.group_name, "sub_path": getattr(ng, "sub_path", ""), "tag": getattr(ng, "tag", ""), "nodes": [copy_node_to_dict(n) for n in ng.nodes]}

def copy_obj_to_dict(o):
    return {"name": o.name, "name_override": o.name_override, "export": o.export, "tag": getattr(o, "tag", ""), "sub_path": getattr(o, "sub_path", ""), "nodegroups": [copy_ng_to_dict(ng) for ng in o.nodegroups]}

def copy_collection_to_dict(c):
    return {"collection_name": c.collection_name, "use_tag": c.use_tag, "tag": c.tag, "sub_path": c.sub_path, "objects": [copy_obj_to_dict(o) for o in c.objects], "nodegroups": [copy_ng_to_dict(ng) for ng in c.nodegroups]}

def copy_preset_to_dict(src):
    return {"name": src.name, "preset_prefix": src.preset_prefix, "collections": [copy_collection_to_dict(c) for c in src.collections], "nodegroups": [copy_ng_to_dict(ng) for ng in src.nodegroups]}

# v1.0.0 kept numbers and booleans in typed fields, with typed sweep fields; they are all text now.
LEGACY_VALUE_KEYS = ("value_bool", "value_int", "value_float", "sweep_start_float", "sweep_step_float", "sweep_count_float",
                     "sweep_start_int", "sweep_step_int", "sweep_count_int")

def legacy_number_text(value, kind):
    return str(int(value)) if kind == "int" else f"{float(value):.7g}"

def migrate_legacy_value(v, legacy, override_type, fill_defaults=False):
    """Move a value's v1.0.0 typed fields (read with `legacy(key)`, None when absent) into the text fields of its input
    type. With `fill_defaults` a missing typed value gets its old default: .blend files do not store unedited fields."""
    kind = "int" if override_type == 'INT' else "float"
    if override_type in ('INT', 'FLOAT') and not v.value_string:
        old = legacy(f"value_{kind}")
        if old is None and fill_defaults: old = 0
        if old is not None: v.value_string = legacy_number_text(old, kind)
    elif override_type == 'BOOLEAN' and not v.value_menu:
        old = legacy("value_bool")
        if old is None and fill_defaults: old = True
        if old is not None: v.value_menu = str(bool(old))
    for field in ("start", "step", "count"):
        old = legacy(f"sweep_{field}_{kind}")
        if old is not None: setattr(v, f"sweep_{field}", legacy_number_text(old, "int" if field == "count" else kind))

def paste_val_from_dict(new_v, data, override_type='FLOAT'):
    for k, v in data.items():
        if hasattr(new_v, k): setattr(new_v, k, v)
    # Presets saved before the split used one tag for both the filename and the folder
    if "dir_tag" not in data: new_v.dir_tag = data.get("tag", "")
    migrate_legacy_value(new_v, data.get, override_type)

def paste_input_from_dict(new_i, data):
    new_i.name = data["name"]
    new_i.override_type = data.get("override_type", 'FLOAT')
    for v_data in data.get("values", []): paste_val_from_dict(new_i.values.add(), v_data, new_i.override_type)

def paste_node_from_dict(new_n, data):
    new_n.name = data.get("name", "")
    new_n.sub_path = data.get("sub_path", "")
    new_n.tag = data.get("tag", "")
    for i_data in data.get("inputs", []): paste_input_from_dict(new_n.inputs.add(), i_data)

def paste_ng_from_dict(ng_list, data):
    """Append a node group to `ng_list`. If the list already has that group, the new entry keeps its nodes but
    gets a blank group name (to be re-pointed at another group) instead of creating a duplicate."""
    group = data.get("group", "")
    if any(ng.group_name == group for ng in ng_list): group = ""
    with raw_edits():
        new_ng = ng_list.add()
        new_ng.group_name = group
        new_ng.sub_path = data.get("sub_path", "")
        new_ng.tag = data.get("tag", "")
        for n_data in data.get("nodes", []): paste_node_from_dict(new_ng.nodes.add(), n_data)

def paste_obj_from_dict(new_o, data):
    new_o.name = data.get("name", "")
    new_o.name_override = data.get("name_override", "")
    new_o.export = data.get("export", True)
    new_o.tag = data.get("tag", "")
    new_o.sub_path = data.get("sub_path", "")
    for ng_data in data.get("nodegroups", []): paste_ng_from_dict(new_o.nodegroups, ng_data)

def paste_collection_from_dict(new_c, data):
    new_c.collection_name = data.get("collection_name", "")
    new_c.use_tag = data.get("use_tag", True)
    new_c.tag = data.get("tag", "")
    new_c.sub_path = data.get("sub_path", "")
    for o_data in data.get("objects", []): paste_obj_from_dict(new_c.objects.add(), o_data)
    for ng_data in data.get("nodegroups", []): paste_ng_from_dict(new_c.nodegroups, ng_data)

def paste_preset_from_dict(new_p, data):
    new_p.name = data.get("name", "Imported Preset")
    new_p.preset_prefix = data.get("preset_prefix", "")
    for c_data in data.get("collections", []): paste_collection_from_dict(new_p.collections.add(), c_data)
    for ng_data in data.get("nodegroups", []): paste_ng_from_dict(new_p.nodegroups, ng_data)

# --- UI CACHE ENGINE ---
def is_override_group_valid(ng):
    if not ng.group_name or not bpy.data.node_groups.get(ng.group_name):
        return False
    return True

def is_override_node_valid(ng_ptr, node):
    if not node.name or node.name == "<Modifier Interface>":
        return True
    if not ng_ptr:
        return False
    return clean_node_name(node.name) in ng_ptr.nodes

def is_override_input_valid(ng_ptr, node, inp):
    if not inp.name or not ng_ptr:
        return False
    is_mod = not node.name or node.name == "<Modifier Interface>"
    if is_mod:
        return find_interface_input(ng_ptr, inp.name) is not None
    target_n = ng_ptr.nodes.get(clean_node_name(node.name))
    if not target_n:
        return False
    return inp.name in target_n.inputs

def is_override_val_valid(inp, val, ng_ptr=None, node=None):
    if val is None or inp.override_type not in SUPPORTED_OVERRIDE_TYPES:
        return False
    parse = float if inp.override_type == 'FLOAT' else int
    if getattr(val, "use_sweep", False):
        if inp.override_type in ('FLOAT', 'INT'):
            try:
                parse(val.sweep_start)
                parse(val.sweep_step)
                return 1 <= int(getattr(val, "sweep_count", "2")) <= MAX_SWEEP_STEPS
            except ValueError: return False
        elif inp.override_type == 'STRING':
            return bool(val.sweep_range and val.sweep_range.strip())
        return True
    if inp.override_type in ('FLOAT', 'INT'):
        # get_input_value() would silently export an empty or malformed number as 0
        try: parse(val.value_string)
        except ValueError: return False
        return True
    if inp.override_type == 'STRING':
        return bool(val.value_string and val.value_string.strip())
    elif inp.override_type == 'MENU':
        if not (val.value_menu and val.value_menu.strip()):
            return False
        if ng_ptr and node:
            valid_items = get_menu_switch_items(ng_ptr, node.name, inp.name)
            if valid_items and val.value_menu not in valid_items:
                return False
        return True
    return True

def validate_overrides(nodegroups):
    for ng in nodegroups:
        if not is_override_group_valid(ng):
            return False
        ng_ptr = bpy.data.node_groups.get(ng.group_name)
        for node in ng.nodes:
            if not is_override_node_valid(ng_ptr, node):
                return False
            for inp in node.inputs:
                if not is_override_input_valid(ng_ptr, node, inp):
                    return False
                if not inp.values:
                    return False
                for val in inp.values:
                    if not is_override_val_valid(inp, val, ng_ptr, node):
                        return False
    return True

def is_preset_setup_valid(scene, preset):
    if not validate_overrides(scene.batch_stl_global_nodegroups):
        return False
    if not validate_overrides(preset.nodegroups):
        return False
    for c in preset.collections:
        if not validate_overrides(c.nodegroups):
            return False
        for obj in c.objects:
            if obj.export:
                if not validate_overrides(obj.nodegroups):
                    return False
    return True

def check_ng_for_overrides(nodegroups):
    has_ovr = len(nodegroups) > 0
    has_perm = any(len(i.values) > 1 or any(getattr(v, "use_sweep", False) for v in i.values) for ng in nodegroups for n in ng.nodes for i in n.inputs)
    return has_ovr, has_perm

def _get_preset_status(scene, preset, g_has_ovr=False, g_has_perm=False):
    pho, php = check_ng_for_overrides(preset.nodegroups)
    has_ovr = g_has_ovr or pho
    has_perm = g_has_perm or php

    for c in preset.collections:
        cho, chp = check_ng_for_overrides(c.nodegroups)
        has_ovr = has_ovr or cho
        has_perm = has_perm or chp
        for obj in c.objects:
            if obj.export:
                oho, ohp = check_ng_for_overrides(obj.nodegroups)
                has_ovr = has_ovr or oho
                has_perm = has_perm or ohp
        if has_ovr and has_perm:
            return True, True
    return has_ovr, has_perm

def rebuild_ui_cache_if_dirty():
    if not _ui_cache.get("is_dirty", False): return 0.1
    _ui_cache["is_dirty"] = False

    try:
        with cached_menu_items(): return _rebuild_ui_cache()
    except Exception:
        _ui_cache["is_dirty"] = True  # retry, otherwise the UI shows stale data until the next unrelated edit
        try:
            if bpy.context.window_manager.batch_stl_verbose_console:
                traceback.print_exc()
        except Exception:
            pass
        return 0.25

def _rebuild_ui_cache():
    context = bpy.context
    if not hasattr(context, "scene") or not context.scene:
        _ui_cache["is_dirty"] = True
        return 0.1

    scene, wm = context.scene, context.window_manager
    stamp_data_version(scene)
    ensure_preset_uids(scene)
    refresh_menu_items(scene)
    _ui_cache["objects_state"] = mapped_objects_state(scene)

    gho, ghp = check_ng_for_overrides(scene.batch_stl_global_nodegroups)
    preset_metrics = {}
    for p_idx, p in enumerate(scene.batch_stl_presets):
        ho, hp = _get_preset_status(scene, p, gho, ghp)
        preset_metrics[p_idx] = {"has_ovr": ho, "has_perm": hp}
    _ui_cache["preset_metrics"] = preset_metrics

    live = _ui_cache["live"] = live_collection_names(context.view_layer)

    # "capped": a count stopped at COUNT_LIMIT, so the number is a lower bound
    g_stats = {"presets": len(scene.batch_stl_presets), "cols": 0, "objs": 0, "exp": 0, "capped": False}
    preset_stats, col_stats = {}, {}
    global_ovrs = get_flat_overrides(scene.batch_stl_global_nodegroups, "GLOBAL")
    count_cache = {}  # objects with identical overrides have the same number of variants

    for p_idx, p in enumerate(scene.batch_stl_presets):
        for c in p.collections: sync_collection_objects(c)
        p_stats = preset_stats[p_idx] = {"cols": len(p.collections), "objs": 0, "exp": 0, "capped": False}
        for c_idx in range(len(p.collections)): col_stats[(p_idx, c_idx)] = {"objs": 0, "exp": 0, "capped": False}
        for c_idx, _c, _obj_prop, _bl_obj, ovrs in iter_export_objects(scene, p, live, global_ovrs):
            n = count_override_combinations(ovrs, count_cache)
            for stats in (col_stats[(p_idx, c_idx)], p_stats, g_stats):
                stats["objs"] += 1
                stats["exp"] += min(n, COUNT_LIMIT)
                stats["capped"] |= n > COUNT_LIMIT
        g_stats["cols"] += p_stats["cols"]

    _ui_cache["stats"] = {"global": g_stats, "presets": preset_stats, "cols": col_stats}

    if wm.batch_stl_info_tab == 'TREE':
        preset = get_active_preset(scene)
        is_global = wm.batch_stl_info_global
        _ui_cache["tree"] = build_tree_dict(context, live, is_global) if (preset or is_global) else ({}, set())

    redraw_sidebars(context)
    return 0.1

def refresh_menu_items(scene):
    """Look up the items of every overridden menu socket outside of drawing: draw code cannot run the lookup of
    socket_menu_items() and reuses what this finds."""
    for nodegroups in iter_scene_nodegroup_lists(scene):
        for ng in nodegroups:
            ng_ptr = bpy.data.node_groups.get(ng.group_name)
            if not ng_ptr: continue
            for node in ng.nodes:
                for inp in node.inputs:
                    if inp.override_type == 'MENU': get_menu_switch_items(ng_ptr, node.name, inp.name)

def mapped_objects_state(scene):
    """(collection, object name, hidden) of every supported object in a mapped collection: renaming or hiding one
    changes the object lists, counts and tree."""
    state = set()
    for name in {c.collection_name for p in scene.batch_stl_presets for c in p.collections if c.collection_name}:
        col = bpy.data.collections.get(name)
        if col: state.update((name, o.name, o.hide_viewport) for o in col.all_objects if o.type in SUPPORTED_OBJECT_TYPES)
    return frozenset(state)

@persistent
def batch_stl_undo_handler(*args):
    # A running export carries on: its worker exports from its own copy of the file, and its job is found by preset uid.
    mark_dirty()

@persistent
def batch_stl_depsgraph_handler(scene, depsgraph):
    if _ui_cache.get("is_dirty", False): return
    if not any(c.collection_name for p in scene.batch_stl_presets for c in p.collections): return

    # Objects linked to / unlinked from collections change the object lists, counts and tree.
    if depsgraph.id_type_updated('COLLECTION'):
        mark_dirty()
        return

    if not depsgraph.id_type_updated('OBJECT') and not depsgraph.id_type_updated('SCENE'):
        return

    # Renamed or hidden objects; excluded or re-included collections
    if (mapped_objects_state(scene) != _ui_cache.get("objects_state")
            or live_collection_names(bpy.context.view_layer) != _ui_cache.get("live")):
        mark_dirty()

# --- TREE VISUALIZER LOGIC ---
def build_tree_dict(context, live=None, is_global=False):
    """Folder tree of the files the active preset (or every preset) writes, and the clash keys of paths written more
    than once. Stops above COUNT_LIMIT files; the export runs its own collision check without a limit."""
    scene = context.scene
    if live is None: live = live_collection_names(context.view_layer)
    root_name = os.path.normpath(bpy.path.abspath(scene.batch_stl_root_dir) if scene.batch_stl_root_dir else "//")
    tree, all_filepaths, duplicates = {}, set(), set()
    file_count = 0
    global_ovrs = get_flat_overrides(scene.batch_stl_global_nodegroups, "GLOBAL")
    active = get_active_preset(scene)
    target_presets = scene.batch_stl_presets if is_global else ([active] if active else [])
    variants_by_sig = {}  # objects with identical overrides share their variants

    for preset in target_presets:
        for _c_idx, c, obj_prop, bl_obj, ovrs in iter_export_objects(scene, preset, live, global_ovrs):
            sig = get_override_signature(ovrs)
            variants = variants_by_sig.get(sig)
            if variants is None:
                named = itertools.islice(generate_named_combinations(ovrs), COUNT_LIMIT + 1)
                variants = variants_by_sig[sig] = [(tags, paths) for _combo, tags, paths in named]
            for tags_by_level, paths_by_level in variants:
                file_count += 1
                if file_count > COUNT_LIMIT:
                    return {root_name: {"_files": [f"Tree preview limited to {COUNT_LIMIT} files (the export still checks every file for collisions)"]}}, set()
                full_dir_parts, filename = export_file_parts(preset, c, obj_prop, bl_obj, tags_by_level, paths_by_level)

                combo_root = tree
                for part in full_dir_parts:
                    combo_root = combo_root.setdefault(part, {})
                combo_root.setdefault('_files', []).append(filename)

                full_path_key = clash_key(os.path.join(root_name, *full_dir_parts, filename))
                if full_path_key in all_filepaths:
                    duplicates.add(full_path_key)
                else:
                    all_filepaths.add(full_path_key)

    return {root_name: tree}, duplicates

# Expansion state: `WindowManager.batch_stl_collapsed_dirs` stores the directory paths whose state is flipped from the
# default. The root folder starts expanded and everything below it collapsed.
def load_toggled_dirs(wm):
    try: return set(json.loads(wm.batch_stl_collapsed_dirs))
    except Exception: return set()

def save_toggled_dirs(wm, toggled):
    wm.batch_stl_collapsed_dirs = json.dumps(sorted(toggled))

def is_dir_collapsed(toggled, dir_path, is_root):
    return (dir_path in toggled) if is_root else (dir_path not in toggled)

def set_dir_collapsed(toggled, dir_path, is_root, collapsed):
    if collapsed == is_root: toggled.add(dir_path)  # the requested state differs from the default
    else: toggled.discard(dir_path)

def iter_tree_dirs(tree_node, current_path=""):
    """Yield (dir_path, node, is_root) for every directory, keyed exactly like draw_tree_dict."""
    for k, child in tree_node.items():
        if k == '_files' or not isinstance(child, dict): continue
        dir_path = f"{current_path}/{k}"
        yield dir_path, child, current_path == ""
        yield from iter_tree_dirs(child, dir_path)

def expand_last_dirs(tree_node, toggled, current_path=""):
    """Expand the last subdirectory of every directory, recursively."""
    subdirs = [k for k, v in tree_node.items() if k != '_files' and isinstance(v, dict)]
    is_root = current_path == ""
    if subdirs: set_dir_collapsed(toggled, f"{current_path}/{subdirs[-1]}", is_root, False)
    for k in subdirs:
        dir_path = f"{current_path}/{k}"
        expand_last_dirs(tree_node[k], toggled, dir_path)

def draw_tree_dict(layout, tree_node, current_path="", toggled_list=None, duplicates=None, actual_path=""):
    if toggled_list is None: toggled_list = load_toggled_dirs(bpy.context.window_manager)
    if duplicates is None: duplicates = set()

    dirs = [k for k in tree_node.keys() if k != '_files']
    for k in dirs:
        dir_path = f"{current_path}/{k}"
        next_actual = os.path.normpath(os.path.join(actual_path, k)) if actual_path else os.path.normpath(k)
        is_root = current_path == ""
        is_collapsed = False if is_root else is_dir_collapsed(toggled_list, dir_path, is_root)

        split = layout.split(factor=0.005)
        split.column()
        box = split.column().box()
        row = box.row(align=True)
        if is_root:
            row.label(text="", icon=ICONS['DOWN'])
        else:
            row.operator_context = 'INVOKE_DEFAULT'
            row.operator("batch_stl.toggle_dir_tree", text="", icon=ICONS['RIGHT'] if is_collapsed else ICONS['DOWN'], emboss=False).dir_path = dir_path
            row.operator_context = 'EXEC_DEFAULT'
        row.scale_y = 0.8
        row.label(text=str(k), icon=ICONS['DIR'])
        if not is_collapsed and isinstance(tree_node[k], dict):
            draw_tree_dict(box, tree_node[k], dir_path, toggled_list, duplicates, next_actual)

    for f in tree_node.get('_files', []):
        split = layout.split(factor=0.12)
        split.column()
        col = split.column()

        row = col.row()
        row.scale_y = 0.8
        check_path = os.path.normpath(os.path.join(actual_path, f)) if actual_path else os.path.normpath(f)
        if clash_key(check_path) in duplicates: row.alert = True
        row.label(text=str(f), icon=ICONS['OBJECT'])

# --- HEADLESS EXPORT EXECUTION ROUTINE ---
def run_headless_export(job_file_path):
    try:
        with open(job_file_path, 'r', encoding="utf-8") as f: job_data = json.load(f)
        preset_index, root_dir, start_time_unix, skip_direct = job_data["preset_index"], job_data["root_dir"], job_data.get("start_time", time.time()), job_data.get("skip_direct", False)
        preset_uid = job_data.get("preset_uid", "")
    except Exception as e:
        print(f"ERROR: Failed to load job manifest: {e}", flush=True)
        sys.exit(1)

    scene = bpy.context.scene
    presets = scene.batch_stl_presets
    preset = next((p for p in presets if preset_uid and p.uid == preset_uid), None)
    if preset is None and 0 <= preset_index < len(presets): preset = presets[preset_index]
    if preset is None:
        print("ERROR: Preset not found", flush=True)
        sys.exit(1)

    print("\n  [Phase 0] Evaluating Targets and Building Depsgraph Culling Maps...", flush=True)
    t_phase0_start = time.perf_counter()

    # Objects whose effective overrides are identical share one batch (one set of variants, one depsgraph pass).
    # Objects without overrides were already written by the main process (skip_direct).
    targets = iter_export_objects(scene, preset, live_collection_names(bpy.context.view_layer))
    batches = group_by_signature(t for t in targets if t[4] or not skip_direct)

    layer_collection_map, layer_collection_parents = {}, {}
    def map_layer_collections(lc, parent=None):
        if lc.collection:
            layer_collection_map[lc.collection.name] = lc
            layer_collection_parents[lc.collection.name] = parent
        for child in lc.children: map_layer_collections(child, lc)
    map_layer_collections(bpy.context.view_layer.layer_collection)

    print(f"    ├─ Mapped {len(layer_collection_map)} collections for depsgraph culling in {time.perf_counter() - t_phase0_start:.4f}s", flush=True)

    if not batches:
        print("  └─ No active objects to export.\nBATCH_STL_DONE", flush=True)
        sys.exit(0)

    with cached_menu_items():
        batch_counts = {sig: count_override_combinations(ovrs, limit=None) for sig, (ovrs, _items) in batches.items()}
    total_ops = sum(len(items) * batch_counts[sig] for sig, (_ovrs, items) in batches.items())
    print(f"BATCH_STL_TOTAL:{total_ops}", flush=True)

    current_op_step, batch_counter, first_export_started = 0, 1, False

    for signature, (all_overrides, batch_items) in batches.items():
        num_combinations = batch_counts[signature]
        batch_objects = {item[2] for item in batch_items}

        isolated_collections = []
        if len(all_overrides) > 0:
            batch_col_names = {col.name for item in batch_items for col in item[2].users_collection}
            visible_hierarchy = set()
            for c_name in batch_col_names:
                curr = c_name
                while curr in layer_collection_map:
                    visible_hierarchy.add(curr)
                    parent_lc = layer_collection_parents.get(curr)
                    curr = parent_lc.collection.name if (parent_lc and parent_lc.collection) else None
            for name, lc in layer_collection_map.items():
                if name not in visible_hierarchy and not lc.exclude:
                    lc.exclude = True
                    isolated_collections.append(name)

        baseline_global_states, baseline_mod_states = capture_baseline_states(all_overrides, batch_objects)

        try:
            for combo_idx, (combo, tags_by_level, paths_by_level) in enumerate(generate_named_combinations(all_overrides)):
                t_perm_start = time.perf_counter()
                if not first_export_started:
                    print(f"=== Headless init took {time.time() - start_time_unix:.2f} s to start first export ===", flush=True)
                    first_export_started = True

                # A parameter left out of this variant (no value applies to its branch) must be back at its baseline,
                # not keep what the previous variant set.
                combo_overrides = reconstruct_overrides_for_combo(combo)
                revert_overrides(baseline_global_states, baseline_mod_states, batch_objects, skip=override_targets(combo_overrides))
                apply_overrides(combo_overrides, batch_objects)
                bpy.context.view_layer.update()
                depsgraph = bpy.context.evaluated_depsgraph_get()
                instance_arrays = collect_instance_arrays(depsgraph, batch_objects)

                for c, obj_prop, bl_obj in batch_items:
                    full_dir_parts, filename = export_file_parts(preset, c, obj_prop, bl_obj, tags_by_level, paths_by_level)
                    out_dir = os.path.normpath(os.path.join(root_dir, *full_dir_parts)) if full_dir_parts else root_dir
                    filepath = os.path.join(out_dir, filename)

                    result_text = export_object_stl(filepath, bl_obj, depsgraph, instance_arrays.get(bl_obj.name_full, ()))

                    print(f"{preset.name} | {c.collection_name} | {bl_obj.name} | Permutation {combo_idx + 1}/{num_combinations} | Batch {batch_counter}/{len(batches)}\n  └─ {result_text} | {time.perf_counter() - t_perm_start:.2f} s", flush=True)
                    current_op_step += 1
                    print(f"BATCH_STL_PROGRESS:{current_op_step}", flush=True)

        finally:
            revert_overrides(baseline_global_states, baseline_mod_states, batch_objects)
            for name in isolated_collections:
                if name in layer_collection_map: layer_collection_map[name].exclude = False
            bpy.context.view_layer.update()

        batch_counter += 1

    print("BATCH_STL_DONE", flush=True)
    sys.exit(0)

# ==============================================================================
# === [ 3. PROPERTY GROUPS ] ===
# ==============================================================================

# Undo: Blender records an undo step for every property edited in the UI, and the operators below declare
# 'UNDO' in bl_options, so property updates only refresh the UI cache. The exception is search fields
# (any StringProperty with search=, including the folder fields with upstream suggestions), which push their own
# step through a labelled edit_callback / search_field_update. A search field without a label gets no undo step.

class HierarchyIterator:
    """Helper to yield all active node groups in the hierarchy context."""
    @staticmethod
    def iterate_lists(scene):
        yield ('GLOBAL', scene.batch_stl_global_nodegroups)
        preset = get_active_preset(scene)
        if not preset: return
        yield ('PRESET', preset.nodegroups)
        col = get_active_collection(preset)
        if not col: return
        yield ('COLLECTION', col.nodegroups)
        obj = get_active_object(col)
        if not obj: return
        yield ('OBJECT', obj.nodegroups)

    @staticmethod
    def iterate(scene):
        for lvl, container in HierarchyIterator.iterate_lists(scene):
            for ng in container: yield (lvl, ng)

SUPPORTED_OVERRIDE_TYPES = ('FLOAT', 'INT', 'BOOLEAN', 'STRING', 'MENU')

def classify_socket_type(socket_type):
    """Map a node socket / interface socket type to an override type; 'UNSUPPORTED' for vectors, colors, objects, ..."""
    if socket_type in ('VALUE', 'FLOAT') or 'Float' in socket_type: return 'FLOAT'
    if socket_type == 'INT' or socket_type.startswith('NodeSocketInt') and 'Vector' not in socket_type: return 'INT'
    if socket_type == 'BOOLEAN' or 'Bool' in socket_type: return 'BOOLEAN'
    if socket_type == 'STRING' or 'String' in socket_type: return 'STRING'
    if socket_type == 'MENU' or 'Menu' in socket_type: return 'MENU'
    return 'UNSUPPORTED'

def infer_input_type(group_ptr, node_name, input_name):
    if not group_ptr or not input_name: return 'FLOAT'
    if not node_name or node_name == "<Modifier Interface>":
        item = find_interface_input(group_ptr, input_name)
        if item:
            return classify_socket_type(getattr(item, "socket_type", "") if hasattr(group_ptr, "interface") else item.type)
    else:
        node = group_ptr.nodes.get(clean_node_name(node_name))
        if node and input_name in node.inputs:
            return classify_socket_type(node.inputs[input_name].type)
    return 'FLOAT'

def get_supported_inputs(group_ptr, node_name):
    """Names of the sockets that can be overridden. Overrides are identified by name, so repeated names count once."""
    supported = []
    for name in _supported_socket_names(group_ptr, node_name):
        if name not in supported: supported.append(name)
    return supported

def _supported_socket_names(group_ptr, node_name):
    if not group_ptr: return []
    supported = []
    if not node_name or node_name == "<Modifier Interface>":
        if hasattr(group_ptr, "interface"):
            for it in group_ptr.interface.items_tree:
                if getattr(it, "item_type", "") == 'SOCKET' and getattr(it, "in_out", "INPUT") == 'INPUT':
                    if classify_socket_type(getattr(it, "socket_type", "")) != 'UNSUPPORTED':
                        supported.append(it.name)
        elif hasattr(group_ptr, "inputs"):
            for inp in group_ptr.inputs:
                if classify_socket_type(inp.type) != 'UNSUPPORTED':
                    supported.append(inp.name)
    else:
        node = group_ptr.nodes.get(clean_node_name(node_name))
        if node:
            for inp in node.inputs:
                if not getattr(inp, "is_unavailable", False) and not getattr(inp, "hide", False):
                    if classify_socket_type(inp.type) != 'UNSUPPORTED':
                        supported.append(inp.name)
    return supported

def get_target_node_names(group_ptr, exclude=()):
    """Node targets of a node group that have overridable sockets, as shown in the node field, skipping `exclude`
    (clean names). '<Modifier Interface>' comes last, and is the fallback when nothing else is offered."""
    targets = []
    if group_ptr:
        # Group Input has no input sockets. Group Output is a valid target (its unlinked inputs set the group's
        # outputs) but goes after the regular nodes, so it never becomes the default target of a new override.
        for n in sorted(group_ptr.nodes, key=lambda n: n.type == 'GROUP_OUTPUT'):
            if n.type != 'GROUP_INPUT' and clean_node_name(n.name) not in exclude:
                if get_supported_inputs(group_ptr, n.name):
                    targets.append(f"{n.name} [{n.node_tree.name if n.type == 'GROUP' and getattr(n, 'node_tree', None) else n.type}]")
    if "<Modifier Interface>" not in exclude and (get_supported_inputs(group_ptr, "<Modifier Interface>") or not targets):
        targets.append("<Modifier Interface>")
    return targets

def get_socket_default_value(group_ptr, node_name, input_name):
    if not group_ptr: return None
    if not node_name or node_name == "<Modifier Interface>":
        return get_modifier_socket_default(group_ptr, input_name)
    else:
        node = group_ptr.nodes.get(clean_node_name(node_name))
        if node and input_name in node.inputs:
            return getattr(node.inputs[input_name], "default_value", None)
    return None

def default_value_text(override_type, default_val, menu_items):
    """Text for a value field holding a socket's default, or None when there is no usable default. A menu default
    that is not one of the items (the modifier interface reports '') falls back to the first item."""
    try:
        if override_type == 'FLOAT': return str(round(float(default_val), 4))
        if override_type == 'INT': return str(int(default_val))
        if override_type == 'BOOLEAN': return str(bool(default_val))
        if override_type == 'STRING': return None if default_val is None else str(default_val)
        if override_type == 'MENU':
            if default_val and str(default_val) in menu_items: return str(default_val)
            return menu_items[0] if menu_items else None
    except (TypeError, ValueError): pass
    return None

def sync_input_type(inp, scene, update_value=True):
    for _lvl, ng in HierarchyIterator.iterate(scene):
        for n in ng.nodes:
            if inp in n.inputs.values():
                ng_ptr = bpy.data.node_groups.get(ng.group_name)
                new_type = infer_input_type(ng_ptr, n.name, inp.name)
                # These writes are consistent data, so the edit callbacks must not run on them: they treat an emptied
                # value as "delete it", which would remove the input (and with it its node and node group).
                with raw_edits():
                    inp.override_type = new_type
                    if update_value:
                        items = get_menu_switch_items(ng_ptr, n.name, inp.name) if new_type == 'MENU' else []
                        text = default_value_text(new_type, get_socket_default_value(ng_ptr, n.name, inp.name), items)
                        field = "value_menu" if new_type in ('BOOLEAN', 'MENU') else "value_string"
                        for v in inp.values:
                            v.use_sweep = False
                            if text is not None: setattr(v, field, text)
                return

@edit_callback("Edit Override Input", "name", "prev_name")
def on_input_name_update(self, context):
    is_duplicate = False
    is_invalid = False
    my_node = None
    my_ng = None
    other_input = None
    if hasattr(context.scene, "batch_stl_presets"):
        for _lvl, ng in HierarchyIterator.iterate(context.scene):
            for n in ng.nodes:
                if self in n.inputs.values():
                    my_node = n
                    my_ng = ng
                    for other in n.inputs:
                        if other != self and other.name == self.name and self.name != "":
                            is_duplicate = True
                            other_input = other
                            break
                    break
            if my_node: break

    if my_node and my_ng and self.name != "":
        ng_ptr = bpy.data.node_groups.get(my_ng.group_name)
        source_inputs = get_supported_inputs(ng_ptr, my_node.name)
        if self.name not in source_inputs:
            is_invalid = True

    if self.name == "" and my_node:
        for i, inp in enumerate(my_node.inputs):
            if inp == self:
                with raw_edits(): my_node.inputs.remove(i)
                if len(my_node.inputs) == 0:
                    my_node.name = ""
                return DELETED

    if is_invalid:
        with raw_edits():
            self.name = self.prev_name
        return

    if is_duplicate:
        with raw_edits():
            self.name = self.prev_name

        my_idx = -1
        other_idx = -1
        if my_node:
            for i, inp in enumerate(my_node.inputs):
                if inp == self: my_idx = i
                elif inp == other_input: other_idx = i

            if my_idx != -1 and other_idx != -1:
                i, j = my_idx, other_idx
                if i > j:
                    i, j = j, i
                my_node.inputs.move(j, i)
                my_node.inputs.move(i+1, j)
        return

    if self.name == "" and self.prev_name != "":
        with raw_edits(): self.name = self.prev_name
        return

    name_changed = (self.prev_name != self.name)
    self.prev_name = self.name
    if name_changed and self.name != "":
        self.values.clear()
        self.values.add()

    if hasattr(context.scene, "batch_stl_presets"):
        try: sync_input_type(self, context.scene, update_value=name_changed)
        except Exception: pass

def search_target_node_cb(self, context, edit_text):
    if not context or not getattr(context, "scene", None): return ["<Modifier Interface>"]
    if edit_text == self.name: edit_text = ""
    res = []

    for _lvl, ng in HierarchyIterator.iterate(context.scene):
        if self in ng.nodes.values():
            ng_ptr = bpy.data.node_groups.get(ng.group_name)
            if ng_ptr:
                if get_supported_inputs(ng_ptr, "<Modifier Interface>"):
                    res.append("<Modifier Interface>")

                for node in sorted(ng_ptr.nodes, key=lambda n: n.type == 'GROUP_OUTPUT'):
                    if node.type != 'GROUP_INPUT':
                        if get_supported_inputs(ng_ptr, node.name):
                            val = f"{node.name} [{node.node_tree.name if node.type == 'GROUP' and getattr(node, 'node_tree', None) else node.type}]"
                            if not edit_text or edit_text.lower() in val.lower(): res.append(val)
            else:
                res.append("<Modifier Interface>")
            return res

    res.append("<Modifier Interface>")
    return res

def search_input_name_cb(self, context, edit_text):
    """Sockets of the target node (or modifier interface) that can still be overridden."""
    if not context or not getattr(context, "scene", None): return []
    if edit_text == self.name: edit_text = ""
    for _lvl, ng in HierarchyIterator.iterate(context.scene):
        for n in ng.nodes:
            if self in n.inputs.values():
                names = get_supported_inputs(bpy.data.node_groups.get(ng.group_name), n.name)
                return [s for s in names if edit_text.lower() in s.lower()] if edit_text else names
    return []

def search_menu_items_cb(self, context, edit_text):
    if not context or not getattr(context, "scene", None): return []
    for _lvl, ng in HierarchyIterator.iterate(context.scene):
        for n in ng.nodes:
            for i in n.inputs:
                if self in i.values.values():
                    if i.override_type == 'BOOLEAN':
                        items = ['True', 'False']
                    else:
                        ng_ptr = bpy.data.node_groups.get(ng.group_name)
                        items = get_menu_switch_items(ng_ptr, n.name, i.name) if ng_ptr else []
                    if edit_text == self.value_menu: edit_text = ""
                    return [item for item in items if edit_text.lower() in item.lower()] if edit_text else items
    return []

def on_value_update(prop_name, label=""):
    @edit_callback(label, prop_name, "prev_" + prop_name)
    def update(self, context):
        is_duplicate = False
        is_invalid = False
        my_input = None
        for _lvl, ng in HierarchyIterator.iterate(context.scene):
            for n in ng.nodes:
                for i in n.inputs:
                    if self in i.values.values():
                        my_input = i
                        my_val = getattr(self, prop_name)

                        if prop_name == "value_menu" and my_val != "":
                            if i.override_type == 'BOOLEAN':
                                items = ['True', 'False']
                            else:
                                ng_ptr = bpy.data.node_groups.get(ng.group_name)
                                items = get_menu_switch_items(ng_ptr, n.name, i.name) if ng_ptr else []
                            # Items that cannot be listed are checked when exporting (a rejected value is logged)
                            if items and my_val not in items:
                                is_invalid = True

                        if prop_name in ("value_string", "sweep_start", "sweep_step"):
                            if my_val != "":
                                try:
                                    if i.override_type == 'FLOAT': float(my_val)
                                    elif i.override_type == 'INT': int(my_val)
                                except ValueError:
                                    is_invalid = True
                            elif prop_name != "value_string":
                                is_invalid = True

                        if prop_name == "sweep_count":
                            try:
                                if not 1 <= int(my_val) <= MAX_SWEEP_STEPS: is_invalid = True
                            except ValueError:
                                is_invalid = True

                        for other in i.values:
                            if other != self and getattr(other, prop_name) == my_val and not getattr(other, "use_sweep", False) and not getattr(self, "use_sweep", False):
                                is_duplicate = True
                                break
                        break
                if my_input: break
            if my_input: break

        my_val = getattr(self, prop_name)
        if my_input and prop_name in ("value_string", "value_menu") and my_val == "":
            for idx, val_item in enumerate(my_input.values):
                if val_item == self:
                    with raw_edits(): my_input.values.remove(idx)
                    if len(my_input.values) == 0:
                        my_input.name = ""
                    return DELETED

        if is_invalid or is_duplicate:
            with raw_edits(): setattr(self, prop_name, getattr(self, "prev_" + prop_name))
        else:
            setattr(self, "prev_" + prop_name, getattr(self, prop_name))
    return update

def on_no_spaces_update(prop_name, label=""):
    @edit_callback(label, prop_name, "prev_" + prop_name)
    def update(self, context):
        val = getattr(self, prop_name)
        if " " in val:
            with raw_edits(): setattr(self, prop_name, getattr(self, "prev_" + prop_name))
        else:
            setattr(self, "prev_" + prop_name, val)
    return update

_PATH_STEP = re.compile(r"(\w+)\[(\d+)\]")

# --- Merge predictions -------------------------------------------------------------------------------------------
# BranchScope mirrors walk_override_branch for the UI, without enumerating permutations. It records every folder and
# filename token that exists at a point of the override stack together with the branch context it was created in,
# so suggestions and merge highlights only offer names that can actually coexist with the merges made above a field.
#
# - Keys are (kind, name) with kind "dir" (folder) or "tag" (filename token).
# - A branch name is created by a value. Values of one input are alternatives: the names of different values (or
#   sweep steps) of the same input never appear in the same permutation, so they exclude each other. The folder and
#   the token of the *same* value do appear together. A name created by several inputs (e.g. "1" from two number
#   inputs) excludes nothing, since it can come from either.
# - Only the anchors typed in a value field (value_name_parts) merge; names derived from the value are new.
# - A context is the set of branch keys a block is merged into (plus what those names themselves require).
# - A name created inside a merged block exists only in that context, e.g. "X" made under a block merged into "A"
#   is never available inside a block merged into "C".

SCOPE_KINDS = ("dir", "tag")

def block_parts(block, kind):
    """Folder names (sub_path) or filename tokens (tag) a node group / node block adds."""
    return split_path_parts(getattr(block, "sub_path", "")) if kind == "dir" else split_tag_parts(getattr(block, "tag", ""))

def _value_step_names(ng_ptr, node, inp, val):
    """[{kind: (anchors, names)}] per step of one UI value (one step unless it is a sweep)."""
    target = 'MODIFIER' if not node.name or node.name == "<Modifier Interface>" else 'NODE'
    tmp = MockInput(inp, val, is_temp=True)
    steps = parse_sweep_values(MockOverride(target, ng_ptr, node.name, [tmp]), tmp) if tmp.use_sweep else [get_input_value(tmp)]
    return [value_parts(tmp, v) for v in steps]

def _block_where(ng, node=None):
    return ng.group_name if node is None else f"{ng.group_name} › {clean_node_name(node.name) or 'Modifier'}"

def _block_desc(kind, ng, node=None):
    return f"{'Folder' if kind == 'dir' else 'Tag'} · {_block_where(ng, node)}"

class BranchScope:
    def __init__(self):
        self.entries = {}  # (kind, name) -> {"desc", "groups": set, "alts": {(group, alt)}, "reqs": [frozenset(keys)]}

    def __contains__(self, key): return key in self.entries
    def desc(self, key): return self.entries[key]["desc"]
    def is_branch(self, key): return key in self.entries and bool(self.entries[key]["groups"])

    def _excludes(self, a, b):
        """Names of different values of one single input never coexist."""
        ea, eb = self.entries.get(a), self.entries.get(b)
        if a == b or not ea or not eb or len(ea["groups"]) != 1 or ea["groups"] != eb["groups"]: return False
        return ea["alts"].isdisjoint(eb["alts"])

    def _req_set(self, key, ctx):
        """Branches `key` certainly requires alongside the context `ctx` (shared by every way it can exist there),
        or None when it can never exist in `ctx`."""
        e = self.entries.get(key)
        if not e or any(self._excludes(key, k) for k in ctx): return None
        fits = [reqs for reqs in e["reqs"] if not any(self._excludes(r, k) for r in reqs for k in ctx)]
        return frozenset.intersection(*fits) if fits else None

    def available(self, key, ctx): return self._req_set(key, ctx) is not None

    def add(self, key, desc, ctx, group=None, alt=None):
        e = self.entries.setdefault(key, {"desc": desc, "groups": set(), "alts": set(), "reqs": []})
        if group is not None:
            e["groups"].add(group)
            e["alts"].add((group, alt))
        reqs = frozenset(k for k in ctx if k != key)
        if reqs not in e["reqs"]: e["reqs"].append(reqs)

    def resolve(self, kind, parts, ctx, desc=None):
        """Walk folder names / filename tokens inside context `ctx` like the exporter does.

        Returns (new_ctx, hits, dead) with names: `hits` merge into existing ones, `dead` are branch names that cannot
        exist in this context (the exporter then never applies the block). With `desc`, new names are recorded."""
        ctx, hits, dead = set(ctx), [], []
        for p in self.expand(kind, parts):
            key = (kind, p)
            reqs = self._req_set(key, ctx)
            if reqs is not None:
                hits.append(p)
                if self.is_branch(key): ctx.add(key)
                ctx.update(reqs)
            elif self.is_branch(key):
                dead.append(p)
            elif desc is not None:
                self.add(key, desc, ctx)
        return frozenset(ctx), hits, dead

    def expand(self, kind, parts):
        """Tag chains split into their tokens like the exporter does (see segment_tag)."""
        return segment_tags(parts, lambda n: (kind, n) in self.entries) if kind == "tag" else parts

    def enter_block(self, block, ctx, ng, node=None, record=True):
        """Context inside a node group / node block (its sub-folder, then its tag), recording its new names."""
        for kind in SCOPE_KINDS:
            ctx = self.resolve(kind, block_parts(block, kind), ctx, _block_desc(kind, ng, node) if record else None)[0]
        return ctx

    def resolve_value(self, anchors, ctx):
        """resolve() for the anchors of one value ({kind: names}, see value_field_anchors) in context `ctx`; names
        derived from the value are always new. Returns (new_ctx, {kind: (hits, dead)})."""
        res = {}
        for kind in SCOPE_KINDS:
            ctx, hits, dead = self.resolve(kind, anchors[kind], ctx)
            res[kind] = (hits, dead)
        return frozenset(ctx), res

    def add_input(self, ng, ng_ptr, node, inp, ctx):
        """Record the folders and tokens an input's values create inside context `ctx`."""
        group = (ng.group_name, clean_node_name(node.name), inp.name)
        descs = {"dir": f"Branch · {ng.group_name} › {inp.name}", "tag": f"Name branch · {ng.group_name} › {inp.name}"}
        for v_idx, val in enumerate(inp.values):
            for s_idx, parts in enumerate(_value_step_names(ng_ptr, node, inp, val)):
                anchors = {kind: self.expand(kind, parts[kind][0]) for kind in SCOPE_KINDS}
                v_ctx, res = self.resolve_value(anchors, ctx)
                if res["dir"][1] or res["tag"][1]: continue  # this value never applies, so its names never exist
                for kind in SCOPE_KINDS:
                    for n in anchors[kind] + parts[kind][1]:
                        if n not in res[kind][0]: self.add((kind, n), descs[kind], v_ctx, group, (v_idx, s_idx))

    def suggestions(self, kind, ctx, exclude=()):
        """{name: description} of the names of `kind` available in context `ctx`, in stack order."""
        return {n: e["desc"] for (k, n), e in self.entries.items()
                if k == kind and (k, n) not in ctx and n not in exclude and self.available((k, n), ctx)}

def merge_tooltip(hits, dead, scope):
    """`hits` / `dead` are lists of (kind, name) keys."""
    lines = [f"{name}  ({scope.desc((kind, name))})" for kind, name in hits]
    if dead:
        lines += [f"{name}  ({scope.desc((kind, name))}) cannot exist here" for kind, name in dead]
        lines.append("No branch contains all of these names together, so this block and everything inside it never applies")
    elif any(scope.is_branch(k) for k in hits):
        lines.append("This block and everything inside it only applies to the branches that contain these names")
    else:
        lines.append("This block and everything inside it is placed inside these existing folders / name tokens")
    return "\n".join(lines)

def upstream_scope(item):
    """(BranchScope, context) at `item` in the override stack: the names that exist before it and the branches the
    blocks around it are merged into.

    Order matches the exporter: Global, Preset, Collection, Object lists; inside a list node groups and nodes top to
    bottom; inside a block its sub-folder, then its tag, then (for a node) its inputs. Only what comes before `item`
    exists yet."""
    scope, empty = BranchScope(), frozenset()
    scene = item.id_data
    try: steps = _PATH_STEP.findall(item.path_from_id())
    except (ValueError, AttributeError): return scope, empty
    if not steps: return scope, empty

    # Resolve the chain of override lists that lead to the item, plus the item's position in its own list.
    lists, pos = [scene.batch_stl_global_nodegroups], {}
    if steps[0][0] == "batch_stl_presets":
        owner = scene
        for attr, idx in steps:
            if attr == "nodegroups" or attr == "batch_stl_global_nodegroups": break
            owner = getattr(owner, attr)[int(idx)]
            lists.append(owner.nodegroups)
    for attr, idx in steps:
        if attr in ("nodegroups", "batch_stl_global_nodegroups", "nodes", "inputs"): pos[attr if attr != "batch_stl_global_nodegroups" else "nodegroups"] = int(idx)
    own = lists[-1]

    def add_node(ng, ng_ptr, node, ng_ctx, input_limit=None):
        n_ctx = scope.enter_block(node, ng_ctx, ng, node)
        for i_idx, inp in enumerate(node.inputs):
            if input_limit is not None and i_idx >= input_limit: break
            scope.add_input(ng, ng_ptr, node, inp, n_ctx)
        return n_ctx

    def add_group(ng):
        ng_ptr = bpy.data.node_groups.get(ng.group_name)
        ng_ctx = scope.enter_block(ng, empty, ng)
        for node in ng.nodes: add_node(ng, ng_ptr, node, ng_ctx)

    for lst in lists[:-1]:
        for ng in lst: add_group(ng)

    g = pos.get("nodegroups", len(own))
    for ng in list(own)[:g]: add_group(ng)
    if g >= len(own) or not isinstance(item, (BatchSTLNode, BatchSTLValue)): return scope, empty

    # Inside the item's own group: its sub-folder / tag (and the node's, for a value) already exist and set the context.
    ng = own[g]
    ng_ptr = bpy.data.node_groups.get(ng.group_name)
    ng_ctx = scope.enter_block(ng, empty, ng)
    n_pos = pos.get("nodes", 0)
    for node in list(ng.nodes)[:n_pos]: add_node(ng, ng_ptr, node, ng_ctx)
    if isinstance(item, BatchSTLNode) or n_pos >= len(ng.nodes): return scope, ng_ctx
    return scope, add_node(ng, ng_ptr, ng.nodes[n_pos], ng_ctx, input_limit=pos.get("inputs", 0))

def make_scope_search_cb(kind, prop_name, multi_part=True):
    """Search list for a folder (kind "dir") or filename-tag (kind "tag") field: upstream names that can exist in this
    branch, to merge into. Free text is still allowed.

    The suggestion completes the last '/'-separated part (in a value tag field also '_'-separated) inside the branch
    chosen so far, so 'C/' offers 'C/2', 'C/3', ... but not 'C/A' (A and C are alternatives of the same input).
    `multi_part` is False for a value field. A value field ending in its "names, then the value" mark (folder '\\' or '/', tag '_', see value_name_parts)
    completes the last name before the mark and keeps the mark, and offers the next link of the chain: 'B\\' offers
    'B\\' and 'B\\1\\', 'B\\2\\', ...; tag 'B_' offers 'B_' and 'B_1_', 'B_2_', ... (a tag chain is split at '_')."""
    split = split_path_parts if kind == "dir" else split_tag_parts if multi_part else split_value_tag_parts
    other = "tag" if kind == "dir" else "dir"
    marks = "" if multi_part else ("\\/" if kind == "dir" else "_")
    seps = "/\\_" if marks == "_" else "/\\"
    def search(self, context, edit_text):
        current = getattr(self, prop_name, "")
        mark = ""
        if edit_text[-1:] and edit_text[-1] in marks:
            mark, edit_text = edit_text[-1], edit_text[:-1]
        if edit_text + mark == current and not any(c in edit_text for c in seps): edit_text = ""
        scope, ctx = upstream_scope(self)
        if isinstance(self, (BatchSTLNodeGroup, BatchSTLNode)):
            # The block's other field (sub-folder vs tag) belongs to the same block and narrows the branch too
            ctx = scope.resolve(other, block_parts(self, other), ctx)[0]

        def complete(text):
            prefix, query = "", text
            cut = max(text.rfind(c) for c in seps)
            if cut >= 0: prefix, query = text[:cut + 1], text[cut + 1:]
            used = scope.expand(kind, split(prefix))
            names = scope.suggestions(kind, scope.resolve(kind, used, ctx)[0], exclude=used)
            q = query.lower()
            return [(prefix + name + mark, desc) for name, desc in names.items() if not q or q in name.lower()]

        items = complete(edit_text)
        if mark and edit_text: items += complete(edit_text + mark)  # the next link of the chain
        return list(dict.fromkeys(items))
    return search

class BatchSTLLogLine(bpy.types.PropertyGroup): text: bpy.props.StringProperty()
class BatchSTLValue(bpy.types.PropertyGroup):
    prev_value_string: bpy.props.StringProperty(default="", options={'HIDDEN'})
    # Plain text field (no search list): Blender records its undo step, so the callback must not push another one.
    value_string: bpy.props.StringProperty(name="Value", default="", update=on_value_update("value_string"))
    prev_value_menu: bpy.props.StringProperty(default="", options={'HIDDEN'})
    value_menu: bpy.props.StringProperty(name="Value", default="", search=search_menu_items_cb, update=on_value_update("value_menu", "Edit Override Value"))
    use_tag: bpy.props.BoolProperty(name="Use Tag", description="Add this value to the filename, named by the tag field. Drag across several buttons to toggle them together", default=False, update=mark_dirty)
    prev_tag: bpy.props.StringProperty(default="", options={'HIDDEN'})
    tag: bpy.props.StringProperty(name="Tag", description="Filename tag: 'name' replaces the value, 'name_' adds the token 'name' and then the value. Chain names with '_' or '/' ('B_2_' merges into branch B, then its branch 2). A name that exists upstream merges into that branch", default="", search=make_scope_search_cb("tag", "tag", multi_part=False), update=on_no_spaces_update("tag", "Edit Override Tag"))
    use_dir: bpy.props.BoolProperty(name="Use Dir", description="Put this value's files in a sub-folder, named by the directory field. Drag across several buttons to toggle them together", default=True, update=mark_dirty)
    prev_dir_tag: bpy.props.StringProperty(default="", options={'HIDDEN'})
    dir_tag: bpy.props.StringProperty(name="Directory", description="Folder name: 'name' replaces the value, 'name\\' puts a folder per value inside folder 'name'. Chain folders with '\\' or '/' ('B\\2\\' merges into folder B, then its folder 2). A folder that exists upstream merges into that branch", default="", search=make_scope_search_cb("dir", "dir_tag", multi_part=False), update=on_no_spaces_update("dir_tag", "Edit Override Directory"))
    use_sweep: bpy.props.BoolProperty(name="Sweep", default=False, update=mark_dirty)
    sweep_range: bpy.props.StringProperty(name="Sweep Range", default="", update=mark_dirty)
    prev_sweep_start: bpy.props.StringProperty(default="0", options={'HIDDEN'})
    sweep_start: bpy.props.StringProperty(name="Start", description="Value start", default="0", update=on_value_update("sweep_start"))
    prev_sweep_step: bpy.props.StringProperty(default="1", options={'HIDDEN'})
    sweep_step: bpy.props.StringProperty(name="Step", description="Value step", default="1", update=on_value_update("sweep_step"))
    prev_sweep_count: bpy.props.StringProperty(default="2", options={'HIDDEN'})
    sweep_count: bpy.props.StringProperty(name="Steps", description=f"Number of steps (1 to {MAX_SWEEP_STEPS})", default="2", update=on_value_update("sweep_count"))

class BatchSTLInput(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Input Socket", default="", search=search_input_name_cb, update=on_input_name_update)
    prev_name: bpy.props.StringProperty(default="", options={'HIDDEN'})
    override_type: bpy.props.StringProperty(default='FLOAT', update=mark_dirty)
    values: bpy.props.CollectionProperty(type=BatchSTLValue)

@edit_callback("Edit Override Node", "name", "prev_name")
def on_node_name_update(self, context):
    is_duplicate = False
    is_invalid = False
    my_ng = None
    for _lvl, ng in HierarchyIterator.iterate(context.scene):
        if self in ng.nodes.values():
            my_ng = ng
            is_duplicate = any(other != self and clean_node_name(other.name) == clean_node_name(self.name) for other in ng.nodes) and self.name != ""
            break

    if self.name == "" and my_ng:
        for i, n in enumerate(my_ng.nodes):
            if n == self:
                with raw_edits(): my_ng.nodes.remove(i)
                if len(my_ng.nodes) == 0:
                    my_ng.group_name = ""
                return DELETED

    if self.name != "" and my_ng:
        ng_ptr = bpy.data.node_groups.get(my_ng.group_name)
        targets = get_target_node_names(ng_ptr) if ng_ptr else []
        if self.name not in targets:
            is_invalid = True

    if is_invalid or is_duplicate or (self.name == "" and self.prev_name != ""):
        with raw_edits(): self.name = self.prev_name
        return

    if self.prev_name != self.name and self.name != "" and my_ng:
        self.inputs.clear()

        ng_ptr = bpy.data.node_groups.get(my_ng.group_name)
        source_inputs = get_supported_inputs(ng_ptr, self.name)

        if source_inputs:
            inp = self.inputs.add()
            inp.name = source_inputs[0]
        else:
            self.inputs.add().values.add()

    self.prev_name = self.name

class BatchSTLNode(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Target Node", default="", search=search_target_node_cb, update=on_node_name_update, description="Select <Modifier Interface> to target the modifier directly")
    prev_name: bpy.props.StringProperty(default="", options={'HIDDEN'})
    prev_sub_path: bpy.props.StringProperty(default="", options={'HIDDEN'})
    sub_path: bpy.props.StringProperty(name="Sub-folder", default="", description="Sub-folder path; pick an upstream folder to merge into that branch", search=make_scope_search_cb("dir", "sub_path"), update=on_no_spaces_update("sub_path", "Edit Override Node Sub-folder"))
    prev_tag: bpy.props.StringProperty(default="", options={'HIDDEN'})
    tag: bpy.props.StringProperty(name="Tag", default="", description="Filename tokens added by this node, '/' between tokens; pick an upstream name token to merge into that branch", search=make_scope_search_cb("tag", "tag"), update=on_no_spaces_update("tag", "Edit Override Node Tag"))
    inputs: bpy.props.CollectionProperty(type=BatchSTLInput)

@edit_callback("Edit Override Node Group", "group_name", "prev_group_name")
def on_group_name_update(self, context):
    is_duplicate = False
    my_container = None
    for _lvl, container in HierarchyIterator.iterate_lists(context.scene):
        if self in container.values():
            my_container = container
            is_duplicate = any(other != self and other.group_name == self.group_name for other in container) and self.group_name != ""
            break

    if self.group_name == "" and my_container:
        for i, ng in enumerate(my_container):
            if ng == self:
                with raw_edits(): my_container.remove(i)
                return DELETED

    is_invalid = self.group_name != "" and not bpy.data.node_groups.get(self.group_name)

    if is_invalid or is_duplicate or (self.group_name == "" and self.prev_group_name != ""):
        with raw_edits(): self.group_name = self.prev_group_name
        return

    # A blank group (new, or a pasted duplicate that had its name cleared) keeps the nodes it already has when it is
    # given a group; switching from one group to another starts over with that group's first node.
    prev = self.prev_group_name
    if prev != self.group_name and self.group_name != "" and not (prev == "" and self.nodes):
        self.nodes.clear()
        targets = get_target_node_names(bpy.data.node_groups.get(self.group_name))
        if targets:
            self.nodes.add().name = targets[0]

    self.prev_group_name = self.group_name

def search_group_name_cb(self, context, edit_text):
    if not context or not getattr(context, "scene", None): return []
    if edit_text == getattr(self, "group_name", ""): edit_text = ""
    res = [ng.name for ng in bpy.data.node_groups]
    return [item for item in res if edit_text.lower() in item.lower()] if edit_text else res

class BatchSTLNodeGroup(bpy.types.PropertyGroup):
    group_name: bpy.props.StringProperty(name="Node Group", default="", search=search_group_name_cb, update=on_group_name_update)
    prev_group_name: bpy.props.StringProperty(default="", options={'HIDDEN'})
    prev_sub_path: bpy.props.StringProperty(default="", options={'HIDDEN'})
    sub_path: bpy.props.StringProperty(name="Sub-folder", default="", description="Sub-folder path; pick an upstream folder to merge into that branch", search=make_scope_search_cb("dir", "sub_path"), update=on_no_spaces_update("sub_path", "Edit Override Group Sub-folder"))
    prev_tag: bpy.props.StringProperty(default="", options={'HIDDEN'})
    tag: bpy.props.StringProperty(name="Tag", default="", description="Filename tokens added by this node group, '/' between tokens; pick an upstream name token to merge into that branch", search=make_scope_search_cb("tag", "tag"), update=on_no_spaces_update("tag", "Edit Override Group Tag"))
    nodes: bpy.props.CollectionProperty(type=BatchSTLNode)

def get_export_name(self):
    return self.name_override or self.name

def set_export_name(self, value):
    # Empty, or the object's own name, means "follow the object" (renames included)
    value = value.strip()
    self.name_override = "" if value == self.name else value
    mark_dirty()

class BatchSTLObject(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    name_override: bpy.props.StringProperty(default="", options={'HIDDEN'})
    # Shows the object name until edited; clearing the field returns to the object name
    export_name: bpy.props.StringProperty(name="File Name", description="Name the exported files start with. Clear the field to use the object name again", get=get_export_name, set=set_export_name, options={'SKIP_SAVE'})
    # session_uid of the object, to follow renames (see sync_collection_objects); not an ID pointer, which adds a user
    obj_uid: bpy.props.IntProperty(default=0, options={'HIDDEN'})
    export: bpy.props.BoolProperty(default=True, update=mark_dirty)
    prev_tag: bpy.props.StringProperty(default="", options={'HIDDEN'})
    tag: bpy.props.StringProperty(name="Tag", default="", description="Filename token(s), '/' between tokens. Placed in hierarchy order: Global, Preset, Collection, Object", update=on_no_spaces_update("tag"))
    prev_sub_path: bpy.props.StringProperty(default="", options={'HIDDEN'})
    sub_path: bpy.props.StringProperty(name="Sub-folder", default="", update=on_no_spaces_update("sub_path"))
    nodegroups: bpy.props.CollectionProperty(type=BatchSTLNodeGroup)

@edit_callback("Edit Collection", "collection_name", "prev_collection_name")
def on_collection_name_update(self, context):
    self.prev_collection_name = self.collection_name

class BatchSTLCollection(bpy.types.PropertyGroup):
    collection_name: bpy.props.StringProperty(name="Collection", default="", update=on_collection_name_update)
    prev_collection_name: bpy.props.StringProperty(default="", options={'HIDDEN'})
    use_tag: bpy.props.BoolProperty(name="Use Tag", default=True, update=mark_dirty)
    prev_tag: bpy.props.StringProperty(default="", options={'HIDDEN'})
    tag: bpy.props.StringProperty(name="Tag", default="", description="Filename token(s), '/' between tokens. Placed in hierarchy order: Global, Preset, Collection, Object", update=on_no_spaces_update("tag"))
    prev_sub_path: bpy.props.StringProperty(default="", options={'HIDDEN'})
    sub_path: bpy.props.StringProperty(name="Sub-folder", default="", update=on_no_spaces_update("sub_path"))
    objects: bpy.props.CollectionProperty(type=BatchSTLObject)
    object_index: bpy.props.IntProperty(default=0, update=mark_dirty)
    nodegroups: bpy.props.CollectionProperty(type=BatchSTLNodeGroup)

class BatchSTLJob(bpy.types.PropertyGroup):
    """Runtime state of one preset's export. Lives on the WindowManager so undo never rolls it back; cleared when a
    file loads. Keyed by preset uid and scene session_uid (see get_job)."""
    preset_uid: bpy.props.StringProperty(default="")
    scene_uid: bpy.props.IntProperty(default=0)
    is_exporting: bpy.props.BoolProperty(default=False)
    cancel_export: bpy.props.BoolProperty(default=False)
    export_progress: bpy.props.FloatProperty(name="Progress", default=0.0, min=0.0, max=1.0)
    export_status: bpy.props.StringProperty(default="")
    console_logs: bpy.props.CollectionProperty(type=BatchSTLLogLine)
    console_index: bpy.props.IntProperty(default=0)

class BatchSTLExportPreset(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Preset Name", default="New Preset", update=mark_dirty)
    uid: bpy.props.StringProperty(default="", options={'HIDDEN'})  # stable identity of a running export's preset
    prev_preset_prefix: bpy.props.StringProperty(default="", options={'HIDDEN'})
    preset_prefix: bpy.props.StringProperty(name="Preset Root Directory", default="", update=on_no_spaces_update("preset_prefix"))
    collections: bpy.props.CollectionProperty(type=BatchSTLCollection)
    collection_index: bpy.props.IntProperty(name="Collection Index", default=0, update=mark_dirty)
    nodegroups: bpy.props.CollectionProperty(type=BatchSTLNodeGroup)
    last_export_time: bpy.props.FloatProperty(name="Last Export Time", default=0.0)


# ==============================================================================
# === [ 4. OPERATORS ] ===
# ==============================================================================

class BATCH_STL_OT_export_presets_json(bpy.types.Operator, ExportHelper):
    bl_idname = "batch_stl.export_presets_json"
    bl_label = "Export JSON"
    bl_description = "Export all presets to a JSON file"
    filename_ext = ".json"
    filter_glob: bpy.props.StringProperty(default="*.json", options={'HIDDEN'})
    def execute(self, context):
        try:
            with open(self.filepath, 'w', encoding="utf-8") as f:
                json.dump([copy_preset_to_dict(p) for p in context.scene.batch_stl_presets], f, indent=4)
            self.report({'INFO'}, f"Presets exported to {os.path.basename(self.filepath)}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed to export presets: {e}")
            return {'CANCELLED'}

class BATCH_STL_OT_import_presets_json(bpy.types.Operator, ImportHelper):
    bl_idname = "batch_stl.import_presets_json"
    bl_label = "Import JSON"
    bl_description = "Import presets from a JSON file"
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = ".json"
    filter_glob: bpy.props.StringProperty(default="*.json", options={'HIDDEN'})
    @inside_operator
    def execute(self, context):
        before = len(context.scene.batch_stl_presets)
        try:
            with open(self.filepath, 'r', encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                self.report({'ERROR'}, "Invalid JSON: expected a list of preset objects.")
                return {'CANCELLED'}
            for p_data in data:
                paste_preset_from_dict(context.scene.batch_stl_presets.add(), p_data)
            ensure_preset_uids(context.scene)
            self.report({'INFO'}, f"Presets imported from {os.path.basename(self.filepath)}")
        except Exception as e:
            while len(context.scene.batch_stl_presets) > before:
                context.scene.batch_stl_presets.remove(len(context.scene.batch_stl_presets) - 1)
            self.report({'ERROR'}, f"Failed to import presets: {e}")
            return {'CANCELLED'}
        mark_dirty()
        return {'FINISHED'}

class BATCH_STL_OT_clear_console(bpy.types.Operator):
    bl_idname = "batch_stl.clear_console"
    bl_label = "Clear Console"
    bl_description = "Clear console logs for the current view"
    def execute(self, context):
        job = get_job(get_active_preset(context.scene))
        if job: job.console_logs.clear()
        return {'FINISHED'}

class ListActionHandler:
    """Utility class to handle standardized ADD/REMOVE/UP/DOWN/COPY/PASTE operations for UI lists."""
    @staticmethod
    def perform_action(action, lst, index, shift_pressed, clipboard_key, copy_func, paste_func):
        new_index = index
        if action == 'ADD':
            lst.add()
            new_index = len(lst) - 1
        elif action == 'REMOVE' and lst:
            if 0 <= index < len(lst):
                lst.remove(index)
                new_index = max(0, min(index, len(lst) - 1))
        elif action == 'UP' and 0 < index < len(lst):
            target = 0 if shift_pressed else index - 1
            lst.move(index, target)
            new_index = target
        elif action == 'DOWN' and 0 <= index < len(lst) - 1:
            target = len(lst) - 1 if shift_pressed else index + 1
            lst.move(index, target)
            new_index = target
        elif action == 'COPY' and lst and 0 <= index < len(lst):
            _clipboard[clipboard_key] = copy_func(lst[index])
        elif action == 'PASTE' and _clipboard.get(clipboard_key):
            paste_func(lst.add(), _clipboard[clipboard_key])
            new_index = len(lst) - 1
        return new_index

class BATCH_STL_OT_preset_actions(bpy.types.Operator):
    bl_idname = "batch_stl.preset_actions"
    bl_label = "Preset Actions"
    bl_options = {'UNDO', 'INTERNAL'}  # no REGISTER: hides the "Adjust Last Operation" redo panel
    action: bpy.props.EnumProperty(items=(('ADD', "", ""), ('REMOVE', "", ""), ('UP', "", ""), ('DOWN', "", ""), ('COPY', "", ""), ('PASTE', "", "")))
    shift_pressed: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'}, default=False)

    @classmethod
    def description(cls, context, properties):
        act = properties.action
        if act == 'ADD': return "Add new preset"
        if act == 'REMOVE': return "Remove active preset"
        if act == 'UP': return "Move preset up (Shift: Move to top)"
        if act == 'DOWN': return "Move preset down (Shift: Move to bottom)"
        if act == 'COPY': return "Copy active preset"
        if act == 'PASTE': return "Paste preset from clipboard"
        return "Preset action"

    def invoke(self, context, event):
        self.shift_pressed = event.shift
        return self.execute(context)

    @inside_operator
    def execute(self, context):
        scene = context.scene
        lst = scene.batch_stl_presets
        idx = scene.batch_stl_preset_index
        # Jobs are keyed by preset uid, so reordering is safe while an export runs; removing the exporting preset is not.
        removed = lst[idx] if self.action == 'REMOVE' and 0 <= idx < len(lst) else None
        removed_job = get_job(removed)

        if removed_job and removed_job.is_exporting:
            self.report({'WARNING'}, "Cannot remove a preset while it is exporting.")
        else:
            if removed_job: remove_job(scene.session_uid, removed.uid)  # its log
            scene.batch_stl_preset_index = ListActionHandler.perform_action(
                self.action, lst, idx, self.shift_pressed, "preset", copy_preset_to_dict, paste_preset_from_dict
            )
            ensure_preset_uids(scene)  # added and pasted presets

        mark_dirty()
        return {'FINISHED'}

class BATCH_STL_OT_collection_actions(bpy.types.Operator):
    bl_idname = "batch_stl.collection_actions"
    bl_label = "Collection Actions"
    bl_options = {'UNDO', 'INTERNAL'}
    action: bpy.props.EnumProperty(items=(('ADD', "", ""), ('REMOVE', "", ""), ('UP', "", ""), ('DOWN', "", ""), ('COPY', "", ""), ('PASTE', "", "")))
    shift_pressed: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'}, default=False)

    @classmethod
    def description(cls, context, properties):
        act = properties.action
        if act == 'ADD': return "Add new collection target"
        if act == 'REMOVE': return "Remove active collection"
        if act == 'UP': return "Move collection up (Shift: Move to top)"
        if act == 'DOWN': return "Move collection down (Shift: Move to bottom)"
        if act == 'COPY': return "Copy active collection target"
        if act == 'PASTE': return "Paste collection target from clipboard"
        return "Collection action"

    def invoke(self, context, event):
        self.shift_pressed = event.shift
        return self.execute(context)

    @inside_operator
    def execute(self, context):
        preset = get_active_preset(context.scene)
        if not preset: return {'CANCELLED'}

        preset.collection_index = ListActionHandler.perform_action(
            self.action, preset.collections, preset.collection_index, self.shift_pressed, "collection", copy_collection_to_dict, paste_collection_from_dict
        )

        mark_dirty()
        return {'FINISHED'}

class BATCH_STL_OT_table_action(bpy.types.Operator):
    bl_idname = "batch_stl.table_action"
    bl_label = "Table Action"
    bl_options = {'UNDO', 'INTERNAL'}

    action: bpy.props.StringProperty()
    is_global: bpy.props.BoolProperty(default=False)
    is_preset: bpy.props.BoolProperty(default=False)
    is_collection: bpy.props.BoolProperty()
    c_idx: bpy.props.IntProperty(default=-1)
    o_idx: bpy.props.IntProperty(default=-1)
    ng_idx: bpy.props.IntProperty(default=-1)
    n_idx: bpy.props.IntProperty(default=-1)
    i_idx: bpy.props.IntProperty(default=-1)
    v_idx: bpy.props.IntProperty(default=-1)
    shift_pressed: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'}, default=False)

    @classmethod
    def description(cls, context, properties):
        act = properties.action
        if act == 'ADD_GROUP': return "Add an Override Group to this level"
        if act == 'COPY_GROUP': return "Copy this Override Group to clipboard"
        if act == 'PASTE_GROUP': return "Paste an Override Group from clipboard"
        if act == 'MOVE_GROUP_UP': return "Move group up (Shift: Move directly to parent hierarchy level)"
        if act == 'MOVE_GROUP_DOWN': return "Move group down (Shift: Localize and copy group to every nested child)"
        if act == 'ADD_NODE': return "Add a Target Node filter"
        if act == 'MOVE_NODE_UP': return "Move Target Node up"
        if act == 'MOVE_NODE_DOWN': return "Move Target Node down"
        if act == 'ADD_INPUT': return "Add an Input Parameter override (Shift: Auto-populate all available socket inputs)"
        if act == 'VALUE_ACTION': return "Add Permutation (Shift: Toggle Sweep range mode)"
        if act == 'CLEAR_VALUE_STRING': return "Clear this value (deletes it, and the input if it is the last value)"
        if act == 'TOGGLE_COLLECTION_USE_TAG': return "Toggle collection-level filename prefix/suffix tag"
        if act == 'TOGGLE_OBJECT_EXPORT': return "Toggle object active export state"
        return "Perform structural table action"

    def invoke(self, context, event):
        self.shift_pressed = event.shift
        return self.execute(context)

    def _resolve_context_list(self, context, preset):
        if self.is_global: return context.scene.batch_stl_global_nodegroups
        elif self.is_preset: return preset.nodegroups

        active_col = get_active_collection(preset)
        if not active_col: return None
        if self.is_collection: return active_col.nodegroups

        active_obj = get_active_object(active_col)
        return active_obj.nodegroups if active_obj else None

    def _handle_group_action(self, ng_list, context, preset):
        if self.action == 'ADD_GROUP':
            ng_list.add()
        elif self.action == 'COPY_GROUP' and 0 <= self.ng_idx < len(ng_list):
            _clipboard["nodegroup"] = copy_ng_to_dict(ng_list[self.ng_idx])
        elif self.action == 'PASTE_GROUP' and _clipboard.get("nodegroup"):
            paste_ng_from_dict(ng_list, _clipboard["nodegroup"])
        elif self.action == 'MOVE_GROUP_UP' and 0 <= self.ng_idx < len(ng_list):
            if self.shift_pressed:
                src_ng = ng_list[self.ng_idx]
                dst = None
                if self.is_preset: dst = context.scene.batch_stl_global_nodegroups
                elif self.is_collection: dst = preset.nodegroups
                elif not self.is_global:
                    col = get_active_collection(preset)
                    dst = col.nodegroups if col else None
                if dst is not None:
                    paste_ng_from_dict(dst, copy_ng_to_dict(src_ng))
                    ng_list.remove(self.ng_idx)
            elif self.ng_idx > 0: ng_list.move(self.ng_idx, self.ng_idx - 1)
        elif self.action == 'MOVE_GROUP_DOWN' and 0 <= self.ng_idx < len(ng_list):
            if self.shift_pressed:
                src_ng = ng_list[self.ng_idx]
                copied_data = copy_ng_to_dict(src_ng)
                pushed = False
                if self.is_global:
                    for p in context.scene.batch_stl_presets:
                        paste_ng_from_dict(p.nodegroups, copied_data)
                        pushed = True
                elif self.is_preset:
                    for c in preset.collections:
                        paste_ng_from_dict(c.nodegroups, copied_data)
                        pushed = True
                elif self.is_collection:
                    col = get_active_collection(preset)
                    if col:
                        for o in col.objects:
                            paste_ng_from_dict(o.nodegroups, copied_data)
                            pushed = True
                if pushed:
                    ng_list.remove(self.ng_idx)
            elif self.ng_idx < len(ng_list) - 1:
                ng_list.move(self.ng_idx, self.ng_idx + 1)

    def _handle_node_action(self, ng_list):
        if not (0 <= self.ng_idx < len(ng_list)): return
        nodes = ng_list[self.ng_idx].nodes
        if self.action == 'ADD_NODE':
            ng_ptr = bpy.data.node_groups.get(ng_list[self.ng_idx].group_name)
            valid_targets = get_target_node_names(ng_ptr, {clean_node_name(n.name) for n in nodes})
            if valid_targets:
                nodes.add().name = valid_targets[0]
        elif self.action == 'MOVE_NODE_UP' and 0 < self.n_idx < len(nodes):
            nodes.move(self.n_idx, self.n_idx - 1)
        elif self.action == 'MOVE_NODE_DOWN' and 0 <= self.n_idx < len(nodes) - 1:
            nodes.move(self.n_idx, self.n_idx + 1)

    def _handle_input_action(self, ng_list):
        if not (0 <= self.ng_idx < len(ng_list)): return
        ng = ng_list[self.ng_idx]
        if not (0 <= self.n_idx < len(ng.nodes)): return
        node = ng.nodes[self.n_idx]
        inputs = node.inputs
        if self.action == 'ADD_INPUT':
            ng_ptr = bpy.data.node_groups.get(ng.group_name)
            source_inputs = get_supported_inputs(ng_ptr, node.name)

            existing = {i.name for i in node.inputs}

            if self.shift_pressed and source_inputs:
                for s_name in source_inputs:
                    if s_name and s_name not in existing:
                        inp = node.inputs.add()
                        inp.values.add()
                        inp.name = s_name
                return

            if source_inputs:
                for s_name in source_inputs:
                    if s_name and s_name not in existing:
                        inp = inputs.add()
                        inp.values.add()
                        inp.name = s_name
                        break
            else:
                inputs.add().values.add()

    def _handle_value_action(self, ng_list):
        if not (0 <= self.ng_idx < len(ng_list)): return
        ng = ng_list[self.ng_idx]
        if not (0 <= self.n_idx < len(ng.nodes)): return
        node = ng.nodes[self.n_idx]
        if not (0 <= self.i_idx < len(node.inputs)): return
        inp_obj = node.inputs[self.i_idx]
        vals = inp_obj.values
        if self.action == 'VALUE_ACTION':
            def add_smart_value():
                if inp_obj.override_type == 'BOOLEAN':
                    existing = {v.value_menu for v in vals if not v.use_sweep}
                    if 'True' not in existing:
                        v = vals.add()
                        v.value_menu = 'True'
                        return v
                    elif 'False' not in existing:
                        v = vals.add()
                        v.value_menu = 'False'
                        return v
                    return None
                elif inp_obj.override_type == 'MENU':
                    ng_ptr = bpy.data.node_groups.get(ng.group_name)
                    items = get_menu_switch_items(ng_ptr, node.name, inp_obj.name) if ng_ptr else []
                    existing = {v.value_menu for v in vals if not v.use_sweep}
                    for item in items:
                        if item not in existing:
                            v = vals.add()
                            v.value_menu = item
                            return v
                    return None
                elif inp_obj.override_type in ('FLOAT', 'INT'):
                    existing = []
                    for v in vals:
                        if not v.use_sweep:
                            try:
                                existing.append(float(v.value_string))
                            except ValueError:
                                pass
                    v = vals.add()
                    if existing:
                        new_val = max(existing) + 1
                        v.value_string = str(int(new_val) if inp_obj.override_type == 'INT' else round(new_val, 4))
                    return v
                elif inp_obj.override_type == 'STRING':
                    # A blank string is invalid and would duplicate the previous blank one: fill it in first.
                    if any(not v.use_sweep and v.value_string == "" for v in vals): return None
                    return vals.add()
                else:
                    return vals.add()

            if self.v_idx < 0:
                add_smart_value()
            elif 0 <= self.v_idx < len(vals):
                val = vals[self.v_idx]
                if not val.use_sweep:
                    if self.shift_pressed:
                        val.use_sweep = True
                        for j in reversed(range(len(vals))):
                            if j != self.v_idx: vals.remove(j)
                    else:
                        add_smart_value()
                else:
                    val.use_sweep = False
                    if self.shift_pressed and inp_obj.override_type in ['FLOAT', 'INT', 'MENU', 'BOOLEAN', 'STRING']:
                        ng_obj = ng_list[self.ng_idx]
                        ng_ptr = bpy.data.node_groups.get(ng_obj.group_name)
                        node_obj = ng_obj.nodes[self.n_idx]
                        target = 'MODIFIER' if not node_obj.name or node_obj.name == "<Modifier Interface>" else 'NODE'
                        temp_inp = MockInput(inp_obj, val, is_temp=True)
                        # An empty value (unknown menu items, empty string range) would delete the value it is written to
                        parsed_vals = [x for x in parse_sweep_values(MockOverride(target, ng_ptr, node_obj.name, [temp_inp]), temp_inp) if x != ""]
                        if parsed_vals:
                            for p_idx, p_val in enumerate(parsed_vals):
                                v = val if p_idx == 0 else vals.add()
                                v.use_sweep = False
                                v.use_dir, v.dir_tag, v.use_tag, v.tag = val.use_dir, val.dir_tag, val.use_tag, val.tag  # generated values inherit the sweep's tag settings
                                if inp_obj.override_type in ('FLOAT', 'INT', 'STRING'): v.value_string = str(p_val)
                                elif inp_obj.override_type in ('MENU', 'BOOLEAN'): v.value_menu = str(p_val)

    @inside_operator
    def execute(self, context):
        preset = get_active_preset(context.scene)
        if not preset: return {'CANCELLED'}

        try:
            if self.action == 'TOGGLE_COLLECTION_USE_TAG':
                if 0 <= self.c_idx < len(preset.collections): preset.collections[self.c_idx].use_tag = not preset.collections[self.c_idx].use_tag
            elif self.action == 'TOGGLE_OBJECT_EXPORT':
                active_col = get_active_collection(preset)
                if active_col and 0 <= self.o_idx < len(active_col.objects): active_col.objects[self.o_idx].export = not active_col.objects[self.o_idx].export
            else:
                ng_list = self._resolve_context_list(context, preset)
                if ng_list is None: return {'CANCELLED'}

                if self.action == 'CLEAR_VALUE_STRING':
                    # Same as emptying the field by hand: on_value_update deletes the value (and an emptied input)
                    if 0 <= self.ng_idx < len(ng_list) and 0 <= self.n_idx < len(ng_list[self.ng_idx].nodes) and 0 <= self.i_idx < len(ng_list[self.ng_idx].nodes[self.n_idx].inputs) and 0 <= self.v_idx < len(ng_list[self.ng_idx].nodes[self.n_idx].inputs[self.i_idx].values):
                        ng_list[self.ng_idx].nodes[self.n_idx].inputs[self.i_idx].values[self.v_idx].value_string = ""
                elif 'GROUP' in self.action: self._handle_group_action(ng_list, context, preset)
                elif 'NODE' in self.action: self._handle_node_action(ng_list)
                elif 'INPUT' in self.action: self._handle_input_action(ng_list)
                elif 'VALUE' in self.action: self._handle_value_action(ng_list)
        except IndexError:
            self.report({'WARNING'}, "UI Sync Error: List mutated unexpectedly. Please try again.")
            return {'CANCELLED'}

        mark_dirty()
        return {'FINISHED'}

class BATCH_STL_OT_toggle_dir_tree(bpy.types.Operator):
    bl_idname = "batch_stl.toggle_dir_tree"
    bl_label = "Toggle Directory Tree"
    bl_options = {'INTERNAL'}
    bl_description = "Toggle directory tree expansion (Shift: also expand/collapse all child directories)"
    dir_path: bpy.props.StringProperty()
    recursive: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'}, default=False)

    def invoke(self, context, event):
        self.recursive = event.shift
        return self.execute(context)

    def execute(self, context):
        wm = context.window_manager
        toggled = load_toggled_dirs(wm)
        subtree = []
        if self.recursive:
            subtree = [(p, is_root) for p, _node, is_root in iter_tree_dirs(_ui_cache["tree"][0])
                       if p == self.dir_path or p.startswith(self.dir_path + "/")]
        clicked = next((is_root for p, is_root in subtree if p == self.dir_path), None)
        if clicked is not None:
            # The clicked folder flips and every folder below it follows its new state.
            collapse = not is_dir_collapsed(toggled, self.dir_path, clicked)
            for p, is_root in subtree: set_dir_collapsed(toggled, p, is_root, collapse)
        elif self.dir_path in toggled: toggled.discard(self.dir_path)
        else: toggled.add(self.dir_path)
        save_toggled_dirs(wm, toggled)
        return {'FINISHED'}

class BATCH_STL_OT_tree_expansion(bpy.types.Operator):
    bl_idname = "batch_stl.tree_expansion"
    bl_label = "Directory Tree Expansion"
    bl_options = {'INTERNAL'}
    mode: bpy.props.EnumProperty(items=(('EXPAND_ALL', "", ""), ('COLLAPSE_ALL', "", ""), ('EXPAND_LAST', "", "")))

    @classmethod
    def description(cls, context, properties):
        if properties.mode == 'EXPAND_ALL': return "Expand all directories"
        if properties.mode == 'COLLAPSE_ALL': return "Collapse all directories"
        return "Collapse all, then expand the last subdirectory of every directory"

    def execute(self, context):
        wm = context.window_manager
        tree = _ui_cache["tree"][0]
        toggled = load_toggled_dirs(wm)
        if self.mode == 'EXPAND_LAST':
            for p, _node, is_root in iter_tree_dirs(tree):
                set_dir_collapsed(toggled, p, is_root, True)
            expand_last_dirs(tree, toggled)
        else:
            collapse = self.mode == 'COLLAPSE_ALL'
            for p, _node, is_root in iter_tree_dirs(tree):
                set_dir_collapsed(toggled, p, is_root, collapse)
        save_toggled_dirs(wm, toggled)
        return {'FINISHED'}

class BATCH_STL_OT_merge_info(bpy.types.Operator):
    """Highlight bar of an override block that merges into an upstream folder; the tooltip explains the merge."""
    bl_idname = "batch_stl.merge_info"
    bl_label = "Merged Branch"
    bl_options = {'INTERNAL'}
    info: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return properties.info

    def execute(self, context):
        return {'CANCELLED'}

class BATCH_STL_OT_cancel_export(bpy.types.Operator):
    bl_idname = "batch_stl.cancel_export"
    bl_label = "Cancel Export"
    bl_description = "Cancel the active batch export"
    preset_uid: bpy.props.StringProperty(options={'HIDDEN'})
    def execute(self, context):
        job = find_job(context.scene.session_uid, self.preset_uid)
        if job and job.is_exporting:
            job.cancel_export = True
            log_to_console(job, "[!] Export cancelled manually.")
        return {'FINISHED'}

class EXPORT_OT_batch_stl_multi(bpy.types.Operator):
    bl_idname = "export_scene.batch_stl_multi"
    bl_label = "Export"
    bl_description = "Start batch STL export"
    bl_options = {"REGISTER"}
    preset_index: bpy.props.IntProperty(default=-1)

    @classmethod
    def poll(cls, context): return len(context.scene.batch_stl_presets) > 0

    def invoke(self, context, event):
        self._timer = self.process = None
        self.total_operations, self.current_op = 1, 0
        self.export_start_time = time.perf_counter()
        scene = context.scene

        preset_idx = self.preset_index if self.preset_index >= 0 else scene.batch_stl_preset_index
        if preset_idx < 0 or preset_idx >= len(scene.batch_stl_presets): return {"CANCELLED"}
        scene.batch_stl_preset_index = preset_idx
        ensure_preset_uids(scene)
        preset = scene.batch_stl_presets[preset_idx]
        # Undo, reordering and scene switches invalidate pointers and indices: modal() and cleanup() find the preset
        # and its job again by these.
        self.scene_uid, self.preset_uid, self.preset_name = scene.session_uid, preset.uid, preset.name
        job = get_job(preset, create=True)

        if job.is_exporting: return {'CANCELLED'}
        if not scene.batch_stl_root_dir:
            self.report({'ERROR'}, "Missing Root Directory")
            return {"CANCELLED"}
        if scene.batch_stl_root_dir.startswith("//") and not bpy.data.is_saved:
            self.report({'ERROR'}, "Please save the .blend file before exporting to a relative path (//)")
            return {"CANCELLED"}
        if not is_preset_setup_valid(scene, preset):
            self.report({'ERROR'}, "Improper setup: One or more override fields are missing or invalid.")
            return {"CANCELLED"}

        for c in preset.collections: sync_collection_objects(c)
        live = live_collection_names(context.view_layer)
        preset_root = os.path.normpath(bpy.path.abspath(scene.batch_stl_root_dir))
        with cached_menu_items():
            clashes = find_export_clashes(scene, preset, live, preset_root)
            targets = list(iter_export_objects(scene, preset, live))
        if clashes:
            self.report({'ERROR'}, f"Export aborted: {len(clashes)} files would be written more than once (e.g. {next(iter(clashes.values()))}). Check the tree view.")
            return {"CANCELLED"}

        context.window_manager.batch_stl_info_tab = 'LOG'
        job.console_logs.clear()

        for c in preset.collections:
            # e.g. the collection was renamed or deleted: say so instead of reporting a complete export
            if c.collection_name and not bpy.data.collections.get(c.collection_name):
                log_to_console(job, f"SKIPPED (collection not found): {c.collection_name}")

        objects_to_export_directly = [(c, obj_prop, bl_obj) for _c_idx, c, obj_prop, bl_obj, ovrs in targets if not ovrs]
        needs_headless = any(t[4] for t in targets)

        if objects_to_export_directly:
            depsgraph = context.evaluated_depsgraph_get()
            instance_arrays = collect_instance_arrays(depsgraph, [item[2] for item in objects_to_export_directly])
            log_to_console(job, f"=== STARTING NATIVE DIRECT EXPORT ({len(objects_to_export_directly)} Objects) ===")
            for c, obj_prop, bl_obj in objects_to_export_directly:
                t_dir_start = time.perf_counter()
                full_dir_parts, filename = export_file_parts(preset, c, obj_prop, bl_obj)
                out_dir = os.path.normpath(os.path.join(preset_root, *full_dir_parts)) if full_dir_parts else preset_root
                filepath = os.path.join(out_dir, filename)

                result_text = export_object_stl(filepath, bl_obj, depsgraph, instance_arrays.get(bl_obj.name_full, ()))

                log_to_console(job, f"{preset.name} | {c.collection_name} | {bl_obj.name} | Permutation 1/1 | Batch 1/1\n  └─ {result_text} | {time.perf_counter() - t_dir_start:.2f} s")

        if not needs_headless:
            preset.last_export_time = time.perf_counter() - self.export_start_time
            log_to_console(job, f"=== BATCH EXPORT COMPLETE ({preset.last_export_time:.4f}s) ===")
            self.report({'INFO'}, f"Batch Export {preset.name} Complete in {preset.last_export_time:.2f}s.")
            redraw_sidebars(context)
            self.cleanup(context)
            return {'FINISHED'}

        job.export_status = "Spawning Worker... (0.0s)"
        t_spawn_start = time.perf_counter()
        self.temp_dir = tempfile.mkdtemp(prefix="fast_batch_stl_")
        self.temp_blend = os.path.join(self.temp_dir, "batch_stl_export_temp.blend")
        self.job_json = os.path.join(self.temp_dir, "job.json")

        bpy.ops.wm.save_as_mainfile(filepath=self.temp_blend, copy=True, compress=False)
        with open(self.job_json, 'w', encoding="utf-8") as f: json.dump({"preset_index": preset_idx, "preset_uid": preset.uid, "root_dir": bpy.path.abspath(scene.batch_stl_root_dir), "start_time": time.time(), "skip_direct": True}, f)

        # The worker runs this very file as a script (see the __main__ block at the bottom).
        # --factory-startup disables Python auto-run, so mirror the user's setting to keep scripted drivers working.
        # --python-exit-code: a Python error in the worker exits with code 1 instead of 0.
        worker_args = [bpy.app.binary_path, "--factory-startup", "--python-exit-code", "1"]
        if context.preferences.filepaths.use_scripts_auto_execute: worker_args.append("--enable-autoexec")
        worker_args += ["-b", self.temp_blend, "-P", __file__, "--", "--batch-stl-headless", self.job_json]

        try:
            sub_env = dict(os.environ, PYTHONUNBUFFERED="1")
            # blender.exe is a console program: without this flag Windows opens a console window for the worker.
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self.process = subprocess.Popen(worker_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=sub_env, creationflags=flags)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to spawn headless Blender: {e}")
            self.cleanup(context)
            return {'CANCELLED'}

        log_to_console(job, f"=== INITIATING HEADLESS EXPORT '{preset.name}' [{time.perf_counter() - t_spawn_start:.4f}s Boot] ===")

        self.q = queue.Queue()
        def enqueue_output(out, q):
            for line in iter(out.readline, ''): q.put(line)
            out.close()
        self.t = threading.Thread(target=enqueue_output, args=(self.process.stdout, self.q)); self.t.daemon = True; self.t.start()

        job.is_exporting, job.cancel_export, job.export_progress = True, False, 0.0
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _preset(self):
        """The preset being exported, or None when an undo removed it (the export carries on regardless)."""
        return find_preset(self.scene_uid, self.preset_uid)

    def _job(self):
        return find_job(getattr(self, "scene_uid", 0), getattr(self, "preset_uid", ""))

    def _drain_output(self, job):
        """Process queued worker output. Returns True once the worker reported BATCH_STL_DONE."""
        while True:
            try: line = self.q.get_nowait().rstrip('\r\n')
            except queue.Empty: return False
            if line.startswith("BATCH_STL_TOTAL:"):
                try: self.total_operations = int(line.split(":")[1])
                except Exception: pass
            elif line.startswith("BATCH_STL_PROGRESS:"):
                try:
                    self.current_op = int(line.split(":")[1])
                    job.export_progress = self.current_op / max(1, self.total_operations)
                except Exception: pass
            elif line.startswith("BATCH_STL_DONE"):
                return True
            elif line:
                log_to_console(job, line)

    def modal(self, context, event):
        try:
            job = self._job()
            if job is None:  # a file load cleared the jobs: this export belonged to a file that is gone
                self.cleanup(context)
                return {'CANCELLED'}

            if job.cancel_export:
                self.cleanup(context)
                self.report({'WARNING'}, f"Export cancelled for {self.preset_name}.")
                return {'CANCELLED'}

            if event.type == 'TIMER':
                elapsed = time.perf_counter() - self.export_start_time
                finished = self._drain_output(job)
                worker_exited = self.process.poll() is not None
                if worker_exited and not finished:
                    # The worker prints BATCH_STL_DONE right before exiting; let the reader thread flush before judging.
                    self.t.join(timeout=2.0)
                    finished = self._drain_output(job)

                if finished:
                    self.cleanup(context)
                    preset = self._preset()
                    if preset: preset.last_export_time = elapsed
                    log_to_console(job, f"=== BATCH EXPORT COMPLETE ({elapsed:.4f}s) ===")
                    self.report({'INFO'}, f"Batch Export {self.preset_name} Complete in {elapsed:.2f}s.")
                    redraw_sidebars(context)
                    return {'FINISHED'}

                if worker_exited:
                    exit_code = self.process.returncode
                    self.cleanup(context)
                    log_to_console(job, f"[!] CRASH DETECTED: Worker exited with code {exit_code} before finishing.")
                    self.report({'ERROR'}, f"Background worker crashed for preset {self.preset_name}.")
                    redraw_sidebars(context)
                    return {'CANCELLED'}

                job.export_status = f"Obj {self.current_op}/{self.total_operations} | {elapsed:.1f}s" if (self.total_operations > 1 or self.current_op > 0) else f"Spawning Worker... ({elapsed:.1f}s)"
                redraw_sidebars(context)
        except Exception:
            traceback.print_exc()
            self.cleanup(context)
            self.report({'ERROR'}, "Unexpected error during batch export.")
            return {'CANCELLED'}
        return {'PASS_THROUGH'}

    def cancel(self, context):
        # Called by Blender when the modal is torn down from outside (e.g. loading another file).
        self.cleanup(context)

    def cleanup(self, context=None):
        if context and getattr(self, '_timer', None):
            try: context.window_manager.event_timer_remove(self._timer)
            except Exception: pass
            self._timer = None
        try:
            job = self._job()
            if job: job.is_exporting, job.cancel_export, job.export_progress, job.export_status = False, False, 0.0, ""
        except Exception: pass
        if getattr(self, 'process', None):
            try:
                if self.process.poll() is None:
                    self.process.kill()
                    self.process.wait(timeout=1.0)
            except Exception: pass
        if getattr(self, 't', None) and self.t.is_alive():
            self.t.join(timeout=0.5)
        if hasattr(self, 'temp_dir') and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

# ==============================================================================
# === [ 5. UI LISTS & PANELS ] ===
# ==============================================================================

class BATCH_STL_UL_presets(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        job = get_job(item)
        row = layout.row(align=True)
        prop_row = row.row(align=True)
        prop_row.enabled = not is_any_exporting()
        prop_row.prop(item, "name", text="", emboss=False)

        metrics = _ui_cache.get("preset_metrics", {}).get(index, {"has_ovr": False, "has_perm": False})
        icon_row = prop_row.row(align=True)
        icon_row.alignment = 'RIGHT'
        icon_row.label(text="", icon=ICONS['SWEEP'] if metrics["has_perm"] else ICONS['BLANK'])
        icon_row.label(text="", icon=ICONS['NODE'] if metrics["has_ovr"] else ICONS['BLANK'])
        prop_row.prop(item, "preset_prefix", text="", emboss=False, icon=ICONS['DIR'])

        if job and job.is_exporting:
            row.prop(job, "export_progress", text=job.export_status, slider=True)
            row.operator("batch_stl.cancel_export", text="", icon=ICONS['CANCEL']).preset_uid = item.uid
        else:
            row.operator("export_scene.batch_stl_multi", text="", icon=ICONS['EXPORT']).preset_index = index

class BATCH_STL_UL_collections(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop_search(item, "collection_name", bpy.data, "collections", text="", icon=ICONS['COLLECTION'])
        row.separator(factor=0.5)
        sub_row = row.row(align=True)
        op = sub_row.operator("batch_stl.table_action", text="", icon=ICONS['TAG'], depress=item.use_tag)
        op.action = 'TOGGLE_COLLECTION_USE_TAG'; op.c_idx = index
        sub_row.separator(factor=0.5)
        sub_row.row(align=True).prop(item, "tag", text="", emboss=False)
        row.prop(item, "sub_path", text="", emboss=False, icon=ICONS['DIR'])

class BATCH_STL_UL_objects(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        split = layout.split(factor=0.45)
        row = split.row(align=True)
        op = row.operator("batch_stl.table_action", text="", icon=ICONS['CHECK_ON'] if item.export else ICONS['CHECK_OFF'], emboss=False)
        op.action = 'TOGGLE_OBJECT_EXPORT'; op.o_idx = index
        row.prop(item, "export_name", text="", emboss=False)

        tools = split.row(align=True)
        tools.label(text="", icon=ICONS['TAG'])
        tools.prop(item, "tag", text="", emboss=False)
        tools.separator(factor=0.5)
        tools.label(text="", icon=ICONS['DIR'])
        tools.prop(item, "sub_path", text="", emboss=False)

class BATCH_STL_UL_console_logs(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.text)

def draw_inline_controls(layout, operator_id, use_clipboard=False):
    row = layout.row(align=True)
    row.operator_context = 'INVOKE_DEFAULT'
    row.operator(operator_id, icon=ICONS['ADD'], text="").action = 'ADD'
    row.operator(operator_id, icon=ICONS['DEL'], text="").action = 'REMOVE'
    row.operator(operator_id, icon=ICONS['UP'], text="").action = 'UP'
    row.operator(operator_id, icon=ICONS['DOWN'], text="").action = 'DOWN'
    if use_clipboard:
        row.operator(operator_id, icon=ICONS['COPY'], text="").action = 'COPY'
        row.operator(operator_id, icon=ICONS['PASTE'], text="").action = 'PASTE'

def format_count(stats, key="exp"):
    """A stats count; '5000+' when counting stopped at COUNT_LIMIT for some object."""
    return f"{stats[key]}+" if stats.get("capped") else stats[key]

def draw_stats_table(parent_layout, stats_list):
    box = parent_layout.box()
    row = box.row(align=True)
    row.alignment = 'CENTER'
    for i, (val, icon) in enumerate(stats_list):
        if i > 0: row.separator(factor=2.0)
        row.label(text=str(val), icon=icon)

def draw_overrides_table(*args, **kwargs):
    with cached_menu_items(): _draw_overrides_table(*args, **kwargs)

def _draw_overrides_table(layout, wm, nodegroups, is_collection, is_open_prop, title_text, is_preset=False, is_global=False, is_locked=False):
    # Setup inline helper to simplify conditional operator generation drastically
    def draw_op(parent, action, icon, depress=False, ng_idx=-1, n_idx=-1, i_idx=-1, v_idx=-1):
        parent.operator_context = 'INVOKE_DEFAULT'
        op = parent.operator("batch_stl.table_action", text="", icon=icon, depress=depress)
        op.action, op.is_collection, op.is_preset, op.is_global = action, is_collection, is_preset, is_global
        op.ng_idx, op.n_idx, op.i_idx, op.v_idx = ng_idx, n_idx, i_idx, v_idx
        return op

    def draw_input_name(parent, inp, ng, node):
        row = parent.row(align=True)
        row.alert = not is_override_input_valid(bpy.data.node_groups.get(ng.group_name), node, inp)
        row.prop(inp, "name", text="")

    box = layout.box()

    header_row = box.row()
    header_row.enabled = not is_locked
    is_open = getattr(wm, is_open_prop)
    icon_open = ICONS['DOWN'] if is_open else ICONS['RIGHT']
    header_row.prop(wm, is_open_prop, text="", icon=icon_open, emboss=False)

    icon_header = ICONS['GLOBAL'] if is_global else (ICONS['PRESET'] if is_preset else (ICONS['COLLECTION'] if is_collection else ICONS['OBJECT']))
    header_row.label(text="", icon=ICONS['OVR'])
    header_row.label(text=title_text, icon=icon_header)

    op_row = header_row.row(align=True)
    op_row.enabled = not is_locked
    draw_op(op_row, 'ADD_GROUP', ICONS['ADD'])
    draw_op(op_row, 'PASTE_GROUP', ICONS['PASTE'])

    # Collapsed, a table with inputs switches to the clear view: only the inputs and their values, without the node
    # group / node rows around them. Without inputs it collapses completely.
    clear = not is_open
    if clear and not any(node.inputs for ng in nodegroups for node in ng.nodes):
        return

    content_col = box.column()
    content_col.enabled = not is_locked

    if len(nodegroups) == 0:
        content_col.label(text="No overrides defined.")
        return

    def resolve_block(block, ctx, ng, node=None):
        """Context inside a block plus its merge result per kind: {kind: (hits, dead)}, recording its new names."""
        res = {}
        for kind in SCOPE_KINDS:
            ctx, hits, dead = scope.resolve(kind, block_parts(block, kind), ctx, _block_desc(kind, ng, node))
            res[kind] = (hits, dead)
        return ctx, res

    def merge_state(res):
        """(dead, text, tooltip) of a block whose sub-folder or tag merges into upstream names, else None. Dead when
        the chosen names can never exist together (the block never applies). The tooltip is taken now: names added
        further down the stack must not change it."""
        hits = [(k, n) for k in SCOPE_KINDS for n in res[k][0]]
        dead = [(k, n) for k in SCOPE_KINDS for n in res[k][1]]
        if not hits and not dead: return None
        segments = []
        d_names, t_names = res["dir"][0] + res["dir"][1], res["tag"][0] + res["tag"][1]
        if d_names: segments.append(" / ".join(d_names))
        if t_names: segments.append("name " + " + ".join(t_names))
        text = ("Never applies: " if dead else "Merges into ") + " · ".join(segments)
        return bool(dead), text, merge_tooltip(hits, dead, scope)

    def draw_merge_bar(parent, state):
        """Bar on top of a merging block: marks the block and its children. Blue for a merge, red when it is dead."""
        if not state: return
        dead, text, tooltip = state
        row = parent.row(align=True)
        row.alert = dead
        row.operator("batch_stl.merge_info", text=text, icon=ICONS['ERROR'] if dead else ICONS['MERGE'], depress=not dead).info = tooltip

    def draw_merge_icon(parent, merges):
        """Clear view: the merge bars of an input's hidden node group / node as one icon at the start of its row.
        `merges` is [(block name, merge_state)]; without merges the slot stays blank, so the names line up."""
        if merges:
            dead = any(state[0] for _where, state in merges)
            row = parent.row(align=True)
            row.alert = dead
            op = row.operator("batch_stl.merge_info", text="", icon=ICONS['ERROR'] if dead else ICONS['MERGE'], depress=not dead)
            op.info = "\n\n".join(f"{where}: {text}\n{tooltip}" for where, (_dead, text, tooltip) in merges)
        else:
            parent.label(text="", icon=ICONS['BLANK'])
        parent.separator(factor=0.5)

    def merged(res, kind): return bool(res[kind][0] or res[kind][1])

    def draw_input(parent, ng_idx, ng, ng_ptr, n_idx, node, n_ctx, i_idx, inp, merges=None):
        """Rows of one input (its first value on the input's row, the others below), then record what it creates.
        `merges` is given in the clear view only (see draw_merge_icon)."""
        input_layout = parent.column()
        # Wrap in array to ensure rendering block triggers at least once even if 'values' logic is empty
        values = inp.values if inp.values else [None]

        for visual_v, (v_idx, val) in enumerate(get_sorted_values(ng_ptr, node, inp, values)):
            i_first = (visual_v == 0)
            i_row = input_layout.row(align=True)

            s_main = i_row.split(factor=0.35, align=False)
            c_inp = s_main.row(align=True)

            if i_first:
                if merges is not None: draw_merge_icon(c_inp, merges)
                action_icon = ICONS['SWEEP'] if val and getattr(val, "use_sweep", False) else ICONS['ADD']
                draw_op(c_inp, 'VALUE_ACTION', action_icon, depress=(action_icon == ICONS['SWEEP']),
                        ng_idx=ng_idx, n_idx=n_idx, i_idx=i_idx, v_idx=v_idx if val else -1)
                draw_input_name(c_inp, inp, ng, node)
            else:
                c_inp.alignment = 'RIGHT'

            s_val = s_main.split(factor=0.5, align=False)
            c_val = s_val.row(align=True)
            c_dir = s_val.row(align=True)

            # Handing unpopulated values safely
            if val is None:
                continue

            # Render Value Properties
            val_valid = is_override_val_valid(inp, val, ng_ptr, node)
            c_val_prop = c_val.row(align=True)
            c_val_prop.alert = not val_valid
            if getattr(val, "use_sweep", False):
                if inp.override_type in ('FLOAT', 'INT'):
                    c_val_prop.prop(val, "sweep_start", text="")
                    c_val_prop.prop(val, "sweep_step", text="")
                    c_val_prop.prop(val, "sweep_count", text="")
                elif inp.override_type == 'STRING':
                    c_val_prop.prop(val, "sweep_range", text="")
                elif inp.override_type in ['BOOLEAN', 'MENU']:
                    sub = c_val_prop.row(align=True); sub.active = False
                    if inp.override_type == 'BOOLEAN':
                        sub.label(text="True & False")
                    else:
                        n_items = len(get_menu_switch_items(ng_ptr, node.name, inp.name)) if ng_ptr else 0
                        sub.label(text=f"{n_items} values")
            else:
                prop_map = {'BOOLEAN': "value_menu", 'INT': "value_string", 'FLOAT': "value_string", 'STRING': "value_string", 'MENU': "value_menu"}
                prop_name = prop_map.get(inp.override_type)
                if prop_name:
                    c_val_prop.prop(val, prop_name, text="")
                    if prop_name == "value_string":
                        # Plain text fields have no built-in clear button (only search fields do), so add one
                        op = c_val_prop.operator("batch_stl.table_action", text="", icon='PANEL_CLOSE', emboss=False)
                        op.action, op.is_collection, op.is_preset, op.is_global = 'CLEAR_VALUE_STRING', is_collection, is_preset, is_global
                        op.ng_idx, op.n_idx, op.i_idx, op.v_idx = ng_idx, n_idx, i_idx, v_idx
                else:
                    c_val_prop.label(text="Unsupported socket type", icon=ICONS['ERROR'])

            # Directory and filename tag: each toggle drives only its own field. The toggles are property buttons, not
            # operators, so Blender's drag-toggle sets several of them in one stroke (and one undo step).
            # A folder / token that merges into an upstream one shows the merge icon on its toggle; red when
            # it cannot exist in this block's branch (the value never applies).
            v_res = scope.resolve_value(value_field_anchors(val), n_ctx)[1]
            (vd_hits, vd_dead), (vt_hits, vt_dead) = v_res["dir"], v_res["tag"]
            s_dir_tag = c_dir.split(factor=0.5, align=True)
            c_d = s_dir_tag.row(align=True)
            c_d.alert = bool(vd_dead)
            c_d.prop(val, "use_dir", text="", icon=ICONS['MERGE'] if vd_hits or vd_dead else ICONS['DIR'], toggle=True)
            c_d_field = c_d.row(align=True); c_d_field.active = val.use_dir
            c_d_field.prop(val, "dir_tag", text="")
            c_t = s_dir_tag.row(align=True)
            c_t.alert = bool(vt_dead)
            c_t.prop(val, "use_tag", text="", icon=ICONS['MERGE'] if vt_hits or vt_dead else ICONS['TAG'], toggle=True)
            c_t_field = c_t.row(align=True); c_t_field.active = val.use_tag
            c_t_field.prop(val, "tag", text="")

        scope.add_input(ng, ng_ptr, node, inp, n_ctx)

    # Folders / filename tokens that exist so far and their branch contexts, built in export order while drawing.
    # The clear view walks the same blocks (it needs their context and names) without drawing their rows.
    scope = upstream_scope(nodegroups[0])[0]
    clear_layout = content_col.box().column() if clear else None

    for ng_idx, ng in enumerate(nodegroups):
        ng_ptr = bpy.data.node_groups.get(ng.group_name)
        ng_ctx, ng_res = resolve_block(ng, frozenset(), ng)
        ng_merge = merge_state(ng_res)

        if not clear:
            ng_layout = content_col.box().column()
            draw_merge_bar(ng_layout, ng_merge)
            ng_row = ng_layout.row(align=True)

            if ng.group_name:
                draw_op(ng_row, 'ADD_NODE', ICONS['ADD'], ng_idx=ng_idx)
            ng_sub = ng_row.row(align=True)
            ng_sub.alert = not is_override_group_valid(ng)
            ng_sub.prop(ng, "group_name", text="")
            ng_row.prop(ng, "sub_path", text="", icon=ICONS['MERGE'] if merged(ng_res, "dir") else ICONS['DIR'])
            ng_row.prop(ng, "tag", text="", icon=ICONS['MERGE'] if merged(ng_res, "tag") else ICONS['TAG'])
            draw_op(ng_row, 'MOVE_GROUP_UP', ICONS['UP'], ng_idx=ng_idx)
            draw_op(ng_row, 'MOVE_GROUP_DOWN', ICONS['DOWN'], ng_idx=ng_idx)
            draw_op(ng_row, 'COPY_GROUP', ICONS['COPY'], ng_idx=ng_idx)

            if not ng.nodes:
                continue

            n_split = ng_layout.split(factor=0.03)
            n_split.column()
            nodes_col = n_split.column()
            nodes_box = nodes_col.box() if len(ng.nodes) > 1 else nodes_col
            nodes_layout = nodes_box.column()

        for n_idx, node in enumerate(ng.nodes):
            n_ctx, n_res = resolve_block(node, ng_ctx, ng, node)
            n_merge = merge_state(n_res)

            if clear:
                inputs_layout = clear_layout
                merges = [(where, state) for where, state in ((_block_where(ng), ng_merge), (_block_where(ng, node), n_merge)) if state]
            else:
                node_layout = nodes_layout.box().column()
                draw_merge_bar(node_layout, n_merge)

                n_row = node_layout.row(align=True)
                draw_op(n_row, 'ADD_INPUT', ICONS['ADD'], ng_idx=ng_idx, n_idx=n_idx)
                n_sub = n_row.row(align=True)
                n_sub.alert = not is_override_node_valid(ng_ptr, node)
                n_sub.prop(node, "name", text="", icon=ICONS['NODE'])
                n_row.prop(node, "sub_path", text="", icon=ICONS['MERGE'] if merged(n_res, "dir") else ICONS['DIR'])
                n_row.prop(node, "tag", text="", icon=ICONS['MERGE'] if merged(n_res, "tag") else ICONS['TAG'])

                if len(ng.nodes) > 1:
                    draw_op(n_row, 'MOVE_NODE_UP', ICONS['UP'], ng_idx=ng_idx, n_idx=n_idx)
                    draw_op(n_row, 'MOVE_NODE_DOWN', ICONS['DOWN'], ng_idx=ng_idx, n_idx=n_idx)

                i_split = node_layout.split(factor=0.03)
                i_split.column()
                inputs_layout = i_split.column().box().column()
                merges = None

            for i_idx, inp in enumerate(node.inputs):
                draw_input(inputs_layout, ng_idx, ng, ng_ptr, n_idx, node, n_ctx, i_idx, inp, merges)


class VIEW3D_PT_batch_export_stl_main(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Combi Export"
    bl_label = "Combi Export"

    def draw_header(self, context):
        self.layout.label(text="", icon=ICONS['EXPORT'])

    def draw_header_preset(self, context):
        layout = self.layout
        any_exporting = is_any_exporting()

        row = layout.row(align=True)
        sub_row = row.row(align=True)
        sub_row.enabled = not any_exporting
        sub_row.operator("batch_stl.import_presets_json", text="", icon=ICONS['IMPORT'])
        sub_row.operator("batch_stl.export_presets_json", text="", icon=ICONS['EXPORT'])
        row.separator()

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        any_exporting = is_any_exporting()

        dir_col = layout.column()
        dir_col.enabled = not any_exporting
        dir_col.prop(scene, "batch_stl_root_dir")

        layout.separator()

        # Global Overrides
        g_col = layout.column()
        g_col.enabled = not any_exporting
        draw_overrides_table(g_col, context.window_manager, scene.batch_stl_global_nodegroups, False, "batch_stl_ui_global_ovr", "Global Overrides", is_global=True, is_locked=any_exporting)


class VIEW3D_PT_batch_export_stl_info(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Combi Export"
    bl_label = "Info"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon=ICONS['INFO'])

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        any_exporting = is_any_exporting()

        tab_row = layout.row()
        wm = context.window_manager
        tab_row.prop(wm, "batch_stl_info_tab", expand=True)

        if wm.batch_stl_info_tab == 'LOG':
            active_job = get_job(get_active_preset(scene))
            log_row = layout.row()

            if active_job:
                col = log_row.column(align=True)
                col.template_list("BATCH_STL_UL_console_logs", "", active_job, "console_logs", active_job, "console_index", rows=6)
            else:
                col = log_row.column()
                col.label(text="No export log for this preset yet.", icon=ICONS['INFO'])

            log_tools = log_row.column(align=True)
            log_tools.prop(wm, "batch_stl_verbose_console", text="", toggle=True, icon=ICONS['CONSOLE'])

            if active_job:
                log_tools.separator()
                clear_col = log_tools.column(align=True)
                clear_col.enabled = not any_exporting
                clear_col.operator("batch_stl.clear_console", text="", icon=ICONS['DEL'])

        elif wm.batch_stl_info_tab == 'TREE':
            tree_dict, duplicates = _ui_cache.get("tree", ({}, set()))
            if duplicates:
                warn_box = layout.box()
                warn_row = warn_box.row()
                warn_row.label(text=f"WARNING: {len(duplicates)} naming collisions detected! Files will be overwritten.", icon=ICONS['ERROR'])

            # Tree on the left, a vertical strip of icon buttons on the right (same layout as UIList side buttons).
            tree_row = layout.row()
            col = tree_row.column(align=True)
            draw_tree_dict(col, tree_dict, duplicates=duplicates)
            expand_tools = tree_row.column(align=True)
            expand_tools.prop(wm, "batch_stl_info_global", text="", toggle=True, icon=ICONS['GLOBAL'])
            expand_tools.separator()
            expand_tools.operator("batch_stl.tree_expansion", text="", icon=ICONS['EXPAND_ALL']).mode = 'EXPAND_ALL'
            expand_tools.operator("batch_stl.tree_expansion", text="", icon=ICONS['COLLAPSE_ALL']).mode = 'COLLAPSE_ALL'
            expand_tools.operator("batch_stl.tree_expansion", text="", icon=ICONS['EXPAND_LAST']).mode = 'EXPAND_LAST'

        layout.separator()

        tip_box = layout.box()
        tip_header = tip_box.row()
        icon_tip = ICONS['DOWN'] if wm.batch_stl_ui_tips else ICONS['RIGHT']
        tip_header.prop(wm, "batch_stl_ui_tips", text="", icon=icon_tip, emboss=False)
        tip_header.label(text="EXTENSION GUIDE", icon=ICONS['INFO'])

        if wm.batch_stl_ui_tips:
            box_col = tip_box.column()

            box_col.label(text="Hierarchical Overrides (Priority):", icon=ICONS['OVR'])
            split = box_col.split(factor=0.05)
            split.column()
            child_col = split.column()
            child_col.label(text="  • Global > Preset > Collection > Object", icon=ICONS['BLANK'])
            child_col.label(text="  • Modifiers: Leave Node empty or set to <Modifier Interface>", icon=ICONS['BLANK'])
            child_col.label(text="  • Group Output: Can be targeted to override what the group outputs", icon=ICONS['NODE'])
            box_col.separator()

            box_col.label(text="Shortcuts & Ergonomics:", icon=ICONS['INFO'])
            split = box_col.split(factor=0.05)
            split.column()
            child_col = split.column()
            child_col.label(text="  • Shift + Add Input (+): Auto-populates all exposed inputs", icon=ICONS['ADD'])
            child_col.label(text="  • Shift + Up/Down: Move group to parent tier / copy to all child tiers", icon=ICONS['UP'])
            child_col.label(text="  • Copy/Paste/Import/Export: Transfer configurations seamlessly", icon=ICONS['COPY'])
            child_col.label(text="  • Instant Deletion: Empty a field & submit, or click its X, to delete it", icon=ICONS['DEL'])
            child_col.label(text="  • Validation: Invalid inputs are rejected and reset safely", icon=ICONS['CHECK_ON'])
            box_col.separator()

            box_col.label(text="Parametric Sweeping:", icon=ICONS['SWEEP'])
            split = box_col.split(factor=0.05)
            split.column()
            child_col = split.column()
            child_col.label(text="  • Float/Int Ranges: Define Start, Step, and Count", icon=ICONS['BLANK'])
            child_col.label(text="  • Menus/Bools: Auto-iterates True/False and Enum options", icon=ICONS['BLANK'])
            child_col.label(text="  • Shift + Sweep (+): Populates all menu/bool values automatically", icon=ICONS['SWEEP'])
            box_col.separator()

            box_col.label(text="Directory & Naming Tags:", icon=ICONS['FILE'])
            split = box_col.split(factor=0.05)
            split.column()
            child_col = split.column()
            child_col.label(text="  • Directory (Folder): Routes variant to a sub-folder named by its field", icon=ICONS['DIR'])
            child_col.label(text="  • Tag (Bookmark): Adds the value to the filename, named by its field", icon=ICONS['TAG'])
            child_col.label(text="  • Filename: Object, then tags in stack order (Global > ... > Object)", icon=ICONS['BLANK'])
            child_col.label(text="  • Collection/Object/Group/Node tags: plain tokens, '/' between tokens", icon=ICONS['BLANK'])
            child_col.label(text="  • Each toggle enables only its own field", icon=ICONS['BLANK'])
            child_col.label(text="  • Value rules (both fields): [ tag ] = Replace, [ _tag ] = Append, [ tag_ ] = Prepend", icon=ICONS['BLANK'])
            box_col.separator()

            box_col.label(text="Branch Merging:", icon=ICONS['MERGE'])
            split = box_col.split(factor=0.05)
            split.column()
            child_col = split.column()
            child_col.label(text="  • Name an upstream folder or filename token to merge into that branch only", icon=ICONS['BLANK'])
            child_col.label(text="  • Values merge only by a plain name; a name derived from the value is new", icon=ICONS['BLANK'])
            child_col.label(text="  • Tag merges append new tokens at the end of the filename", icon=ICONS['TAG'])
            child_col.label(text="  • B = only B permutations, C/2 = only C2, 2 = every 2 branch", icon=ICONS['BLANK'])
            child_col.label(text="  • Folder/tag fields list upstream names that fit the current branch", icon=ICONS['DIR'])
            child_col.label(text="  • Blue bar: block and children merge (hover for sources)", icon=ICONS['MERGE'])
            child_col.label(text="  • Red bar: names never exist together, block never applies", icon=ICONS['ERROR'])
            child_col.label(text="  • Collapsed table: inputs only, a merging group / node shows at the row start", icon=ICONS['RIGHT'])
            box_col.separator()

            box_col.label(text="Tree View & Output Console:", icon=ICONS['TREE'])
            split = box_col.split(factor=0.05)
            split.column()
            child_col = split.column()
            child_col.label(text="  • Global View: Show all presets or isolate current", icon=ICONS['GLOBAL'])
            child_col.label(text="  • Expand/Collapse All: Toggle full directory visibility", icon=ICONS['EXPAND_ALL'])
            child_col.label(text="  • Expand Last: Collapses tree and expands only the last sub-folders", icon=ICONS['EXPAND_LAST'])
            child_col.label(text="  • Verbose Console / Clear Log: Manage background worker output", icon=ICONS['CONSOLE'])



class VIEW3D_PT_batch_export_stl_presets(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Combi Export"
    bl_label = "Presets"

    @classmethod
    def poll(cls, context):
        return True

    def draw_header(self, context):
        self.layout.label(text="", icon=ICONS['PRESET'])

    def draw_header_preset(self, context):
        layout = self.layout
        scene = context.scene
        any_exporting = is_any_exporting()
        active_preset = get_active_preset(scene)

        row = layout.row(align=True)
        if active_preset and active_preset.last_export_time > 0:
            row.label(text=f"{active_preset.last_export_time:.2f}s", icon=ICONS['TIME'])
            row.separator()
        row.enabled = not any_exporting
        draw_inline_controls(row, "batch_stl.preset_actions", use_clipboard=True)
        row.separator()

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        any_exporting = is_any_exporting()
        active_preset = get_active_preset(scene)
        stats = _ui_cache.get("stats", {})

        content_col = layout.column(align=True)
        list_box = content_col.box()
        list_box.template_list("BATCH_STL_UL_presets", "", scene, "batch_stl_presets", scene, "batch_stl_preset_index", rows=3)

        locked_col = content_col.column(align=True)
        locked_col.enabled = not any_exporting

        g_stats = stats.get("global", {"presets": 0, "cols": 0, "objs": 0, "exp": 0})
        draw_stats_table(locked_col, [
            (g_stats['presets'], ICONS['PRESET']),
            (g_stats['cols'], ICONS['COLLECTION']),
            (g_stats['objs'], ICONS['OBJECT']),
            (format_count(g_stats), ICONS['SWEEP'])
        ])

        if active_preset:
            draw_overrides_table(locked_col, context.window_manager, active_preset.nodegroups, False, "batch_stl_ui_preset_ovr", f"Overrides for [ {active_preset.name} ]", is_preset=True, is_locked=any_exporting)


class VIEW3D_PT_batch_export_stl_collections(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Combi Export"
    bl_label = "Collections"

    @classmethod
    def poll(cls, context):
        return get_active_preset(context.scene) is not None

    def draw_header(self, context):
        self.layout.label(text="", icon=ICONS['COLLECTION'])

    def draw_header_preset(self, context):
        layout = self.layout
        any_exporting = is_any_exporting()

        row = layout.row(align=True)
        row.enabled = not any_exporting
        draw_inline_controls(row, "batch_stl.collection_actions", use_clipboard=True)
        row.separator()

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        any_exporting = is_any_exporting()
        active_preset = get_active_preset(scene)
        stats = _ui_cache.get("stats", {})

        layout.enabled = not any_exporting
        active_col = get_active_collection(active_preset)

        content_col = layout.column(align=True)
        list_box = content_col.box()
        list_box.template_list("BATCH_STL_UL_collections", "", active_preset, "collections", active_preset, "collection_index", rows=5)

        p_stats = stats.get("presets", {}).get(scene.batch_stl_preset_index, {"cols": 0, "objs": 0, "exp": 0})
        draw_stats_table(content_col, [
            (p_stats['cols'], ICONS['COLLECTION']),
            (p_stats['objs'], ICONS['OBJECT']),
            (format_count(p_stats), ICONS['SWEEP'])
        ])

        if active_col:
            draw_overrides_table(content_col, context.window_manager, active_col.nodegroups, True, "batch_stl_ui_collection_ovr", f"Overrides [ {active_col.collection_name or 'Shared'} ]", is_locked=any_exporting)


class VIEW3D_PT_batch_export_stl_objects(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Combi Export"
    bl_label = "Objects"

    @classmethod
    def poll(cls, context):
        active_preset = get_active_preset(context.scene)
        if not active_preset: return False
        return get_active_collection(active_preset) is not None

    def draw_header(self, context):
        self.layout.label(text="", icon=ICONS['OBJECT'])

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        any_exporting = is_any_exporting()
        active_preset = get_active_preset(scene)
        stats = _ui_cache.get("stats", {})

        layout.enabled = not any_exporting
        active_col = get_active_collection(active_preset)
        active_obj = get_active_object(active_col)

        content_col = layout.column(align=True)
        list_box = content_col.box()
        list_box.template_list("BATCH_STL_UL_objects", "", active_col, "objects", active_col, "object_index", rows=5)

        c_idx = active_preset.collection_index
        col_key = (scene.batch_stl_preset_index, c_idx)
        c_stats = stats.get("cols", {}).get(col_key, {"objs": 0, "exp": 0})
        draw_stats_table(content_col, [
            (c_stats['objs'], ICONS['OBJECT']),
            (format_count(c_stats), ICONS['SWEEP'])
        ])

        if active_obj:
            draw_overrides_table(content_col, context.window_manager, active_obj.nodegroups, False, "batch_stl_ui_object_ovr", f"Overrides [ {active_obj.name} ]", is_locked=any_exporting)

# ==============================================================================
# === [ 6. REGISTRATION & LIFECYCLE ] ===
# ==============================================================================

VALUE_PROPS = ('value_string', 'value_menu')

def sync_prev_values(scene):
    """The `prev_*` properties (what a rejected edit reverts to) are not stored in files saved before they existed:
    make them match the stored data."""
    def sync_groups(nodegroups):
        for ng in nodegroups:
            ng.prev_group_name = ng.group_name
            for n in ng.nodes:
                n.prev_name = n.name
                for inp in n.inputs:
                    inp.prev_name = inp.name
                    for v in inp.values:
                        for prop in VALUE_PROPS: setattr(v, "prev_" + prop, getattr(v, prop))

    sync_groups(scene.batch_stl_global_nodegroups)
    for preset in scene.batch_stl_presets:
        sync_groups(preset.nodegroups)
        for col in preset.collections:
            col.prev_collection_name = col.collection_name
            sync_groups(col.nodegroups)
            for obj in col.objects: sync_groups(obj.nodegroups)

# Version of the stored override data. A scene without a stored version predates versioning and is migrated on load.
# 1: the value folder name (dir_tag) is split from the filename tag (tag).
# 2: v1.0.0 typed values and sweeps become text; object entries drop `obj_ptr` (an ID pointer, which kept deleted
#    objects alive) for `obj_uid`; the UI state moves from the Scene to the WindowManager.
DATA_VERSION = 2

# Scene properties of the UI state before it moved to the WindowManager (version 2)
OLD_SCENE_UI_PROPS = ("batch_stl_ui_global_ovr_main", "batch_stl_ui_preset_ovr", "batch_stl_ui_global_ovr", "batch_stl_ui_local_ovr",
                      "batch_stl_ui_global_ovr_nested", "batch_stl_ui_local_ovr_nested", "batch_stl_ui_tips", "batch_stl_collapsed_dirs",
                      "batch_stl_info_tab", "batch_stl_info_global", "batch_stl_verbose_console")

def iter_scene_nodegroup_lists(scene):
    yield scene.batch_stl_global_nodegroups
    for preset in scene.batch_stl_presets:
        yield preset.nodegroups
        for col in preset.collections:
            yield col.nodegroups
            for obj in col.objects: yield obj.nodegroups

def iter_scene_inputs(scene):
    for nodegroups in iter_scene_nodegroup_lists(scene):
        for ng in nodegroups:
            for n in ng.nodes: yield from n.inputs

def del_stored_prop(owner, key):
    """Remove a stored value of a property this version no longer registers."""
    if key in owner.keys(): del owner[key]

def migrate_scene_data(scene):
    """Bring override data saved by older versions up to DATA_VERSION."""
    version = scene.batch_stl_data_version if scene.is_property_set("batch_stl_data_version") else 0
    if version >= DATA_VERSION: return
    with raw_edits():
        for inp in iter_scene_inputs(scene):
            for v in inp.values:
                # Before the split one tag named both the folder and the filename token
                if version < 1 and not v.dir_tag and v.tag: v.dir_tag = v.tag
                if version < 2:
                    # Only unversioned scenes can be v1.0.0 data, whose unedited typed fields were not stored
                    migrate_legacy_value(v, v.get, inp.override_type, fill_defaults=version < 1)
                    for key in LEGACY_VALUE_KEYS: del_stored_prop(v, key)
        if version < 2:
            for preset in scene.batch_stl_presets:
                for col in preset.collections:
                    for entry in col.objects: del_stored_prop(entry, "obj_ptr")
            for key in OLD_SCENE_UI_PROPS: del_stored_prop(scene, key)
        scene.batch_stl_data_version = DATA_VERSION

def stamp_data_version(scene):
    """Scenes created in this session hold current data: record that, so a later load does not migrate them."""
    if not scene.is_property_set("batch_stl_data_version"): scene.batch_stl_data_version = DATA_VERSION

def prepare_scenes():
    """Bring the data of every scene in the file up to date (after a load, or when the add-on is enabled)."""
    for scene in bpy.data.scenes:
        try:
            migrate_scene_data(scene)
            sync_prev_values(scene)
            restamp_object_uids(scene)
            ensure_preset_uids(scene)
        except Exception: traceback.print_exc()

@persistent
def reset_batch_stl_state(*args):
    try:
        for wm in bpy.data.window_managers: wm.batch_stl_jobs.clear()  # no export survives a file load
    except Exception: pass
    if hasattr(bpy.data, "scenes"): prepare_scenes()
    else: bpy.app.timers.register(prepare_scenes, first_interval=0.0)  # enabled from Preferences: bpy.data is still restricted
    mark_dirty()
    if "--batch-stl-headless" not in sys.argv and not bpy.app.timers.is_registered(rebuild_ui_cache_if_dirty):
        bpy.app.timers.register(rebuild_ui_cache_if_dirty)

classes = (
    BatchSTLLogLine, BatchSTLJob, BatchSTLValue, BatchSTLInput, BatchSTLNode, BatchSTLNodeGroup, BatchSTLObject, BatchSTLCollection, BatchSTLExportPreset,
    BATCH_STL_UL_presets, BATCH_STL_UL_collections, BATCH_STL_UL_objects, BATCH_STL_UL_console_logs,
    BATCH_STL_OT_clear_console, BATCH_STL_OT_preset_actions, BATCH_STL_OT_collection_actions, BATCH_STL_OT_table_action, BATCH_STL_OT_toggle_dir_tree, BATCH_STL_OT_tree_expansion, BATCH_STL_OT_merge_info, BATCH_STL_OT_cancel_export, BATCH_STL_OT_export_presets_json, BATCH_STL_OT_import_presets_json, EXPORT_OT_batch_stl_multi,
    VIEW3D_PT_batch_export_stl_main, VIEW3D_PT_batch_export_stl_info, VIEW3D_PT_batch_export_stl_presets, VIEW3D_PT_batch_export_stl_collections, VIEW3D_PT_batch_export_stl_objects
)

# Open state of the override tables (Global, Preset, Collection, Object)
UI_OPEN_PROPS = ("batch_stl_ui_global_ovr", "batch_stl_ui_preset_ovr", "batch_stl_ui_collection_ovr", "batch_stl_ui_object_ovr")
WM_PROPS = ("batch_stl_jobs", "batch_stl_verbose_console", "batch_stl_ui_tips", "batch_stl_collapsed_dirs", "batch_stl_info_tab",
            "batch_stl_info_global") + UI_OPEN_PROPS
SCENE_PROPS = ("batch_stl_data_version", "batch_stl_root_dir", "batch_stl_presets", "batch_stl_preset_index", "batch_stl_global_nodegroups")

def remove_stale_temp_dirs():
    """Remove the temp folders of exports that crashed. A recent one may belong to an export running in another Blender
    instance (its worker reads the folder after loading the file), so only old ones go."""
    try:
        tmp = tempfile.gettempdir()
        for f in os.listdir(tmp):
            path = os.path.join(tmp, f)
            if f.startswith("fast_batch_stl_") and time.time() - os.path.getmtime(path) > STALE_TEMP_AGE:
                shutil.rmtree(path, ignore_errors=True)
    except Exception: pass

def register():
    for cls in classes: bpy.utils.register_class(cls)

    if "--batch-stl-headless" not in sys.argv: remove_stale_temp_dirs()

    # Runtime and UI state lives on the WindowManager: not part of the scene's data, so undo never touches it
    WM = bpy.types.WindowManager
    WM.batch_stl_jobs = bpy.props.CollectionProperty(type=BatchSTLJob)
    WM.batch_stl_verbose_console = bpy.props.BoolProperty(name="Verbose Console Output", default=False)
    for prop in UI_OPEN_PROPS:
        setattr(WM, prop, bpy.props.BoolProperty(name="Show Node Groups & Nodes", default=True, description=(
            "Show the node group and node rows. Collapsed, only the inputs and their values are listed, "
            "and an input whose node group or node merges into an upstream branch shows the merge icon at its row start")))
    WM.batch_stl_ui_tips = bpy.props.BoolProperty(default=False)
    WM.batch_stl_collapsed_dirs = bpy.props.StringProperty(default="[]")
    WM.batch_stl_info_tab = bpy.props.EnumProperty(items=[('LOG', "Console Log", "", ICONS['CONSOLE'], 0), ('TREE', "Tree View", "", ICONS['TREE'], 1)], name="Info Tab", default='LOG', update=lambda s, c: mark_dirty())
    WM.batch_stl_info_global = bpy.props.BoolProperty(name="Global Mode", default=False, update=lambda s, c: mark_dirty())

    Scene = bpy.types.Scene
    Scene.batch_stl_root_dir = bpy.props.StringProperty(name="Root", default="//", subtype="DIR_PATH", update=mark_dirty)
    Scene.batch_stl_presets = bpy.props.CollectionProperty(type=BatchSTLExportPreset)
    Scene.batch_stl_global_nodegroups = bpy.props.CollectionProperty(type=BatchSTLNodeGroup)
    Scene.batch_stl_preset_index = bpy.props.IntProperty(name="Active Preset", default=0, update=mark_dirty)
    Scene.batch_stl_data_version = bpy.props.IntProperty(default=0, options={'HIDDEN'})

    reset_batch_stl_state(None)
    if reset_batch_stl_state not in bpy.app.handlers.load_post: bpy.app.handlers.load_post.append(reset_batch_stl_state)

    if "--batch-stl-headless" not in sys.argv:
        if batch_stl_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.append(batch_stl_depsgraph_handler)
        if batch_stl_undo_handler not in bpy.app.handlers.undo_post: bpy.app.handlers.undo_post.append(batch_stl_undo_handler)
        if batch_stl_undo_handler not in bpy.app.handlers.redo_post: bpy.app.handlers.redo_post.append(batch_stl_undo_handler)
        if not bpy.app.timers.is_registered(rebuild_ui_cache_if_dirty): bpy.app.timers.register(rebuild_ui_cache_if_dirty)

def unregister():
    if reset_batch_stl_state in bpy.app.handlers.load_post: bpy.app.handlers.load_post.remove(reset_batch_stl_state)
    if batch_stl_depsgraph_handler in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.remove(batch_stl_depsgraph_handler)
    if batch_stl_undo_handler in bpy.app.handlers.undo_post: bpy.app.handlers.undo_post.remove(batch_stl_undo_handler)
    if batch_stl_undo_handler in bpy.app.handlers.redo_post: bpy.app.handlers.redo_post.remove(batch_stl_undo_handler)
    if bpy.app.timers.is_registered(rebuild_ui_cache_if_dirty): bpy.app.timers.unregister(rebuild_ui_cache_if_dirty)

    for cls in reversed(classes):
        try: bpy.utils.unregister_class(cls)
        except RuntimeError: pass

    for p in WM_PROPS:
        if hasattr(bpy.types.WindowManager, p): delattr(bpy.types.WindowManager, p)
    for p in SCENE_PROPS:
        if hasattr(bpy.types.Scene, p): delattr(bpy.types.Scene, p)

if __name__ == "__main__":
    if "--batch-stl-headless" in sys.argv:
        if not hasattr(bpy.types.Scene, "batch_stl_root_dir"): register()
        run_headless_export(sys.argv[sys.argv.index("--batch-stl-headless") + 1])
    else:
        if not hasattr(bpy.types.Scene, "batch_stl_root_dir"): register()
