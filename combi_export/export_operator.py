"""The export operator: objects without overrides are written directly, the others by a headless worker process
(headless_main.py) that the operator follows through a modal timer (ARCHITECTURE.md, Execution Pipelines)."""
import os
import time
import threading
import queue
import subprocess
import tempfile
import sys
import shutil
import traceback

import bpy

from .common import redraw_sidebars
from .nodes import cached_menu_items
from .targets import (export_file_parts, find_export_clashes, iter_export_objects, live_collection_names,
    sync_collection_objects)
from .stl import collect_instance_arrays, export_object_stl
from .data import ensure_preset_uids, find_job, find_preset, get_job, log_to_console
from .validation import is_preset_setup_valid
from .headless import WORKER_SCRIPT, write_job_file

# Temp folders of a crashed export are removed at startup once they are this old; a younger one may belong to an
# export running in another Blender instance.
STALE_TEMP_AGE = 24 * 3600

class BATCH_STL_OT_cancel_export(bpy.types.Operator):
    bl_idname = "batch_stl.cancel_export"
    bl_label = "Cancel Export"
    bl_description = "Cancel the active batch export"
    preset_uid: bpy.props.StringProperty(options={'HIDDEN'})
    def execute(self, context):
        job = find_job(context.scene.session_uid, self.preset_uid)
        if job and job.is_exporting:
            job.cancel_export = True
            log_to_console(job, "[!] Export cancelled manually.")
        return {'FINISHED'}

class EXPORT_OT_batch_stl_multi(bpy.types.Operator):
    bl_idname = "export_scene.batch_stl_multi"
    bl_label = "Export"
    bl_description = "Start batch STL export"
    bl_options = {"REGISTER"}
    preset_index: bpy.props.IntProperty(default=-1)

    @classmethod
    def poll(cls, context): return len(context.scene.batch_stl_presets) > 0

    def invoke(self, context, event):
        self._timer = self.process = None
        self.total_operations, self.current_op = 1, 0
        self.export_start_time = time.perf_counter()
        scene = context.scene

        preset_idx = self.preset_index if self.preset_index >= 0 else scene.batch_stl_preset_index
        if preset_idx < 0 or preset_idx >= len(scene.batch_stl_presets): return {"CANCELLED"}
        scene.batch_stl_preset_index = preset_idx
        ensure_preset_uids(scene)
        preset = scene.batch_stl_presets[preset_idx]
        # Undo, reordering and scene switches invalidate pointers and indices: modal() and cleanup() find the preset
        # and its job again by these.
        self.scene_uid, self.preset_uid, self.preset_name = scene.session_uid, preset.uid, preset.name
        job = get_job(preset, create=True)

        if job.is_exporting: return {'CANCELLED'}
        if not scene.batch_stl_root_dir:
            self.report({'ERROR'}, "Missing Root Directory")
            return {"CANCELLED"}
        if scene.batch_stl_root_dir.startswith("//") and not bpy.data.is_saved:
            self.report({'ERROR'}, "Please save the .blend file before exporting to a relative path (//)")
            return {"CANCELLED"}
        if not is_preset_setup_valid(scene, preset):
            self.report({'ERROR'}, "Improper setup: One or more override fields are missing or invalid.")
            return {"CANCELLED"}

        for c in preset.collections: sync_collection_objects(c)
        live = live_collection_names(context.view_layer)
        preset_root = os.path.normpath(bpy.path.abspath(scene.batch_stl_root_dir))
        with cached_menu_items():
            clashes = find_export_clashes(scene, preset, live, preset_root)
            targets = list(iter_export_objects(scene, preset, live))
        if clashes:
            self.report({'ERROR'}, f"Export aborted: {len(clashes)} files would be written more than once (e.g. {next(iter(clashes.values()))}). Check the tree view.")
            return {"CANCELLED"}

        context.window_manager.batch_stl_info_tab = 'LOG'
        job.console_logs.clear()

        for c in preset.collections:
            # e.g. the collection was renamed or deleted: say so instead of reporting a complete export
            if c.collection_name and not bpy.data.collections.get(c.collection_name):
                log_to_console(job, f"SKIPPED (collection not found): {c.collection_name}")

        objects_to_export_directly = [(c, obj_prop, bl_obj) for _c_idx, c, obj_prop, bl_obj, ovrs in targets if not ovrs]
        needs_headless = any(t[4] for t in targets)

        if objects_to_export_directly:
            depsgraph = context.evaluated_depsgraph_get()
            instance_arrays = collect_instance_arrays(depsgraph, [item[2] for item in objects_to_export_directly])
            log_to_console(job, f"=== STARTING NATIVE DIRECT EXPORT ({len(objects_to_export_directly)} Objects) ===")
            for c, obj_prop, bl_obj in objects_to_export_directly:
                t_dir_start = time.perf_counter()
                full_dir_parts, filename = export_file_parts(preset, c, obj_prop, bl_obj)
                out_dir = os.path.normpath(os.path.join(preset_root, *full_dir_parts)) if full_dir_parts else preset_root
                filepath = os.path.join(out_dir, filename)

                result_text = export_object_stl(filepath, bl_obj, depsgraph, instance_arrays.get(bl_obj.name_full, ()))

                log_to_console(job, f"{preset.name} | {c.collection_name} | {bl_obj.name} | Permutation 1/1 | Batch 1/1\n  └─ {result_text} | {time.perf_counter() - t_dir_start:.2f} s")

        if not needs_headless:
            preset.last_export_time = time.perf_counter() - self.export_start_time
            log_to_console(job, f"=== BATCH EXPORT COMPLETE ({preset.last_export_time:.4f}s) ===")
            self.report({'INFO'}, f"Batch Export {preset.name} Complete in {preset.last_export_time:.2f}s.")
            redraw_sidebars(context)
            self.cleanup(context)
            return {'FINISHED'}

        job.export_status = "Spawning Worker... (0.0s)"
        t_spawn_start = time.perf_counter()
        self.temp_dir = tempfile.mkdtemp(prefix="fast_batch_stl_")
        self.temp_blend = os.path.join(self.temp_dir, "batch_stl_export_temp.blend")
        self.job_json = os.path.join(self.temp_dir, "job.json")

        bpy.ops.wm.save_as_mainfile(filepath=self.temp_blend, copy=True, compress=False)
        # context.view_layer, as for `live` above: the worker must export what was checked for clashes here
        write_job_file(self.job_json, scene, context.view_layer, preset, bpy.path.abspath(scene.batch_stl_root_dir), skip_direct=True)

        # The worker starts with WORKER_SCRIPT, which loads this package from its folder and runs the job.
        # --factory-startup disables Python auto-run, so mirror the user's setting to keep scripted drivers working.
        # --python-exit-code: a Python error in the worker exits with code 1 instead of 0.
        worker_args = [bpy.app.binary_path, "--factory-startup", "--python-exit-code", "1"]
        if context.preferences.filepaths.use_scripts_auto_execute: worker_args.append("--enable-autoexec")
        worker_args += ["-b", self.temp_blend, "-P", WORKER_SCRIPT, "--", "--batch-stl-headless", self.job_json]

        try:
            sub_env = dict(os.environ, PYTHONUNBUFFERED="1")
            # blender.exe is a console program: without this flag Windows opens a console window for the worker.
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self.process = subprocess.Popen(worker_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=sub_env, creationflags=flags)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to spawn headless Blender: {e}")
            self.cleanup(context)
            return {'CANCELLED'}

        log_to_console(job, f"=== INITIATING HEADLESS EXPORT '{preset.name}' [{time.perf_counter() - t_spawn_start:.4f}s Boot] ===")

        self.q = queue.Queue()
        def enqueue_output(out, q):
            for line in iter(out.readline, ''): q.put(line)
            out.close()
        self.t = threading.Thread(target=enqueue_output, args=(self.process.stdout, self.q)); self.t.daemon = True; self.t.start()

        job.is_exporting, job.cancel_export, job.export_progress = True, False, 0.0
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _preset(self):
        """The preset being exported, or None when an undo removed it (the export carries on regardless)."""
        return find_preset(self.scene_uid, self.preset_uid)

    def _job(self):
        return find_job(getattr(self, "scene_uid", 0), getattr(self, "preset_uid", ""))

    def _drain_output(self, job):
        """Process queued worker output. Returns True once the worker reported BATCH_STL_DONE."""
        while True:
            try: line = self.q.get_nowait().rstrip('\r\n')
            except queue.Empty: return False
            if line.startswith("BATCH_STL_TOTAL:"):
                try: self.total_operations = int(line.split(":")[1])
                except Exception: pass
            elif line.startswith("BATCH_STL_PROGRESS:"):
                try:
                    self.current_op = int(line.split(":")[1])
                    job.export_progress = self.current_op / max(1, self.total_operations)
                except Exception: pass
            elif line.startswith("BATCH_STL_DONE"):
                return True
            elif line:
                log_to_console(job, line)

    def modal(self, context, event):
        try:
            job = self._job()
            if job is None:  # a file load cleared the jobs: this export belonged to a file that is gone
                self.cleanup(context)
                return {'CANCELLED'}

            if job.cancel_export:
                self.cleanup(context)
                self.report({'WARNING'}, f"Export cancelled for {self.preset_name}.")
                return {'CANCELLED'}

            if event.type == 'TIMER':
                elapsed = time.perf_counter() - self.export_start_time
                finished = self._drain_output(job)
                worker_exited = self.process.poll() is not None
                if worker_exited and not finished:
                    # The worker prints BATCH_STL_DONE right before exiting; let the reader thread flush before judging.
                    self.t.join(timeout=2.0)
                    finished = self._drain_output(job)

                if finished:
                    self.cleanup(context)
                    preset = self._preset()
                    if preset: preset.last_export_time = elapsed
                    log_to_console(job, f"=== BATCH EXPORT COMPLETE ({elapsed:.4f}s) ===")
                    self.report({'INFO'}, f"Batch Export {self.preset_name} Complete in {elapsed:.2f}s.")
                    redraw_sidebars(context)
                    return {'FINISHED'}

                if worker_exited:
                    exit_code = self.process.returncode
                    self.cleanup(context)
                    log_to_console(job, f"[!] CRASH DETECTED: Worker exited with code {exit_code} before finishing.")
                    self.report({'ERROR'}, f"Background worker crashed for preset {self.preset_name}.")
                    redraw_sidebars(context)
                    return {'CANCELLED'}

                job.export_status = f"Obj {self.current_op}/{self.total_operations} | {elapsed:.1f}s" if (self.total_operations > 1 or self.current_op > 0) else f"Spawning Worker... ({elapsed:.1f}s)"
                redraw_sidebars(context)
        except Exception:
            traceback.print_exc()
            self.cleanup(context)
            self.report({'ERROR'}, "Unexpected error during batch export.")
            return {'CANCELLED'}
        return {'PASS_THROUGH'}

    def cancel(self, context):
        # Called by Blender when the modal is torn down from outside (e.g. loading another file).
        self.cleanup(context)

    def cleanup(self, context=None):
        if context and getattr(self, '_timer', None):
            try: context.window_manager.event_timer_remove(self._timer)
            except Exception: pass
            self._timer = None
        try:
            job = self._job()
            if job: job.is_exporting, job.cancel_export, job.export_progress, job.export_status = False, False, 0.0, ""
        except Exception: pass
        if getattr(self, 'process', None):
            try:
                if self.process.poll() is None:
                    self.process.kill()
                    self.process.wait(timeout=1.0)
            except Exception: pass
        if getattr(self, 't', None) and self.t.is_alive():
            self.t.join(timeout=0.5)
        if hasattr(self, 'temp_dir') and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

def remove_stale_temp_dirs():
    """Remove the temp folders of exports that crashed. A recent one may belong to an export running in another Blender
    instance (its worker reads the folder after loading the file), so only old ones go."""
    try:
        tmp = tempfile.gettempdir()
        for f in os.listdir(tmp):
            path = os.path.join(tmp, f)
            if f.startswith("fast_batch_stl_") and time.time() - os.path.getmtime(path) > STALE_TEMP_AGE:
                shutil.rmtree(path, ignore_errors=True)
    except Exception: pass

classes = (BATCH_STL_OT_cancel_export, EXPORT_OT_batch_stl_multi)
