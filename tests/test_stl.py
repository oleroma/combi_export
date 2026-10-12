"""The vectorized binary STL writer: file layout, world transforms, normals and winding, instances, skips."""
import math
import os
import unittest

import numpy as np

import bpy
import combi_export as ce
from helpers import BlenderTestCase, cube_mesh, make_instancing_group, make_object, read_stl


class STLWriterTests(BlenderTestCase):
    def write(self, ob, name="out.stl"):
        path = os.path.join(self.tmp, name)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        arrays = ce.collect_instance_arrays(depsgraph, [ob])
        return path, ce.export_object_stl(path, ob, depsgraph, arrays.get(ob.name_full, ()))

    def assertNormalsMatchGeometry(self, recs, center):
        """Each stored normal is the unit normal of its triangle as wound, and points away from `center`."""
        cross = np.cross(recs["v1"] - recs["v0"], recs["v2"] - recs["v0"])
        cross /= np.linalg.norm(cross, axis=1, keepdims=True)
        np.testing.assert_allclose(np.linalg.norm(recs["normals"], axis=1), 1.0, atol=1e-5)
        np.testing.assert_allclose(np.einsum("ij,ij->i", cross, recs["normals"]), 1.0, atol=1e-4)
        centroids = (recs["v0"] + recs["v1"] + recs["v2"]) / 3
        self.assertTrue(np.all(np.einsum("ij,ij->i", centroids - center, recs["normals"]) > 0), "a normal points inward")

    def test_file_layout(self):
        path, result = self.write(make_object("Cube", mesh=cube_mesh()))
        self.assertEqual(result, path)
        header, recs = read_stl(path)
        self.assertEqual(header, ce.STL_HEADER)
        self.assertEqual(len(header), 80)
        self.assertFalse(header.startswith(b"solid"), "readers would take the file for ASCII STL")
        self.assertEqual(len(recs), 12)
        self.assertTrue(np.all(recs["attr"] == 0))

    def test_world_transforms(self):
        cases = {
            "moved": dict(location=(1, 2, 3)),
            "rotated, non-uniform scale": dict(location=(0, 0, 1), rotation_euler=(0.3, 0.7, 1.1), scale=(1, 2, 5)),
            "mirrored": dict(scale=(-1, 1, 1)),
            "mirrored twice": dict(scale=(-1, -2, 1), rotation_euler=(0, 0, math.pi / 3)),
        }
        for label, transform in cases.items():
            with self.subTest(label):
                ob = make_object(label, mesh=cube_mesh(label))
                for attr, value in transform.items(): setattr(ob, attr, value)
                bpy.context.view_layer.update()
                path, _ = self.write(ob, f"{label}.stl")
                _header, recs = read_stl(path)
                world = np.array([tuple(ob.matrix_world @ v.co) for v in ob.data.vertices], dtype=np.float32)
                written = np.concatenate([recs["v0"], recs["v1"], recs["v2"]])
                np.testing.assert_allclose(np.sort(np.unique(written.round(4), axis=0), axis=0), np.sort(world.round(4), axis=0), atol=1e-4)
                self.assertNormalsMatchGeometry(recs, np.array(ob.matrix_world.translation, dtype=np.float32))

    def test_empty_mesh_writes_nothing(self):
        ob = make_object("Empty", mesh=bpy.data.meshes.new("empty"))
        path = os.path.join(self.tmp, "sub", "empty.stl")
        depsgraph = bpy.context.evaluated_depsgraph_get()
        self.assertTrue(ce.export_object_stl(path, ob, depsgraph).startswith("SKIPPED (no geometry)"))
        self.assertFalse(os.path.exists(os.path.dirname(path)), "the folder is created without a file")

    def test_unwritable_path_is_reported(self):
        ob = make_object("Cube", mesh=cube_mesh())
        target = os.path.join(self.tmp, "taken.stl")
        os.makedirs(target)  # a folder where the file should go
        depsgraph = bpy.context.evaluated_depsgraph_get()
        self.assertTrue(ce.export_object_stl(target, ob, depsgraph).startswith("FAILED"))

    def test_object_disabled_in_viewports_is_not_written(self):
        ob = make_object("Cube", mesh=cube_mesh())
        ob.hide_viewport = True
        bpy.context.view_layer.update()
        path = os.path.join(self.tmp, "cube.stl")
        result = ce.export_object_stl(path, ob, bpy.context.evaluated_depsgraph_get())
        self.assertTrue(result.startswith("SKIPPED (not evaluated"), result)
        self.assertFalse(os.path.exists(path))

    def test_unrealized_instances_are_included(self):
        ob = make_object("Inst", group=make_instancing_group(count=10))
        path, _ = self.write(ob)
        self.assertEqual(len(read_stl(path)[1]), 10 * 12)

    # BUG: depsgraph.object_instances skips objects hidden with the eye icon, so an eye-hidden object loses its
    # unrealized instances (README: hiding with the eye icon does not skip an object). An object whose output is
    # only instances is then not written at all ("SKIPPED (no geometry)").
    @unittest.expectedFailure
    def test_instances_of_an_eye_hidden_object_are_included(self):
        ob = make_object("Inst", group=make_instancing_group(count=10))
        ob.hide_set(True)
        path, result = self.write(ob)
        self.assertEqual(result, path)
        self.assertEqual(len(read_stl(path)[1]), 10 * 12)

    def test_text_object(self):
        txt = bpy.data.objects.new("Text", bpy.data.curves.new("Text", 'FONT'))
        bpy.context.scene.collection.objects.link(txt)
        path, _ = self.write(txt)
        self.assertGreater(len(read_stl(path)[1]), 0)
