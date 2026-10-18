"""The standby worker (standby.py): a background Blender started ahead of time that an export hands its job to."""
import os
import time
import types
from unittest import mock

import bpy
import combi_export as ce
from helpers import BlenderTestCase, make_gn_group, make_object, make_preset, override, planned_paths, run_export, written_files

READY_TIMEOUT = 60.0


def wait_ready(worker):
    deadline = time.monotonic() + READY_TIMEOUT
    while not worker.is_ready():
        if time.monotonic() > deadline: raise AssertionError("standby worker never got ready")
        time.sleep(0.05)


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
        output = os.path.join(self.tmp, "worker.log")
        open(output, "wb").close()
        def fake(alive=True, ready=False, autoexec=False, path=output):
            worker = types.SimpleNamespace(autoexec=autoexec, is_ready=lambda: ready, alive=lambda: alive, stopped=False,
                                           output=types.SimpleNamespace(path=path))
            worker.stop = lambda: setattr(worker, "stopped", True)
            return worker
        booting = fake()
        ce.standby._standby = booting
        self.assertIsNone(ce.standby.take_standby(False))
        self.assertIs(ce.standby._standby, booting)
        gone = os.path.join(self.tmp, "removed", "worker.log")  # another Blender removed its folder as stale
        for worker in (fake(ready=True, autoexec=True), fake(alive=False, ready=True), fake(ready=True, path=gone)):
            ce.standby._standby = worker
            self.assertIsNone(ce.standby.take_standby(False))
            self.assertTrue(worker.stopped)
            self.assertIsNone(ce.standby._standby)
        ready = fake(ready=True)
        ce.standby._standby = ready
        self.assertIs(ce.standby.take_standby(False), ready)
        self.assertIsNone(ce.standby._standby)

    def test_output_is_read_by_complete_lines(self):
        """The worker's output file is read a line at a time: a line still being written waits for the next read,
        Windows line ends are dropped, and a read can stop after a given line."""
        output = ce.standby.WorkerOutput(os.path.join(self.tmp, "worker.log"))
        self.assertEqual(output.read_lines(), [], "no file yet: the worker has not started writing")
        with open(output.path, "wb") as f: f.write(b"Blender 5.2\r\nBATCH_STL_READY\r\nBATCH_STL_TOTAL:2\r\nBATCH_STL_PROG")
        self.assertEqual(output.read_lines(stop="BATCH_STL_READY"), ["Blender 5.2", "BATCH_STL_READY"])
        self.assertEqual(output.read_lines(), ["BATCH_STL_TOTAL:2"])
        with open(output.path, "ab") as f: f.write("RESS:1\nP | Bö\r\n".encode("utf-8"))
        self.assertEqual(output.read_lines(), ["BATCH_STL_PROGRESS:1", "P | Bö"])
        self.assertEqual(output.read_lines(), [])

    def test_worker_exits_when_its_input_closes(self):
        """Blender that started it is gone (its end of the pipe closes): the waiting worker ends by itself."""
        worker = ce.standby.StandbyWorker(autoexec=False)
        self.addCleanup(worker.stop)
        wait_ready(worker)
        worker.process.stdin.close()
        self.assertEqual(worker.process.wait(timeout=30), 0)
        folder = worker.folder
        worker.stop()
        self.assertFalse(os.path.exists(folder), "a stopped worker leaves no temp folder")

    def test_stopped_on_unregister(self):
        """Reload Scripts or an update must not hand the next export to a worker running the old code."""
        with mock.patch.object(ce.standby, "stop_standby") as stop:
            ce.unregister()
            ce.register()
        stop.assert_called_once()
