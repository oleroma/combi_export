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
   - Optional sub-folder path (`sub_path`) and filename tag tokens (`tag`), edited together in the table through `naming` (`\C\2\_B`, see *Expression Fields*).
   - Contains a list of `BatchSTLNode` blocks.
5. **`BatchSTLNode`**:
   - Targets an internal node name within the node group, or `"<Modifier Interface>"` to target the modifier interface sockets directly. `Group Output` is a valid target (listed after the regular nodes so it never becomes the default); `Group Input` has no inputs and is not offered.
   - Optional sub-folder path (`sub_path`) and filename tag tokens (`tag`), edited through `naming` like the node group's.
6. **`BatchSTLInput`**:
   - Targets an input socket (`name`) and its inferred data type (`override_type`: `FLOAT`, `INT`, `BOOLEAN`, `STRING`, `MENU`).
7. **`BatchSTLValue`**:
   - Unified string-based concrete parameter value (`value_string`) for Ints, Floats, and Strings, and `value_menu` for Enum/Booleans.
   - Sweep definitions using updated explicit terminology (`sweep_start`, `sweep_step`, `sweep_count`, `sweep_range`).
   - Configures the filename tag (`use_tag`, `tag`) and subfolder routing (`use_dir`, `dir_tag`). Since `DATA_VERSION` 3 both hold a template with `VALUE_SLOT` (`*`) where the value goes: folder names separated by `/` (`*`, `big`, `B/*`, `size_*`, `*_mm/x`), filename tokens separated by `_` (`*`, `big`, `B_*`, `*_mm`). `value_name_parts` turns a template into `(anchors, names)`: the typed names before the value are anchors, the value's own name (joined with the text around it) and everything after it are always new. An empty template is the value alone (never-edited fields).
   - The table edits all of it through one field, `expr` (`\B\15\_x_15`, see *Expression Fields*). It is computed (`get` / `set`), so the fields above stay the only stored data.
   - New values are plain (`add_value` writes `use_dir = False`; the RNA default stays `True` because `.blend` files and old JSON do not store unedited fields). `name_permutations` names them once the input permutes (`+` adds a second value, Shift + `+` makes a sweep): while every value is plain, all get a folder per value (`\15`); otherwise a plain new value copies the last value whose naming names each value (`names_each_value`: `\15`, `\B\15`, `_x_15`, not a typed `\big`), else a folder per value. Typing a value without sections over a blank value keeps its naming (`set_value_expr`). Typing `*` (menu / boolean) records a `solo` edit: `on_value_expr_update` removes the input's other values and runs `name_permutations`, like Shift + `+`; without typed sections the sweep keeps the stored naming.
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
  - Computes evaluated geometry using `obj.evaluated_get(depsgraph).to_mesh()`, plus unrealized instances found through `depsgraph.object_instances` (`collect_instance_arrays`). That iterator leaves out objects hidden with the eye icon (`hide_set`), instances included; a collection's eye does not matter. `eye_unhidden` therefore unhides such targets for the pass and hides them again in a `finally`. Toggling the eye re-evaluates no geometry, pushes no undo step from the operator (it has no `UNDO` flag) and leaves the selection as it was (a hidden object is never selected).
  - Streams geometry to disk using `write_object_stl`.
  - Cleans up evaluated mesh data via `to_mesh_clear()`.

### B. Headless Worker Pipeline (Process Isolation)
- **Condition**: Any override or sweep is defined in Global, Preset, Collection, or Object scope.
- **Workflow**:
  1. **Preparation**:
     - Checks every output path of the preset for collisions (`find_export_clashes`, no limit); a collision aborts the export before anything is written.
     - Saves a snapshot of current memory to a temporary file via `bpy.ops.wm.save_as_mainfile(filepath=..., copy=True)`.
     - Writes a job manifest `job.json` (`write_job_file`): the scene and view layer the export was started from (the window's, as used for the clash check), the preset's `uid`, the root directory path, the start time and `skip_direct` (objects without overrides were already written on the direct path). Job files are temporary and always written by the same add-on version, so they carry no version or fallbacks.
  2. **Process Spawn**:
     - Spawns background Blender: `[blender, "--factory-startup", "--python-exit-code", "1", ("--enable-autoexec",) "-b", temp_blend, "-P", script_file, "--", "--batch-stl-headless", job_json]`. `--enable-autoexec` is only added when the user allows Python auto-execution.
  3. **IPC & Streaming**:
     - A background reader thread drains worker `stdout` into a thread-safe `queue.Queue`.
     - A Blender modal timer operator (`0.1s`) polls the queue, updates preset progress bars (`BATCH_STL_PROGRESS:X`), and logs stdout into the preset's console UI.
  4. **Headless Execution Engine (`run_headless_export`)**:
     - **Job Scope**: Resolves the job's scene (`bpy.data.scenes`) and view layer, and looks the preset up by `uid` in that scene only. The context can't be used for this: a background load's `bpy.context.scene` is the scene of the saved file's first window, not necessarily the one the export was started from. A missing scene, view layer or preset prints an `ERROR:` line and exits with code 1 (reported as a crash); there is no fallback to another preset. The window (when there is one) is switched to the job's scene, then its view layer, so the context matches (e.g. for scripted drivers); the worker itself uses the view layer and its depsgraph explicitly.
     - **Phase 0 (Depsgraph Culling)**: Groups objects by override signature (`execution_batches`). Excludes unrelated layer collections of the job's view layer (`lc.exclude = True`) to prevent Blender from evaluating unneeded geometry trees during node updates.
     - **Baseline Capture (`capture_baseline_states`)**: Caches baseline modifier socket values, internal node socket defaults, and node link connections.
     - **Branch-Aware Combinations (`generate_named_combinations`)**: Recursively picks one value per parameter pool (sweeps expanded) and keeps only the values that apply to the branch built so far, yielding each variant with its folder and filename naming. See *Branching & Merging* below.
     - **Evaluation Loop**:
       - Restores the baseline of every socket the variant leaves out (`revert_overrides(..., skip=override_targets(combo))`), so a parameter without a value in this branch is back at its default instead of keeping the previous variant's value.
       - Applies overrides via `apply_overrides`.
       - Calls `view_layer.update()` on the job's view layer and takes its depsgraph (`view_layer.depsgraph`).
       - Collects the batch's unrealized instances (`collect_instance_arrays`, eye-hidden objects included as on the direct path).
       - Writes STL files via `export_object_stl` (a wrapper around `write_object_stl` that logs an unwritable file as `FAILED` and continues).
     - **Restoration (`revert_overrides`)**: Re-applies baseline values and links, restores layer collection exclusion states.
  5. **Teardown**:
     - Worker exits with code 0.
     - Main process modal handler catches `BATCH_STL_DONE`, stops modal timer, and removes temporary directory. Temp folders a crash left behind are removed when the add-on registers, once they are older than a day (a younger one may belong to an export running in another Blender instance).
     - A worker that exits without printing `BATCH_STL_DONE` is reported as a crash together with its exit code (1 for an uncaught Python error).

### C. Branching & Merging (`walk_override_branch`)
Folders and filename tokens created by values form branches. Overrides further down the stack can merge into an existing branch instead of multiplying every permutation. Folders (`dir`) and tokens (`tag`) are tracked as two parallel branches with the same rules; an override or value is active only when both its folder and its tag anchors fit.

- **One walker for generation and naming**: `walk_override_branch(overrides, combo)` walks the resolved overrides top to bottom (Global → Preset → Collection → Object, then node groups, nodes and inputs in UI order). It keeps the full folder path and token list of the branch and returns the active inputs, the filename tokens per level (`tags_by_level`) and the folder parts per level (`paths_by_level`). `generate_named_combinations` uses it to prune values that do not apply to a branch and yields the naming of the final walk with each variant, so generation and naming can never disagree (and each variant is walked once).
- **Branch names (`_known_branch_dirs`)**: `("dir", folder)` and `("tag", token)` keys that values *earlier* in the stack can generate (every sweep step included), recorded per override and per input, and per override for its node group (`("group", id(ovr))`: what was known before the group, see below).
- **Blocks (`_place_block`)**: a node group's sub-folder / tag is placed once, before its first node (`MockOverride.group_uid`), against the names known before the group, so it means the same for all of its nodes; its names go into the branch with the first node that applies. Each node then places its own sub-folder / tag as a separate chain, which can merge into the group's. (Placing the group again per node made a chain like `Y/2`, Y new and 2 upstream, find Y behind 2 and drop every later node.)
- **Only typed value names anchor (`value_name_parts`)**: a value field gives `(anchors, names)`. Only the anchors (the names typed in the field) go through placement; the value's own folder / token is always new. Tag anchors are chains split at `_` (`segment_tag`, longest known token first): `B_2` merges into `B`, then `2`. `_place_dir_parts` first splits against the branch's own tokens and only falls back to the known upstream names when that reading does not fit, so a token `a_b` and tokens `a`, `b` from two inputs both work. The value never anchors, otherwise two independent inputs sharing a value (`1`, `True`) would merge into each other and silently lose variants. They are still branch names that blocks can anchor to.
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

### Expression Fields (`BatchSTLValue.expr`, `naming`)
A value row has one text field instead of a value field, a folder field and a tag field: `\` starts the folder section, `_` the filename section, and the value is written where it goes in the names (`15`, `\15`, `_15`, `\B\15\_x_15`, `\15_mm`, `\big`). A node group / node row has one `naming` field (`\C\2\_B`) instead of a sub-folder and a tag field.
- **Lexing** (`lex_expr`, `split_field`): chars carry a quoted flag and their position; double quotes make text literal (`""` is a quote inside quotes) and `{...}` groups a string sweep. The value part runs to the first bare `\`, or to a `_` at the start or after a space; inside it a `_` starts the filename section after the longest text that is a value (`_value_cut`: `15_big` is 15 + token `big`, the string `a_b` and the menu item `Mode_A` stay whole, a quoted value ends at its quote). A folder section runs until a `_` right after `\` (the closing separator) or a space, a filename section until a `\`. In a block field text without a mark is a folder.
- **Value and templates** (`parse_value_field`): without a value part the value is read from the names (`_infer_value`): (1) the stored value where it is written, so editing the names around a value never changes it; (2) a value written where the stored one was (`_slot_shape`: same text around it in its name, same names after it; a missing section counts as the value alone), unless that name exists upstream (`_upstream_check`, from `upstream_scope`), where it is a typed name that merges; (3) a sweep or a value with a space (text that cannot be a typed name, `_can_be_typed`), or before a blank value is set any value; (4) else the stored value stays and every name is typed (`\big` on 15). Values a sibling has (`taken`) are skipped. `section_template` then puts `VALUE_SLOT` at the last place the value is written (`_slot_run`) and validates the typed names (no space, none of `<>:"/\|?*`). An empty section is the value alone; a section ending in its separator (`\B\`, `_B_`, not a `\` that closes the folder before `_`) puts the value after its names and never holds the value itself. Quoted text is never the value.
- **Display** (`compose_value_field`): the first of a few spellings that `parse_value_field` reads back as the same value and templates, given what the field holds (`current`, by default the result itself): the naming alone first, the value written where it goes and not shown at all when typed names replace it (`\big`); then with typed names quoted where they could be read as the value (the last such name, every one, all names); then with the value written first (compact, with a space before `_`, quoted, or with an empty / open section for a value that cannot be written inside a name). Display never needs the upstream names: a stored template without the value skips rule (2). Memoized per value, templates and menu items (`_compose_memo`) when it composes what the field holds.
- **Computed properties:** `expr` and `naming` are `StringProperty(get, set, search, update)`. `get` composes the text from the stored fields (`value_expr`, `compose_block_naming`), so only the fields are saved. `set` parses and writes the fields inside `raw_edits()`; text that does not fit the input type, a single value the input already has (numbers compare by value), a typed name that cannot be a file name or a section given twice is rejected, and the field shows the stored data again. An empty value field is recorded as a delete, `*` as a `solo` edit (the update removes the input's other values).
- **Owner lookup:** `get` / `set` / `search` receive only the value, so `value_owner` finds its node group, node and input from `path_from_id()` (about 40 µs; a 200-value table draws in about 45 ms, as before the syntax change).
- **Update:** `set` records its outcome in `_expr_edits` (keyed by `as_pointer()`), and the `update` that Blender calls right after it pushes one undo step (search fields get none from Blender) or deletes the value. The delete runs in `update`, never in `set`: removing an item inside `set` would leave Blender calling `update` on freed memory. Deleting the last value empties the input name, which removes the input, an emptied node and node group (`nested_edits()` keeps that cascade to one undo step).
- **Search** (`search_value_expr`, `search_block_naming`): the section typed last completes its chain of typed names through `naming_completions` (names quoted as needed by `_quote_suggested`), with the other section's anchors as the "must still fit" constraint. In a value field the chain is the text before the name holding the value (`_chain_end`); each completion is offered with the value after it (`\B\1\15`) and, while the chain is typed at the end, ending in its separator (`\B\1\`, the value follows on Enter). A block offers each name ending in its separator (`\C\`; the closing `\` means nothing there). A value also gets menu / boolean items (keeping its naming), the other reading of ambiguous text (`Value 2` when a name merges, `Typed names, the value stays 15` when a value was typed in place), a folder / token per value for a missing section and, without sections, each upstream name to merge into, all composed by `compose_value_field` against what the field holds and the upstream names, so they read back as offered when picked.
- **Tab completion** (`search_results(typed, offers, tab)`): Blender's Tab on a search field fills the longest start shared by every result that begins with the typed text (exact, case-sensitive; `autocomplete_update_name`), and ends the edit when only one result begins with it (`AUTOCOMPLETE_FULL_MATCH`). The searches pass the separator-ended completions as `tab`; when they share more than the typed text, `search_results` leaves out the results that would stop Tab short, including the text as typed, so `\A` + Tab gives `\A\`. Two results always begin with the completion (value fields: `\A\` and `\A\15`; blocks: `\C\` and, for a single name, `\C\_` / `_B_\`), so the edit goes on and the next name can be typed. Verified by replaying that algorithm on the real result lists (Blender's source checked on 2026-10-11).
- **Icon** (`value_info`): an info button before each value field shows a folder / tag / plain icon, the merge icon (blue) or an error icon (red, the value never applies), and its tooltip decodes the field (`Value: 5`, `Folder: A / 5`) and lists the merges.

### Merge Predictions (`BranchScope`)
The folder and tag sections of the value fields (`BatchSTLValue.expr`) and the block naming fields (`naming` of node groups / nodes) offer upstream names and highlight merges without enumerating permutations. `BranchScope` mirrors the walker's rules:
- Entries are keyed `(kind, name)` with kind `dir` or `tag`, with a description (*Branch* / *Name branch* for value names, *Folder* / *Tag* for block names), whether the exporter knows it as a branch name (`known`), and one **instance** per way it exists: the value that makes it (input + value / sweep step, none for a block), the context keys it requires, its creation position, and whether it can exist without an earlier instance (`base`). Values of one input exclude each other; names of one branch keep their creation order.
- A context is a set of keys a branch contains: names and chosen values (`("alt", input, value)`), plus `("not", keys)` for a branch that lacks at least one of `keys` and `("any", rests)` for a branch that has every key of at least one of `rests`. `candidates` filters instances pairwise; `satisfiable` searches one choice of values that gives every key together (pairwise checks miss three-way conflicts; it gives up and says yes after a step budget), trying each rest of an `any` key in turn. It checks the `not` keys at the end, against the solved keys plus every name whose instance's requirements that choice already meets.
- `chain_states(kind, names, ctx)` lists every way a chain can be placed (each merge picks an instance after the previous one, a branch name must merge, a plain block name merges or is created anew). A merge brings what its instance requires (`_merged`): for a branch name the name, its value and the instance's requirements; for a plain block name only the requirements, since a plain name exists wherever they hold, so contexts never hold plain names (the search leaves out the names in a field's context). Merging plain `Y/Y` into a second Y that a block made under I0=C therefore requires I0=C. Created anew, the name is missing from that branch, so the way adds a `not` key per instance it skipped: block `x/A` on the path `A/x` cannot merge into A after creating x again, since a branch with A has x. `segmentation` splits tag chains at `_` like `_place_dir_parts`. `place(chains, ctx)` places a block's or value's folder and tag chains together, carrying each way of the first into the second, and returns the merged names (`hits`), the stuck branch name of a chain that never applies (`dead`) and the names it creates.
- The context inside a block or value is `join` of its ways' contexts: the keys every way requires, plus an `any` key with what each way requires beyond them (left out when one way requires nothing more). The keys alone lose the alternatives: a node `B/B` that merges through I0=B and I1=B, or through I0=B and I3=B, shares only I0=B, and a value inside it whose tag needs I1=C and I3=C looked possible. The ways are capped at `MAX_WAYS` (64) per step; when a step drops some (`cut`), `join` keeps only the shared keys, since the others no longer cover every branch.
- `enter_block` / `add_input` record what blocks and values create. A dead block or value records its value names as known but never existing (the exporter still anchors to them). Values of one input resolve against the scope before the input, never against each other.
- A node group's names exist wherever the group fits only while its nodes are entered, so the nodes and their values merge into them. `close_group` (called by the next group's `enter_block`, and by `upstream_scope` after each group it enters in full) then replaces each of them with one instance per context a node of the group applies in. These instances keep the group's position, before the names of its nodes. This matches the walker, which adds a group's names with the first node that applies. Without it, a group sub-folder `X/2/A` whose only node has a tag that never fits there would still offer `X` to later fields and predict merges into it.
- `upstream_scope(item)` builds the scope and context at any field from its `path_from_id()`. `naming_completions(scope, ctx, kind, text, ...)` uses it for the search lists: it places the typed prefix (which must merge as a whole), offers the names that can merge next in chain order (`suggestions`), and keeps only those the other section (folder vs tag of the same block or value) still fits, so `\C\` only offers names that exist under `C`.
- `draw_overrides_table` builds the scope incrementally while drawing, in export order. Merging blocks get a blue `batch_stl.merge_info` bar (`Merges into C / 2 · name B`; red with *Never applies* for dead merges) whose tooltip lists the source of each name, and the merge icon on their naming field. A value whose folder or tag merges gets the merge icon on the info button before its field (`value_info`).
- Collapsed (the table's `batch_stl_ui_*_ovr` arrow), a table with inputs draws the **clear view**: the same walk in export order, without the node group / node rows. The merge bars of an input's hidden blocks become one `batch_stl.merge_info` icon at the start of its row (red if one is dead) whose tooltip joins their texts and tooltips. A table without inputs collapses completely.

### Undo Stack Protection & Validation
- **Single Undo Step per Edit**: Plain text fields (collection / object sub-folders and tags, the preset prefix) get their undo step from Blender when the edit is confirmed. Search fields (group, node, input, the value `expr` and block `naming` fields) are created without `UI_BUT_UNDO`, so their labelled `@edit_callback` / update pushes exactly one step; nested callbacks of a cascade are suppressed.
- **Empty Field Deletion**: Submitting an empty field (`""`) triggers an automatic GC deletion routine for Node Groups, Nodes, Inputs, and Values (a value through its `expr` field, whose clear button does the same). Code that writes consistent data (socket defaults in `sync_input_type`, paste, import, migration, the `expr` setter) runs inside `raw_edits()`, so an empty default (a menu on the modifier interface reports `''`) never triggers a deletion.
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
- `.blend` data is versioned the same way: `Scene.batch_stl_data_version` (`DATA_VERSION`, now 3) is stamped on scenes edited with this version, and `migrate_scene_data` (on load / register, via `prepare_scenes`) brings older scenes up to date: 1 gives unversioned scenes `dir_tag = tag`; 2 migrates 1.0.0 typed values, removes the `obj_ptr` pointers of object entries (they kept deleted objects alive) and the UI properties that used to live on the Scene; 3 turns the value naming fields into templates (`legacy_naming_template`: blank = `*`, folder `B\` = `B/*`, folder `size_` = `size_*`, tag `B_` = `B_*`). JSON presets carry `data_version`; `upgrade_preset_dict` converts older ones the same way on import.
- Provides deep-copy and paste support across presets, collections, and node groups via internal clipboard buffers (`_clipboard`).
