"""The standby worker (standby.py): a background Blender started ahead of time that an export hands its job to."""
import time
import types
from unittest import mock

import bpy
import combi_export as ce
from helpers import BlenderTestCase, make_gn_group, make_object, make_preset, override, planned_paths, run_export, written_files

READY_TIMEOUT = 60.0


def wait_ready(worker):
    if not worker.ready.wait(READY_TIMEOUT): raise AssertionError("standby worker never got ready")


class StandbyTests(BlenderTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(ce.standby.stop_standby)

    def enabled(self, on=True):
        return mock.patch.object(ce.standby, "standby_enabled", return_value=on)

    def test_off_by_default(self):
        """Off without the preference, and always in background Blender (scripts, the tests, the workers)."""
        self.assertFalse(ce.standby.standby_enabled())
        ce.standby.ensure_standby()
        self.assertIsNone(ce.standby._standby)
        self.assertEqual(ce.standby.BatchSTLPreferences.bl_rna.properties["keep_worker_ready"].default, False)

    def test_export_through_the_standby_worker(self):
        """With a standby worker ready, the export hands it the job instead of starting a worker: the same files, and a
        new standby is started once the export is done."""
        make_object("Box", group=make_gn_group())
        override(make_preset().nodegroups, "Size", "FLOAT", "\\1", "\\2")
        expected = sorted(planned_paths())
        with self.enabled():
            ce.standby.ensure_standby()
            first = ce.standby._standby
            wait_ready(first)
            with mock.patch.object(ce.standby.subprocess, "Popen", wraps=ce.standby.subprocess.Popen) as popen:
                result, run = run_export()
            self.assertEqual(result, {'FINISHED'})
            self.assertEqual(written_files(self.tmp), expected)
            self.assertTrue(any("standby worker" in line for line in run.log()))
            # Only the replacement was started, after the export
            self.assertEqual(popen.call_count, 1)
            self.assertIn("--standby", popen.call_args.args[0])
            self.assertIsNot(ce.standby._standby, first)
            self.assertTrue(ce.standby._standby.alive())

    def test_worker_that_is_not_ready_or_does_not_fit_is_not_used(self):
        """A booting standby is kept for the next export; one with another auto-run setting, or a dead one, is stopped.
        In both cases the export starts its own worker."""
        def fake(alive=True, ready=False, autoexec=False):
            worker = types.SimpleNamespace(autoexec=autoexec, ready=types.SimpleNamespace(is_set=lambda: ready),
                                           alive=lambda: alive, stopped=False)
            worker.stop = lambda: setattr(worker, "stopped", True)
            return worker
        booting = fake()
        ce.standby._standby = booting
        self.assertIsNone(ce.standby.take_standby(False))
        self.assertIs(ce.standby._standby, booting)
        for worker in (fake(ready=True, autoexec=True), fake(alive=False, ready=True)):
            ce.standby._standby = worker
            self.assertIsNone(ce.standby.take_standby(False))
            self.assertTrue(worker.stopped)
            self.assertIsNone(ce.standby._standby)
        ready = fake(ready=True)
        ce.standby._standby = ready
        self.assertIs(ce.standby.take_standby(False), ready)
        self.assertIsNone(ce.standby._standby)

    def test_worker_exits_when_its_input_closes(self):
        """Blender that started it is gone (its end of the pipe closes): the waiting worker ends by itself."""
        worker = ce.standby.StandbyWorker(autoexec=False)
        self.addCleanup(worker.stop)
        wait_ready(worker)
        worker.process.stdin.close()
        self.assertEqual(worker.process.wait(timeout=30), 0)

    def test_stopped_on_unregister(self):
        """Reload Scripts or an update must not hand the next export to a worker running the old code."""
        with mock.patch.object(ce.standby, "stop_standby") as stop:
            ce.unregister()
            ce.register()
        stop.assert_called_once()
