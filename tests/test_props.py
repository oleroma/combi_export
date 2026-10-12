"""Override table edits through the stored properties, their callbacks and the table operator; socket discovery;
applying and reverting overrides on the scene."""
import contextlib
import io
import types

import bpy
import combi_export as ce
from helpers import MODIFIER, BlenderTestCase, add_group, add_node, make_gn_group, make_object, make_preset


class TableTestCase(BlenderTestCase):
    def setUp(self):
        super().setUp()
        self.group = make_gn_group()
        self.box = make_object("Box", group=self.group)
        self.preset = make_preset()
        self.ng = add_group(self.preset.nodegroups)
        self.node = add_node(self.ng)

    def new_input(self, name):
        inp = self.node.inputs.add()
        inp.name = name  # the callback infers the type and fills in the socket's default
        return inp

    def table_action(self, action, **kwargs):
        return bpy.ops.batch_stl.table_action(action=action, is_preset=True, ng_idx=0, n_idx=0, **kwargs)

    def exprs(self, inp):
        items = ce.input_items(self.ng, self.node, inp)
        return [ce.value_expr(v, inp.override_type, items) for v in inp.values]


class InputTests(TableTestCase):
    def test_type_and_default_value(self):
        for name, override_type, expr in (("Size", "FLOAT", "1.0"), ("Count", "INT", "2"), ("Flag", "BOOLEAN", "False"),
                                          ("Word", "STRING", "a"), ("Mode", "MENU", "A")):
            with self.subTest(name):
                inp = self.new_input(name)
                self.assertEqual((inp.override_type, self.exprs(inp)), (override_type, [expr]))

    def test_unsupported_unknown_and_duplicate_inputs_are_rejected(self):
        self.new_input("Size")
        for name in ("Offset", "Nope", "Size"):
            with self.subTest(name):
                self.new_input(name)  # a duplicate swaps the two rows, so look at the names, not the new item
                self.assertEqual(sorted(i.name for i in self.node.inputs), ["", "Size"])
                with ce.raw_edits(): self.node.inputs.remove(next(k for k, i in enumerate(self.node.inputs) if not i.name))

    def test_shift_add_input_adds_every_supported_input(self):
        self.table_action('ADD_INPUT', shift_pressed=True)
        self.assertEqual([i.name for i in self.node.inputs], ["Size", "Count", "Flag", "Word", "Mode"])
        self.assertTrue(ce.is_preset_setup_valid(bpy.context.scene, self.preset))

    def test_node_target(self):
        self.node.name = "Cube [MESH_PRIMITIVE_CUBE]"
        self.assertEqual([(i.name, i.override_type, self.exprs(i)) for i in self.node.inputs], [("Vertices X", "INT", ["2"])])
        self.node.name = "Nope"
        self.assertEqual(self.node.name, "Cube [MESH_PRIMITIVE_CUBE]")

    def test_group_name(self):
        make_gn_group("GN2")
        self.ng.group_name = "Nope"
        self.assertEqual(self.ng.group_name, "GN")
        second = self.preset.nodegroups.add()
        second.group_name = "GN"  # already listed on this level
        self.assertEqual(second.group_name, "")
        second.group_name = "GN2"
        self.assertEqual([n.name for n in second.nodes], ["Cube [MESH_PRIMITIVE_CUBE]"])
        second.group_name = ""
        self.assertEqual([ng.group_name for ng in self.preset.nodegroups], ["GN"])


class ValueFieldTests(TableTestCase):
    def test_edits(self):
        val = self.new_input("Size").values[0]
        for typed, shown in (("\\2", "\\2"), ("abc", "\\2"), ("\\big", "\\big"), ("16\\", "\\16"), ("_B_", "_B_16")):
            with self.subTest(typed=typed):
                val.expr = typed
                self.assertEqual(val.expr, shown)
        self.assertEqual(val.value_string, "16")

    def test_value_another_value_has_is_rejected(self):
        inp = self.new_input("Count")
        self.table_action('VALUE_ACTION', i_idx=0, v_idx=0)
        inp.values[1].expr = "2"
        self.assertEqual(self.exprs(inp), ["\\2", "\\3"])

    def test_clearing_the_last_value_removes_its_rows(self):
        self.new_input("Size").values[0].expr = ""
        self.assertEqual(len(self.preset.nodegroups), 0)

    def test_star_replaces_the_other_values(self):
        inp = self.new_input("Mode")
        self.table_action('VALUE_ACTION', i_idx=0, v_idx=0)
        self.assertEqual(self.exprs(inp), ["\\A", "\\B"])
        inp.values[0].expr = "*"
        self.assertEqual(self.exprs(inp), ["\\*"])


class ValueActionTests(TableTestCase):
    def test_second_value_names_the_permutation(self):
        inp = self.new_input("Count")
        self.table_action('VALUE_ACTION', i_idx=0, v_idx=0)
        self.assertEqual(self.exprs(inp), ["\\2", "\\3"])

    def test_new_value_takes_the_inputs_naming(self):
        inp = self.new_input("Count")
        inp.values[0].expr = "_2"
        self.table_action('VALUE_ACTION', i_idx=0, v_idx=0)
        self.assertEqual(self.exprs(inp), ["_2", "_3"])

    def test_boolean_has_two_values(self):
        inp = self.new_input("Flag")
        for _ in range(3): self.table_action('VALUE_ACTION', i_idx=0, v_idx=0)
        self.assertEqual(self.exprs(inp), ["\\False", "\\True"])

    def test_shift_toggles_a_sweep(self):
        inp = self.new_input("Count")
        self.table_action('VALUE_ACTION', i_idx=0, v_idx=0)
        self.table_action('VALUE_ACTION', i_idx=0, v_idx=0, shift_pressed=True)
        self.assertEqual(len(inp.values), 1)
        self.assertTrue(inp.values[0].use_sweep)
        inp.values[0].expr = "\\0:2:3"
        self.table_action('VALUE_ACTION', i_idx=0, v_idx=0, shift_pressed=True)  # back to single values
        self.assertEqual(self.exprs(inp), ["\\0", "\\2", "\\4"])


class ValidationTests(TableTestCase):
    def test_invalid_values_block_the_export(self):
        scene = bpy.context.scene
        cases = (("Count", "abc"), ("Size", "0:1:1001"), ("Word", ""), ("Mode", "Z"))
        for name, text in cases:
            with self.subTest(name=name, value=text):
                inp = self.new_input(name)
                self.assertTrue(ce.is_preset_setup_valid(scene, self.preset))
                with ce.raw_edits():  # stored data the field would reject (e.g. from a hand-edited JSON file)
                    if text.count(":") == 2:
                        val = inp.values[0]
                        val.use_sweep, (val.sweep_start, val.sweep_step, val.sweep_count) = True, text.split(":")
                    else:
                        setattr(inp.values[0], "value_menu" if inp.override_type == "MENU" else "value_string", text)
                self.assertFalse(ce.is_preset_setup_valid(scene, self.preset))
                with ce.raw_edits(): self.node.inputs.remove(len(self.node.inputs) - 1)

    def test_missing_group_blocks_the_export(self):
        with ce.raw_edits(): self.preset.collections[0].nodegroups.add().group_name = "Gone"
        self.assertFalse(ce.is_preset_setup_valid(bpy.context.scene, self.preset))


class SocketDiscoveryTests(TableTestCase):
    def test_menu_items(self):
        self.assertEqual(ce.get_menu_switch_items(self.group, "Switch [MENU_SWITCH]", "Menu"), ["A", "B", "C"])
        self.assertEqual(ce.get_menu_switch_items(self.group, MODIFIER, "Mode"), ["A", "B", "C"])
        self.assertEqual(ce.get_menu_switch_items(self.group, "Merge [MERGE_BY_DISTANCE]", "Mode"), ["All", "Connected"])
        self.box.modifiers.clear()  # no modifier to ask: read from the Menu Switch the input feeds
        self.assertEqual(ce.get_menu_switch_items(self.group, MODIFIER, "Mode"), ["A", "B", "C"])

    def test_menu_behind_a_reroute(self):
        links = self.group.links
        switch, reroute = self.group.nodes["Switch"], self.group.nodes.new("NodeReroute")
        links.new(switch.inputs["Menu"].links[0].from_socket, reroute.inputs[0])
        links.new(reroute.outputs[0], switch.inputs["Menu"])
        self.assertEqual(ce.get_menu_switch_items(self.group, MODIFIER, "Mode"), ["A", "B", "C"])

    def test_socket_classification(self):
        cases = {"NodeSocketFloat": "FLOAT", "NodeSocketFloatDistance": "FLOAT", "VALUE": "FLOAT", "NodeSocketInt": "INT",
                 "INT": "INT", "NodeSocketBool": "BOOLEAN", "BOOLEAN": "BOOLEAN", "NodeSocketString": "STRING",
                 "NodeSocketMenu": "MENU", "MENU": "MENU", "NodeSocketVector": "UNSUPPORTED", "VECTOR": "UNSUPPORTED",
                 "RGBA": "UNSUPPORTED", "NodeSocketColor": "UNSUPPORTED", "NodeSocketObject": "UNSUPPORTED", "ROTATION": "UNSUPPORTED"}
        for socket_type, expected in cases.items():
            with self.subTest(socket_type):
                self.assertEqual(ce.classify_socket_type(socket_type), expected)

    def test_target_node_order(self):
        self.group.interface.new_socket("Height", in_out="OUTPUT", socket_type="NodeSocketFloat")
        targets = ce.get_target_node_names(self.group)
        self.assertEqual(targets[-2:], ["Group Output [GROUP_OUTPUT]", MODIFIER])
        self.assertFalse(any(t.startswith("Group Input") for t in targets))
        self.assertEqual(targets[0], "Cube [MESH_PRIMITIVE_CUBE]")


class ApplyOverridesTests(TableTestCase):
    def mock(self, node, input_name, override_type, value):
        base = types.SimpleNamespace(input_name=input_name, override_type=override_type)
        target = 'MODIFIER' if node == MODIFIER else 'NODE'
        return ce.MockOverride(target, self.group, node, [ce.MockInput(base, value)])

    def size_of_box(self):
        bpy.context.view_layer.update()
        mesh = self.box.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
        xs = [v.co.x for v in mesh.vertices]
        return round(max(xs) - min(xs), 4), len(mesh.polygons)

    def test_modifier_input_is_applied_and_reverted(self):
        ovrs = [self.mock(MODIFIER, "Size", "FLOAT", 3.0)]
        states = ce.capture_baseline_states(ovrs, [self.box])
        ce.apply_overrides(ovrs, [self.box])
        self.assertEqual(self.size_of_box()[0], 3.0)
        ce.revert_overrides(*states, [self.box], skip=ce.override_targets(ovrs))
        self.assertEqual(self.size_of_box()[0], 3.0, "a skipped socket was reverted")
        ce.revert_overrides(*states, [self.box])
        self.assertEqual(self.size_of_box()[0], 1.0)

    def test_linked_node_socket_is_applied_and_reverted(self):
        cube_socket = self.group.nodes["Cube"].inputs["Vertices X"]
        ovrs = [self.mock("Cube [MESH_PRIMITIVE_CUBE]", "Vertices X", "INT", 4)]
        states = ce.capture_baseline_states(ovrs, [self.box])
        before = self.size_of_box()
        ce.apply_overrides(ovrs, [self.box])
        self.assertFalse(cube_socket.is_linked)
        self.assertEqual(self.size_of_box()[1], 14)  # 3 segments along X: 4 x 3 + 2 quads
        ce.revert_overrides(*states, [self.box])
        self.assertTrue(cube_socket.is_linked)
        self.assertEqual(cube_socket.default_value, 2)
        self.assertEqual(self.size_of_box(), before)

    def test_rejected_value_is_reported_once(self):
        ovrs = [self.mock(MODIFIER, "Mode", "MENU", "Z")]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ce.apply_overrides(ovrs, [self.box])
            ce.apply_overrides(ovrs, [self.box])
        self.assertEqual(out.getvalue().count("WARNING: could not set"), 1)


class SearchResultTests(TableTestCase):
    def test_tab_reaches_the_separator(self):
        offers = [("\\A\\", "Branch"), ("\\A\\15", "Branch"), ("\\B\\", "Branch")]
        texts = [t for t, _d in ce.search_results("\\A", offers, tab=["\\A\\"])]
        self.assertEqual(texts, ["\\A\\", "\\A\\15", "\\B\\"])
        texts = [t for t, _d in ce.search_results("\\A", offers)]
        self.assertEqual(texts[0], "\\A", "the text as typed comes first")
