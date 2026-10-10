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
  - Keeps the main Blender viewport and UI completely responsive and interactive. Undo, switching scenes and reordering presets do not interrupt a running export.

### 3. Hierarchical Parameter Overrides (8 Tiers)
Define temporary parameter overrides and sweeps across an 8-tier hierarchy:
`Global` → `Preset` → `Collection` → `Object` → `NodeGroup` → `Node` → `Input Socket` → `Value / Sweep`

* **Override Semantics:** A more specific level replaces the inherited value of the same socket (an Object value replaces a Collection, Preset or Global value). Several values on the *same* level are variants and are all exported.
* **Target Flexibility:** Target either exposed modifier interface sockets (`<Modifier Interface>`) or specific internal nodes within a Geometry Node tree, including the `Group Output` node (its unlinked inputs set what the group outputs).
* **Type Auto-Detection:** Automatically inspects the node tree interface and infers socket data types (`FLOAT`, `INT`, `BOOLEAN`, `STRING`, `MENU`). Other socket types (vectors, colors, objects, ...) are flagged as unsupported and block the export until removed.
* **Menu/Enum Auto-Populate:** Lists the items of every menu socket: Menu Switch nodes, menus on the modifier interface (also behind reroutes and nested groups), group node menus and the menus of built-in nodes (e.g. *Merge by Distance* mode).

### 4. Parametric Sweeping & Combinatorial Engine
Generate variant permutations across any socket:
* **Float / Int Ranges:** Define numeric sweeps via explicit Value start, Value step, and Number of steps (1 to 1000) controls.
* **String Lists:** String sweeps take a comma-separated list of values.
* **Boolean & Menu Combinations:** Automatically iterates through `True`/`False` states or all enum options.
* **Branch-Aware Combinations:** Every parameter combination is generated, except where an override is limited to one branch of the folder tree (see *Branch Merging*). Such an override only multiplies the permutations of that branch.

### 5. Predictive Directory Tree & Overwrite Protection
* **Interactive Nested UI Hierarchy:** Calculates permutations ahead of time and displays an interactive directory hierarchy using collapsible nested UI boxes and indentation. Dedicated toolbar buttons let you quickly toggle the Global View, Expand All, Collapse All, or recursively Expand Last subdirectory.
* **Pre-Export Clash Detection:** The tree view highlights output paths written more than once. Every export checks all of its output paths first (no matter how many) and stops with an error if two files would land on the same path, before anything is written.
* **Decoupled UI Cache:** Background timer cache (~10Hz). Variant counts are computed without listing the variants whenever every combination of values is exported; only branch-limited setups are counted one by one, up to 5,000 per object (shown as `5000+`). The tree preview shows up to 5,000 files.

### 6. Power-User Shortcuts & Ergonomics
* **Shift + Up/Down Arrows:** Shift + Up moves an override group to the parent tier (e.g., from Object up to its Collection). Shift + Down copies it to every nested child tier and removes it from the current one (e.g., from Preset down to all its Collections, or Collection down to all its Objects).
* **Shift + Add Input (+):** Auto-populates all available and exposed inputs for the selected Geometry Node.
* **Instant Deletion:** Emptying a Node Group, Node, Input, or Value field (and submitting) instantly deletes the iteration.
* **Clear Button (X):** Float, integer and string value fields have an inline clear button that deletes the value (and the input when it was the last value) in a single undo step. Menu and boolean values keep Blender's own clear button.
* **Validation Check:** Real-time socket validation rejects invalid inputs/nodes and instantly reverts to the last known valid state.
* **Clear View:** The arrow of an override table that has inputs collapses only its node group and node rows: the inputs and their values stay listed and editable. An input whose node group or node merges shows the merge icon at the start of its row (red when that block never applies); hover it for the merge details.

### 7. Scoped Live Console & Progress Tracking
* **Preset-Isolated Logging:** Each export preset tracks its own console log and export duration.
* **Real-Time Progress Streaming:** Non-blocking background worker output is piped directly into the Blender panel with operation counters and elapsed time display.
* **Auto-View Switching:** Flips to the live console when an export starts, and allows instant cancellation.
* **Side Tool Column:** Clear the log or toggle Verbose output directly from the side column tools.
* **Per-File Error Reporting:** A file that cannot be written (e.g. locked by a slicer) is logged as `FAILED` and the export continues with the next file.

### 8. Collection Mapping & Granular Exclusion Filters
* **Collection Bindings:** Map multiple collections per preset, configure custom sub-folder destinations, and add collection tags to the filename.
* **Object-Level Filtering:** Enable or disable specific mesh objects within collections without affecting viewport visibility. Renamed objects keep their settings.
* **Evaluated Objects Only:** Objects Blender does not evaluate are skipped: in an excluded collection, in a collection disabled in viewports (monitor icon, also on a parent collection), or disabled in viewports themselves. They would otherwise be exported without their modifiers. Hiding with the eye icon does not skip an object.
* **Per-Object Overrides:** Assign distinct tags, sub-folders, and dedicated node override groups down to individual objects.

### 9. Dynamic Tagging & Directory Formatting
* **File Name:** The name field of an object in the object list starts out as the object name. Type another name to export the object's files under it; clear the field to go back to the object name (which then follows renames again).
* **Filename Structure:** The file name (object name unless edited) followed by every tag token in hierarchy order, joined with `_`:
  `Object` → Global override tags → Preset override tags → Collection tag → Collection override tags → Object tag → Object override tags.
  Example: object `Box`, Global letter `A`, collection tag `v2`, Collection number `1`, object tag `lid` → `Box_A_v2_1_lid.stl`.
* **Collection, Object, Node Group and Node Tags:** Plain filename tokens placed at their level (several tokens separated by `/`; leading/trailing `_` are ignored, so `_lid` and `lid` are the same). The object tag no longer renames the object.
* **Two Separate Fields per Value:** Each value has a directory field and a filename-tag field side by side. Each toggle button enables only its own field (a disabled field is greyed out).
* **Sub-Directory Creation (`FILE_FOLDER`):** Route variant exports into dedicated sub-folders per value iteration, named by the directory field.
* **Filename Tag (`BOOKMARKS`):** Add the value to the exported filename, formatted by the tag field.
* **Value Naming Rules:**
  - Blank: The formatted parameter value (`15`).
  - `name`: Replaces the value (`name`).
  - Directory `name\` (or `name/`): Folder `name` with a folder per value inside it (`name/15`).
  - Tag `name_`: Token `name` followed by the value (`Box_name_15`).
  - `/` separates several names: `C/2\` gives `C/2/15`, tag `C/2_` gives `Box_C_2_15`.
  - Only the last character is the mark, so names may contain `_`: tag `x__` is the token `x_` followed by the value.
  - Typed names merge into an upstream folder / token of the same name (see *Branch Merging*); the value itself is always new.

### 10. Branch Merging
Values with a folder or filename tag create branches, e.g. letters `A B C` × numbers `1 2 3` give `A/1 … C/3` (folders) or `Box_A_1 … Box_C_3` (names). An override further down the stack can be attached to an existing branch instead of multiplying every permutation:
* **Merge by Folder:** When a node group sub-folder, node sub-folder, or value directory names a folder that already exists upstream, the override merges into it. A value merges through the folder names typed in its directory field: `B` (the value is replaced by `B`) or `B\` (merges into `B`, then a folder per value inside it: numbers `1 2` → `B/1`, `B/2`, while `A` and `C` stay as they are). A folder made from the value itself (blank, or the value folder after `B\`) is always new, so two inputs that share a value (`1`, `True`, a menu item) still multiply: `A=1, B=1` → `1/1`. Its own folders are created inside the branch, below the folders already there.
* **Merge by Filename:** The same works with filename tokens (again, only the tokens typed in a value tag merge: `B` replaces the value, `B_` merges into `B` and adds the value after it, so numbers `1 2` → `Box_B_1`, `Box_B_2`, while `Box_A` and `Box_C` stay as they are). A typed token that reads like the filename also matches tokens made by separate inputs: with letter `a` and letter `b` upstream, tag `a_b_` merges into `Box_a_b` just like `a/b_`; a single upstream token `a_b` wins over the pieces. A node group tag, node tag, or value tag that names an upstream token only applies to the files that already contain it, and its own tokens are appended at the end of the name: tag `B` → `Box_B_1_X`, `Box_B_1_Y`, … while `Box_A_*` and `Box_C_*` stay as they are. `C/2` and `2` work like their folder counterparts. Folder and tag anchors of one block must both match.
  - Sub-folder `B` → only the B permutations get the new values: `B/1/X`, `B/1/Y`, … The A and C branches stay as they are.
  - Sub-folder `C/2` → only `C/2/X`, `C/2/Y`, …
  - Sub-folder `2` → every `2` branch: `A/2/X`, `B/2/X`, `C/2/X`, …
* **Matching Rules:** A name may match any parent folder (or any earlier token) of the branch, not only the last one. A preset prefix, collection or object sub-folder (or collection / object tag) equal to the neighbouring override folder (or token) collapses into it. Several names (`C/2`) must appear in that order. A branch name that is missing from a branch means the override does not apply there; any other name simply becomes a new folder.
* **Upstream Suggestions:** Folder and tag fields are searchable and list the folders / name tokens that exist above them, labelled *Branch* / *Name branch* (created by a value) or *Folder* / *Tag* (a plain sub-folder or block tag) with their source. Free text is still allowed. In sub-folder paths the suggestion completes the last part, so typing `C/` offers `C/1`, `C/2`, … A value field ending in `\` (directory) or `_` (tag) keeps that ending on its suggestions: `B\` offers `A\`, `B\`, `C\`.
* **Branch-Aware Predictions:** Suggestions only list names that can exist together with the merges already made above the field, across folders and tags: inside a block placed in folder `C`, the alternatives `A` and `B` are not offered, and neither are folders or tokens that only exist under `A`.
* **Merge Highlighting:** A node group or node that merges shows a blue *Merges into …* bar on top of its block (folders first, then `name …` for tokens), covering everything inside it. The merging field's icon becomes a merge icon. A value whose folder or tag merges shows the merge icon on that toggle. Hover the bar to see where each folder comes from.
* **Dead Merge Warning:** If the chosen folders can never exist together (e.g. `C/X` when `X` only exists under `A`), the bar turns red (*Never applies*) because that block is never exported. A value with such a folder or tag gets a red toggle.

### 11. JSON Preset Portability & Clipboard Buffer
* **Import / Export Setup:** Save or restore presets, collections, object lists, exclusion states, and override matrices to external JSON files. Presets saved before directory and tag were split into two fields reuse their old tag as the directory name, so their folders keep their names. Files and `.blend` data from version 1.0.0 keep their number, boolean and sweep values.
* **Internal Clipboard:** Copy and paste presets, collections, and node groups between tiers with one click.

---

## Installation & Requirements

* **Blender:** 5.2.0 or newer.
* **Dependencies:** Standard Blender Python environment (`numpy` is included with Blender).
* **Installation:** Install as an extension from the Blender Preferences extensions menu or place the `combi_export` folder into your Blender extensions directory.
