# Combi Export

A high-performance batch export pipeline and parametric permutation engine for Blender 5.2+. Built around a custom vectorized NumPy binary STL generator and an adaptive dual-path execution engine, it enables one-click exporting of scene collections, multi-dimensional parameter sweeping via Geometry Nodes and modifiers, and automated variant generation.

---

## Architecture & Technical Overview

For in-depth architectural details, execution flowcharts, and engine design, see [ARCHITECTURE.md](ARCHITECTURE.md).

* **Blender Version Support:** Blender 5.2.0+ (Manifest schema v1.0.0, extension version 1.0.0).
* **Package Format:** Blender 5.2 Extension (`blender_manifest.toml`).
* **Design Philosophy:** Non-destructive execution, isolated subprocess execution for mutations, and zero undo-stack pollution.

---

## Core Features

### 1. Vectorized Binary STL Generator
* **NumPy Direct Memory Buffering:** Reads vertex coordinates, face indices, and loop normals directly into continuous memory buffers via `foreach_get`.
* **Vectorized Transformations:** Transforms vertex positions and face normals using SIMD matrix arithmetic in NumPy, accounting for world matrices and negative determinant winding flips.
* **Instances Included:** Unrealized Geometry Nodes instances are exported together with the object's own mesh.
* **Instant Disk Streaming:** Meshes with hundreds of thousands of triangles are triangulated, evaluated, and streamed to disk in milliseconds.

### 2. Adaptive Dual-Path Execution Model
* **Synchronous Bypass (Fast Path):** Exports without node overrides or parameter sweeps are evaluated natively in the current Blender process and written directly to disk.
* **Headless Background Worker (Safe Isolation Path):** When geometry node overrides, modifier changes, or parameter sweeps are present:
  - Automatically creates a temporary copy of the `.blend` file.
  - Spawns an isolated background Blender worker (`--factory-startup --python-exit-code 1 -b <temp.blend> -P ...`).
  - Performs intelligent dependency graph culling (`lc.exclude`) so unneeded collections are ignored during geometry updates.
  - Restores baseline socket connections and values after export.
  - Keeps the main Blender viewport and UI completely responsive and interactive.

### 3. Hierarchical Parameter Overrides (8 Tiers)
Define temporary parameter overrides and sweeps across an 8-tier hierarchy:
`Global` → `Preset` → `Collection` → `Object` → `NodeGroup` → `Node` → `Input Socket` → `Value / Sweep`

* **Override Semantics:** A more specific level replaces the inherited value of the same socket (an Object value replaces a Collection, Preset or Global value). Several values on the *same* level are variants and are all exported.
* **Target Flexibility:** Target either exposed modifier interface sockets (`<Modifier Interface>`) or specific internal nodes within a Geometry Node tree, including the `Group Output` node (its unlinked inputs set what the group outputs).
* **Type Auto-Detection:** Automatically inspects the node tree interface and infers socket data types (`FLOAT`, `INT`, `BOOLEAN`, `STRING`, `MENU`). Other socket types (vectors, colors, objects, ...) are flagged as unsupported and block the export until removed.
* **Menu/Enum Auto-Populate:** Searches and lists available items for Menu Switch nodes.

### 4. Parametric Sweeping & Combinatorial Engine
Generate variant permutations across any socket:
* **Float / Int Ranges:** Define numeric sweeps via explicit Value start, Value step, and Number of steps controls, or string ranges.
* **Boolean & Menu Combinations:** Automatically iterates through `True`/`False` states or all enum options.
* **Branch-Aware Combinations:** Every parameter combination is generated, except where an override is limited to one branch of the folder tree (see *Branch Merging*). Such an override only multiplies the permutations of that branch.

### 5. Predictive Directory Tree & Overwrite Protection
* **Interactive Nested UI Hierarchy:** Calculates permutations ahead of time and displays an interactive directory hierarchy using collapsible nested UI boxes and indentation. Dedicated toolbar buttons let you quickly toggle the Global View, Expand All, Collapse All, or recursively Expand Last subdirectory.
* **Pre-Export Clash Detection:** Instantly flags duplicate output file paths with visual alerts before export starts, preventing accidental file overwrites.
* **Decoupled UI Cache:** Background timer cache (~10Hz) prevents UI stalls when evaluating large permutation matrices.

### 6. Power-User Shortcuts & Ergonomics
* **Shift + Up/Down Arrows:** Shift + Up moves an override group to the parent tier (e.g., from Object up to its Collection). Shift + Down copies it to every nested child tier and removes it from the current one (e.g., from Preset down to all its Collections, or Collection down to all its Objects).
* **Shift + Add Input (+):** Auto-populates all available and exposed inputs for the selected Geometry Node.
* **Instant Deletion:** Emptying a Node Group, Node, Input, or Value field (and submitting) instantly deletes the iteration.
* **Clear Button (X):** Float, integer and string value fields have an inline clear button that deletes the value (and the input when it was the last value) in a single undo step. Menu and boolean values keep Blender's own clear button.
* **Validation Check:** Real-time socket validation rejects invalid inputs/nodes and instantly reverts to the last known valid state.

### 7. Scoped Live Console & Progress Tracking
* **Preset-Isolated Logging:** Each export preset tracks its own console log and export duration.
* **Real-Time Progress Streaming:** Non-blocking background worker output is piped directly into the Blender panel with operation counters and elapsed time display.
* **Auto-View Switching:** Displays the directory tree during setup, flips to the live console on export start, and allows instant cancellation.
* **Side Tool Column:** Clear the log or toggle Verbose output directly from the side column tools.
* **Per-File Error Reporting:** A file that cannot be written (e.g. locked by a slicer) is logged as `FAILED` and the export continues with the next file.

### 8. Collection Mapping & Granular Exclusion Filters
* **Collection Bindings:** Map multiple collections per preset, configure custom sub-folder destinations, and append collection tags.
* **Object-Level Filtering:** Enable or disable specific mesh objects within collections without affecting viewport visibility.
* **Per-Object Overrides:** Assign distinct tags, sub-folders, and dedicated node override groups down to individual objects.

### 9. Dynamic Tagging & Directory Formatting
* **Two Separate Fields per Value:** Each value has a directory field and a filename-tag field side by side. Each toggle button enables only its own field (a disabled field is greyed out).
* **Sub-Directory Creation (`FILE_FOLDER`):** Route variant exports into dedicated sub-folders per value iteration, named by the directory field.
* **Filename Tag (`BOOKMARKS`):** Add the value to the exported filename, formatted by the tag field.
* **Naming Rules (same for both fields):**
  - `tag`: Replaces the socket value label entirely (`tag`).
  - `tag_`: Prepends the tag to the value (`tag_15`).
  - `_tag`: Appends the tag to the value (`15_tag`).
  - Blank: Defaults to the formatted parameter value.

### 10. Branch Merging
Values with a folder create branches in the output tree, e.g. letters `A B C` × numbers `1 2 3` give `A/1 … C/3`. An override further down the stack can be attached to an existing branch instead of multiplying every permutation:
* **Merge by Name:** When a node group sub-folder, node sub-folder, or value directory names a folder that already exists upstream, the override merges into it. Its own folders are created inside the branch, below the folders already there.
  - Sub-folder `B` → only the B permutations get the new values: `B/1/X`, `B/1/Y`, … The A and C branches stay as they are.
  - Sub-folder `C/2` → only `C/2/X`, `C/2/Y`, …
  - Sub-folder `2` → every `2` branch: `A/2/X`, `B/2/X`, `C/2/X`, …
* **Matching Rules:** A name may match any parent folder of the branch, not only the last one. Several names (`C/2`) must appear in that order. A branch name that is missing from a branch means the override does not apply there; any other name simply becomes a new folder.
* **Upstream Suggestions:** Folder fields are searchable and list the folders that exist above them, each labelled as a *Branch* (created by a value) or a *Folder* (a plain sub-folder) with its source. Free text is still allowed. In sub-folder paths the suggestion completes the last part, so typing `C/` offers `C/1`, `C/2`, …
* **Branch-Aware Predictions:** Suggestions only list folders that can exist together with the merges already made above the field. Inside a block merged into `C`, the alternatives `A` and `B` are not offered, and neither are folders that exist only under `A`.
* **Merge Highlighting:** A node group or node that merges shows a blue *Merges into …* bar on top of its block, covering everything inside it. Its folder icon becomes a merge icon. A value whose folder merges shows the merge icon on its folder toggle. Hover the bar to see where each folder comes from.
* **Dead Merge Warning:** If the chosen folders can never exist together (e.g. `C/X` when `X` only exists under `A`), the bar turns red (*Never applies*) because that block is never exported. A value with such a folder gets a red folder toggle.

### 11. JSON Preset Portability & Clipboard Buffer
* **Import / Export Setup:** Save or restore presets, collections, object lists, exclusion states, and override matrices to external JSON files. Presets saved before directory and tag were split into two fields reuse their old tag as the directory name, so their folders keep their names.
* **Internal Clipboard:** Copy and paste presets, collections, and node groups between tiers with one click.

---

## Installation & Requirements

* **Blender:** 5.2.0 or newer.
* **Dependencies:** Standard Blender Python environment (`numpy` is included with Blender).
* **Installation:** Install as an extension from the Blender Preferences extensions menu or place the `combi_export` folder into your Blender extensions directory.
