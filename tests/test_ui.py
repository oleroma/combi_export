"""The data behind the panels: the depsgraph handler that marks the UI cache dirty, and the merge predictions the
override tables keep between redraws (drawn into a recording layout: the panels themselves need a window)."""
import types
from unittest import mock

import bpy
import combi_export as ce
from combi_export.common import _ui_cache
from helpers import BlenderTestCase, make_gn_group, make_object, make_preset, override


class DepsgraphHandlerTests(BlenderTestCase):
    def setUp(self):
        super().setUp()
        self.group = make_gn_group()
        self.box = make_object("Box", group=self.group)
        make_preset()
        bpy.context.view_layer.update()
        self.settle()

    def settle(self):
        """The state a UI cache rebuild leaves (its timer never runs in background Blender)."""
        _ui_cache["objects_state"] = ce.ui_cache.mapped_objects_state(bpy.context.scene)
        _ui_cache["live"] = ce.targets.live_collection_names(bpy.context.view_layer)
        _ui_cache["is_dirty"] = False

    def update(self, change):
        """(whether the handler walked the mapped objects, whether it marked the cache dirty) for one change."""
        walks, walk = [], ce.ui_cache.mapped_objects_state
        with mock.patch.object(ce.ui_cache, "mapped_objects_state", lambda scene: walks.append(1) or walk(scene)):
            change()
            bpy.context.view_layer.update()
        dirty = _ui_cache["is_dirty"]
        self.settle()
        return bool(walks), dirty

    def test_transforms_skip_the_walk(self):
        """Every step of a transform drag is an object update that only re-evaluates it."""
        self.assertEqual(self.update(lambda: setattr(self.box, "location", (1, 2, 3))), (False, False))
        self.assertEqual(self.update(lambda: setattr(self.box, "rotation_euler", (0, 0, 1))), (False, False))

    def test_renames_and_visibility_still_mark_the_cache_dirty(self):
        self.assertEqual(self.update(lambda: setattr(self.box, "name", "Lid")), (True, True))
        layer_collection = bpy.context.view_layer.layer_collection.children["Parts"]
        for change in (lambda: setattr(layer_collection, "exclude", True), lambda: setattr(layer_collection, "exclude", False),
                       lambda: setattr(self.box, "hide_viewport", True), lambda: setattr(bpy.data.collections["Parts"], "hide_viewport", True)):
            self.assertTrue(self.update(change)[1])

    def test_node_tree_updates_renew_the_predictions(self):
        revision = _ui_cache["revision"]
        self.update(lambda: setattr(self.group.nodes["Switch"].inputs["A"], "default_value", 3.0))
        self.assertGreater(_ui_cache["revision"], revision)


class FakeLayout:
    """Records what is drawn into it. prop() reads the property, as Blender does (computed fields run their get())."""

    def __init__(self, log=None): object.__setattr__(self, "log", [] if log is None else log)
    def __setattr__(self, key, value): pass  # alert, enabled, alignment, ...
    def _child(self, *_args, **_kwargs): return FakeLayout(self.log)
    row = column = box = split = _child
    def separator(self, **_kwargs): pass
    def label(self, text="", icon='NONE'): self.log.append(types.SimpleNamespace(kind="label", text=text, icon=icon))

    def prop(self, data, prop, text=None, icon='NONE', **_kwargs):
        getattr(data, prop)
        self.log.append(types.SimpleNamespace(kind="prop", prop=prop, icon=icon))

    def operator(self, idname, text="", icon='NONE', **_kwargs):
        op = types.SimpleNamespace(kind="operator", idname=idname, text=text, icon=icon, info="")
        self.log.append(op)
        return op


class PredictionCacheTests(BlenderTestCase):
    def setUp(self):
        super().setUp()
        make_object("Box", group=make_gn_group())
        preset = make_preset()
        override(bpy.context.scene.batch_stl_global_nodegroups, "Word", "STRING", "\\A", "\\B")
        self.entry = preset.collections[0].objects[0]
        self.size = override(self.entry.nodegroups, "Size", "FLOAT", "\\1", "\\2", group_naming="\\A")

    def draw(self, is_open=True):
        wm = bpy.context.window_manager
        wm.batch_stl_ui_object_ovr = is_open
        layout = FakeLayout()
        ce.ui.draw_overrides_table(layout, wm, self.entry.nodegroups, False, "batch_stl_ui_object_ovr", "Object")
        return [item for item in layout.log if item.kind == "operator" and item.idname == "batch_stl.merge_info"]

    @staticmethod
    def bars(ops): return [op.text for op in ops if op.text]

    def test_predictions_are_kept_until_the_data_changes(self):
        builds, build = [], ce.ui.predict_table
        with mock.patch.object(ce.ui, "predict_table", lambda nodegroups: builds.append(1) or build(nodegroups)):
            self.assertEqual(self.bars(self.draw()), ["Merges into A"])
            # The clear view shows the block's bar as the merge icon at the start of the input's row
            self.assertEqual(self.draw(is_open=False)[0].icon, 'AUTOMERGE_ON')
            self.assertEqual(len(builds), 1, "redraws must reuse the predictions")

            self.entry.nodegroups[0].naming = "\\C"  # an edit: the block no longer merges
            self.assertEqual(self.bars(self.draw()), [])
            self.assertEqual(len(builds), 2)

            with ce.common.raw_edits(): self.size.values.add()  # a row added by a script, without any callback
            self.draw()
            self.assertEqual(len(builds), 3)

    def test_kept_predictions_match_new_ones(self):
        self.draw()
        kept = ce.ui.table_predictions(self.entry.nodegroups)
        self.assertEqual(kept, ce.ui.predict_table(self.entry.nodegroups))
