"""Path settings (paths.py, and the folder steps naming.py builds from them) under the path rules of Windows and Linux,
whatever the machine. Runs without Blender:

    python -m unittest tests/test_paths.py

and with the rest of the suite in Blender (run.py)."""
import ast
import importlib
import ntpath
import os
import posixpath
import sys
import types
import unittest

PACKAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "combi_export")


def load_blender_free(name):
    """Module `name` of the add-on, imported without running its __init__ (which needs Blender)."""
    if "combi_export_blender_free" not in sys.modules:
        package = types.ModuleType("combi_export_blender_free")
        package.__path__ = [PACKAGE]
        sys.modules[package.__name__] = package
    return importlib.import_module(f"combi_export_blender_free.{name}")


paths = load_blender_free("paths")
naming = load_blender_free("naming")

WIN_BLEND = "C:\\work\\project"   # folder of the .blend file on Windows
LINUX_BLEND = "/home/me/project"  # and on Linux


class Windows:
    flavor, blend = ntpath, WIN_BLEND

class Linux:
    flavor, blend = posixpath, LINUX_BLEND


class ResolveTests(unittest.TestCase):
    """resolve_path: a top-level setting (the Root folder, a JSON file)."""

    def check(self, os_rules, cases):
        for text, expected in cases:
            with self.subTest(os=os_rules.__name__, text=text):
                self.assertEqual(paths.resolve_path(text, os_rules.blend, os_rules.flavor), expected)

    def test_relative_paths_start_at_the_blend_folder(self):
        cases = ["../dir/other_dir", "..\\dir\\other_dir", "//../dir/other_dir", "//..\\dir\\other_dir", "..\\dir/other_dir\\"]
        self.check(Windows, [(c, "C:\\work\\dir\\other_dir") for c in cases])
        self.check(Linux, [(c, "/home/me/dir/other_dir") for c in cases])

    def test_a_plain_name_is_a_folder_next_to_the_blend_file(self):
        self.check(Windows, [("dir", "C:\\work\\project\\dir"), ("//dir", "C:\\work\\project\\dir")])
        self.check(Linux, [("dir", "/home/me/project/dir"), ("//dir", "/home/me/project/dir"), ("./dir", "/home/me/project/dir")])

    def test_the_blend_folder_itself(self):
        self.check(Windows, [("//", WIN_BLEND), (".", WIN_BLEND), ("//.\\", WIN_BLEND)])
        self.check(Linux, [("//", LINUX_BLEND), (".", LINUX_BLEND), ("//./", LINUX_BLEND)])

    def test_a_file_set_up_on_windows_works_on_linux_and_back(self):
        self.check(Linux, [("//..\\stl\\", "/home/me/stl"), ("//..\\stl\\parts", "/home/me/stl/parts")])
        self.check(Windows, [("//../stl/", "C:\\work\\stl"), ("//../stl/parts", "C:\\work\\stl\\parts")])

    def test_absolute_paths_stay_as_they_are(self):
        self.check(Linux, [("/home/me/stl", "/home/me/stl"), ("/home/me/stl/", "/home/me/stl")])
        self.check(Windows, [("D:\\stl", "D:\\stl"), ("D:/stl", "D:\\stl"), ("D:\\stl\\", "D:\\stl"),
                             ("D:\\stl\\..\\other", "D:\\other")])

    def test_a_network_path_stays_a_network_path(self):
        self.check(Windows, [("\\\\server\\share\\stl", "\\\\server\\share\\stl"), ("\\\\server/share/stl", "\\\\server\\share\\stl")])
        self.check(Linux, [("\\\\server\\share\\stl", "//server/share/stl")])
        self.assertEqual(paths.path_kind("\\\\server\\share"), "network")
        self.assertEqual(paths.path_kind("//server/share"), "blend")  # Blender reads '//' as its own prefix

    def test_a_single_separator_is_the_top_of_the_blend_files_drive(self):
        self.check(Windows, [("\\stl", "C:\\stl"), ("/stl", "C:\\stl")])
        self.assertEqual(paths.resolve_path("\\stl", "E:\\blends", ntpath), "E:\\stl")  # not the drive Blender started from
        self.assertEqual(paths.resolve_path("/stl", "\\\\server\\share\\blends", ntpath), "\\\\server\\share\\stl")
        self.check(Linux, [("\\stl", "/stl"), ("/stl", "/stl")])

    def test_empty_stays_empty(self):
        self.check(Windows, [("", "")])
        self.check(Linux, [("", "")])

    def test_folders_starting_with_dots_are_names(self):
        self.check(Windows, [("..hidden", "C:\\work\\project\\..hidden"), ("//..hidden\\x", "C:\\work\\project\\..hidden\\x")])
        self.check(Linux, [("..hidden", "/home/me/project/..hidden"), (".cache/x", "/home/me/project/.cache/x")])


class ResolveBelowTests(unittest.TestCase):
    """resolve_below: a setting below another folder (a sub-folder below the root)."""

    def test_relative_starts_at_the_folder_above(self):
        for flavor, root, text, expected in (
                (posixpath, "/out/stl", "../dir\\x", "/out/dir/x"),
                (posixpath, "/out/stl", "a/b", "/out/stl/a/b"),
                (ntpath, "D:\\out\\stl", "../dir\\x", "D:\\out\\dir\\x"),
                (ntpath, "D:\\out\\stl", "a/b", "D:\\out\\stl\\a\\b")):
            with self.subTest(flavor=flavor.__name__, text=text):
                self.assertEqual(paths.resolve_below(root, text, "/blend" if flavor is posixpath else "C:\\blend", flavor), expected)

    def test_an_absolute_setting_replaces_the_folder_above(self):
        self.assertEqual(paths.resolve_below("/out", "/abs/x", "/blend", posixpath), "/abs/x")
        self.assertEqual(paths.resolve_below("/out", "//x", "/blend", posixpath), "/blend/x")
        self.assertEqual(paths.resolve_below("D:\\out", "E:/x", "C:\\blend", ntpath), "E:\\x")
        self.assertEqual(paths.resolve_below("D:\\out", "//..\\x", "C:\\blend\\b", ntpath), "C:\\blend\\x")
        self.assertEqual(paths.resolve_below("D:\\out", "\\\\srv\\share", "C:\\blend", ntpath), "\\\\srv\\share")

    def test_a_single_separator_is_the_top_of_the_drive_of_the_folder_above(self):
        self.assertEqual(paths.resolve_below("D:\\out\\stl", "\\x", "C:\\blend", ntpath), "D:\\x")
        self.assertEqual(paths.resolve_below("\\\\srv\\share\\out", "/x", "C:\\blend", ntpath), "\\\\srv\\share\\x")
        self.assertEqual(paths.resolve_below("/out/stl", "\\x", "/blend", posixpath), "/x")

    def test_empty_is_the_folder_above(self):
        self.assertEqual(paths.resolve_below("/out", "", "/blend", posixpath), "/out")
        self.assertEqual(paths.resolve_below("D:\\out", "", "C:\\blend", ntpath), "D:\\out")


class RelativeBelowTests(unittest.TestCase):
    """relative_below: a file stored below a root."""

    def test_a_file_below_the_root_is_stored_with_slashes(self):
        self.assertEqual(paths.relative_below("/out", "/out/a/b.stl", posixpath), "a/b.stl")
        self.assertEqual(paths.relative_below("D:\\out", "D:\\out\\a\\b.stl", ntpath), "a/b.stl")
        self.assertEqual(paths.relative_below("D:/out/", "d:\\OUT\\a\\b.stl", ntpath), "a/b.stl")  # case-insensitive
        self.assertEqual(paths.relative_below("\\\\srv\\share\\out", "\\\\srv\\share\\out\\a.stl", ntpath), "a.stl")

    def test_outside_the_root_stays_absolute(self):
        self.assertEqual(paths.relative_below("/out/stl", "/out/x.stl", posixpath), "/out/x.stl")
        self.assertEqual(paths.relative_below("D:\\out\\stl", "D:\\out\\x.stl", ntpath), "D:\\out\\x.stl")

    def test_another_drive_stays_absolute(self):
        self.assertEqual(paths.relative_below("D:\\out", "E:\\out\\x.stl", ntpath), "E:\\out\\x.stl")
        self.assertEqual(paths.relative_below("D:\\out", "\\\\srv\\share\\x.stl", ntpath), "\\\\srv\\share\\x.stl")

    def test_a_folder_starting_with_two_dots_is_inside(self):
        self.assertEqual(paths.relative_below("/out", "/out/..hidden/x.stl", posixpath), "..hidden/x.stl")
        self.assertEqual(paths.relative_below("D:\\out", "D:\\out\\..hidden\\x.stl", ntpath), "..hidden/x.stl")
        self.assertEqual(paths.parts_below("/out", "/out", posixpath), [])
        self.assertIsNone(paths.parts_below("/out", "/", posixpath))


class PickedFolderTests(unittest.TestCase):
    """picked_setting and nearest_existing_dir: the Root field's folder button."""

    def test_a_picked_folder_is_stored_relative_with_slashes(self):
        self.assertEqual(paths.picked_setting("C:\\work\\stl", WIN_BLEND, True, ntpath), "//../stl/")
        self.assertEqual(paths.picked_setting("C:\\work\\project\\", WIN_BLEND, True, ntpath), "//")
        self.assertEqual(paths.picked_setting("/home/me/stl/", LINUX_BLEND, True, posixpath), "//../stl/")
        self.assertEqual(paths.picked_setting("/home/me/project/..hidden", LINUX_BLEND, True, posixpath), "//..hidden/")

    def test_absolute_when_asked_or_on_another_drive(self):
        self.assertEqual(paths.picked_setting("/home/me/stl/", LINUX_BLEND, False, posixpath), "/home/me/stl")
        self.assertEqual(paths.picked_setting("D:\\stl\\", WIN_BLEND, True, ntpath), "D:\\stl")
        self.assertEqual(paths.picked_setting("\\\\srv\\share\\stl", WIN_BLEND, True, ntpath), "\\\\srv\\share\\stl")

    def test_the_browser_opens_at_the_closest_existing_folder(self):
        existing = {"C:\\work", "C:\\"}
        self.assertEqual(paths.nearest_existing_dir("C:\\work\\stl\\new", existing.__contains__, ntpath), "C:\\work")
        self.assertEqual(paths.nearest_existing_dir("/gone/x", lambda p: False, posixpath), "")
        self.assertEqual(paths.nearest_existing_dir("", lambda p: True, posixpath), "")


class PortableSettingTests(unittest.TestCase):
    def test_relative_settings_are_stored_with_slashes(self):
        self.assertEqual(paths.portable_setting("//..\\stl\\"), "//../stl/")
        self.assertEqual(paths.portable_setting("..\\y\\"), "../y/")
        for text in ("D:\\stl", "\\\\server\\share", "\\top", ""):
            with self.subTest(text=text):
                self.assertEqual(paths.portable_setting(text), text)


class FolderStepTests(unittest.TestCase):
    """naming.folder_setting_parts and export_dir: the preset root directory and collection / object sub-folders."""

    def test_relative_folders_with_either_separator(self):
        steps = naming.folder_setting_parts
        self.assertEqual(steps("a/b\\c"), ["a", "b", "c"])
        self.assertEqual(steps("../x\\..\\y/"), ["..", "x", "..", "y"])
        self.assertEqual(steps("./a//b"), ["a", "b"])
        self.assertEqual(steps("..hidden/a b."), ["..hidden", "a_b"])
        self.assertEqual(steps(""), [])

    def test_an_absolute_setting_is_one_step_as_typed(self):
        for text in ("/home/me/stl", "D:\\My Prints\\stl", "D:/stl", "\\\\server\\share\\x", "//..\\stl", "\\top"):
            with self.subTest(text=text):
                self.assertEqual(naming.folder_setting_parts(text), [text])

    def test_dots_and_absolute_steps_do_not_collapse(self):
        """A plain folder equal to its neighbour collapses into it, '..' twice goes up twice."""
        self.assertEqual(naming.build_export_dir_parts("../..", "", ""), ["..", ".."])
        self.assertEqual(naming.build_export_dir_parts("a", "a", "../b"), ["a", "..", "b"])
        self.assertEqual(naming.build_export_dir_parts("/x", "/x", ""), ["/x", "/x"])

    def test_export_folders_on_both_systems(self):
        parts = naming.build_export_dir_parts("../presets", "parts\\big", "..\\one")
        self.assertEqual(naming.export_dir("/out/stl", parts, LINUX_BLEND, posixpath), "/out/presets/parts/one")
        self.assertEqual(naming.export_dir("D:\\out\\stl", parts, WIN_BLEND, ntpath), "D:\\out\\presets\\parts\\one")

    def test_an_absolute_sub_folder_replaces_the_folders_above(self):
        parts = naming.build_export_dir_parts("preset", "/abs/col", "obj")
        self.assertEqual(naming.export_dir("/out", parts, LINUX_BLEND, posixpath), "/abs/col/obj")
        parts = naming.build_export_dir_parts("preset", "E:\\col", "\\obj")
        self.assertEqual(naming.export_dir("D:\\out", parts, WIN_BLEND, ntpath), "E:\\obj")
        parts = naming.build_export_dir_parts("preset", "//..\\col", "")
        self.assertEqual(naming.export_dir("D:\\out", parts, WIN_BLEND, ntpath), "C:\\work\\col")
        self.assertEqual(naming.export_dir("/out", parts, LINUX_BLEND, posixpath), "/home/me/col")

    def test_override_folders_between_the_settings(self):
        parts = naming.build_export_dir_parts("p", "../c", "", {"PRESET": ["15"], "OBJECT": ["A"]})
        self.assertEqual(parts, ["p", "15", "..", "c", "A"])
        self.assertEqual(naming.export_dir("/out", parts, LINUX_BLEND, posixpath), "/out/p/c/A")


class ReloadTests(unittest.TestCase):
    def test_reload_scripts_reloads_every_module(self):
        """__init__._MODULES is what Reload Scripts reloads: every module of the add-on, paths.py before its users."""
        with open(os.path.join(PACKAGE, "__init__.py"), encoding="utf-8") as f: tree = ast.parse(f.read())
        listed = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "_MODULES")
        modules = {f[:-3] for f in os.listdir(PACKAGE) if f.endswith(".py")} - {"__init__", "headless_main"}
        self.assertEqual(set(listed), modules)
        self.assertLess(listed.index("paths"), min(listed.index(m) for m in ("common", "naming")))


if __name__ == "__main__":
    unittest.main()
