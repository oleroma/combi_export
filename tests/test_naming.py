"""File / folder name building: sanitizing, splitting, joining, value templates and legacy templates."""
import sys
import unittest

import combi_export as ce


class SanitizeTests(unittest.TestCase):
    def test_invalid_characters_become_underscores(self):
        self.assertEqual(ce.naming.sanitize_name('a<b>c:d"e|f?g*h'), "a_b_c_d_e_f_g_h")

    def test_windows_reserved_names_are_prefixed(self):
        for name in ("con", "CON.txt", "com1", "Lpt9", "nul.stl", "COM0", "lpt0", "COM¹", "lpt³"):
            with self.subTest(name=name):
                self.assertTrue(ce.naming.sanitize_name(name).startswith("_"))
        for name in ("console", "CON_", "a.con", "COM10", "com_1", "nul_.txt"):
            with self.subTest(name=name):
                self.assertEqual(ce.naming.sanitize_name(name), name)

    def test_spaces_become_underscores(self):
        self.assertEqual(ce.naming.sanitize_name("my tag  x"), "my_tag__x")
        self.assertEqual(ce.naming.sanitize_whole_name(" My Box. "), "My_Box")
        self.assertEqual(ce.naming.split_path_parts("my dir/ sub dir /x"), ["my_dir", "sub_dir", "x"])
        self.assertEqual(ce.naming.split_tag_parts("my tag/ b "), ["my_tag", "b"])
        self.assertEqual(ce.naming.split_value_tag_parts(" my tag /x_"), ["my_tag", "x_"])
        self.assertEqual(ce.naming.format_export_filename("My Box", "a b", True, "v 2", {"GLOBAL": ["g"]}), "My_Box_g_v_2_a_b.stl")

    def test_underscore_spaces(self):
        under = ce.naming.underscore_spaces
        self.assertEqual(under("my tag"), "my_tag")
        self.assertEqual(under("a  b"), "a__b")
        self.assertEqual(under("  a b  "), "a_b")
        self.assertEqual(under("a / b\\ c d"), "a/b\\c_d")
        self.assertEqual(under(""), "")
        self.assertEqual(under("already_fine"), "already_fine")

    def test_reserved_name_is_caught_after_stripping(self):
        self.assertNotEqual(ce.naming.split_path_parts("CON "), ["CON"])
        self.assertNotEqual(ce.naming.format_export_filename("CON ", "", False, ""), "CON.stl")
        self.assertEqual(ce.naming.split_path_parts(" nul./x"), ["_nul", "x"])
        self.assertEqual(ce.naming.format_export_filename(".Box. ", "", False, ""), "Box.stl")
        # A value's own spaces become '_', but the text a folder template puts around the value is stripped
        self.assertEqual(ce.naming.value_name("CON ", "dir"), "CON_")
        self.assertEqual(ce.naming.value_name_parts("nul", "* ", "dir"), ([], ["_nul"]))

    def test_split_path_parts(self):
        self.assertEqual(ce.naming.split_path_parts("a\\b/c"), ["a", "b", "c"])
        self.assertEqual(ce.naming.split_path_parts("./../x"), ["x"])
        self.assertEqual(ce.naming.split_path_parts(" a. "), ["a"])
        self.assertEqual(ce.naming.split_path_parts("a:b"), ["a_b"])
        self.assertEqual(ce.naming.split_path_parts(""), [])

    def test_split_tag_parts(self):
        self.assertEqual(ce.naming.split_tag_parts("_lid"), ["lid"])
        self.assertEqual(ce.naming.split_tag_parts("a/b"), ["a", "b"])
        self.assertEqual(ce.naming.split_tag_parts("a_b"), ["a_b"])
        self.assertEqual(ce.naming.split_value_tag_parts("x_"), ["x_"])

    def test_segment_tag_prefers_known_tokens(self):
        unknown = lambda name: False
        self.assertEqual(ce.naming.segment_tag("B_2_X", unknown), ["B", "2", "X"])
        self.assertEqual(ce.naming.segment_tag("a_b_X", lambda name: name == "a_b"), ["a_b", "X"])

    @unittest.skipUnless(sys.platform in ("win32", "darwin"), "case-insensitive file systems only")
    def test_clash_key_ignores_case(self):
        self.assertEqual(ce.naming.clash_key("Out/A/box.stl"), ce.naming.clash_key("out/a/BOX.STL"))


class AssemblyTests(unittest.TestCase):
    def test_readme_filename_example(self):
        # Object Box, Global letter A, collection tag v2, Collection number 1, object tag lid
        tags = {"GLOBAL": ["A"], "COLLECTION": ["1"]}
        self.assertEqual(ce.naming.format_export_filename("Box", "lid", True, "v2", tags), "Box_A_v2_1_lid.stl")
        self.assertEqual(ce.naming.format_export_filename("Box", "lid", False, "v2", tags), "Box_A_1_lid.stl")

    def test_fixed_name_collapses_into_equal_neighbour(self):
        self.assertEqual(ce.naming.join_name_segments([(["A"], False), (["A"], True)]), ["A"])
        self.assertEqual(ce.naming.join_name_segments([(["v2"], True), (["v2"], False)]), ["v2"])
        # Two override names stay: A=1, B=1 gives 1/1
        self.assertEqual(ce.naming.join_name_segments([(["1"], False), (["1"], False)]), ["1", "1"])

    def test_dir_parts_order_and_prefix_collapse(self):
        paths = {"GLOBAL": ["g"], "PRESET": ["out", "1"], "COLLECTION": ["c"], "OBJECT": ["o"]}
        self.assertEqual(ce.naming.build_export_dir_parts("out", "sub", "obj", paths), ["g", "out", "1", "sub", "c", "obj", "o"])


class ValueNameTests(unittest.TestCase):
    def test_value_name_formats(self):
        self.assertEqual(ce.naming.value_name(0.1 + 0.2, "dir"), "0.3")
        self.assertEqual(ce.naming.value_name(2.0, "dir"), "2")
        self.assertEqual(ce.naming.value_name(True, "tag"), "True")
        self.assertEqual(ce.naming.value_name("my text", "tag"), "my_text")
        self.assertEqual(ce.naming.value_name("v1.", "dir"), "v1")

    def test_templates(self):
        cases = [
            ("*", "dir", ([], ["15"])), ("", "dir", ([], ["15"])), ("big", "dir", (["big"], [])),
            ("B/*", "dir", (["B"], ["15"])), ("size_*", "dir", ([], ["size_15"])), ("*_mm/x", "dir", ([], ["15_mm", "x"])),
            ("*", "tag", ([], ["15"])), ("big", "tag", (["big"], [])), ("B_*", "tag", (["B"], ["15"])),
            ("*_mm", "tag", ([], ["15_mm"])), ("B_2_*", "tag", (["B_2"], ["15"])),
        ]
        for template, kind, expected in cases:
            with self.subTest(template=template, kind=kind):
                self.assertEqual(ce.naming.value_name_parts(15, template, kind), expected)

    def test_legacy_templates(self):
        cases = [("", "dir", "*"), ("B\\", "dir", "B/*"), ("B/", "dir", "B/*"), ("size_", "dir", "size_*"), ("x", "dir", "x"),
                 ("A\\B", "dir", "A/B"), ("a*b", "dir", "a_b"), ("", "tag", "*"), ("B_", "tag", "B_*"), ("x", "tag", "x")]
        for spec, kind, expected in cases:
            with self.subTest(spec=spec, kind=kind):
                self.assertEqual(ce.naming.legacy_naming_template(spec, kind), expected)
        # A legacy folder that is only dropped names ('.') gives no folder at all, not the value
        self.assertEqual(ce.naming.value_name_parts(15, ce.naming.legacy_naming_template(".", "dir"), "dir"), ([], []))
