# Architecture Overview: Combi Export

`Combi Export` (`combi_export`, v1.0.0) is a high-performance, parametric batch export add-on for Blender 5.2+. It combines vectorized binary STL generation, multi-tier geometry node / modifier socket overrides, combinatorial parametric sweeping, and a dual-path execution engine (in-process synchronous export vs. isolated headless subprocess export).

---

## 1. Design Principles & Goals

1. **Non-destructive & Isolated**: Overriding geometry nodes or modifier inputs must never corrupt the active project file, viewport state, or undo history.
2. **Dual-Path Execution Model**:
   - **Direct Fast-Path**: Mesh objects without parameter permutations or overrides evaluate natively in the active scene using Blender's evaluated depsgraph and stream directly to disk.
   - **Headless Worker Process**: When overrides or sweeps are active, the add-on forks an independent background Blender process (`--factory-startup --python-exit-code 1 -b <temp.blend> -P <script> -- --batch-stl-headless <job.json>`) on a temporary copy of the project file. The main Blender UI stays completely responsive and interactive.
3. **Vectorized NumPy Disk I/O**: Direct memory extraction via `foreach_get` into contiguous NumPy structured arrays avoiding per-polygon Python iterations.
4. **Predictive UI & Safety**: Cached calculation (~10Hz) of directory structures and STL filenames with collapsible nested box visualization and automatic collision detection before executing exports.

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
                                      |  • Itertools combinatorial sweep          |
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
   - Holds subfolder paths (`sub_path`), tagging flags (`use_tag`, `tag`), collection-level pinned overrides (`nodegroups`), and synchronized object entries (`objects`).
3. **`BatchSTLObject`**:
   - Mirrors individual meshes/curves within a collection.
   - Provides per-object export toggling (`export`), filename tag (`tag`), object subfolder (`sub_path`), and object-level overrides (`nodegroups`).
4. **`BatchSTLNodeGroup`**:
   - Targets a Geometry Node tree by name (`group_name`).
   - Contains a list of `BatchSTLNode` blocks.
5. **`BatchSTLNode`**:
   - Targets an internal node name within the node group, or `"<Modifier Interface>"` to target the modifier interface sockets directly.
6. **`BatchSTLInput`**:
   - Targets an input socket (`name`) and its inferred data type (`override_type`: `FLOAT`, `INT`, `BOOLEAN`, `STRING`, `MENU`).
7. **`BatchSTLValue`**:
   - Unified string-based concrete parameter value (`value_string`) for Ints, Floats, and Strings, and `value_menu` for Enum/Booleans.
   - Sweep definitions using updated explicit terminology (`sweep_start`, `sweep_step`, `sweep_count`, `sweep_range`).
   - Configures the filename tag (`use_tag`, `tag`) and subfolder routing (`use_dir`, `dir_tag`); both names use the same append/prepend/replace rules.
8. **`BatchSTLJob`** (`WindowManager.batch_stl_jobs`):
   - Runtime export state of one preset, keyed by `preset_index`: `is_exporting`, `cancel_export`, `export_progress`, `export_status` and the console log (`console_logs`).
   - Lives on the WindowManager, so it is never saved to the `.blend` file nor rolled back by undo. Cleared on file load and whenever presets are removed or reordered.
9. **`BatchSTLLogLine`**:
   - Discrete line items of a job's console log (one entry per line, capped at 300).

### Scoping & Inheritance of Overrides

When generating variations for an object, overrides are gathered in hierarchical order:
1. `Global` (`scene.batch_stl_global_nodegroups`)
2. `Preset` (`preset.nodegroups`)
3. `Collection` (`collection.nodegroups`)
4. `Object` (`object.nodegroups`)

`resolve_overrides` then applies **most-specific-wins**: if a lower level defines the same socket (same target, node group, node and input name), the inherited values of higher levels for that socket are dropped. Several values on the same level remain variants. Objects are batched for the headless worker by `get_override_signature`, a fingerprint of the resolved overrides (values, sweeps, tags, folder flags and level).

---

## 4. Execution Pipelines

### A. Fast Direct Path (Native In-Process)
- **Condition**: Preset contains zero overrides across all hierarchy tiers.
- **Mechanism**:
  - Resolves active objects across included collections.
  - Computes evaluated geometry using `obj.evaluated_get(depsgraph).to_mesh()`, plus unrealized instances found through `depsgraph.object_instances` (`collect_instance_arrays`).
  - Streams geometry to disk using `write_object_stl`.
  - Cleans up evaluated mesh data via `to_mesh_clear()`.

### B. Headless Worker Pipeline (Process Isolation)
- **Condition**: Any override or sweep is defined in Global, Preset, Collection, or Object scope.
- **Workflow**:
  1. **Preparation**:
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
     - **Combinatorial Cartesian Product (`generate_override_combinations`)**: Evaluates `itertools.product` across all parameter pools and sweeps.
     - **Evaluation Loop**:
       - Applies overrides via `apply_overrides`.
       - Calls `bpy.context.view_layer.update()` and fetches updated depsgraph.
       - Writes STL files via `export_object_stl` (a wrapper around `write_object_stl` that logs an unwritable file as `FAILED` and continues).
     - **Restoration (`revert_overrides`)**: Re-applies baseline values and links, restores layer collection exclusion states.
  5. **Teardown**:
     - Worker exits with code 0.
     - Main process modal handler catches `BATCH_STL_DONE`, stops modal timer, and removes temporary directory.
     - A worker that exits without printing `BATCH_STL_DONE` is reported as a crash together with its exit code (1 for an uncaught Python error).

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
- Driven by a background timer (`bpy.app.timers`) running at ~10Hz with a dirty flag (`mark_dirty()`).
- The flag is set by property updates, undo/redo, and a `depsgraph_update_post` handler that reacts to collection changes (objects linked/unlinked), to objects in mapped collections being renamed or hidden, and to exclusion changes of mapped collections.
- Recomputes statistics (preset counts, collections, exported object count, total permutation iterations).
- Computes directory hierarchies and leaf files in advance. The UI displays this with an uncollapsable root directory and dedicated side-column toolbar buttons for toggling global view and bulk expanding/collapsing.
- **Naming Collision Detection**: Analyzes all destination paths and flags collisions when two permutations or objects resolve to the identical output file path.

### Undo Stack Protection & Validation
- **Single Undo Step per Edit**: Plain text fields (`value_string` for floats, ints and strings, sub-folders, tags) get their undo step from Blender when the edit is confirmed. Search fields (group, node, input, menu value, folder fields with upstream suggestions) are created without `UI_BUT_UNDO`, so their labelled `@edit_callback` pushes exactly one step; nested callbacks of a cascade are suppressed.
- **Empty Field Deletion**: Submitting an empty field (`""`) triggers an automatic GC deletion routine for Node Groups, Nodes, Inputs, and Values.
- **Validation Engine**: Real-time validation checks against depsgraph interfaces ensure node groups, nodes, and inputs exist. Invalid targets are visually flagged and gracefully rejected or reset to the last known valid state.
- **Numeric Validation**: Float/Int values and sweep start/step must parse as numbers and the sweep step count must be at least 1; otherwise the field is flagged and export is blocked.
- **Hierarchical Propagation**: Multi-level operations (Shift + Up moves a group to the parent tier, Shift + Down copies it to every child tier) use `operator_context = 'INVOKE_DEFAULT'` to intercept modifier keys and explicitly iterate down the hierarchy.
- **Tree Expand Logic**: Complex layout logic (e.g., Expand Last) leverages recursive path traversal, resetting directory states and selectively expanding leaf nodes seamlessly without UI lockup.

---

## 7. Configuration Portability

The add-on implements full JSON schema serialization and deserialization (`BATCH_STL_OT_export_presets_json` / `BATCH_STL_OT_import_presets_json`):
- Serializes presets, collections, object lists, exclusion states, node group overrides, input types, values, sweeps, and tagging configurations into clean, version-agnostic JSON files.
- Provides deep-copy and paste support across presets, collections, and node groups via internal clipboard buffers (`_clipboard`).
