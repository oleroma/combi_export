Batch-export your scene to **STL files in a folder structure you define**, and generate **variants** by sweeping Geometry Nodes and modifier inputs, all from one panel and without freezing Blender.

Set up where each collection and object goes, name the files with tags, and let *Combi Export* write every combination of parameters: sizes, counts, switches, menu choices. Variant folders and file names are previewed before anything is written.

## Highlights

- **Fast STL writer.** Meshes are read straight into NumPy buffers, transformed in bulk and streamed to binary STL. Objects with hundreds of thousands of triangles take milliseconds. Unrealized Geometry Nodes instances are exported with the object's own mesh.
- **Presets, collections, objects.** Each preset has a root sub-folder and a list of collections. A collection can have its own sub-folder and filename tag, and individual objects can be switched off or renamed for export.
- **Parameter overrides and sweeps.** Override or sweep inputs of a modifier's Geometry Nodes interface or of nodes inside a node tree, at any level: Global, Preset, Collection or Object. A more specific level replaces the inherited value; several values on one level become variants.
- **Variant syntax.** Type `0:0.5:5` for a range, `{a, b, c}` for a string list, or `*` for every menu item or `True` / `False`. Put the value in a folder (`\15`), in the file name (`_15`) or both.
- **Branch merging.** Attach an override to one branch of the folder tree (for example only the `B` variants) instead of multiplying every combination.
- **Directory tree preview.** See every file that will be written, with variant counts per preset, collection and object. Paths that would be written twice are highlighted, and an export with a clash stops before it writes anything.
- **Stays responsive.** Exports without overrides run in the current session and write a few objects per timer tick. Exports with overrides or sweeps run in a **background Blender process**, so your scene is never modified and the undo stack stays clean. Each preset has its own live console, progress bar and Cancel button.
- **JSON import / export** of presets, collections, objects and overrides.
- **Scriptable.** `bpy.ops.export_scene.batch_stl_multi(preset_index=0)` runs a preset from a script or from the command line and returns once every file is written.

## Quick start

1. Open the 3D Viewport sidebar (**N**) and select the **Combi Export** tab.
2. Choose a **Root** folder.
3. In **Presets**, add a preset and give it a name and a sub-folder.
4. In **Collections**, add the collections to export. Set a sub-folder and a tag per collection if you want them.
5. In **Objects**, tick the objects to include.
6. Press the export button at the start of the preset row.

To make variants, use the override button on the Global, Preset, Collection or Object level. Add a node group, a node and an input, then type a value or a sweep such as `\0:0.5:5` (a folder per value) or `_*` (every menu item in the file name).

## Example

A cube with a Geometry Nodes modifier that has a `Size` input, in a collection tagged `v2`. Sweeping `Size` with `\1:1:3` writes:

```
<root>/preset/1/Cube_v2.stl
<root>/preset/2/Cube_v2.stl
<root>/preset/3/Cube_v2.stl
```

## Requirements and notes

- **Blender 5.2 or newer.** NumPy ships with Blender, so there is nothing else to install.
- **STL output only** (binary STL, mesh objects).
- **Supported input types:** float, integer, boolean, string and menu. Other socket types (vectors, colors, objects and so on) block the export until their override is removed.
- **The ticks decide what is exported.** Each collection and each object has an export tick, and the view layer does not matter: a collection excluded in the view layer or disabled in viewports, or an object disabled in viewports, is exported all the same (the background worker switches it on in its copy of the file). Hiding with the eye icon never mattered. Files from earlier versions come in with what their view layer skipped unticked, so they export the same files.
- **File permission.** The add-on writes the exported STL files, and a temporary copy of the `.blend` file for the background worker. It uses no network access.
- If a file cannot be written (for example, it is locked by a slicer), it is logged as `FAILED` and the export continues with the next file.

## Documentation and support

The full reference (value field syntax, branch merging rules, shortcuts, architecture) is in the repository:

- Full README: https://github.com/oleroma/combi_export/blob/main/README.md
- Architecture: https://github.com/oleroma/combi_export/blob/main/ARCHITECTURE.md
- Issues and feedback: https://github.com/oleroma/combi_export/issues

Licensed under GPL-3.0-or-later.
