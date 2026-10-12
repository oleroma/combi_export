"""End to end: the export operator (invoke / modal, and execute for scripts) on the direct path and with the background
worker, and the worker's output protocol."""
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

    def test_only_evaluated_objects_are_exported(self):
        parts = get_collection("Parts")
        make_object("Visible", mesh=cube_mesh())
        eye_hidden = make_object("EyeHidden", mesh=cube_mesh())
        make_object("Disabled", mesh=cube_mesh()).hide_viewport = True
        make_object("Unticked", mesh=cube_mesh())
        get_collection("Excluded", parent=parts).objects.link(bpy.data.objects.new("InExcluded", cube_mesh("x")))
        get_collection("Off", parent=parts).objects.link(bpy.data.objects.new("InOff", cube_mesh("o")))
        bpy.data.collections["Off"].hide_viewport = True
        bpy.context.view_layer.layer_collection.children["Parts"].children["Excluded"].exclude = True
        eye_hidden.hide_set(True)
        preset = make_preset()
        next(o for o in preset.collections[0].objects if o.name == "Unticked").export = False

        self.assertEqual(sorted(planned_paths()), ["EyeHidden.stl", "Visible.stl"])
        result, _run = run_export()
        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(written_files(self.tmp), ["EyeHidden.stl", "Visible.stl"])

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
        """The job's view layer decides which objects are exported and evaluates them, not the context's: here the
        mapped collection is excluded in the context's view layer only."""
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
