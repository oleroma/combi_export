# Combi Export — Project Review (2026-10-09)

Scope: `combi_export/__init__.py`, README.md, ARCHITECTURE.md. Every finding was reproduced in background Blender 5.2.0, then fixed and re-tested with probe scripts (kept outside the repo). Most items of the previous review (commit `8569394`) were already fixed before this round.

## Findings and fixes

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | High | Picking a Menu socket on `<Modifier Interface>` deleted the whole override group. Its default is `''`, and writing it into the value counts as "delete". String inputs with an empty default did the same. Shift+Add Input silently dropped menu inputs. | `sync_input_type` writes defaults inside `raw_edits()`. Menus fall back to their first item. |
| 2 | High | The export's collision check came from the tree preview. Above 5,000 files it reported nothing, so the export overwrote files. | `find_export_clashes` checks every path before an export starts, with no limit. |
| 3 | High | Switching scenes during a headless export cancelled it. The job then stayed "exporting", which locked every panel until the file was reloaded. | Jobs are keyed by preset uid and the scene's `session_uid`, never by the current scene. |
| 4 | High | Objects in a collection disabled in viewports were exported without their modifiers. | `live_collection_names` / `iter_export_objects` skip objects Blender does not evaluate. The writer also checks `is_evaluated`. |
| 5 | Medium | Menus reached through a reroute or nested group, and built-in node menu sockets (e.g. Merge by Distance > Mode), listed no items, so every value was rejected. | `modifier_menu_items` reads the modifier's enum items. `socket_menu_items` reads the items from the error of an invalid assignment. |
| 6 | Medium | Any undo cancelled running exports (handler plus Ctrl+Z interception). | Removed. Undo cannot affect the worker's copy, and jobs no longer depend on preset indices. |
| 7 | Medium | The stats rebuild enumerated every variant: 1.5 s per edit for 20 objects × 1,000 variants. | Without branch anchors the count is a product of pool sizes, and walker results are cached. The same case now takes 7 ms; generation went from ~40 to ~7 µs per variant. |
| 8 | Medium | `register()` deleted every `fast_batch_stl_*` temp folder, including one used by another Blender instance's export. | Only folders older than a day are removed. |
| 9 | Medium | `obj_ptr` (an ID pointer) added a user, so deleting an exported object only unlinked it. Pasted or imported entries never got a pointer. | Entries store `session_uid` (`obj_uid`), re-stamped by name on load. The migration removes the old pointers. |
| 10 | Medium | `use_combine` was lost by copy/paste, JSON and Shift+Up/Down moves. | Serialized. |
| 11 | Low | Sweep step counts above 1000 were silently cut to 1000. | Rejected and flagged invalid. |
| 12 | Low | v1.0.0 data: JSON import ignored the input type (an INT `5` became `0.0`), sweeps were dropped, and `.blend` values were lost. | `migrate_legacy_value` for JSON and `DATA_VERSION` 2 for `.blend` files. |
| 13 | Low | UI state lived on the Scene: saved, and rolled back by undo (`SKIP_SAVE` has no effect on Scene properties). | Moved to the WindowManager. The migration drops the old keys. |
| 14 | Low | Dead code: `mod[...]` modifier access, menu index mapping, unused table actions, two unused UI properties. | Removed. |

## Verification (background Blender 5.2.0)
- **Fix checks, 33/33:** each issue above, plus drawing every panel and list against a strict fake layout (unknown properties or operators fail the check).
- **Export end to end, 8/8:** direct export through the operator, collision abort, worker run with a menu behind a reroute and a built-in node menu (geometry checked per variant), disabled collection skipped.
- **Migration, 10/10:** files saved by v1.0.0 and by `HEAD`, the v1.0.0 JSON, and uid re-stamping after save and reload.
- **Naming regression:** variant folders and tokens match `HEAD` on four merge scenarios. The fast count equals full enumeration on 30 random stacks.

## Not covered
- **Interactive UI:** search dropdowns, merge bars, undo entries and the modal progress UI were not checked in an interactive session. The modal loop cannot run in background mode.
- **Item lookup for built-in node menus:** it relies on the wording of Blender's enum error message. Re-check it after a Blender upgrade.
