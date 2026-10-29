"""Stored data: JSON presets, clipboard and tier moves, migrations of older data, .blend round trips, object entries."""
import json
import os

import bpy
import combi_export as ce
from helpers import BlenderTestCase, cube_mesh, get_collection, make_gn_group, make_object, make_preset, override, planned_paths


def preset_dicts(scene=None):
    return [ce.data.copy_preset_to_dict(p) for p in (scene or bpy.context.scene).batch_stl_presets]


def live_paths(scene, preset):
    """The files a preset wrote before data version 4: the ticked objects that the view layer evaluates."""
    live = ce.targets.live_collection_names(bpy.context.view_layer)
    return sorted(f"{bl_obj.name}.stl" for c in preset.collections if bpy.data.collections.get(c.collection_name)
                  and c.collection_name in live for o in c.objects if o.export
                  for bl_obj in [bpy.data.objects.get(o.name)] if bl_obj and ce.targets.is_object_live(bl_obj, live))


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
    make_object("Spare", collection="Spares", group=group)
    spares = preset.collections.add()
    spares.collection_name, spares.export = "Spares", False
    ce.targets.sync_collection_objects(spares)
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

    def test_file_holds_the_global_overrides_and_the_root_folder(self):
        """They apply to every preset: without them the imported presets would write other files."""
        build_rich_preset()
        scene = bpy.context.scene
        override(scene.batch_stl_global_nodegroups, "Count", "INT", "\\1", "\\2", group_naming="\\G")
        scene.batch_stl_root_dir = "//out/"
        paths, global_ngs = planned_paths(), [ce.data.copy_ng_to_dict(ng) for ng in scene.batch_stl_global_nodegroups]
        path = os.path.join(self.tmp, "setup.json")
        bpy.ops.batch_stl.export_presets_json(filepath=path)

        def reset():
            scene.batch_stl_presets.clear()
            scene.batch_stl_global_nodegroups.clear()
            override(scene.batch_stl_global_nodegroups, "Size", "FLOAT", "\\9")
            scene.batch_stl_root_dir = self.tmp
        reset()
        self.assertEqual(bpy.ops.batch_stl.import_presets_json(filepath=path), {'FINISHED'})
        self.assertEqual([ce.data.copy_ng_to_dict(ng) for ng in scene.batch_stl_global_nodegroups], global_ngs)
        self.assertEqual(scene.batch_stl_root_dir, "//out/")
        self.assertEqual(planned_paths(), paths)

        # Unticked, the scene keeps its own
        reset()
        bpy.ops.batch_stl.import_presets_json(filepath=path, use_global_overrides=False, use_root_dir=False)
        self.assertEqual([ng.nodes[0].inputs[0].name for ng in scene.batch_stl_global_nodegroups], ["Size"])
        self.assertEqual(scene.batch_stl_root_dir, self.tmp)

        # A file of an earlier version (the list of presets only) leaves them alone
        with open(path, encoding="utf-8") as f: presets = json.load(f)["presets"]
        with open(path, "w", encoding="utf-8") as f: json.dump(presets, f)
        reset()
        self.assertEqual(bpy.ops.batch_stl.import_presets_json(filepath=path), {'FINISHED'})
        self.assertEqual(len(scene.batch_stl_presets), 1)
        self.assertEqual([ng.nodes[0].inputs[0].name for ng in scene.batch_stl_global_nodegroups], ["Size"])
        self.assertEqual(scene.batch_stl_root_dir, self.tmp)

    def test_invalid_files_change_nothing(self):
        build_rich_preset()
        scene = bpy.context.scene
        override(scene.batch_stl_global_nodegroups, "Count", "INT", "\\1", "\\2")
        def state(): return preset_dicts(), [ce.data.copy_ng_to_dict(ng) for ng in scene.batch_stl_global_nodegroups], scene.batch_stl_root_dir
        before = state()
        for content in ({"name": "not a list"}, [{"name": "fine"}, 5],
                        {"presets": [{"name": "fine"}], "root_dir": "//x", "global_nodegroups": [{"group": "GN"}, 5]}):
            with self.subTest(content=content):
                path = os.path.join(self.tmp, "bad.json")
                with open(path, "w", encoding="utf-8") as f: json.dump(content, f)
                with self.assertRaises(RuntimeError):  # the operator reports an error
                    bpy.ops.batch_stl.import_presets_json(filepath=path)
                self.assertEqual(state(), before)

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
        # The tag 'lid' replaced the value in the file name; the value itself stays, shown in brackets
        self.assertEqual([ce.fields.value_expr(i.values[0], i.override_type) for i in inputs], ["\\5", "\\0.5:0.25:3", "[False]_lid"])
        self.assertEqual(inputs["Flag"].values[0].value_menu, "False")

    def test_earlier_field_syntax(self):
        # '15 @B\ #x_' was folder 'B\' and tag 'x_' before the one-field syntax (data version 2)
        value = {"value_string": "15", "use_dir": True, "dir_tag": "B\\", "use_tag": True, "tag": "x_"}
        data = {"name": "Old", "data_version": 2, "collections": [], "nodegroups": [{"group": "GN", "nodes": [
            {"name": "<Modifier Interface>", "inputs": [{"name": "Size", "override_type": "FLOAT", "values": [value]}]}]}]}
        preset = bpy.context.scene.batch_stl_presets.add()
        ce.data.paste_preset_from_dict(preset, data)
        self.assertEqual(ce.fields.value_expr(preset.nodegroups[0].nodes[0].inputs[0].values[0], "FLOAT"), "\\B\\15\\_x_15")


    def test_version_3_file_unticks_what_the_view_layer_skips(self):
        """Before version 4 the view layer decided what a preset exported: a file of that version comes in with what
        the importing view layer does not evaluate unticked, so it exports the same files. A current file keeps its
        ticks whatever the view layer says."""
        make_object("Box", mesh=cube_mesh())
        make_object("Hidden", collection="Excluded", mesh=cube_mesh("h"))
        preset = make_preset(collections=("Parts", "Excluded"))
        current = ce.data.copy_setup_to_dict(bpy.context.scene)
        old = json.loads(json.dumps(current))
        old["data_version"] = old["presets"][0]["data_version"] = 3
        for c in old["presets"][0]["collections"]: del c["export"]
        bpy.context.view_layer.layer_collection.children["Excluded"].exclude = True
        bpy.context.scene.batch_stl_presets.clear()
        for name, data, ticks in (("old", old, [True, False]), ("current", current, [True, True])):
            with self.subTest(name):
                path = os.path.join(self.tmp, f"{name}.json")
                with open(path, "w", encoding="utf-8") as f: json.dump(data, f)
                self.assertEqual(bpy.ops.batch_stl.import_presets_json(filepath=path), {'FINISHED'})
                self.assertEqual([c.export for c in bpy.context.scene.batch_stl_presets[-1].collections], ticks)


class SavedJsonTests(BlenderTestCase):
    """Saving the .blend file writes <scene>_preset.json next to it (Scene.batch_stl_save_json, on by default)."""

    def save(self, folder="", name="file.blend", copy=False):
        path = os.path.join(self.tmp, folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=copy)
        return path

    def saved_json(self, folder="", scene_name="Scene"):
        return os.path.join(self.tmp, folder, scene_name + ce.data.SAVED_JSON_SUFFIX)

    def test_saving_writes_the_presets_and_their_files(self):
        build_rich_preset()
        scene = bpy.context.scene
        self.assertTrue(scene.batch_stl_save_json)
        override(scene.batch_stl_global_nodegroups, "Count", "INT", "\\1", "\\2")
        self.save()
        with open(self.saved_json(), encoding="utf-8") as f: data = json.load(f)
        self.assertEqual(data["presets"], preset_dicts())
        self.assertEqual(data["root_dir"], scene.batch_stl_root_dir)
        self.assertEqual(data["global_nodegroups"], [ce.data.copy_ng_to_dict(ng) for ng in scene.batch_stl_global_nodegroups])
        self.assertEqual(data["exports"], {"complete": True, "presets": [{"name": "Main", "files": sorted(set(planned_paths()))}]})

        # Import JSON reads it like an exported file
        before, paths = preset_dicts(), planned_paths()
        scene.batch_stl_presets.clear()
        self.assertEqual(bpy.ops.batch_stl.import_presets_json(filepath=self.saved_json()), {'FINISHED'})
        self.assertEqual(preset_dicts(), before)
        self.assertEqual(planned_paths(), paths)

    def test_named_after_the_scene_and_one_per_scene(self):
        make_gn_group()
        make_object("Box")
        make_preset("Main")
        bpy.context.scene.name = "Grip V"
        other = bpy.data.scenes.new("Other")
        make_preset("Second", scene=other)
        bpy.data.scenes.new("Empty")  # no presets: no file
        self.save()
        self.assertEqual(sorted(f for f in os.listdir(self.tmp) if f.endswith(".json")),
                         ["Grip_V_preset.json", "Other_preset.json"])

    def test_new_objects_are_listed_and_saved(self):
        """The object entries are synced before the file is written, so the file and the JSON agree."""
        make_gn_group()
        make_object("Box")
        make_preset("Main")
        make_object("Lid")  # no entry yet: the UI timer never runs in the background
        path = self.save()
        with open(self.saved_json(), encoding="utf-8") as f: files = json.load(f)["exports"]["presets"][0]["files"]
        self.assertEqual(files, ["Box.stl", "Lid.stl"])
        bpy.ops.wm.open_mainfile(filepath=path)
        self.assertEqual([o.name for o in bpy.context.scene.batch_stl_presets[0].collections[0].objects], ["Box", "Lid"])

    def test_copies_and_the_tick_off_write_nothing(self):
        make_gn_group()
        make_object("Box")
        make_preset("Main")
        self.save("a")
        os.remove(self.saved_json("a"))
        self.save("b", "copy.blend", copy=True)  # also what the export worker's snapshot is
        self.assertFalse(os.path.exists(self.saved_json("a")))
        self.assertFalse(os.path.exists(self.saved_json("b")))
        bpy.context.scene.batch_stl_save_json = False
        self.save("a")
        self.assertFalse(os.path.exists(self.saved_json("a")))
        # Save As writes next to the new file
        bpy.context.scene.batch_stl_save_json = True
        self.save("c")
        self.assertTrue(os.path.exists(self.saved_json("c")))

    def test_unchanged_json_is_left_alone(self):
        make_gn_group()
        make_object("Box")
        preset = make_preset("Main")
        self.save()
        path = self.saved_json()
        os.utime(path, (1000000000, 1000000000))
        bpy.ops.wm.save_mainfile()
        self.assertEqual(os.path.getmtime(path), 1000000000)
        preset.collections[0].objects[0].export_name = "Base"
        bpy.ops.wm.save_mainfile()
        self.assertNotEqual(os.path.getmtime(path), 1000000000)
        with open(path, encoding="utf-8") as f: self.assertEqual(json.load(f)["exports"]["presets"][0]["files"], ["Base.stl"])

    def test_list_stops_at_the_limit(self):
        group = make_gn_group()
        make_object("Box", group=group)
        make_object("Lid", group=group)
        preset = make_preset("Main")
        override(preset.nodegroups, "Size", "FLOAT", "\\1", "\\2")
        make_preset("Second")
        scene = bpy.context.scene
        for c in preset.collections: ce.targets.sync_collection_objects(c)
        full = ce.targets.export_manifest(scene)
        self.assertEqual(full["complete"], True)
        self.assertEqual(full["presets"][0]["files"], ["1/Box.stl", "1/Lid.stl", "2/Box.stl", "2/Lid.stl"])
        self.assertEqual(ce.targets.export_manifest(scene, limit=6)["complete"], True)
        capped = ce.targets.export_manifest(scene, limit=3)
        self.assertEqual(capped["complete"], False)
        self.assertEqual([len(p["files"]) for p in capped["presets"]], [3])


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

    def test_version_3_unticks_what_the_view_layer_skipped(self):
        """Data version 4: the ticks alone decide. What the view layer kept from exporting comes in unticked, so the
        first export after the update writes the same files: a collection it excludes, the objects of a mapped
        collection it does not evaluate (also ones without an entry yet). A missing collection keeps its tick."""
        scene = bpy.context.scene
        make_object("Box", mesh=cube_mesh())
        make_object("Off", mesh=cube_mesh("o")).hide_viewport = True
        get_collection("Inner", parent=get_collection("Parts"))
        make_object("InInner", collection="Inner", mesh=cube_mesh("i"))
        make_object("Gone", collection="Away", mesh=cube_mesh("g"))
        preset = make_preset(collections=("Parts", "Away", "Missing"))
        make_object("Late", collection="Inner", mesh=cube_mesh("l"))  # no entry yet: added while the add-on was off
        layers = bpy.context.view_layer.layer_collection.children
        layers["Parts"].children["Inner"].exclude = True
        layers["Away"].exclude = True
        before = live_paths(scene, preset)
        self.assertEqual(before, ["Box.stl"])
        scene.batch_stl_data_version = 3

        ce.data.migrate_scene_data(scene)
        self.assertEqual(scene.batch_stl_data_version, ce.data.DATA_VERSION)
        self.assertEqual([c.export for c in preset.collections], [True, False, True])
        self.assertEqual({o.name: o.export for o in preset.collections[0].objects},
                         {"Box": True, "Off": False, "InInner": False, "Late": False})
        self.assertEqual(planned_paths(preset), before)

    def test_migration_follows_the_view_layer_the_window_shows(self):
        scene = bpy.context.scene
        make_object("Box", mesh=cube_mesh())
        preset = make_preset()
        second = scene.view_layers.new("Second")
        second.layer_collection.children["Parts"].exclude = True
        bpy.context.window.view_layer = second
        scene.batch_stl_data_version = 3
        ce.data.migrate_scene_data(scene)
        self.assertFalse(preset.collections[0].export)

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

    def test_export_name_has_no_spaces(self):
        entry = self.col.objects[0]
        entry.export_name = " Big Lid "
        self.assertEqual((entry.name_override, entry.export_name, planned_paths()), ("Big_Lid", "Big_Lid", ["Big_Lid.stl"]))
        # An object name with spaces shows (and exports) with underscores, and typing it either way follows the object
        self.box.name = "My Box"
        self.sync()
        entry = self.col.objects[0]
        self.assertEqual(entry.name_override, "Big_Lid")
        entry.export_name = "My Box"
        self.assertEqual((entry.name_override, entry.export_name, planned_paths()), ("", "My_Box", ["My_Box.stl"]))
        entry.export_name = "Big Lid"
        entry.export_name = "My_Box"
        self.assertEqual(entry.name_override, "")

    def test_name_fields_replace_spaces(self):
        entry = self.col.objects[0]
        fields = [(self.preset, "preset_prefix"), (self.col, "tag"), (self.col, "sub_path"), (entry, "tag"), (entry, "sub_path")]
        for i, (owner, prop) in enumerate(fields):
            with self.subTest(prop=prop):
                setattr(owner, prop, f" my {prop} {i} / x y ")
                self.assertEqual(getattr(owner, prop), f"my_{prop}_{i}/x_y")
        self.assertEqual(planned_paths(), ["my_preset_prefix_0/x_y/my_sub_path_2/x_y/my_sub_path_4/x_y/"
                                           "Box_my_tag_1_x_y_my_tag_3_x_y.stl"])

    def test_preset_uids_are_unique(self):
        copy = bpy.context.scene.batch_stl_presets.add()
        copy.uid = self.preset.uid
        ce.data.ensure_preset_uids(bpy.context.scene)
        self.assertNotEqual(copy.uid, self.preset.uid)
