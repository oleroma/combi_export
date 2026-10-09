# Architecture Overview: Combi Export

`Combi Export` (`combi_export`, v1.0.0) is a high-performance, parametric batch export add-on for Blender 5.2+. It combines vectorized binary STL generation, multi-tier geometry node / modifier socket overrides, combinatorial parametric sweeping, and a dual-path execution engine (in-process synchronous export vs. isolated headless subprocess export).

---

## 1. Design Principles & Goals

1. **Non-destructive & Isolated**: Overriding geometry nodes or modifier inputs must never corrupt the active project file, viewport state, or undo history.
2. **Dual-Path Execution Model**:
   - **Direct Fast-Path**: Mesh objects without parameter permutations or overrides evaluate natively in the active scene using Blender's evaluated depsgraph and stream directly to disk.
   - **Headless Worker Process**: When overrides or sweeps are active, the add-on forks an independent background Blender process (`--factory-startup --python-exit-code 1 -b <temp.blend> -P <script> -- --batch-stl-headless <job.json>`) on a temporary copy of the project file. The main Blender UI stays completely responsive and interactive: undo, switching scenes and reordering presets do not affect a running export.
3. **Vectorized NumPy Disk I/O**: Direct memory extraction via `foreach_get` into contiguous NumPy structured arrays avoiding per-polygon Python iterations.
4. **Predictive UI & Safety**: Cached calculation (~10Hz) of directory structures and STL filenames with collapsible nested box visualization, and a collision check of every output path before an export starts.

---

## 2. System Architecture Diagram

```
+---------------------------------------------------------------------------------+
|                                Blender Main UI                                  |
|                                                                                 |
|  [ VIEW3D Panels ] ──> UI Cache Engine (10Hz Timer) ──> Directory Tree / Clashes |
|         │                                                                       |
|         ▼                                                                       |
|  [ Operator: EXPORT_OT_batch_stl_multi ]                                        |
+─────────┬───────────────────────────────────────┬───────────────────────────────+
          │                                       │
          │ (No Overrides / Sweeps)               │ (Overrides / Sweeps Present)
          ▼                                       ▼
+───────────────────────────+         +───────────────────────────────────────────+
|   Native Direct Export    |         |        Headless Worker Spawn Pipeline     |
|                           |         |                                           |
|  • Evaluated depsgraph    |         |  1. Save copy: wm.save_as_mainfile(copy)  |
|  • write_object_stl       |         |  2. Write job manifest (job.json)         |
|  • Main thread sync       |         |  3. subprocess.Popen(blender -b ...)      |
+───────────────────────────+         |  4. Modal handler / thread stdout queue   |
                                      +─────────────────────┬─────────────────────+
                                                            │
                                                            ▼
                                      +───────────────────────────────────────────+
                                      |         Headless Subprocess Engine        |
                                      |                                           |
                                      |  • Group objects by override signature    |
                                      |  • Dynamic depsgraph culling (lc.exclude) |
                                      |  • Branch-aware combinatorial sweep       |
                                      |  • Apply overrides -> depsgraph -> mesh   |
                                      |  • write_object_stl                       |
                                      |  • Revert baseline states                 |
                                      +───────────────────────────────────────────+
```

---

## 3. Data Model & Hierarchy

The add-on structures export configurations in a strictly scoped 8-tier hierarchy:

`Global` → `Preset` → `Collection` → `Object` → `NodeGroup` → `Node` → `Input Socket` → `Value / Sweep`

### Property Groups (`combi_export/__init__.py`)

1. **`BatchSTLExportPreset`**:
   - Holds preset name, root directory prefix (`preset_prefix`) and the duration of the last export (`last_export_time`).
   - Owns a collection of `BatchSTLCollection` mappings and preset-level `BatchSTLNodeGroup` overrides.
2. **`BatchSTLCollection`**:
   - Maps a Blender `bpy.data.collections` entry.
   - Holds subfolder paths (`sub_path`), tagging flags (`use_tag`, `tag`: filename token(s) at Collection level), collection-level pinned overrides (`nodegroups`), and synchronized object entries (`objects`).
3. **`BatchSTLObject`**:
   - Mirrors individual meshes/curves within a collection. `obj_uid` stores the object's `session_uid`, so an entry follows its object through renames (`sync_collection_objects`; name first, uid when the name is gone, a one-gone-one-new fallback for renames made while the add-on was off). It is not an ID pointer: that would add a user to the object, and deleting the object would then only unlink it. Session uids change when a file loads, so `restamp_object_uids` re-points the entries by name on load.
   - Provides per-object export toggling (`export`), file name (`export_name`: a get/set property that shows `name_override` or else the object name; setting it empty or to the object name clears the override, so an unedited entry follows renames), filename tag (`tag`: token(s) at Object level, no longer renames the object), object subfolder (`sub_path`), and object-level overrides (`nodegroups`).
4. **`BatchSTLNodeGroup`**:
   - Targets a Geometry Node tree by name (`group_name`).
   - Optional sub-folder path (`sub_path`) and filename tag tokens (`tag`, `/`-separated).
   - Contains a list of `BatchSTLNode` blocks.
5. **`BatchSTLNode`**:
   - Targets an internal node name within the node group, or `"<Modifier Interface>"` to target the modifier interface sockets directly. `Group Output` is a valid target (listed after the regular nodes so it never becomes the default); `Group Input` has no inputs and is not offered.
   - Optional sub-folder path (`sub_path`) and filename tag tokens (`tag`).
6. **`BatchSTLInput`**:
   - Targets an input socket (`name`) and its inferred data type (`override_type`: `FLOAT`, `INT`, `BOOLEAN`, `STRING`, `MENU`).
7. **`BatchSTLValue`**:
   - Unified string-based concrete parameter value (`value_string`) for Ints, Floats, and Strings, and `value_menu` for Enum/Booleans.
   - Sweep definitions using updated explicit terminology (`sweep_start`, `sweep_step`, `sweep_count`, `sweep_range`).
   - Configures the filename tag (`use_tag`, `tag`) and subfolder routing (`use_dir`, `dir_tag`); both names use the same append/prepend/replace rules.
8. **`BatchSTLJob`** (`WindowManager.batch_stl_jobs`):
   - Runtime export state of one preset, keyed by the preset's `uid` (`BatchSTLExportPreset.uid`, a uuid assigned by `ensure_preset_uids`) and its scene's `session_uid`: `is_exporting`, `cancel_export`, `export_progress`, `export_status` and the console log (`console_logs`). Unlike indices, pointers and names, both keys survive undo, reordering, renaming and scene switches (`get_job`, `find_job`, `find_preset`).
   - Lives on the WindowManager, so undo never rolls it back. Cleared on file load; removing a preset removes its job, and a preset cannot be removed while it exports.
10. **UI state** (`WindowManager.batch_stl_ui_*`, `batch_stl_info_tab`, `batch_stl_info_global`, `batch_stl_collapsed_dirs`, `batch_stl_verbose_console`): open panels, the info tab and the tree expansion live on the WindowManager, outside the scene data and its undo history.
9. **`BatchSTLLogLine`**:
   - Discrete line items of a job's console log (one entry per line, capped at 300).

### Scoping & Inheritance of Overrides

When generating variations for an object, overrides are gathered in hierarchical order:
1. `Global` (`scene.batch_stl_global_nodegroups`)
2. `Preset` (`preset.nodegroups`)
3. `Collection` (`collection.nodegroups`)
4. `Object` (`object.nodegroups`)

`resolve_overrides` then applies **most-specific-wins**: if a lower level defines the same socket (same target, node group, node and input name), the inherited values of higher levels for that socket are dropped. Several values on the same level remain variants. Objects are batched for the headless worker by `get_override_signature`, a fingerprint of the resolved overrides (values, sweeps, tags, folder flags, block sub-folders and block tags, and level).

### Filename Assembly (`format_export_filename`)
`file name` (`BatchSTLObject.export_name`) + `_token` for every token in hierarchy order: Global override tokens, Preset override tokens, Collection tag, Collection override tokens, Object tag, Object override tokens. `join_name_segments` collapses a collection / object tag into an equal neighbouring token; equal neighbouring override tokens stay (the walker already merged what merges). Folders (`build_export_dir_parts`) follow the same rule for the preset prefix and collection / object sub-folders. Override tokens come per level from the walker (`tags_by_level`); block and collection/object tags are split by `split_tag_parts` (`/` separates tokens, edge `_` dropped).

---

## 4. Execution Pipelines

### A. Fast Direct Path (Native In-Process)
- **Condition**: Decided per object: no override applies to it (none is defined, or none targets a node group its modifiers use).
- **Mechanism**:
  - Resolves the objects Blender evaluates in the mapped collections (`iter_export_objects`).
  - Computes evaluated geometry using `obj.evaluated_get(depsgraph).to_mesh()`, plus unrealized instances found through `depsgraph.object_instances` (`collect_instance_arrays`).
  - Streams geometry to disk using `write_object_stl`.
  - Cleans up evaluated mesh data via `to_mesh_clear()`.

### B. Headless Worker Pipeline (Process Isolation)
- **Condition**: Any override or sweep is defined in Global, Preset, Collection, or Object scope.
- **Workflow**:
  1. **Preparation**:
     - Checks every output path of the preset for collisions (`find_export_clashes`, no limit); a collision aborts the export before anything is written.
     - Saves a snapshot of current memory to a temporary file via `bpy.ops.wm.save_as_mainfile(filepath=..., copy=True)`.
     - Writes a job manifest `job.json` containing preset index, root directory path, and flags.
  2. **Process Spawn**:
     - Spawns background Blender: `[blender, "--factory-startup", "--python-exit-code", "1", ("--enable-autoexec",) "-b", temp_blend, "-P", script_file, "--", "--batch-stl-headless", job_json]`. `--enable-autoexec` is only added when the user allows Python auto-execution.
  3. **IPC & Streaming**:
     - A background reader thread drains worker `stdout` into a thread-safe `queue.Queue`.
     - A Blender modal timer operator (`0.1s`) polls the queue, updates preset progress bars (`BATCH_STL_PROGRESS:X`), and logs stdout into the preset's console UI.
  4. **Headless Execution Engine (`run_headless_export`)**:
     - **Phase 0 (Depsgraph Culling)**: Groups objects by override signature (`execution_batches`). Excludes unrelated layer collections (`lc.exclude = True`) to prevent Blender from evaluating unneeded geometry trees during node updates.
     - **Baseline Capture (`capture_baseline_states`)**: Caches baseline modifier socket values, internal node socket defaults, and node link connections.
     - **Branch-Aware Combinations (`generate_named_combinations`)**: Recursively picks one value per parameter pool (sweeps expanded) and keeps only the values that apply to the branch built so far, yielding each variant with its folder and filename naming. See *Branching & Merging* below.
     - **Evaluation Loop**:
       - Restores the baseline of every socket the variant leaves out (`revert_overrides(..., skip=override_targets(combo))`), so a parameter without a value in this branch is back at its default instead of keeping the previous variant's value.
       - Applies overrides via `apply_overrides`.
       - Calls `bpy.context.view_layer.update()` and fetches updated depsgraph.
       - Writes STL files via `export_object_stl` (a wrapper around `write_object_stl` that logs an unwritable file as `FAILED` and continues).
     - **Restoration (`revert_overrides`)**: Re-applies baseline values and links, restores layer collection exclusion states.
  5. **Teardown**:
     - Worker exits with code 0.
     - Main process modal handler catches `BATCH_STL_DONE`, stops modal timer, and removes temporary directory. Temp folders a crash left behind are removed when the add-on registers, once they are older than a day (a younger one may belong to an export running in another Blender instance).
     - A worker that exits without printing `BATCH_STL_DONE` is reported as a crash together with its exit code (1 for an uncaught Python error).

### C. Branching & Merging (`walk_override_branch`)
Folders and filename tokens created by values form branches. Overrides further down the stack can merge into an existing branch instead of multiplying every permutation. Folders (`dir`) and tokens (`tag`) are tracked as two parallel branches with the same rules; an override or value is active only when both its folder and its tag anchors fit.

- **One walker for generation and naming**: `walk_override_branch(overrides, combo)` walks the resolved overrides top to bottom (Global → Preset → Collection → Object, then node groups, nodes and inputs in UI order). It keeps the full folder path and token list of the branch and returns the active inputs, the filename tokens per level (`tags_by_level`) and the folder parts per level (`paths_by_level`). `generate_named_combinations` uses it to prune values that do not apply to a branch and yields the naming of the final walk with each variant, so generation and naming can never disagree (and each variant is walked once).
- **Branch names (`_known_branch_dirs`)**: `("dir", folder)` and `("tag", token)` keys that values *earlier* in the stack can generate (every sweep step included), recorded per override and per input.
- **Explicit value names only (`is_explicit_name`)**: a value's folder / token only goes through placement when its field names it outright (replace rule). Names derived from the value (blank, `_tag`, `tag_`) are always new, otherwise two independent inputs sharing a value (`1`, `True`) would merge into each other and silently lose variants. They are still branch names that blocks can anchor to.
- **Placement (`_place_dir_parts(parts, branch, known, kind)`)**, for each folder name or token of a block sub-folder/tag or explicitly named value folder/tag:
  - The name is already in the branch (any ancestor) → merge into it. Several names must appear in order.
  - It is a branch name missing from this branch → the override or value is inactive for this branch.
  - Any other name → a new folder below the branch's deepest folder / a new token at the end of the filename.
- **Pruning**: if no value of a parameter applies to the current branch, the parameter stays at its default for that branch. Combinations are deduplicated after inactive values are removed.

---

## 5. High-Performance Vectorized STL Writer

Instead of creating intermediate text or using standard single-threaded Python file writers, `mesh_to_stl_array` / `write_object_stl` implement direct binary packing:

1. **Triangulation**: Calls `mesh.calc_loop_triangles()` to ensure valid facet indices.
2. **Memory Extraction**:
   - `mesh.vertices.foreach_get("co", verts.ravel())` reads vertex coordinates directly into NumPy buffers.
   - `mesh.loop_triangles.foreach_get("vertices", tri_verts.ravel())` reads face indices.
   - `mesh.loop_triangles.foreach_get("normal", tri_normals.ravel())` reads precomputed loop normals.
3. **Matrix Transformations**:
   - Vertex coordinates are transformed by `matrix_world` using vectorized NumPy SIMD operations (`V' = V @ M.T + T`).
   - Normal vectors are transformed using the inverse matrix and re-normalized.
   - Automatically handles negative determinant matrices (flipped winding order) by swapping vertices $v_1$ and $v_2$.
4. **Structured Binary Array**:
   - Formatted using structured NumPy dtype:
     ```python
     STL_DTYPE = np.dtype([
         ('normals', np.float32, (3,)),
         ('v0', np.float32, (3,)),
         ('v1', np.float32, (3,)),
         ('v2', np.float32, (3,)),
         ('attr', np.uint16)
     ])
     ```
   - Written to disk as `STL_HEADER`, `struct.pack('<I', num_tris)`, then each record array (object mesh plus its instances) streamed with `ndarray.tofile()`, so the arrays are never concatenated in memory.
   - The output folder is created only when there is geometry to write; objects with no triangles are skipped.

---

## 6. UI Caching & Safety Subsystems

### UI Cache Engine (`rebuild_ui_cache_if_dirty`)
- Variant counts are cached per override signature within one rebuild (`count_override_combinations(ovrs, cache)`), and menu items are cached while the cache rebuilds or a panel draws (`cached_menu_items()`).
- When no block or explicitly named value names an upstream branch name (`_has_branch_anchors`), every combination of values is a variant: the count is the product of the pool sizes and generation skips the pruning walks. Otherwise counting enumerates and stops above `COUNT_LIMIT` (5000); the stats then show `5000+`. The walker caches what it derives from an override or pool value (block parts, value names, parameter keys) on the MockOverride / MockInput.
- Driven by a background timer (`bpy.app.timers`) running at ~10Hz with a dirty flag (`mark_dirty()`).
- The flag is set by property updates, undo/redo, and a `depsgraph_update_post` handler that reacts to collection changes (objects linked/unlinked), to objects in mapped collections being renamed or hidden, and to collections being excluded or disabled in viewports.
- **Evaluated objects only** (`live_collection_names`, `is_object_live`, `iter_export_objects`): counts, tree, collision check and both export paths only take objects Blender evaluates: in a collection that is neither excluded nor disabled in viewports (nor are its parents), and not disabled themselves. `evaluated_get()` returns the unmodified original for any other object, which would be exported without its modifiers; `write_object_stl` also skips an object whose evaluated copy is not `is_evaluated`.
- Recomputes statistics (preset counts, collections, exported object count, total permutation iterations).
- Computes directory hierarchies and leaf files in advance. The UI displays this with an uncollapsable root directory and dedicated side-column toolbar buttons for toggling global view and bulk expanding/collapsing.
- **Naming Collision Detection**: The tree view flags paths written more than once (its preview stops at `COUNT_LIMIT` files). The export runs `find_export_clashes` over every path without a limit, generating the variants once per override signature.

### Merge Predictions (`BranchScope`)
The folder fields (`sub_path` of node groups / nodes, `BatchSTLValue.dir_tag`) and tag fields (`tag` of node groups / nodes / values) offer upstream names and highlight merges without enumerating permutations. `BranchScope` mirrors the walker's rules:
- Keys are `(kind, name)` with kind `dir` or `tag`. Each is recorded with a description (*Branch* / *Name branch* for value names, *Folder* / *Tag* for block names), the inputs that create it and their alternatives (the names of different values or sweep steps of one input exclude each other; the folder and token of the *same* value coexist; a name created by several inputs excludes nothing) and the branch contexts it can exist in. `resolve_value` applies the explicit-name rule for value fields.
- `resolve(kind, parts, ctx)` walks names inside a context and returns the new context, the merged names (`hits`) and the branch names that cannot exist there (`dead`). Contexts mix both kinds, so a block placed in folder `C` only sees tokens of the C branch. A plain name that exists in several branches only adds the branches those places share.
- `upstream_scope(item)` builds the scope and context at any field from its `path_from_id()`. `make_scope_search_cb(kind, prop)` uses it for the search lists; it also applies the block's other field (sub-folder vs tag) and the typed `/` prefix, so `C/` only offers names that exist under `C`.
- `draw_overrides_table` builds the scope incrementally while drawing, in export order. Merging blocks get a blue `batch_stl.merge_info` bar (`Merges into C / 2 · name B`; red with *Never applies* for dead merges) whose tooltip lists the source of each name, and merging value folders / tags get the merge icon on their toggle.

### Undo Stack Protection & Validation
- **Single Undo Step per Edit**: Plain text fields (`value_string` for floats, ints and strings, sub-folders, tags) get their undo step from Blender when the edit is confirmed. Search fields (group, node, input, menu value, folder fields with upstream suggestions) are created without `UI_BUT_UNDO`, so their labelled `@edit_callback` pushes exactly one step; nested callbacks of a cascade are suppressed.
- **Empty Field Deletion**: Submitting an empty field (`""`) triggers an automatic GC deletion routine for Node Groups, Nodes, Inputs, and Values. The inline clear button on `value_string` fields (`table_action` → `CLEAR_VALUE_STRING`) empties the field from inside the operator, so the deletion runs through the same callback and is recorded as the operator's single undo step. Code that writes consistent data (socket defaults in `sync_input_type`, paste, import, migration) runs inside `raw_edits()`, so an empty default (a menu on the modifier interface reports `''`) never triggers a deletion.
- **Menu Items** (`get_menu_switch_items`): Menu Switch nodes list their items; menus on the modifier interface are read from a modifier that uses the group (`modifier_menu_items`, works through reroutes and nested groups); built-in and group node menu sockets have no listing API, so `socket_menu_items` assigns an invalid name, which Blender rejects without changing anything, and reads the items from the error. Draw code may not write, so it reuses the last lists found (`refresh_menu_items` runs in every cache rebuild). A value Blender still rejects at export is logged once as a warning (`report_apply_failure`).
- **Validation Engine**: Real-time validation checks against depsgraph interfaces ensure node groups, nodes, and inputs exist. Invalid targets are visually flagged and gracefully rejected or reset to the last known valid state.
- **Numeric Validation**: Float/Int values and sweep start/step must parse as numbers and the sweep step count must be at least 1; otherwise the field is flagged and export is blocked.
- **Hierarchical Propagation**: Multi-level operations (Shift + Up moves a group to the parent tier, Shift + Down copies it to every child tier) use `operator_context = 'INVOKE_DEFAULT'` to intercept modifier keys and explicitly iterate down the hierarchy.
- **Tree Expand Logic**: Complex layout logic (e.g., Expand Last) leverages recursive path traversal, resetting directory states and selectively expanding leaf nodes seamlessly without UI lockup.

---

## 7. Configuration Portability

The add-on implements full JSON schema serialization and deserialization (`BATCH_STL_OT_export_presets_json` / `BATCH_STL_OT_import_presets_json`):
- Serializes presets, collections, object lists, exclusion states, node group overrides (including block `sub_path` and `tag`), input types, values, sweeps, and tagging configurations (`use_dir`, `dir_tag`, `use_tag`, `tag`) into clean, version-agnostic JSON files.
- Values from files saved before the directory/tag split (no `dir_tag` key) get `dir_tag = tag` on import, so their folders keep their names. Version 1.0.0 files kept numbers in typed fields (`value_float`, `value_int`, `value_bool`, `sweep_start_float`, ...); `migrate_legacy_value` moves them into the text fields of the input's type.
- `.blend` data is versioned the same way: `Scene.batch_stl_data_version` (`DATA_VERSION`, now 2) is stamped on scenes edited with this version, and `migrate_scene_data` (on load / register, via `prepare_scenes`) brings older scenes up to date: 1 gives unversioned scenes `dir_tag = tag`; 2 migrates 1.0.0 typed values, removes the `obj_ptr` pointers of object entries (they kept deleted objects alive) and the UI properties that used to live on the Scene.
- Provides deep-copy and paste support across presets, collections, and node groups via internal clipboard buffers (`_clipboard`).
