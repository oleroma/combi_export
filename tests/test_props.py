"""Override table edits through the stored properties, their callbacks and the table operator; socket discovery;
applying and reverting overrides on the scene."""
import contextlib
import io
import types

import bpy
import combi_export as ce
from helpers import MODIFIER, BlenderTestCase, add_group, add_node, make_gn_group, make_object, make_preset, planned_paths


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
        items = ce.props.input_items(self.ng, self.node, inp)
        return [ce.fields.value_expr(v, inp.override_type, items) for v in inp.values]


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
                with ce.common.raw_edits(): self.node.inputs.remove(next(k for k, i in enumerate(self.node.inputs) if not i.name))

    def test_shift_add_input_adds_every_supported_input(self):
        self.table_action('ADD_INPUT', shift_pressed=True)
        self.assertEqual([i.name for i in self.node.inputs], ["Size", "Count", "Flag", "Word", "Mode"])
        self.assertTrue(ce.validation.is_preset_setup_valid(bpy.context.scene, self.preset))

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
        for typed, shown in (("\\2", "\\2"), ("abc", "\\2"), ("\\big", "[2]\\big"), ("[3]\\big", "[3]\\big"), ("16\\", "\\16"), ("_B_", "_B_16")):
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


class BlockNamingFieldTests(TableTestCase):
    def test_spaces_in_names_become_underscores(self):
        for block in (self.ng, self.node):
            with self.subTest(block=type(block).__name__):
                block.naming = "\\my dir\\sub dir _my tag"
                self.assertEqual((block.sub_path, block.tag, block.naming), ("my_dir/sub_dir", "my_tag", "\\my_dir\\sub_dir\\_my_tag"))
                block.naming = "\\a:b"  # a character a name cannot hold is still rejected
                self.assertEqual(block.sub_path, "my_dir/sub_dir")

    def test_value_with_a_space_is_named_with_an_underscore(self):
        inp = self.new_input("Word")
        inp.values[0].expr = "my text"
        self.table_action('VALUE_ACTION', i_idx=0, v_idx=0)
        inp.values[1].expr = "other text"
        self.assertEqual(self.exprs(inp), ["\\my text", "\\other text"])
        self.assertEqual(planned_paths(), ["my_text/Box.stl", "other_text/Box.stl"])


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

    def test_expanding_a_sweep_skips_values_the_input_has(self):
        """Steps that repeat another value, or each other (a step of 0 in stored data), are left out instead of being
        left blank."""
        inp = self.new_input("Count")
        inp.values[0].expr = "\\2"
        self.table_action('VALUE_ACTION', i_idx=0, v_idx=0)
        inp.values[1].expr = "\\1:1:3"  # 1, 2, 3
        self.table_action('VALUE_ACTION', i_idx=0, v_idx=1, shift_pressed=True)
        self.assertEqual(self.exprs(inp), ["\\2", "\\1", "\\3"])

        inp.values[2].expr = "\\5:1:2"
        with ce.common.raw_edits(): inp.values[2].sweep_step = "0"  # 5, 5
        self.table_action('VALUE_ACTION', i_idx=0, v_idx=2, shift_pressed=True)
        self.assertEqual(self.exprs(inp), ["\\2", "\\1", "\\5"])

        inp.values[2].expr = "\\1:1:2"  # every step is a value already: the sweep goes
        self.table_action('VALUE_ACTION', i_idx=0, v_idx=2, shift_pressed=True)
        self.assertEqual(self.exprs(inp), ["\\2", "\\1"])


class ValidationTests(TableTestCase):
    def test_invalid_values_block_the_export(self):
        scene = bpy.context.scene
        cases = (("Count", "abc"), ("Size", "0:1:1001"), ("Word", ""), ("Mode", "Z"), ("Size", "1:0:3"), ("Size", "nan"),
                 ("Size", "inf"), ("Size", "0:inf:2"))
        for name, text in cases:
            with self.subTest(name=name, value=text):
                inp = self.new_input(name)
                self.assertTrue(ce.validation.is_preset_setup_valid(scene, self.preset))
                with ce.common.raw_edits():  # stored data the field would reject (e.g. from a hand-edited JSON file)
                    if text.count(":") == 2:
                        val = inp.values[0]
                        val.use_sweep, (val.sweep_start, val.sweep_step, val.sweep_count) = True, text.split(":")
                    else:
                        setattr(inp.values[0], "value_menu" if inp.override_type == "MENU" else "value_string", text)
                self.assertFalse(ce.validation.is_preset_setup_valid(scene, self.preset))
                with ce.common.raw_edits(): self.node.inputs.remove(len(self.node.inputs) - 1)

    def test_string_list_with_a_repeat_blocks_the_export(self):
        inp = self.new_input("Word")
        with ce.common.raw_edits():
            inp.values[0].use_sweep, inp.values[0].sweep_range = True, "a, b, a"
        self.assertFalse(ce.validation.is_preset_setup_valid(bpy.context.scene, self.preset))

    def test_numbers_typed_into_the_stored_fields_must_be_finite(self):
        inp = self.new_input("Size")
        for text in ("nan", "inf", "-1e999"):
            inp.values[0].value_string = text  # rejected by the field's callback: the value stays
            self.assertEqual(inp.values[0].value_string, "1.0")

    def test_missing_group_blocks_the_export(self):
        with ce.common.raw_edits(): self.preset.collections[0].nodegroups.add().group_name = "Gone"
        self.assertFalse(ce.validation.is_preset_setup_valid(bpy.context.scene, self.preset))


class SocketDiscoveryTests(TableTestCase):
    def test_menu_items(self):
        self.assertEqual(ce.nodes.get_menu_switch_items(self.group, "Switch [MENU_SWITCH]", "Menu"), ["A", "B", "C"])
        self.assertEqual(ce.nodes.get_menu_switch_items(self.group, MODIFIER, "Mode"), ["A", "B", "C"])
        self.assertEqual(ce.nodes.get_menu_switch_items(self.group, "Merge [MERGE_BY_DISTANCE]", "Mode"), ["All", "Connected"])
        self.box.modifiers.clear()  # no modifier to ask: read from the Menu Switch the input feeds
        self.assertEqual(ce.nodes.get_menu_switch_items(self.group, MODIFIER, "Mode"), ["A", "B", "C"])

    def test_menu_behind_a_reroute(self):
        links = self.group.links
        switch, reroute = self.group.nodes["Switch"], self.group.nodes.new("NodeReroute")
        links.new(switch.inputs["Menu"].links[0].from_socket, reroute.inputs[0])
        links.new(reroute.outputs[0], switch.inputs["Menu"])
        self.assertEqual(ce.nodes.get_menu_switch_items(self.group, MODIFIER, "Mode"), ["A", "B", "C"])

    def test_socket_classification(self):
        cases = {"NodeSocketFloat": "FLOAT", "NodeSocketFloatDistance": "FLOAT", "VALUE": "FLOAT", "NodeSocketInt": "INT",
                 "INT": "INT", "NodeSocketBool": "BOOLEAN", "BOOLEAN": "BOOLEAN", "NodeSocketString": "STRING",
                 "NodeSocketMenu": "MENU", "MENU": "MENU", "NodeSocketVector": "UNSUPPORTED", "VECTOR": "UNSUPPORTED",
                 "RGBA": "UNSUPPORTED", "NodeSocketColor": "UNSUPPORTED", "NodeSocketObject": "UNSUPPORTED", "ROTATION": "UNSUPPORTED"}
        for socket_type, expected in cases.items():
            with self.subTest(socket_type):
                self.assertEqual(ce.nodes.classify_socket_type(socket_type), expected)

    def test_target_node_order(self):
        self.group.interface.new_socket("Height", in_out="OUTPUT", socket_type="NodeSocketFloat")
        targets = ce.nodes.get_target_node_names(self.group)
        self.assertEqual(targets[-2:], ["Group Output [GROUP_OUTPUT]", MODIFIER])
        self.assertFalse(any(t.startswith("Group Input") for t in targets))
        self.assertEqual(targets[0], "Cube [MESH_PRIMITIVE_CUBE]")

    def test_add_node_stops_when_no_target_is_left(self):
        """Once every target with overridable sockets has an entry, adding a node adds nothing (no <Modifier Interface>
        entry for a group whose interface has nothing to override) and the add button is greyed out."""
        small = bpy.data.node_groups.new("Small", "GeometryNodeTree")
        small.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        small.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        small.nodes.new("NodeGroupInput"); small.nodes.new("NodeGroupOutput")
        small.nodes.new("GeometryNodeMeshCube").name = "Cube"
        self.ng.group_name = "Small"
        self.assertEqual([n.name for n in self.ng.nodes], ["Cube [MESH_PRIMITIVE_CUBE]"])
        self.assertFalse(ce.nodes.has_free_target(small, {"Cube"}))
        self.table_action('ADD_NODE')
        self.assertEqual([n.name for n in self.ng.nodes], ["Cube [MESH_PRIMITIVE_CUBE]"])
        self.assertEqual(ce.nodes.get_target_node_names(small, {"Cube"}), [MODIFIER])  # the node field's fallback stays
        self.assertTrue(ce.nodes.has_free_target(self.group, {"Cube"}))


class ApplyOverridesTests(TableTestCase):
    def mock(self, node, input_name, override_type, value):
        base = types.SimpleNamespace(input_name=input_name, override_type=override_type)
        target = 'MODIFIER' if node == MODIFIER else 'NODE'
        return ce.engine.MockOverride(target, self.group, node, [ce.engine.MockInput(base, value)])

    def size_of_box(self):
        bpy.context.view_layer.update()
        mesh = self.box.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
        xs = [v.co.x for v in mesh.vertices]
        return round(max(xs) - min(xs), 4), len(mesh.polygons)

    def test_modifier_input_is_applied_and_reverted(self):
        ovrs = [self.mock(MODIFIER, "Size", "FLOAT", 3.0)]
        states = ce.headless.capture_baseline_states(ovrs, [self.box])
        ce.headless.apply_overrides(ovrs, [self.box])
        self.assertEqual(self.size_of_box()[0], 3.0)
        ce.headless.revert_overrides(*states, [self.box], skip=ce.headless.override_targets(ovrs))
        self.assertEqual(self.size_of_box()[0], 3.0, "a skipped socket was reverted")
        ce.headless.revert_overrides(*states, [self.box])
        self.assertEqual(self.size_of_box()[0], 1.0)

    def test_linked_node_socket_is_applied_and_reverted(self):
        cube_socket = self.group.nodes["Cube"].inputs["Vertices X"]
        ovrs = [self.mock("Cube [MESH_PRIMITIVE_CUBE]", "Vertices X", "INT", 4)]
        states = ce.headless.capture_baseline_states(ovrs, [self.box])
        before = self.size_of_box()
        ce.headless.apply_overrides(ovrs, [self.box])
        self.assertFalse(cube_socket.is_linked)
        self.assertEqual(self.size_of_box()[1], 14)  # 3 segments along X: 4 x 3 + 2 quads
        ce.headless.revert_overrides(*states, [self.box])
        self.assertTrue(cube_socket.is_linked)
        self.assertEqual(cube_socket.default_value, 2)
        self.assertEqual(self.size_of_box(), before)

    def test_rejected_value_is_reported_once(self):
        ovrs = [self.mock(MODIFIER, "Mode", "MENU", "Z")]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ce.headless.apply_overrides(ovrs, [self.box])
            ce.headless.apply_overrides(ovrs, [self.box])
        self.assertEqual(out.getvalue().count("WARNING: could not set"), 1)


class SearchResultTests(TableTestCase):
    def test_tab_reaches_the_separator(self):
        offers = [("\\A\\", "Branch"), ("\\A\\15", "Branch"), ("\\B\\", "Branch")]
        texts = [t for t, _d in ce.props.search_results("\\A", offers, tab=["\\A\\"])]
        self.assertEqual(texts, ["\\A\\", "\\A\\15", "\\B\\"])
        texts = [t for t, _d in ce.props.search_results("\\A", offers)]
        self.assertEqual(texts[0], "\\A", "the text as typed comes first")
