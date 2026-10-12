"""Stored data: JSON presets, clipboard and tier moves, migrations of older data, .blend round trips, object entries."""
import json
import os

import bpy
import combi_export as ce
from helpers import BlenderTestCase, cube_mesh, get_collection, make_gn_group, make_object, make_preset, override, planned_paths


def preset_dicts(scene=None):
    return [ce.data.copy_preset_to_dict(p) for p in (scene or bpy.context.scene).batch_stl_presets]


def build_rich_preset():
    """A preset using every stored field: prefix, collection tag / sub-folder, object entries, block naming, sweeps."""
    group = make_gn_group()
    make_object("Box", group=group)
    make_object("Lid", group=group)
    preset = make_preset("Main", prefix="out")
    col = preset.collections[0]
    col.use_tag, col.tag, col.sub_path = False, "v2", "parts"
    box, lid = col.objects
    lid.export = False
    box.export_name, box.tag, box.sub_path = "Base", "t", "b"
    override(preset.nodegroups, "Size", "FLOAT", "\\0:0.5:3", group_naming="\\G")
    override(col.nodegroups, "Mode", "MENU", "_*")
    override(box.nodegroups, "Word", "STRING", "\\B\\x", "_{a, b}")
    override(box.nodegroups, "Vertices X", "INT", "4", node="Cube [MESH_PRIMITIVE_CUBE]")
    return preset


class JsonTests(BlenderTestCase):
    def test_round_trip_through_a_file(self):
        build_rich_preset()
        before, paths = preset_dicts(), planned_paths()
        path = os.path.join(self.tmp, "presets.json")
        self.assertEqual(bpy.ops.batch_stl.export_presets_json(filepath=path), {'FINISHED'})
        bpy.context.scene.batch_stl_presets.clear()
        self.assertEqual(bpy.ops.batch_stl.import_presets_json(filepath=path), {'FINISHED'})
        self.assertEqual(preset_dicts(), before)
        self.assertEqual(planned_paths(), paths)
        self.assertTrue(bpy.context.scene.batch_stl_presets[0].uid)

    def test_invalid_files_change_nothing(self):
        build_rich_preset()
        before = preset_dicts()
        for content in ({"name": "not a list"}, [{"name": "fine"}, 5]):
            with self.subTest(content=content):
                path = os.path.join(self.tmp, "bad.json")
                with open(path, "w", encoding="utf-8") as f: json.dump(content, f)
                with self.assertRaises(RuntimeError):  # the operator reports an error
                    bpy.ops.batch_stl.import_presets_json(filepath=path)
                self.assertEqual(preset_dicts(), before)

    def test_version_1_0_0_preset(self):
        """Typed number / bool fields, typed sweeps and one tag for folder and file name, as v1.0.0 saved them."""
        def v100(**fields):
            value = {"value_bool": True, "value_int": 0, "value_float": 0.0, "value_string": "", "value_menu": "", "use_tag": False,
                     "tag": "", "use_dir": True, "use_sweep": False, "sweep_range": "", "sweep_start_float": 0.0, "sweep_step_float": 1.0,
                     "sweep_count_float": 2, "sweep_start_int": 0, "sweep_step_int": 1, "sweep_count_int": 2}
            value.update(fields)
            return value
        inputs = [
            {"name": "Count", "override_type": "INT", "values": [v100(value_int=5)]},
            {"name": "Size", "override_type": "FLOAT", "values": [v100(use_sweep=True, sweep_start_float=0.5, sweep_step_float=0.25, sweep_count_float=3)]},
            {"name": "Flag", "override_type": "BOOLEAN", "values": [v100(value_bool=False, use_dir=False, use_tag=True, tag="lid")]},
        ]
        data = {"name": "Old", "preset_prefix": "", "collections": [],
                "nodegroups": [{"group": "GN", "nodes": [{"name": "<Modifier Interface>", "inputs": inputs}]}]}
        make_gn_group()
        preset = bpy.context.scene.batch_stl_presets.add()
        ce.data.paste_preset_from_dict(preset, data)
        inputs = preset.nodegroups[0].nodes[0].inputs
        # The tag 'lid' replaced the value in the file name; the value itself stays (hover shows it)
        self.assertEqual([ce.fields.value_expr(i.values[0], i.override_type) for i in inputs], ["\\5", "\\0.5:0.25:3", "_lid"])
        self.assertEqual(inputs["Flag"].values[0].value_menu, "False")

    def test_earlier_field_syntax(self):
        # '15 @B\ #x_' was folder 'B\' and tag 'x_' before the one-field syntax (data version 2)
        value = {"value_string": "15", "use_dir": True, "dir_tag": "B\\", "use_tag": True, "tag": "x_"}
        data = {"name": "Old", "data_version": 2, "collections": [], "nodegroups": [{"group": "GN", "nodes": [
            {"name": "<Modifier Interface>", "inputs": [{"name": "Size", "override_type": "FLOAT", "values": [value]}]}]}]}
        preset = bpy.context.scene.batch_stl_presets.add()
        ce.data.paste_preset_from_dict(preset, data)
        self.assertEqual(ce.fields.value_expr(preset.nodegroups[0].nodes[0].inputs[0].values[0], "FLOAT"), "\\B\\15\\_x_15")


class ClipboardAndMoveTests(BlenderTestCase):
    def test_copy_paste_preset(self):
        build_rich_preset()
        bpy.ops.batch_stl.preset_actions(action='COPY')
        bpy.ops.batch_stl.preset_actions(action='PASTE')
        scene = bpy.context.scene
        first, second = preset_dicts()
        self.assertEqual(first, second)
        self.assertNotEqual(scene.batch_stl_presets[0].uid, scene.batch_stl_presets[1].uid)
        self.assertEqual(scene.batch_stl_preset_index, 1)

    def test_shift_down_copies_a_group_to_every_collection(self):
        make_object("Box", group=make_gn_group())
        make_object("Lid", collection="Lids")
        preset = make_preset(collections=("Parts", "Lids"))
        override(preset.nodegroups, "Size", "FLOAT", "\\1", "\\2")
        group = ce.data.copy_ng_to_dict(preset.nodegroups[0])
        bpy.ops.batch_stl.table_action(action='MOVE_GROUP_DOWN', is_preset=True, ng_idx=0, shift_pressed=True)
        self.assertEqual(len(preset.nodegroups), 0)
        self.assertEqual([[ce.data.copy_ng_to_dict(ng) for ng in c.nodegroups] for c in preset.collections], [[group], [group]])

    def test_shift_up_moves_a_group_to_the_collection(self):
        make_object("Box", group=make_gn_group())
        preset = make_preset()
        col = preset.collections[0]
        override(col.objects[0].nodegroups, "Size", "FLOAT", "\\1", "\\2")
        group = ce.data.copy_ng_to_dict(col.objects[0].nodegroups[0])
        bpy.ops.batch_stl.table_action(action='MOVE_GROUP_UP', ng_idx=0, shift_pressed=True)
        self.assertEqual(len(col.objects[0].nodegroups), 0)
        self.assertEqual([ce.data.copy_ng_to_dict(ng) for ng in col.nodegroups], [group])

    def test_exporting_preset_cannot_be_removed(self):
        make_object("Box")
        preset = make_preset()
        ce.data.get_job(preset, create=True).is_exporting = True
        bpy.ops.batch_stl.preset_actions(action='REMOVE')
        self.assertEqual(len(bpy.context.scene.batch_stl_presets), 1)
        ce.data.get_job(preset).is_exporting = False
        bpy.ops.batch_stl.preset_actions(action='REMOVE')
        self.assertEqual(len(bpy.context.scene.batch_stl_presets), 0)
        self.assertEqual(len(bpy.context.window_manager.batch_stl_jobs), 0, "the removed preset's job (its log) stays")


class MigrationTests(BlenderTestCase):
    def test_unversioned_scene(self):
        scene = bpy.context.scene
        make_object("Box", group=make_gn_group())
        preset = make_preset()
        val = override(preset.nodegroups, "Count", "INT", "\\1").values[0]
        # Stored as an unversioned (v1.0.0) scene: a typed int, one tag for folder and file name, an ID pointer, UI state
        with ce.common.raw_edits():
            val.value_string, val.dir_tag, val.tag, val.use_tag = "", "", "B_", True
        val["value_int"] = 7
        entry = preset.collections[0].objects[0]
        entry["obj_ptr"] = bpy.data.objects["Box"]
        scene["batch_stl_ui_tips"] = 1
        scene.property_unset("batch_stl_data_version")

        ce.data.migrate_scene_data(scene)
        self.assertEqual(val.value_string, "7")
        self.assertEqual((val.dir_tag, val.tag), ("B_*", "B_*"))
        self.assertNotIn("value_int", val.keys())
        self.assertNotIn("obj_ptr", entry.keys())
        self.assertNotIn("batch_stl_ui_tips", scene.keys())
        self.assertEqual(scene.batch_stl_data_version, ce.data.DATA_VERSION)
        ce.data.migrate_scene_data(scene)  # a current scene is left alone
        self.assertEqual((val.dir_tag, val.tag), ("B_*", "B_*"))

    def test_save_and_reload(self):
        build_rich_preset()
        before, paths = preset_dicts(), planned_paths()
        path = os.path.join(self.tmp, "file.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        self.assertEqual(preset_dicts(), before)
        self.assertEqual(planned_paths(), paths)
        for entry in bpy.context.scene.batch_stl_presets[0].collections[0].objects:
            self.assertEqual(entry.obj_uid, bpy.data.objects[entry.name].session_uid)

    def test_scene_filled_by_a_script_survives_reload(self):
        """A scene created in this session is stamped when saved (the UI timer only stamps the scene in view, and never
        runs in background Blender): the next load must not migrate its current templates again."""
        make_gn_group()
        scripted = bpy.data.scenes.new("Scripted")
        preset = scripted.batch_stl_presets.add()
        override(preset.nodegroups, "Size", "FLOAT", "\\B\\15\\_x_15")
        path = os.path.join(self.tmp, "scripted.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        val = bpy.data.scenes["Scripted"].batch_stl_presets[0].nodegroups[0].nodes[0].inputs[0].values[0]
        self.assertEqual((val.dir_tag, val.tag), ("B/*", "x_*"))

    def save_unversioned_scene(self):
        """A file whose scene 'Old' holds an unversioned (v1.0.0) value (see test_unversioned_scene), saved without the
        stamp like an older version of the add-on did. Opens a fresh file and returns the old file's scene folder."""
        scene = bpy.context.scene
        scene.name = "Old"
        make_object("Box", group=make_gn_group())
        val = override(make_preset().nodegroups, "Count", "INT", "\\1").values[0]
        with ce.common.raw_edits():
            val.value_string, val.dir_tag, val.tag, val.use_tag = "", "", "B_", True
        val["value_int"] = 7
        scene.property_unset("batch_stl_data_version")
        old_path = os.path.join(self.tmp, "old.blend")
        save_pre = bpy.app.handlers.save_pre
        save_pre.remove(ce.data.batch_stl_save_handler)
        try: bpy.ops.wm.save_as_mainfile(filepath=old_path)
        finally: save_pre.append(ce.data.batch_stl_save_handler)
        bpy.ops.wm.read_factory_settings(use_empty=True)
        return old_path + "/Scene/"

    def assertMigratedOnce(self):
        """The imported scene 'Old' is migrated now, so stamping it when saved does not leave it unmigrated, and the
        next load does not migrate it again."""
        def value():
            v = bpy.data.scenes["Old"].batch_stl_presets[0].nodegroups[0].nodes[0].inputs[0].values[0]
            return v.value_string, v.dir_tag, v.tag
        self.assertEqual(value(), ("7", "B_*", "B_*"))
        path = os.path.join(self.tmp, "new.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        self.assertEqual(value(), ("7", "B_*", "B_*"))

    def test_appended_old_scene_is_migrated_once(self):
        bpy.ops.wm.append(directory=self.save_unversioned_scene(), filename="Old")
        self.assertMigratedOnce()

    def test_linked_old_scene_made_local_is_migrated_once(self):
        bpy.ops.wm.link(directory=self.save_unversioned_scene(), filename="Old")
        bpy.data.scenes["Old"].make_local()
        self.assertMigratedOnce()


class ObjectEntryTests(BlenderTestCase):
    def setUp(self):
        super().setUp()
        self.box = make_object("Box", mesh=cube_mesh())
        self.preset = make_preset()
        self.col = self.preset.collections[0]

    def sync(self):
        ce.targets.sync_collection_objects(self.col)
        return [o.name for o in self.col.objects]

    def test_entries_follow_the_collection(self):
        lid = make_object("Lid")
        get_collection("Parts").objects.link(bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam")))
        sub = get_collection("Sub", parent=bpy.data.collections["Parts"])
        sub.objects.link(bpy.data.objects.new("Nested", bpy.data.meshes.new("Nested")))
        self.assertEqual(self.sync(), ["Box", "Lid", "Nested"])
        bpy.data.objects.remove(lid)
        self.assertEqual(self.sync(), ["Box", "Nested"])

    def test_rename_keeps_settings(self):
        entry = self.col.objects[0]
        entry.tag, entry.export = "t", False
        self.box.name = "Base"
        self.assertEqual(self.sync(), ["Base"])
        self.assertEqual((self.col.objects[0].tag, self.col.objects[0].export), ("t", False))

    def test_rename_made_while_the_addon_was_off(self):
        entry = self.col.objects[0]
        entry.tag, entry.obj_uid = "t", 987654321  # the uid of an object that no longer exists
        self.box.name = "Base"
        self.assertEqual(self.sync(), ["Base"])
        self.assertEqual(self.col.objects[0].tag, "t")

    def test_export_name(self):
        entry = self.col.objects[0]
        self.assertEqual(entry.export_name, "Box")
        entry.export_name = "Lid"
        self.assertEqual((entry.name_override, planned_paths()), ("Lid", ["Lid.stl"]))
        entry.export_name = "Box"
        self.assertEqual(entry.name_override, "")
        entry.export_name = "Lid"
        entry.export_name = "   "
        self.assertEqual(entry.name_override, "")
        self.box.name = "Base"
        self.sync()
        self.assertEqual(self.col.objects[0].export_name, "Base")

    def test_preset_uids_are_unique(self):
        copy = bpy.context.scene.batch_stl_presets.add()
        copy.uid = self.preset.uid
        ce.data.ensure_preset_uids(bpy.context.scene)
        self.assertNotEqual(copy.uid, self.preset.uid)
