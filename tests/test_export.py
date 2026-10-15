"""End to end: the export operator (invoke / modal, and execute for scripts) on the direct path and with the background
worker, and the worker's output protocol."""
import collections
import contextlib
import io
import os
import queue
import threading
import time
import types
import warnings
from unittest import mock

import bpy
import combi_export as ce
from helpers import (BlenderTestCase, ContextProxy, ExportRun, cube_mesh, get_collection, make_gn_group,
                     make_instancing_group, make_object, make_preset, override, planned_paths, read_stl, run_export,
                     stl_bounds, write_job, written_files)

TIMER = types.SimpleNamespace(type='TIMER')


class DirectExportTests(BlenderTestCase):
    def test_direct_export(self):
        make_object("Box", mesh=cube_mesh())
        preset = make_preset()
        result, run = run_export()
        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(written_files(self.tmp), ["Box.stl"])
        self.assertEqual(len(read_stl(os.path.join(self.tmp, "Box.stl"))[1]), 12)
        self.assertIn("BATCH EXPORT COMPLETE", run.log()[-1])
        self.assertFalse(ce.data.get_job(preset).is_exporting)

    def test_the_ticks_decide_not_the_view_layer(self):
        """Every ticked object of a ticked collection is written, whatever the view layer says: objects it does not
        evaluate (excluded or disabled collections, disabled objects) go to the worker, which switches them on in its
        copy, so they come with their modifiers. The open file's view layer is left as it was."""
        parts = get_collection("Parts")
        group = make_gn_group()
        make_object("Visible", mesh=cube_mesh())
        eye_hidden = make_object("EyeHidden", mesh=cube_mesh())
        make_object("Disabled", mesh=cube_mesh()).hide_viewport = True
        make_object("Unticked", mesh=cube_mesh())
        get_collection("Excluded", parent=parts).objects.link(bpy.data.objects.new("InExcluded", cube_mesh("x")))
        get_collection("Off", parent=parts).objects.link(bpy.data.objects.new("InOff", cube_mesh("o")))
        ce.nodes.set_modifier_input(make_object("Sized", collection="Excluded", group=group).modifiers[0],
                                    ce.nodes.get_modifier_socket_identifier(group, "Size"), 3.0)
        make_object("Skipped", collection="Other", mesh=cube_mesh("s"))
        bpy.data.collections["Off"].hide_viewport = True
        layers = bpy.context.view_layer.layer_collection.children
        layers["Parts"].children["Excluded"].exclude = True
        eye_hidden.hide_set(True)
        preset = make_preset(collections=("Parts", "Other"))
        next(o for o in preset.collections[0].objects if o.name == "Unticked").export = False
        preset.collections[1].export = False

        expected = ["Disabled.stl", "EyeHidden.stl", "InExcluded.stl", "InOff.stl", "Sized.stl", "Visible.stl"]
        self.assertEqual(sorted(planned_paths()), expected)
        result, run = run_export()
        log = "\n".join(run.log())
        self.assertEqual(result, {'FINISHED'}, log)
        self.assertEqual(written_files(self.tmp), expected)
        for name in expected:
            self.assertEqual(len(read_stl(os.path.join(self.tmp, name))[1]), 12, name)
        lo, hi = stl_bounds(os.path.join(self.tmp, "Sized.stl"))
        self.assertAlmostEqual(hi[0] - lo[0], 3.0, places=4)  # evaluated: the modifier's size, not the empty mesh
        self.assertIn("Exporting collections the view layer excludes or disables: Excluded, Off", log)
        self.assertNotIn("SKIPPED", log)
        # The worker switched them on in its copy only
        self.assertTrue(layers["Parts"].children["Excluded"].exclude)
        self.assertTrue(bpy.data.collections["Off"].hide_viewport)
        self.assertTrue(bpy.data.objects["Disabled"].hide_viewport)

    def test_objects_without_overrides_the_view_layer_evaluates_stay_on_the_direct_path(self):
        """Only what the view layer does not evaluate costs a worker; a plain preset is still written in this process."""
        make_object("A", mesh=cube_mesh())
        make_object("B", mesh=cube_mesh())
        make_preset()
        with mock.patch.object(ce.standby.subprocess, "Popen") as popen:
            result, _run = run_export()
        self.assertEqual(result, {'FINISHED'})
        popen.assert_not_called()
        self.assertEqual(written_files(self.tmp), ["A.stl", "B.stl"])

    def test_eye_hidden_objects_keep_their_instances_and_visibility(self):
        """Objects hidden with the eye icon (their own or their collection's) are written with their instances, and the
        open file's visibility, selection and active object are as before."""
        group = make_instancing_group(count=10)
        make_object("Shown", group=group).select_set(True)
        hidden = make_object("Hidden", group=group)
        get_collection("EyeCol", parent=get_collection("Parts"))
        make_object("InEyeCol", collection="EyeCol", group=group)
        both = make_object("BothHidden", collection="EyeCol", group=group)
        view_layer = bpy.context.view_layer
        eye_col = lambda: view_layer.layer_collection.children["Parts"].children["EyeCol"]
        eye_col().hide_viewport = True
        hidden.hide_set(True)
        both.hide_set(True)
        view_layer.objects.active = hidden
        make_preset()

        def state():
            objects = {ob.name: (ob.hide_get(), ob.visible_get(), ob.select_get()) for ob in bpy.data.objects}
            return objects, eye_col().hide_viewport, view_layer.objects.active.name
        before = state()
        result, run = run_export()
        self.assertEqual(result, {'FINISHED'}, run.log())
        names = ["BothHidden", "Hidden", "InEyeCol", "Shown"]
        self.assertEqual(written_files(self.tmp), [f"{n}.stl" for n in names])
        for name in names:
            self.assertEqual(len(read_stl(os.path.join(self.tmp, f"{name}.stl"))[1]), 10 * 12, name)
        self.assertEqual(state(), before)

    def start_sliced_export(self, names="ABC"):
        """An export of plain objects that writes one of them per poll (a budget of 0 still writes one): (run, context, job)."""
        for name in names: make_object(name, mesh=cube_mesh())
        preset = make_preset()
        run, context = ExportRun(), ContextProxy()
        run.direct_budget = 0
        self.assertEqual(run.invoke(context, None), {'RUNNING_MODAL'})
        self.assertEqual(written_files(self.tmp), [], "the click only queues the objects")
        job = ce.data.get_job(preset)
        self.assertTrue(job.is_exporting)
        return run, context, job

    def test_direct_objects_are_written_a_few_per_poll(self):
        """Each timer poll writes the next objects and returns, so the UI redraws in between instead of freezing."""
        run, context, job = self.start_sliced_export()
        for count in (1, 2):
            self.assertEqual(run.modal(context, TIMER), {'PASS_THROUGH'})
            self.assertEqual(written_files(self.tmp), ["A.stl", "B.stl"][:count])
            self.assertEqual(job.export_status.split(" | ")[0], f"Direct {count}/3")
            self.assertAlmostEqual(job.export_progress, count / 3, places=5)
        self.assertEqual(run.modal(context, TIMER), {'FINISHED'})
        self.assertEqual(written_files(self.tmp), ["A.stl", "B.stl", "C.stl"])
        self.assertFalse(job.is_exporting)
        log = run.log()
        self.assertIn("BATCH EXPORT COMPLETE", log[-1])
        self.assertEqual(sum("STARTING NATIVE DIRECT EXPORT (3 Objects)" in line for line in log), 1)

    def test_a_poll_writes_objects_up_to_its_budget(self):
        make_object("A", mesh=cube_mesh())
        make_object("B", mesh=cube_mesh())
        make_preset()
        run, context = ExportRun(), ContextProxy()
        run.direct_budget = 3600.0
        self.assertEqual(run.invoke(context, None), {'RUNNING_MODAL'})
        self.assertEqual(run.modal(context, TIMER), {'FINISHED'})
        self.assertEqual(written_files(self.tmp), ["A.stl", "B.stl"])

    def test_object_deleted_between_polls_is_skipped(self):
        run, context, _job = self.start_sliced_export("AB")
        self.assertEqual(run.modal(context, TIMER), {'PASS_THROUGH'})
        bpy.data.objects.remove(bpy.data.objects["B"])
        self.assertEqual(run.modal(context, TIMER), {'FINISHED'})
        self.assertEqual(written_files(self.tmp), ["A.stl"])
        self.assertTrue(any("SKIPPED (object no longer in the collection)" in line for line in run.log()))

    def test_cancel_stops_the_direct_export(self):
        run, context, job = self.start_sliced_export()
        self.assertEqual(run.modal(context, TIMER), {'PASS_THROUGH'})
        bpy.ops.batch_stl.cancel_export(preset_uid=job.preset_uid)
        self.assertEqual(run.modal(context, TIMER), {'CANCELLED'})
        self.assertEqual(written_files(self.tmp), ["A.stl"])
        self.assertFalse(job.is_exporting)


class RefusalTests(BlenderTestCase):
    def setUp(self):
        super().setUp()
        make_object("Box", group=make_gn_group())
        self.preset = make_preset()

    def assertRefused(self, message):
        result, run = run_export()
        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(any(message in text for _level, text in run.reports), run.reports)
        self.assertEqual(written_files(self.tmp), [])

    def test_colliding_paths(self):
        override(self.preset.nodegroups, "Size", "FLOAT", "1", "2")
        self.assertRefused("written more than once")

    def test_invalid_value(self):
        inp = override(self.preset.nodegroups, "Size", "FLOAT", "\\1")
        with ce.common.raw_edits(): inp.values[0].value_string = "x"
        self.assertRefused("Improper setup")

    def test_missing_root(self):
        bpy.context.scene.batch_stl_root_dir = ""
        self.assertRefused("Missing Root Directory")

    def test_relative_root_of_an_unsaved_file(self):
        bpy.context.scene.batch_stl_root_dir = "//out"
        self.assertRefused("save the .blend file")

    def test_root_accepts_blend_relative_paths(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            bpy.context.scene.batch_stl_root_dir = "//out"


class WorkerExportTests(BlenderTestCase):
    def test_worker_export(self):
        """A real background worker: menu values, a value merged into one branch, a node override that cuts a link,
        and an object without overrides on the direct path."""
        group = make_gn_group()
        box = make_object("Box", group=group)
        make_object("Plain", mesh=cube_mesh())
        preset = make_preset()
        scene = bpy.context.scene
        override(scene.batch_stl_global_nodegroups, "Mode", "MENU", "\\A", "\\B")
        override(preset.nodegroups, "Size", "FLOAT", "\\A\\3")
        override(preset.collections[0].nodegroups, "Vertices X", "INT", "4", node="Cube [MESH_PRIMITIVE_CUBE]")
        expected = sorted(planned_paths())
        self.assertEqual(expected, ["A/3/Box.stl", "B/Box.stl", "Plain.stl"])

        result, run = run_export()
        log = "\n".join(run.log())
        self.assertEqual(result, {'FINISHED'}, log)
        self.assertNotIn("CRASH", log)
        self.assertEqual(written_files(self.tmp), expected)
        self.assertFalse(ce.data.get_job(preset).is_exporting)
        self.assertFalse(os.path.exists(run.temp_dir), "the worker's temp folder is left behind")

        # Size 3 in the A branch; back at its default 1 in the B branch, moved by Mode B; 4 vertices along X everywhere
        for path, x_range in (("A/3/Box.stl", (-1.5, 1.5)), ("B/Box.stl", (9.5, 10.5))):
            lo, hi = stl_bounds(os.path.join(self.tmp, path))
            self.assertAlmostEqual(lo[0], x_range[0], places=4)
            self.assertAlmostEqual(hi[0], x_range[1], places=4)
            self.assertEqual(len(read_stl(os.path.join(self.tmp, path))[1]), 28)

        # The open file is untouched: the worker exported from its own copy
        self.assertTrue(group.nodes["Cube"].inputs["Vertices X"].is_linked)
        mode = ce.nodes.get_modifier_socket_identifier(group, "Mode")
        self.assertEqual(ce.nodes.get_modifier_input(box.modifiers[0], mode), "A")

        # The same state set by hand and written in this process gives the same bytes
        ce.nodes.set_modifier_input(box.modifiers[0], mode, "B")
        cube_socket = group.nodes["Cube"].inputs["Vertices X"]
        group.links.remove(cube_socket.links[0])
        cube_socket.default_value = 4
        bpy.context.view_layer.update()
        direct = os.path.join(self.tmp, "direct.stl")
        ce.stl.write_object_stl(direct, box, bpy.context.evaluated_depsgraph_get())
        with open(direct, "rb") as a, open(os.path.join(self.tmp, "B/Box.stl"), "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_worker_writes_instances_of_eye_hidden_objects(self):
        make_object("Inst", group=make_instancing_group(count=10)).hide_set(True)
        preset = make_preset()
        override(preset.nodegroups, "Vertices X", "INT", "\\2", "\\3", group="Inst", node="Cube [MESH_PRIMITIVE_CUBE]")
        self.assertEqual(sorted(planned_paths()), ["2/Inst.stl", "3/Inst.stl"])
        result, run = run_export()
        self.assertEqual(result, {'FINISHED'}, "\n".join(run.log()))
        # 10 cubes of 12 triangles; with 3 vertices along X each cube has 2 more quads on 4 sides
        for path, tris in (("2/Inst.stl", 10 * 12), ("3/Inst.stl", 10 * 20)):
            self.assertEqual(len(read_stl(os.path.join(self.tmp, path))[1]), tris, path)
        self.assertTrue(bpy.data.objects["Inst"].hide_get())

    def test_scripted_export_waits_for_the_worker(self):
        """bpy.ops runs execute(): the export is done when the call returns, with its log printed; errors raise."""
        make_object("Box", group=make_gn_group())
        make_object("Plain", mesh=cube_mesh())
        preset = make_preset()
        override(preset.nodegroups, "Size", "FLOAT", "\\1", "\\2")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(bpy.ops.export_scene.batch_stl_multi(), {'FINISHED'})
        self.assertEqual(written_files(self.tmp), ["1/Box.stl", "2/Box.stl", "Plain.stl"])
        self.assertIn("Permutation 2/2", out.getvalue())
        self.assertIn("BATCH EXPORT COMPLETE", out.getvalue())
        self.assertFalse(ce.data.get_job(preset).is_exporting)
        with self.assertRaises(RuntimeError): bpy.ops.export_scene.batch_stl_multi(preset_index=3)
        bpy.context.scene.batch_stl_root_dir = ""
        with self.assertRaises(RuntimeError): bpy.ops.export_scene.batch_stl_multi()

    def test_worker_starts_before_the_direct_objects_are_written(self):
        """The worker boots while the objects without overrides are written, so the click only saves the snapshot and
        starts it."""
        make_object("Box", group=make_gn_group())
        make_object("Plain", mesh=cube_mesh())
        override(make_preset().nodegroups, "Size", "FLOAT", "\\1", "\\2")
        run, context = ExportRun(), ContextProxy()
        run.direct_budget = 0
        result = run.invoke(context, None)
        self.addCleanup(run.cleanup, context)
        self.assertEqual(result, {'RUNNING_MODAL'})
        self.assertIsNotNone(run.process)
        self.assertNotIn("Plain.stl", written_files(self.tmp))
        self.assertEqual(run.drive(context, result), {'FINISHED'})
        self.assertEqual(written_files(self.tmp), ["1/Box.stl", "2/Box.stl", "Plain.stl"])

    def test_worker_crash_still_writes_the_direct_objects(self):
        for name in "AB": make_object(name, mesh=cube_mesh())
        make_object("Box", group=make_gn_group())
        override(make_preset().nodegroups, "Size", "FLOAT", "\\1", "\\2")
        crashed = FakeProcess(returncode=1)
        crashed.stdout = io.StringIO("")
        run, context = ExportRun(), ContextProxy()
        run.direct_budget = 0
        with mock.patch.object(ce.standby.subprocess, "Popen", return_value=crashed):
            self.assertEqual(run.invoke(context, None), {'RUNNING_MODAL'})
        self.assertEqual(run.modal(context, TIMER), {'CANCELLED'})
        self.assertEqual(written_files(self.tmp), ["A.stl", "B.stl"])
        self.assertIn("CRASH DETECTED: Worker exited with code 1", run.log()[-1])
        self.assertFalse(ce.data.get_job(bpy.context.scene.batch_stl_presets[0]).is_exporting)

    def run_worker(self, job):
        """run_headless_export in this process: (exit code, printed output)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit:
            ce.headless.run_headless_export(job)
        return exit.exception.code, out.getvalue()

    def test_worker_generates_merged_variants_once(self):
        """Variants that values merging into a branch limit are only counted by enumerating them: the worker keeps them
        from counting for the export, unless there are more than KEEP_VARIANTS."""
        make_object("Box", group=make_gn_group())
        preset = make_preset()
        override(bpy.context.scene.batch_stl_global_nodegroups, "Mode", "MENU", "\\A", "\\B")
        override(preset.nodegroups, "Size", "FLOAT", "\\A\\3")
        expected = sorted(planned_paths())
        self.assertEqual(expected, ["A/3/Box.stl", "B/Box.stl"])
        generate = ce.engine.generate_named_combinations
        for keep, generations in ((ce.headless.KEEP_VARIANTS, 1), (1, 2)):
            with self.subTest(keep=keep):
                calls = []
                def counted(*args, **kwargs):
                    calls.append(1)
                    return generate(*args, **kwargs)
                job, root = os.path.join(self.tmp, "job.json"), os.path.join(self.tmp, f"out{keep}")
                write_job(job, preset, root)
                with mock.patch.object(ce.engine, "generate_named_combinations", counted), \
                        mock.patch.object(ce.headless, "generate_named_combinations", counted), \
                        mock.patch.object(ce.headless, "KEEP_VARIANTS", keep):
                    code, out = self.run_worker(job)
                self.assertEqual(code, 0, out)
                self.assertIn("BATCH_STL_TOTAL:2", out)
                self.assertEqual(written_files(root), expected)
                self.assertEqual(len(calls), generations)

    def test_worker_exports_the_preset_of_the_job_scene(self):
        """Not the one of the context's scene (in the worker: the saved file's first window), here 'Main'."""
        make_object("Box", group=make_gn_group())
        override(make_preset("Main").nodegroups, "Size", "FLOAT", "\\1", "\\2")
        second = bpy.data.scenes.new("Second")
        second.collection.children.link(bpy.data.collections["Parts"])
        other = make_preset("Other", scene=second)
        override(other.nodegroups, "Size", "FLOAT", "\\5", "\\6")
        job, root = os.path.join(self.tmp, "job.json"), os.path.join(self.tmp, "out")
        write_job(job, other, root)
        code, out = self.run_worker(job)
        self.assertEqual(code, 0, out)
        self.assertEqual(written_files(root), ["5/Box.stl", "6/Box.stl"])

    def test_worker_without_the_job_preset_exports_nothing(self):
        """A uid that is not in the job's scene is an error (reported as a crash), never a fallback to another preset,
        e.g. the one now at the same index."""
        make_object("Box", mesh=cube_mesh())
        job, root = os.path.join(self.tmp, "job.json"), os.path.join(self.tmp, "out")
        write_job(job, make_preset("Removed"), root)
        bpy.context.scene.batch_stl_presets.remove(0)
        make_preset("Other")
        code, out = self.run_worker(job)
        self.assertEqual(code, 1)
        self.assertIn("ERROR: Preset not found", out)
        self.assertEqual(written_files(root), [])

    def run_view_layer_job(self, window=True):
        """The job's view layer evaluates the objects, not the context's: here the mapped collection is excluded in the
        context's view layer only."""
        make_object("Box", group=make_gn_group())
        preset = make_preset()
        override(preset.nodegroups, "Size", "FLOAT", "\\1", "\\2")
        job_layer = bpy.context.scene.view_layers.new("Job")
        bpy.context.view_layer.layer_collection.children["Parts"].exclude = True
        job, root = os.path.join(self.tmp, "job.json"), os.path.join(self.tmp, "out")
        write_job(job, preset, root, view_layer=job_layer)
        with contextlib.nullcontext() if window else bpy.context.temp_override(window=None):
            code, out = self.run_worker(job)
        self.assertEqual(code, 0, out)
        self.assertEqual(written_files(root), ["1/Box.stl", "2/Box.stl"])
        for size in (1, 2):
            lo, hi = stl_bounds(os.path.join(root, f"{size}/Box.stl"))
            self.assertAlmostEqual(hi[0] - lo[0], size, places=4)

    def test_worker_evaluates_the_job_view_layer(self):
        self.run_view_layer_job()
        self.assertEqual(bpy.context.view_layer.name, "Job")  # the window shows it, so the context matches

    def test_worker_evaluates_the_job_view_layer_without_a_window(self):
        self.run_view_layer_job(window=False)
        self.assertEqual(bpy.context.view_layer.name, "ViewLayer")  # the worker used the job's view layer explicitly

    def test_worker_switches_on_only_what_each_batch_exports(self):
        """Each batch includes and enables the collections of its objects and their parents, whatever the saved view
        layer says, and excludes everything else."""
        group = make_gn_group()
        parts = get_collection("Parts")
        make_object("Box", collection=get_collection("Deep", parent=get_collection("Mid", parent=parts)).name, group=group)
        make_object("Lid", collection="Side", group=group).hide_viewport = True
        make_object("Unmapped", collection="Unmapped", group=group)
        layers = bpy.context.view_layer.layer_collection.children
        layers["Parts"].exclude = True
        bpy.data.collections["Side"].hide_viewport = True
        preset = make_preset(collections=("Parts", "Side"))
        override(preset.nodegroups, "Size", "FLOAT", "\\1", "\\2")
        override(preset.collections[1].nodegroups, "Count", "INT", "3")  # Lid gets a batch of its own

        states, isolate = [], ce.headless.isolate_batch
        def recording(tree, batch_objects):
            enabled = isolate(tree, batch_objects)
            states.append(({ob.name for ob in batch_objects}, {lc.collection.name for lc, _p in tree if not lc.exclude}))
            return enabled
        job, root = os.path.join(self.tmp, "job.json"), os.path.join(self.tmp, "out")
        write_job(job, preset, root)
        with mock.patch.object(ce.headless, "isolate_batch", recording):
            code, out = self.run_worker(job)
        self.assertEqual(code, 0, out)
        self.assertEqual(written_files(root), ["1/Box.stl", "1/Lid.stl", "2/Box.stl", "2/Lid.stl"])
        self.assertEqual(sorted(states, key=lambda st: sorted(st[0])), [({"Box"}, {"Parts", "Mid", "Deep"}), ({"Lid"}, {"Side"})])
        for size in (1, 2):
            lo, hi = stl_bounds(os.path.join(root, f"{size}/Lid.stl"))
            self.assertAlmostEqual(hi[0] - lo[0], size, places=4)

    def test_worker_disables_again_what_a_batch_enabled(self):
        """A collection or object disabled in viewports is enabled for its own batch only: the next batch sees it as
        saved (a collection instance leaves out what is disabled)."""
        group = make_gn_group()
        make_object("Lid", collection="Side", group=group).hide_viewport = True
        make_object("Box", collection="Parts", group=group)
        bpy.data.collections["Side"].hide_viewport = True
        preset = make_preset(collections=("Side", "Parts"))
        override(preset.nodegroups, "Size", "FLOAT", "1")
        override(preset.collections[0].nodegroups, "Count", "INT", "3")  # Lid gets a batch of its own, the first

        states, isolate = [], ce.headless.isolate_batch
        def recording(tree, batch_objects):
            side, lid = bpy.data.collections["Side"], bpy.data.objects["Lid"]
            states.append(({ob.name for ob in batch_objects}, side.hide_viewport, lid.hide_viewport))
            return isolate(tree, batch_objects)
        job, root = os.path.join(self.tmp, "job.json"), os.path.join(self.tmp, "out")
        write_job(job, preset, root)
        with mock.patch.object(ce.headless, "isolate_batch", recording):
            code, out = self.run_worker(job)
        self.assertEqual(code, 0, out)
        self.assertEqual(written_files(root), ["Box.stl", "Lid.stl"])
        self.assertEqual(states, [({"Lid"}, True, True), ({"Box"}, True, True)])

    def test_worker_evaluates_what_the_parts_reference_in_excluded_collections(self):
        """Collections a node reads (here a cutter-like object through Object Info, and a collection through Collection
        Info) stay excluded in the worker: Blender evaluates what is referenced all the same, modifiers included."""
        reader = bpy.data.node_groups.new("Reader", "GeometryNodeTree")
        reader.is_modifier = True
        reader.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        reader.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        info, coll_info = reader.nodes.new("GeometryNodeObjectInfo"), reader.nodes.new("GeometryNodeCollectionInfo")
        join, realize, out = reader.nodes.new("GeometryNodeJoinGeometry"), reader.nodes.new("GeometryNodeRealizeInstances"), reader.nodes.new("NodeGroupOutput")
        size = reader.interface.new_socket("Size", in_out="INPUT", socket_type="NodeSocketFloat")
        size.default_value = 1.0
        gi, cube = reader.nodes.new("NodeGroupInput"), reader.nodes.new("GeometryNodeMeshCube")
        reader.links.new(gi.outputs["Size"], cube.inputs["Size"])
        for socket in (info.outputs["Geometry"], coll_info.outputs["Instances"], cube.outputs["Mesh"]): reader.links.new(socket, join.inputs[0])
        reader.links.new(join.outputs[0], realize.inputs[0])
        reader.links.new(realize.outputs[0], out.inputs[0])
        refs = get_collection("Refs")
        info.inputs["Object"].default_value = make_object("Cutter", collection="Refs", group=make_gn_group("CutterGN"))
        coll_info.inputs["Collection"].default_value = refs
        bpy.context.view_layer.layer_collection.children["Refs"].exclude = True
        make_object("Part", group=reader)
        preset = make_preset()
        override(preset.nodegroups, "Size", "FLOAT", "\\1", "\\2", group="Reader")
        job, root = os.path.join(self.tmp, "job.json"), os.path.join(self.tmp, "out")
        write_job(job, preset, root)
        code, out = self.run_worker(job)
        self.assertEqual(code, 0, out)
        # The part's own cube (12) plus the cutter twice (Object Info and Collection Info), each a GN cube of 2 x 2 x 2
        # vertices (12 triangles): without its modifier the cutter's empty mesh would add nothing
        for size in (1, 2):
            self.assertEqual(len(read_stl(os.path.join(root, f"{size}/Part.stl"))[1]), 36, size)

    def test_worker_skips_only_what_was_written_directly(self):
        """With skip_direct the worker leaves out the objects without overrides that the job's view layer evaluates
        (the main process wrote them) and writes the ones it does not."""
        make_object("Plain", mesh=cube_mesh())
        get_collection("Hidden", parent=get_collection("Parts")).objects.link(bpy.data.objects.new("PlainHidden", cube_mesh("h")))
        bpy.context.view_layer.layer_collection.children["Parts"].children["Hidden"].exclude = True
        make_object("Box", group=make_gn_group())
        preset = make_preset()
        override(preset.nodegroups, "Size", "FLOAT", "1")
        job, root = os.path.join(self.tmp, "job.json"), os.path.join(self.tmp, "out")
        scene = bpy.context.scene
        ce.headless.write_job_file(job, scene, scene.view_layers[0], preset, root, skip_direct=True)
        code, out = self.run_worker(job)
        self.assertEqual(code, 0, out)
        self.assertEqual(written_files(root), ["Box.stl", "PlainHidden.stl"])

    def test_worker_links_a_collection_that_is_not_in_the_scene(self):
        """A mapped collection that only another scene holds is linked into the job's scene in the worker's copy. A
        mapped collection inside it comes in with it, and is not linked a second time."""
        group = make_gn_group()
        elsewhere = bpy.data.scenes.new("Elsewhere")
        col, inner = bpy.data.collections.new("Only There"), bpy.data.collections.new("Inner There")
        elsewhere.collection.children.link(col)
        col.children.link(inner)
        for name, owner in (("Box", col), ("Pin", inner)):
            ob = bpy.data.objects.new(name, bpy.data.meshes.new(name))
            ob.modifiers.new("GN", "NODES").node_group = group
            owner.objects.link(ob)
        preset = make_preset(collections=("Inner There", "Only There"))
        preset.collections[0].sub_path = "inner"
        override(preset.nodegroups, "Size", "FLOAT", "2")
        self.assertEqual(sorted(planned_paths()), ["Box.stl", "Pin.stl", "inner/Pin.stl"])
        job, root = os.path.join(self.tmp, "job.json"), os.path.join(self.tmp, "out")
        write_job(job, preset, root)
        code, out = self.run_worker(job)
        self.assertEqual(code, 0, out)
        self.assertIn("Linked 'Only There' into scene 'Scene'", out)
        self.assertNotIn("Linked 'Inner There'", out)
        self.assertEqual(list(bpy.context.scene.collection.children_recursive).count(inner), 1)
        self.assertEqual(written_files(root), ["Box.stl", "Pin.stl", "inner/Pin.stl"])
        for name in ("Box.stl", "inner/Pin.stl"):
            lo, hi = stl_bounds(os.path.join(root, name))
            self.assertAlmostEqual(hi[0] - lo[0], 2.0, places=4)


class FakeProcess:
    def __init__(self, returncode=None):
        self.returncode, self.killed = returncode, False
    def poll(self): return self.returncode
    def kill(self): self.killed, self.returncode = True, -9
    def wait(self, timeout=None): return self.returncode


class WorkerProtocolTests(BlenderTestCase):
    """modal() reading the worker's output: progress, completion, crashes and cancelling."""

    def setUp(self):
        super().setUp()
        make_object("Box", mesh=cube_mesh())
        self.preset = make_preset()
        self.job = ce.data.get_job(self.preset, create=True)
        self.job.is_exporting = True
        run = self.run = ExportRun()
        run.scene_uid, run.preset_uid, run.preset_name = bpy.context.scene.session_uid, self.preset.uid, self.preset.name
        run.export_start_time, run.total_operations, run.current_op, run._timer = time.perf_counter(), 1, 0, None
        run.q, run.t = queue.Queue(), threading.Thread(target=lambda: None)
        run.t.start()

    def feed(self, *lines):
        for line in lines: self.run.q.put(line + "\n")

    def test_progress_and_completion(self):
        self.run.process = FakeProcess()
        self.feed("BATCH_STL_TOTAL:4", "P | Parts | Box | Permutation 1/4", "BATCH_STL_PROGRESS:2")
        self.assertEqual(self.run.modal(ContextProxy(), TIMER), {'PASS_THROUGH'})
        self.assertEqual(self.job.export_progress, 0.5)
        self.assertEqual(self.job.export_status.split(" | ")[0], "Obj 2/4")
        self.feed("BATCH_STL_DONE")
        self.run.process.returncode = 0
        self.assertEqual(self.run.modal(ContextProxy(), TIMER), {'FINISHED'})
        log = self.run.log()
        self.assertIn("P | Parts | Box | Permutation 1/4", log)
        self.assertIn("BATCH EXPORT COMPLETE", log[-1])
        self.assertFalse(self.job.is_exporting)

    def test_worker_finishing_before_the_direct_queue_is_not_a_crash(self):
        """The worker's DONE line is read once; the polls that still write queued objects must not mistake the exited
        worker for a crash."""
        make_object("Box2", mesh=cube_mesh())
        run = self.run
        run.process = FakeProcess(returncode=0)
        run.direct_queue = collections.deque(("Parts", name, os.path.join(self.tmp, f"{name}.stl"), name) for name in ("Box", "Box2"))
        run.direct_total, run.direct_budget = 2, 0
        self.feed("BATCH_STL_DONE")
        self.assertEqual(run.modal(ContextProxy(), TIMER), {'PASS_THROUGH'})
        self.assertEqual(written_files(self.tmp), ["Box.stl"])
        self.assertTrue(self.job.is_exporting)
        self.assertEqual(run.modal(ContextProxy(), TIMER), {'FINISHED'})
        self.assertEqual(written_files(self.tmp), ["Box.stl", "Box2.stl"])
        self.assertNotIn("CRASH", "\n".join(run.log()))

    def test_worker_that_exits_early_is_a_crash(self):
        self.run.process = FakeProcess(returncode=1)
        self.feed("Traceback (most recent call last):")
        self.assertEqual(self.run.modal(ContextProxy(), TIMER), {'CANCELLED'})
        self.assertIn("CRASH DETECTED: Worker exited with code 1", self.run.log()[-1])
        self.assertFalse(self.job.is_exporting)

    def test_cancel_kills_the_worker(self):
        self.run.process = FakeProcess()
        bpy.ops.batch_stl.cancel_export(preset_uid=self.preset.uid)
        self.assertEqual(self.run.modal(ContextProxy(), TIMER), {'CANCELLED'})
        self.assertTrue(self.run.process.killed)
        self.assertFalse(self.job.is_exporting)
