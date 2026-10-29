"""The one-field value syntax ('\\B\\15\\_x_15') and the node group / node naming fields ('\\C\\2\\_B')."""
import unittest

import combi_export as ce

NO_NAMING = {"dir": None, "tag": None}
MENU = ["A", "B", "C"]


def stored_fields(text, override_type, items=None):
    return None if text is None else ce.fields.read_expr_value(ce.fields.lex_expr(text), override_type, items)


def parse(text, override_type="INT", stored="15", items=None, templates=None, upstream=None):
    return ce.fields.parse_value_field(text, override_type, items, stored_fields(stored, override_type, items), None,
                                templates or NO_NAMING, upstream)


def value_of(fields, override_type):
    if fields["use_sweep"]: return "sweep"
    if override_type in ('MENU', 'BOOLEAN'): return fields["value_menu"]
    return {"INT": int, "FLOAT": float}.get(override_type, str)(fields["value_string"])


def naming(text, override_type="INT", stored="15", **kw):
    """(value, folders, filename tokens) a field gives."""
    fields, templates = parse(text, override_type, stored, **kw)
    value = value_of(fields, override_type)
    names = {kind: sum(ce.naming.value_name_parts(value, templates[kind], kind), []) if templates[kind] is not None else []
             for kind in ("dir", "tag")}
    return value, names["dir"], names["tag"]


class ReadmeTableTests(unittest.TestCase):
    """The value field table of the README (value 15, object Box)."""

    def test_table(self):
        rows = [
            ("15", [], []), ("\\15", ["15"], []), ("_15", [], ["15"]), ("\\15\\_15", ["15"], ["15"]),
            ("\\big", ["big"], []), ("_big", [], ["big"]), ("\\15_mm", ["15_mm"], []), ("\\size_15", ["size_15"], []),
            ("_15_mm", [], ["15_mm"]), ("\\B\\15", ["B", "15"], []), ("_B_15", [], ["B", "15"]),
            ("\\big\\_15", ["big"], ["15"]), ("\\big\\_small", ["big"], ["small"]),
        ]
        for text, folders, tokens in rows:
            with self.subTest(field=text):
                self.assertEqual(naming(text), (15, folders, tokens))

    def test_shortcuts_go_on_with_the_value(self):
        for short, full in (("\\B\\", "\\B\\15"), ("_B_", "_B_15"), ("15\\", "\\15"), ("15_", "_15")):
            with self.subTest(short=short):
                self.assertEqual(naming(short), naming(full))

    def test_sweeps(self):
        fields, templates = parse("\\0:0.5:5", "FLOAT", "1")
        self.assertEqual((fields["sweep_start"], fields["sweep_step"], fields["sweep_count"]), ("0", "0.5", "5"))
        self.assertEqual(templates, {"dir": "*", "tag": None})
        fields, templates = parse("_{a, b}", "STRING", "x")
        self.assertEqual((fields["use_sweep"], fields["sweep_range"], templates["tag"]), (True, "a, b", "*"))
        fields, templates = parse("\\*", "MENU", "A", items=MENU)
        self.assertEqual((fields, templates["dir"]), ({"use_sweep": True}, "*"))


class ValueInferenceTests(unittest.TestCase):
    def test_value_typed_in_place_changes_it(self):
        self.assertEqual(naming("\\16"), (16, ["16"], []))
        self.assertEqual(naming("\\B\\16", templates={"dir": "B/*", "tag": None}), (16, ["B", "16"], []))
        self.assertEqual(naming("\\16_mm", templates={"dir": "*_mm", "tag": None}), (16, ["16_mm"], []))

    def test_upstream_name_is_a_typed_name(self):
        upstream = lambda kind, name: (kind, name) == ("dir", "16")
        self.assertEqual(naming("\\16", upstream=upstream), (15, ["16"], []))

    def test_other_text_is_a_typed_name(self):
        self.assertEqual(naming("\\big"), (15, ["big"], []))

    def test_value_written_first(self):
        self.assertEqual(naming("16\\big"), (16, ["big"], []))
        self.assertEqual(naming("2\\"), (2, ["2"], []))

    def test_blank_value_takes_the_last_value_in_the_names(self):
        self.assertEqual(naming("\\B\\1", stored=None), (1, ["B", "1"], []))
        self.assertEqual(naming("_B_2_15", "FLOAT", stored=None), (15.0, [], ["B_2", "15"]))

    def test_underscores(self):
        self.assertEqual(naming("15_big"), (15, [], ["big"]))
        self.assertEqual(naming("my_text", "STRING", "x"), ("my_text", [], []))
        self.assertEqual(naming("Mode_A", "MENU", "Mode_B", items=["Mode_A", "Mode_B"]), ("Mode_A", [], []))
        self.assertEqual(naming("my_text _x", "STRING", "x"), ("my_text", [], ["x"]))

    def test_quotes_are_literal(self):
        self.assertEqual(naming('"a\\b"', "STRING", "x"), ("a\\b", [], []))
        self.assertEqual(naming('"a_b"_x', "STRING", "x"), ("a_b", [], ["x"]))
        self.assertEqual(naming('\\"15"\\15'), (15, ["15", "15"], []))

    def test_menu_and_boolean_items(self):
        self.assertEqual(naming("b", "MENU", "A", items=MENU)[0], "B")
        self.assertEqual(naming("true", "BOOLEAN", "False")[0], "True")


class RejectionTests(unittest.TestCase):
    def test_rejected(self):
        cases = [("1.5", "INT"), ("abc", "INT"), ("0:1:1001", "FLOAT"), ("0:1:0", "FLOAT"), ("0:1", "FLOAT"),
                 ("\\a<b", "INT"), ("\\a b\\15", "INT"), ("\\a\\_b\\c", "INT"), ("Z", "MENU"), ("{ , }", "STRING"),
                 # every step of a sweep differs; numbers are finite
                 ("0:0:5", "FLOAT"), ("\\1:0:3", "INT"), ("0:1e-9:3", "FLOAT"), ("{a, b, a}", "STRING"),
                 ("nan", "FLOAT"), ("inf\\", "FLOAT"), ("-Infinity", "FLOAT"), ("1e999", "FLOAT"), ("0:nan:3", "FLOAT"),
                 ("1e308:1e308:3", "FLOAT")]
        for text, override_type in cases:
            with self.subTest(field=text, type=override_type):
                with self.assertRaises(ValueError):
                    parse(text, override_type, None if override_type != "MENU" else "A", items=MENU if override_type == "MENU" else None)

    def test_single_step_sweep_may_have_any_step(self):
        self.assertTrue(parse("\\2:0:1", "FLOAT")[0]["use_sweep"])

    def test_numbers_never_hold_underscores(self):
        # int('1_0') would accept it: the '_' starts the filename section instead
        with self.assertRaises(ValueError):
            ce.fields.read_expr_value(ce.fields.lex_expr("1_0"), "INT")
        self.assertEqual(naming("1_0"), (1, [], ["0"]))


class ComposeTests(unittest.TestCase):
    """compose_value_field writes stored data as a field that parses back to the same value and naming."""

    def assertRoundTrip(self, text, override_type="INT", stored="15", items=None):
        fields, templates = parse(text, override_type, stored, items=items)
        shown = ce.fields.compose_value_field(fields, templates, override_type, items)
        again = ce.fields.parse_value_field(shown, override_type, items, fields, None, templates)
        self.assertTrue(ce.fields.same_fields(override_type, again[0], fields), f"{text!r} shown as {shown!r}")
        norm = lambda t: {k: (v or "*") if v is not None else None for k, v in t.items()}
        self.assertEqual(norm(again[1]), norm(templates), f"{text!r} shown as {shown!r}")
        return shown

    def test_round_trips(self):
        for text in ("15", "\\15", "_15", "\\15\\_15", "\\big", "_big", "\\15_mm", "\\size_15", "\\B\\15", "_B_15",
                     "\\big\\_small", '\\"15"\\15', "15_big", "16\\big"):
            with self.subTest(field=text):
                self.assertRoundTrip(text)
        self.assertRoundTrip("\\0:0.5:5", "FLOAT", "1")
        self.assertRoundTrip('"a_b"_x', "STRING", "x")
        self.assertRoundTrip("_{a, b}", "STRING", "x")
        self.assertRoundTrip("\\*", "MENU", "A", MENU)
        # A '_' starting a folder name is quoted, in a typed name ('_a') or a text value ('_x')
        self.assertRoundTrip('\\"_"a_15')
        for text in ('\\"_"x_mm', '\\"_"x', '\\"_"_x_mm\\_y', '\\B\\"_"x_mm\\_t_15', '\\"_"_mm'):
            with self.subTest(field=text):
                self.assertRoundTrip(text, "STRING", "x")
        # '{' and '}' of a text value written in a name are quoted, else they would start a sweep
        for text in ('\\"{"x"}"_mm', '\\a"{"b', '_x_a"{"b', '\\B\\"_{"x\\_t_15'):
            with self.subTest(field=text):
                self.assertRoundTrip(text, "STRING", "x")
        # A value hidden by typed names is written first
        self.assertRoundTrip("my text\\big", "STRING", "x")
        self.assertRoundTrip("a _big", "STRING", "x")
        self.assertRoundTrip("B\\big", "MENU", "A", MENU)
        self.assertRoundTrip("0:0.5:5\\big", "FLOAT", "1")

    def test_display(self):
        self.assertEqual(ce.fields.compose_value_field({"use_sweep": False, "value_string": "15"}, {"dir": "big", "tag": None}, "INT"), "15\\big")
        self.assertEqual(ce.fields.compose_value_field({"use_sweep": False, "value_string": "15"}, {"dir": None, "tag": "big"}, "INT"), "15_big")
        self.assertEqual(ce.fields.compose_value_field({"use_sweep": False, "value_string": "15"}, {"dir": "big", "tag": "*"}, "INT"), "\\big\\_15")
        self.assertEqual(ce.fields.compose_value_field({"use_sweep": False, "value_string": "15"}, {"dir": "15", "tag": None}, "INT"), '15\\"15"')
        self.assertEqual(ce.fields.compose_value_field({"use_sweep": False, "value_string": "a"}, {"dir": None, "tag": "big"}, "STRING"), "a _big")
        self.assertEqual(ce.fields.compose_value_field({"use_sweep": False, "value_string": "15"}, NO_NAMING, "INT"), "15")
        self.assertEqual(ce.fields.compose_value_field({"use_sweep": False, "value_string": "15"}, {"dir": "B/*", "tag": "x_*"}, "FLOAT"), "\\B\\15\\_x_15")
        self.assertEqual(ce.fields.compose_value_field({"use_sweep": False, "value_string": "_x"}, {"dir": "*_mm", "tag": None}, "STRING"), '\\"_"x_mm')
        self.assertEqual(ce.fields.compose_value_field({"use_sweep": False, "value_string": "{x}"}, {"dir": "*_mm", "tag": None}, "STRING"), '\\"{"x"}"_mm')
        self.assertEqual(ce.fields.compose_value_field({"use_sweep": False, "value_string": "a{b"}, NO_NAMING, "STRING"), '"a{b"')


class BlockNamingTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ce.fields.parse_block_naming("\\C\\2\\_B"), ("C/2", "B"))
        self.assertEqual(ce.fields.parse_block_naming("C/2"), ("C/2", ""))
        self.assertEqual(ce.fields.parse_block_naming("_B"), ("", "B"))
        self.assertEqual(ce.fields.parse_block_naming(""), ("", ""))
        with self.assertRaises(ValueError):
            ce.fields.parse_block_naming("\\a:b")

    def test_parse_replaces_spaces(self):
        parse = ce.fields.parse_block_naming
        self.assertEqual(parse("\\my dir\\sub dir"), ("my_dir/sub_dir", ""))
        self.assertEqual(parse("_my tag"), ("", "my_tag"))
        self.assertEqual(parse('\\"my dir"'), ("my_dir", ""))
        self.assertEqual(parse("my dir/b"), ("my_dir/b", ""))
        # A space before '_' still ends the folder section, and edge spaces are no part of a name
        self.assertEqual(parse("  \\a b _c d  "), ("a_b", "c_d"))

    def test_compose_round_trip(self):
        for sub_path, tag in (("C/2", "B"), ("X", ""), ("", "B_2"), ("_odd", "")):
            with self.subTest(sub_path=sub_path, tag=tag):
                self.assertEqual(ce.fields.parse_block_naming(ce.fields.compose_block_naming(sub_path, tag)), (sub_path, tag))
