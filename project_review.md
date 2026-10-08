# Combi Export — Project Review

Scope: [`__init__.py`](file:///d:/extensions/combi_export/combi_export/__init__.py) (3203 lines), [blender_manifest.toml](file:///d:/extensions/combi_export/combi_export/blender_manifest.toml), [README.md](file:///d:/extensions/combi_export/README.md), [ARCHITECTURE.md](file:///d:/extensions/combi_export/ARCHITECTURE.md). Working tree clean at `8569394`.

## Overall

The code is in good shape. The core engine is clean: override resolution, lazy combination generation, the vectorized STL writer, worker IPC and crash detection. The undo-isolation machinery is unusual but well commented. The normal transform (inverse, not transpose), the winding flip and the 80-byte header are all correct. The issues below are mostly about scale, silent data loss and doc drift, not basic correctness.

---

## 🔴 High

### 1. Tree view lists every combination on the main thread, with no limit
[`build_tree_dict`](file:///d:/extensions/combi_export/combi_export/__init__.py#L1126-L1173) loops over `generate_override_combinations()` for every object. It runs from the `bpy.app.timers` callback, which is on the main thread. Example: 4 sweeps × 20 steps × 50 objects = 8 M filenames, built again on every `mark_dirty()`. Blender freezes as long as the Tree tab is open. This goes against the README claim *"prevents UI stalls when evaluating large permutation matrices"*.
- **Fix:** compare `count_override_combinations()` to a limit (e.g. 5 000 files). Above it, show the totals and skip the full tree.

### 2. Naming collisions are only a warning, and float names lose precision
- Duplicates are only computed when the **Tree** tab is open. They are only checked across presets in Global view. [`invoke`](file:///d:/extensions/combi_export/combi_export/__init__.py#L2370-L2481) never checks them, so colliding files are silently overwritten. The README's *"preventing accidental file overwrites"* overstates this.
- [`evaluate_combo_naming`](file:///d:/extensions/combi_export/combi_export/__init__.py#L681) formats floats with `f"{val:g}"`, which keeps only **6 significant digits**:
  - `0.1234561` and `0.1234562` → both `0.123456`
  - `1234567.0` → `1.23457e+06`
- **Fix:** use `repr`-style formatting (or `f"{val:.10g}"`, then strip). Before export, compute the target paths (the cheap part of `build_tree_dict`). Block or confirm the export when paths collide.

### 3. Renaming an object silently deletes its per-object settings
Object entries are matched **by name** in [`sync_collection_objects`](file:///d:/extensions/combi_export/combi_export/__init__.py#L691-L707). A rename is detected by the depsgraph handler. The UI-cache timer then removes the old entry (with its tag, sub-folder and override groups) and adds a blank one. This happens in the background with no message. Undo can bring it back, but only if the user notices.
- **Fix:** store an `object: PointerProperty(type=bpy.types.Object)` and use the name only as a fallback. At minimum, move the entry over when exactly one name disappears and one appears in the same sync.

---

## 🟠 Medium

### 4. Overrides affect objects that don't use the node group
A Global or Preset override on `<Modifier Interface>` of group *X* sends **every** object in the preset to the headless worker. Each object is then exported N times under N names, even if it has no modifier using *X*. The result is identical files, and the inflated count also shows in the stats.
- **Fix (MODIFIER target):** in `get_flat_overrides` / batch building, drop overrides whose group is not on any of the object's `NODES` modifiers. NODE targets can do the same with a recursive check for group usage.

### 5. The depsgraph handler does O(n) work on every update
[`batch_stl_depsgraph_handler`](file:///d:/extensions/combi_export/combi_export/__init__.py#L1093-L1123) calls `mapped_objects_state()` on **every** `depsgraph_update_post`, i.e. every frame of a grab, sculpt stroke or playback. That call builds a frozenset of every object in every mapped collection, plus a full layer-collection walk.
- **Fix:** run it only when `depsgraph.id_type_updated('OBJECT')` / `'SCENE'` is true. Or only check `depsgraph.updates` for IDs whose name changed.

### 6. `bpy.path.clean_name` damages object names
[`format_export_filename`](file:///d:/extensions/combi_export/combi_export/__init__.py#L643-L646) replaces every non-`[A-Za-z0-9]` character: `Part-A.v2` → `Part_A_v2`, and `Болт` → `____`. Same-length non-Latin names collide. Tags and folders use the looser [`sanitize_name`](file:///d:/extensions/combi_export/combi_export/__init__.py#L142-L144), so object names are handled inconsistently.
- **Fix:** use `sanitize_name(name).strip(" .")` for object names too.

### 7. Worker culling may exclude referenced objects (needs a check in Blender)
[`run_headless_export`](file:///d:/extensions/combi_export/combi_export/__init__.py#L1309-L1322) excludes every layer collection that doesn't contain a batch object. Objects referenced through *Object Info*, *Collection Info*, Boolean cutters, Array/Curve objects and so on often live in a hidden "helpers" collection. They should still be pulled in as depsgraph dependencies. Still, a regression test of fast path vs. headless output on such a setup is worth doing.

### 8. Export jobs ignore which scene they belong to
`get_job()` uses only `preset_index`. [`_preset()`](file:///d:/extensions/combi_export/combi_export/__init__.py#L2483-L2485) re-resolves through `bpy.context.scene`. If the user switches scenes during a headless export, progress, cancel and `last_export_time` go to preset *i* of the **other** scene.
- **Fix:** store `scene.name` (or `session_uid`) on the job and in the operator.

---

## 🟡 Low / hardening

| # | Issue | Location |
|---|---|---|
| 9 | Undo guard only catches **Ctrl+Z**. Ctrl+Shift+Z, the Edit menu and Undo History get past it. | [L2515](file:///d:/extensions/combi_export/combi_export/__init__.py#L2515) |
| 10 | `get_sorted_values`: for a STRING input with both a sweep and plain values (possible through JSON import), the sort compares `0` with `str` → `TypeError`. The cache timer then retries every 0.25 s forever. | [L376-L383](file:///d:/extensions/combi_export/combi_export/__init__.py#L376-L383) |
| 11 | UI-state props (`batch_stl_ui_*`, `batch_stl_info_tab`, `batch_stl_collapsed_dirs`) are on **Scene**, so they create or get rolled back by undo steps. Moving them to `WindowManager` matches the "zero undo pollution" goal. | [L3164-L3169](file:///d:/extensions/combi_export/combi_export/__init__.py#L3164-L3169) |
| 12 | If Blender crashes mid-export, `fast_batch_stl_*` temp dirs (full `.blend` copies) are left behind. Remove stale ones in `register()`. | [L2446](file:///d:/extensions/combi_export/combi_export/__init__.py#L2446) |
| 13 | Windows reserved names (`CON`, `NUL`, `COM1`…) are not handled by `sanitize_name`. | [L140-L144](file:///d:/extensions/combi_export/combi_export/__init__.py#L140-L144) |
| 14 | No limit on `sweep_count`. A typo like `10000` freezes the stats and tree. | [L911](file:///d:/extensions/combi_export/combi_export/__init__.py#L911) |

### Dead code / naming
- `batch_stl_ui_global_ovr_nested` and `batch_stl_ui_local_ovr_nested` are registered but never used.
- `batch_stl_ui_global_ovr` is actually the **Collection** panel toggle (misleading name).
- The table actions `ADD_VALUE`, `DEL_VALUE`, `TOGGLE_SWEEP`, `DEL_VALUE_MIXED` are never emitted by the UI. `DEL_VALUE_OR_INPUT` only appears in `description`.
- The `(val, val, val)` vector fallback in [`apply_overrides`](file:///d:/extensions/combi_export/combi_export/__init__.py#L541-L544) can't run, because vector sockets are filtered out as unsupported.

---

## 📄 Doc drift (README)
- *"Float / Int Ranges … or string ranges"*: `sweep_range` only applies to STRING inputs.
- *"Displays the directory tree during setup"*: the default tab is `LOG`.
- *"Pre-Export Clash Detection … preventing overwrites"*: it only warns (see #2).
- *"Decoupled UI Cache prevents UI stalls"*: not true for large matrices (see #1).

## 🧹 Tooling
`ruff check` reports 61 findings. Almost all are `BLE001`, `S110` and `RUF012`. `RUF012` is a false positive for `bl_options`, and the blind excepts are intentional around Blender API calls. Add a `ruff.toml` that ignores these so real findings stand out.

## 🧪 Testing
There are no tests. The pure helpers are easy to test with `blender -b --factory-startup -P tests.py`: `apply_tag`, `split_path_parts`, `resolve_overrides`, `evaluate_combo_naming`, `parse_sweep_values`, `mesh_to_stl_array` (cube with mirrored matrix → winding/normals), and a fast-path vs. headless output comparison.

---

## Suggested order
1. #2 (float naming + export-time collision gate). Small change, prevents silent data loss.
2. #1 (tree limit).
3. #3 (object pointer mapping).
4. #4 / #5 (correct export counts, viewport performance).
