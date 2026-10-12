"""The data behind the panels: the depsgraph handler that marks the UI cache dirty, and the merge predictions the
override tables keep between redraws (drawn into a recording layout: the panels themselves need a window)."""
import itertools
import types
from unittest import mock

import bpy
import combi_export as ce
from combi_export.common import _ui_cache
from helpers import BlenderTestCase, add_group, make_gn_group, make_object, make_preset, override


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


class WindowTargetTests(BlenderTestCase):
    """The UI cache is built for the window's scene and view layer. Blender fires no depsgraph update when either
    switches (checked in a live Blender), so the cache timer has to notice the switch itself."""

    def setUp(self):
        super().setUp()
        wm = bpy.context.window_manager
        wm.batch_stl_info_tab, wm.batch_stl_info_global = 'TREE', False
        self.scene_a = bpy.context.scene
        make_object("BoxA")
        make_preset("PA")
        self.scene_b = bpy.data.scenes.new("B")
        bpy.context.window.scene = self.scene_b
        make_object("BoxB", collection="PartsB")
        make_preset("PB", collections=("PartsB",))
        bpy.context.window.scene = self.scene_a
        self.rebuild()

    def rebuild(self):
        """One tick of the UI cache timer."""
        ce.ui_cache.rebuild_ui_cache_if_dirty()
        self.assertFalse(_ui_cache["is_dirty"], "the rebuild failed")

    def tree_files(self):
        def walk(node):
            yield from node.get("_files", [])
            for key, child in node.items():
                if key != "_files": yield from walk(child)
        return sorted(walk(_ui_cache["tree"][0]))

    def test_the_tree_follows_the_windows_scene(self):
        self.assertEqual(self.tree_files(), ["BoxA.stl"])
        bpy.context.window.scene = self.scene_b
        self.rebuild()
        self.assertEqual(self.tree_files(), ["BoxB.stl"])
        self.assertEqual(_ui_cache["stats"]["global"]["objs"], 1)
        bpy.context.window.scene = self.scene_a
        self.rebuild()
        self.assertEqual(self.tree_files(), ["BoxA.stl"])

    def test_the_tree_follows_the_windows_view_layer(self):
        """A view layer with the mapped collection excluded evaluates none of its objects."""
        bare = self.scene_a.view_layers.new("Bare")
        bare.layer_collection.children["Parts"].exclude = True
        bpy.context.window.view_layer = bare
        self.rebuild()
        self.assertEqual(self.tree_files(), [])
        bpy.context.window.view_layer = self.scene_a.view_layers[0]
        self.rebuild()
        self.assertEqual(self.tree_files(), ["BoxA.stl"])

    def test_an_unchanged_window_costs_no_rebuild(self):
        builds, build = [], ce.ui_cache._rebuild_ui_cache
        with mock.patch.object(ce.ui_cache, "_rebuild_ui_cache", lambda: builds.append(1) or build()):
            for _tick in range(3): ce.ui_cache.rebuild_ui_cache_if_dirty()
            self.assertEqual(builds, [])
            bpy.context.window.scene = self.scene_b
            for _tick in range(3): ce.ui_cache.rebuild_ui_cache_if_dirty()
            self.assertEqual(len(builds), 1)


class FakeLayout:
    """Records what is drawn into it. Every item remembers its layout: `path` is the chain of sub-layouts down from the
    root, `attrs` what was set on it (alignment, ui_units_x, ...). prop() reads the property, as Blender does (computed
    fields run their get())."""

    def __init__(self, log=None, path=(), ids=None):
        object.__setattr__(self, "log", [] if log is None else log)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "attrs", {})
        object.__setattr__(self, "ids", itertools.count() if ids is None else ids)
    def __setattr__(self, key, value): self.attrs[key] = value  # alert, enabled, alignment, ...
    def _child(self, *_args, **_kwargs): return FakeLayout(self.log, self.path + (next(self.ids),), self.ids)
    row = column = box = split = _child

    def _record(self, **item):
        item = types.SimpleNamespace(layout=self, **item)
        self.log.append(item)
        return item

    def separator(self, factor=1.0, **_kwargs): self._record(kind="separator", factor=factor)

    def label(self, text="", icon='NONE'): self._record(kind="label", text=text, icon=icon)

    def prop(self, data, prop, text=None, icon='NONE', **_kwargs):
        getattr(data, prop)
        self._record(kind="prop", prop=prop, text=text, icon=icon)

    def template_list(self, list_class, list_id, data, prop, active_data, active_prop, rows=5, maxrows=5, **_kwargs):
        self._record(kind="template_list", list_class=list_class, rows=rows, maxrows=maxrows)

    def operator(self, idname, text="", icon='NONE', **_kwargs):
        return self._record(kind="operator", idname=idname, text=text, icon=icon, info="")


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
        ce.ui.draw_overrides_table(layout, wm, self.entry.nodegroups, False, "Object")
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


class OverrideButtonTests(BlenderTestCase):
    """An empty override table is not drawn: the button at the start of the panel's first row adds its first group,
    and clears the table once it has groups."""
    PANELS = (  # panel, (is_collection, is_preset, is_global), the table's node groups
        (ce.ui.VIEW3D_PT_batch_export_stl_main, (False, False, True), lambda p: bpy.context.scene.batch_stl_global_nodegroups),
        (ce.ui.VIEW3D_PT_batch_export_stl_presets, (False, True, False), lambda p: p.nodegroups),
        (ce.ui.VIEW3D_PT_batch_export_stl_collections, (True, False, False), lambda p: p.collections[0].nodegroups),
        (ce.ui.VIEW3D_PT_batch_export_stl_objects, (False, False, False), lambda p: p.collections[0].objects[0].nodegroups),
    )

    def setUp(self):
        super().setUp()
        make_object("Box", group=make_gn_group())
        self.preset = make_preset()

    def buttons(self, panel):
        """The override buttons the panel draws (its table is left out: it has buttons of its own)."""
        layout = FakeLayout()
        with mock.patch.object(ce.ui, "draw_overrides_table", lambda *args, **kwargs: None):
            panel.draw(types.SimpleNamespace(layout=layout), bpy.context)
        return [item for item in layout.log if item.kind == "operator" and item.idname == "batch_stl.table_action"]

    @staticmethod
    def level(op): return op.is_collection, op.is_preset, op.is_global

    @staticmethod
    def table(nodegroups, level):
        """What draw_overrides_table draws for `nodegroups`."""
        is_collection, is_preset, is_global = level
        layout = FakeLayout()
        ce.ui.draw_overrides_table(layout, bpy.context.window_manager, nodegroups, is_collection, "T",
                                   is_preset=is_preset, is_global=is_global)
        return layout.log

    @staticmethod
    def press(op):
        bpy.ops.batch_stl.table_action(action=op.action, is_collection=op.is_collection, is_preset=op.is_preset, is_global=op.is_global,
                                       ng_idx=op.ng_idx, n_idx=op.n_idx, i_idx=op.i_idx, v_idx=op.v_idx)

    def test_empty_table_has_an_override_button_and_no_table(self):
        for panel, level, nodegroups in self.PANELS:
            with self.subTest(panel=panel.bl_label):
                ops = self.buttons(panel)
                self.assertEqual([(op.action, op.icon, self.level(op)) for op in ops], [('ADD_GROUP', 'DECORATE_OVERRIDE', level)])
                self.assertEqual(self.table(nodegroups(self.preset), level), [])

    def test_override_buttons_add_a_group_to_their_own_table(self):
        for panel, level, nodegroups in self.PANELS:
            with self.subTest(panel=panel.bl_label):
                op, = self.buttons(panel)
                self.press(op)
                self.assertEqual(len(nodegroups(self.preset)), 1)
                self.assertEqual([(op.action, op.icon, self.level(op)) for op in self.buttons(panel)], [('CLEAR_GROUPS', 'X', level)],
                                 "the table has its own add button; the lead button turns into clear")
                self.assertIn('ADD_GROUP', [getattr(item, "action", None) for item in self.table(nodegroups(self.preset), level)])

    def test_clear_button_removes_every_group_of_its_level_only(self):
        for _panel, _level, nodegroups in self.PANELS:
            override(nodegroups(self.preset), "Size", "FLOAT", "\\1", "\\2")  # a group with a node, an input and values
            add_group(nodegroups(self.preset), group="Other")
        counts = lambda: [len(nodegroups(self.preset)) for _panel, _level, nodegroups in self.PANELS]
        self.assertEqual(counts(), [2, 2, 2, 2])
        for done, (panel, level, nodegroups) in enumerate(self.PANELS, 1):
            with self.subTest(panel=panel.bl_label):
                clear, = self.buttons(panel)
                self.assertEqual(clear.action, 'CLEAR_GROUPS')
                self.press(clear)
                self.assertEqual(counts(), [0] * done + [2] * (len(self.PANELS) - done), "only this level is cleared")
                self.assertEqual([op.action for op in self.buttons(panel)], ['ADD_GROUP'], "back to adding the first group")
                self.assertEqual(self.table(nodegroups(self.preset), level), [])

    def test_the_add_button_returns_when_the_last_group_goes(self):
        ng = add_group(self.preset.nodegroups)
        self.assertEqual([op.action for op in self.buttons(ce.ui.VIEW3D_PT_batch_export_stl_presets)], ['CLEAR_GROUPS'])
        ng.group_name = ""  # an emptied field deletes the group
        self.assertEqual(len(self.preset.nodegroups), 0)
        self.assertEqual([op.action for op in self.buttons(ce.ui.VIEW3D_PT_batch_export_stl_presets)], ['ADD_GROUP'])

    def test_paste_button_only_with_a_group_on_the_clipboard_and_an_empty_table(self):
        panel = ce.ui.VIEW3D_PT_batch_export_stl_objects
        self.assertEqual([op.action for op in self.buttons(panel)], ['ADD_GROUP'])
        ce.operators._clipboard["nodegroup"] = ce.data.copy_ng_to_dict(add_group(self.preset.nodegroups))
        self.assertEqual([(op.action, op.icon) for op in self.buttons(panel)], [('ADD_GROUP', 'DECORATE_OVERRIDE'), ('PASTE_GROUP', 'PASTEDOWN')])
        paste, = (op for op in self.buttons(panel) if op.action == 'PASTE_GROUP')
        self.press(paste)
        self.assertEqual(len(self.preset.collections[0].objects[0].nodegroups), 1)
        self.assertEqual([op.action for op in self.buttons(panel)], ['CLEAR_GROUPS'], "a table with groups pastes from its own header row")

    def test_a_group_without_a_name_has_a_remove_button(self):
        """Its empty name field has no clear button, and clearing the field is how a group is deleted."""
        for panel, level, nodegroups in self.PANELS:
            with self.subTest(panel=panel.bl_label):
                self.press(self.buttons(panel)[0])  # adds an empty group
                self.assertEqual([ng.group_name for ng in nodegroups(self.preset)], [""])
                remove, = (item for item in self.table(nodegroups(self.preset), level) if getattr(item, "action", None) == 'REMOVE_GROUP')
                self.assertEqual((remove.icon, remove.ng_idx, self.level(remove)), ('X', 0, level))
                self.press(remove)
                self.assertEqual(len(nodegroups(self.preset)), 0)
                self.assertEqual([op.action for op in self.buttons(panel)], ['ADD_GROUP'], "the table is gone, its first group can be added again")

    def test_only_the_empty_group_gets_the_remove_button_and_goes(self):
        nodegroups = self.preset.nodegroups
        add_group(nodegroups, group="GN")
        add_group(nodegroups, group="")
        add_group(nodegroups, group="Other")
        level = (False, True, False)
        removes = [item for item in self.table(nodegroups, level) if getattr(item, "action", None) == 'REMOVE_GROUP']
        self.assertEqual([op.ng_idx for op in removes], [1], "groups with a name have the field's own clear button")
        self.press(removes[0])
        self.assertEqual([ng.group_name for ng in nodegroups], ["GN", "Other"])

    OPEN_PROPS = ("batch_stl_ui_global_ovr", "batch_stl_ui_preset_ovr", "batch_stl_ui_collection_ovr", "batch_stl_ui_object_ovr")

    def collapse_tables(self):
        for prop in self.OPEN_PROPS: setattr(bpy.context.window_manager, prop, False)

    def open_tables(self):
        return [getattr(bpy.context.window_manager, prop) for prop in self.OPEN_PROPS]

    def test_adding_a_group_opens_the_table_of_its_level(self):
        for (panel, level, nodegroups), prop in zip(self.PANELS, self.OPEN_PROPS):
            with self.subTest(panel=panel.bl_label):
                self.collapse_tables()
                lead_add, = self.buttons(panel)
                self.press(lead_add)
                self.assertEqual(self.open_tables(), [p == prop for p in self.OPEN_PROPS], "only the table of this level opens")
                self.collapse_tables()
                table_add = next(item for item in self.table(nodegroups(self.preset), level) if getattr(item, "action", None) == 'ADD_GROUP')
                self.press(table_add)
                self.assertEqual(self.open_tables(), [p == prop for p in self.OPEN_PROPS], "the table's own add button too")
                self.assertEqual(len(nodegroups(self.preset)), 2)

    def test_pasting_a_group_opens_the_table(self):
        ce.operators._clipboard["nodegroup"] = ce.data.copy_ng_to_dict(add_group(self.preset.nodegroups))
        object_panel = ce.ui.VIEW3D_PT_batch_export_stl_objects
        self.collapse_tables()
        paste, = (op for op in self.buttons(object_panel) if op.action == 'PASTE_GROUP')
        self.press(paste)
        self.assertEqual(self.open_tables(), [False, False, False, True])

    def test_actions_that_add_nothing_leave_the_table_collapsed(self):
        object_panel = ce.ui.VIEW3D_PT_batch_export_stl_objects
        nodegroups = lambda: self.preset.collections[0].objects[0].nodegroups
        self.collapse_tables()
        bpy.ops.batch_stl.table_action(action='PASTE_GROUP')  # an empty clipboard pastes nothing
        self.assertEqual((len(nodegroups()), self.open_tables()), (0, [False] * 4))
        add_group(nodegroups())
        self.collapse_tables()
        clear, = self.buttons(object_panel)
        self.press(clear)
        self.assertEqual((len(nodegroups()), self.open_tables()), (0, [False] * 4))

    def test_no_button_without_an_active_target(self):
        """No active collection or object: nothing to add a table to (and the panels that need one are not drawn)."""
        self.preset.collections.clear()
        self.assertEqual(self.buttons(ce.ui.VIEW3D_PT_batch_export_stl_collections), [])
        self.assertFalse(ce.ui.VIEW3D_PT_batch_export_stl_objects.poll(bpy.context))
        bpy.context.scene.batch_stl_presets.clear()
        self.assertEqual(self.buttons(ce.ui.VIEW3D_PT_batch_export_stl_presets), [])
        self.assertFalse(ce.ui.VIEW3D_PT_batch_export_stl_collections.poll(bpy.context))


class PanelLayoutTests(BlenderTestCase):
    """Where the panels put their buttons: list actions beside their lists, JSON buttons with the root folder."""

    def setUp(self):
        super().setUp()
        make_object("Box", group=make_gn_group())
        make_preset()

    @staticmethod
    def drawn(panel, method="draw"):
        layout = FakeLayout()
        getattr(panel, method)(types.SimpleNamespace(layout=layout), bpy.context)
        return layout.log

    @staticmethod
    def operators(log, prefix):
        return [item for item in log if item.kind == "operator" and item.idname.startswith(prefix)]

    def test_list_buttons_are_a_column_beside_their_list(self):
        for panel, list_class, prefix in ((ce.ui.VIEW3D_PT_batch_export_stl_presets, "BATCH_STL_UL_presets", "batch_stl.preset_actions"),
                                          (ce.ui.VIEW3D_PT_batch_export_stl_collections, "BATCH_STL_UL_collections", "batch_stl.collection_actions")):
            with self.subTest(panel=panel.bl_label):
                log = self.drawn(panel)
                lst, = (item for item in log if item.kind == "template_list")
                self.assertEqual(lst.list_class, list_class)
                self.assertEqual((lst.rows, lst.maxrows), (ce.ui.LIST_ROWS, ce.ui.LIST_ROWS), "tall enough for the buttons")
                ops = self.operators(log, prefix)
                self.assertEqual([(op.action, op.icon) for op in ops], [
                    ('ADD', 'ADD'), ('REMOVE', 'TRASH'), ('UP', 'TRIA_UP'), ('DOWN', 'TRIA_DOWN'), ('COPY', 'COPYDOWN'), ('PASTE', 'PASTEDOWN')])
                column = ops[0].layout
                self.assertEqual({op.layout.path for op in ops}, {column.path}, "one column")
                self.assertEqual(column.path[:-1], lst.layout.path, "in the row that holds the list")

    def test_headers_hold_no_buttons(self):
        bpy.context.scene.batch_stl_presets[0].last_export_time = 1.5
        for panel in (ce.ui.VIEW3D_PT_batch_export_stl_main, ce.ui.VIEW3D_PT_batch_export_stl_presets,
                      ce.ui.VIEW3D_PT_batch_export_stl_collections, ce.ui.VIEW3D_PT_batch_export_stl_objects):
            with self.subTest(panel=panel.bl_label):
                header = getattr(panel, "draw_header_preset", None)
                log = self.drawn(panel, "draw_header_preset") if header else []
                self.assertEqual([item for item in log if item.kind in ("operator", "prop")], [])
        times = [item.text for item in self.drawn(ce.ui.VIEW3D_PT_batch_export_stl_presets, "draw_header_preset") if item.kind == "label"]
        self.assertEqual(times, ["1.50s"], "the last export time is a label, it stays")

    def test_override_buttons_lead_the_stats_row(self):
        for panel in (ce.ui.VIEW3D_PT_batch_export_stl_presets, ce.ui.VIEW3D_PT_batch_export_stl_collections,
                      ce.ui.VIEW3D_PT_batch_export_stl_objects):
            with self.subTest(panel=panel.bl_label):
                log = self.drawn(panel)
                stats = [item for item in log if item.kind == "label" and item.layout.attrs.get("alignment") == 'CENTER']
                self.assertTrue(stats)
                button, = self.operators(log, "batch_stl.table_action")
                self.assertEqual((button.action, button.icon), ('ADD_GROUP', 'DECORATE_OVERRIDE'))
                self.assertEqual(button.layout.attrs["ui_units_x"], 1, "as wide as its button")
                self.assertEqual(button.layout.path[:-1], stats[0].layout.path[:-1], "in the stats row")
                self.assertLess(log.index(button), log.index(stats[0]), "before the counts")

    def test_global_override_button_leads_the_root_folder_row(self):
        """The Combi Export panel has no stats row: its first row is the root folder."""
        log = self.drawn(ce.ui.VIEW3D_PT_batch_export_stl_main)
        button, = self.operators(log, "batch_stl.table_action")
        self.assertEqual((button.action, button.is_global), ('ADD_GROUP', True))
        root, = (item for item in log if item.kind == "prop" and item.prop == "batch_stl_root_dir")
        label, = (item for item in log if item.kind == "label" and item.text == "Root:")
        self.assertEqual(button.layout.path[:-1], root.layout.path, "in the root folder's row")
        self.assertLess(log.index(button), log.index(label))

    def test_json_buttons_share_the_root_folder_row(self):
        log = self.drawn(ce.ui.VIEW3D_PT_batch_export_stl_main)
        root, = (item for item in log if item.kind == "prop" and item.prop == "batch_stl_root_dir")
        self.assertEqual(root.text, "", "the label is drawn apart from the field")
        label, = (item for item in log if item.kind == "label" and item.text == "Root:")
        self.assertEqual(label.layout.path[:-1], root.layout.path, "the label is in the field's row")
        self.assertEqual(label.layout.attrs["alignment"], 'RIGHT')
        self.assertEqual(label.layout.attrs["ui_units_x"], ce.ui.ROOT_LABEL_UNITS)
        json_ops = [op for op in self.operators(log, "batch_stl.") if op.idname.endswith("_presets_json")]
        self.assertEqual([(op.idname, op.icon) for op in json_ops], [("batch_stl.import_presets_json", 'IMPORT'), ("batch_stl.export_presets_json", 'EXPORT')])
        self.assertEqual({op.layout.path[:-1] for op in json_ops}, {root.layout.path}, "in the field's row")
        # The label comes first, then the field, then the buttons
        order = [log.index(label), log.index(root), log.index(json_ops[0])]
        self.assertEqual(order, sorted(order))

    def draw_preset_item(self):
        layout = FakeLayout()
        preset = bpy.context.scene.batch_stl_presets[0]
        ce.ui.BATCH_STL_UL_presets.draw_item(None, bpy.context, layout, bpy.context.scene, preset, 0, None, "", 0)
        return layout.log

    def test_preset_export_button_leads_its_row_set_apart_by_a_gap(self):
        """Export, and Cancel while it runs, come before the name field, with a gap after them."""
        job = ce.data.get_job(bpy.context.scene.batch_stl_presets[0], create=True)
        for exporting, idname, icon in ((False, "export_scene.batch_stl_multi", 'EXPORT'), (True, "batch_stl.cancel_export", 'CANCEL')):
            with self.subTest(exporting=exporting):
                job.is_exporting = exporting
                log = self.draw_preset_item()
                button, = (item for item in log if item.kind == "operator")
                self.assertEqual((button.idname, button.icon), (idname, icon))
                gap, = (item for item in log if item.kind == "separator" and item.layout.path == button.layout.path)
                name, = (item for item in log if item.kind == "prop" and item.prop == "name")
                self.assertLess(log.index(button), log.index(gap))
                self.assertLess(log.index(gap), log.index(name))
                self.assertGreaterEqual(gap.factor, 1.0)
                self.assertEqual(any(item.kind == "prop" and item.prop == "export_progress" for item in log), exporting)
