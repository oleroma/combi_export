"""Variant generation and branch merging (walk_override_branch), checked through the planned output paths of real scene
data, and the UI's merge predictions (BranchScope) against them."""
import random
import types
import unittest
from unittest import mock

import bpy
import combi_export as ce
from helpers import BlenderTestCase, make_gn_group, make_object, make_preset, override, add_group, add_node, add_input, planned_paths


class EngineTestCase(BlenderTestCase):
    def setUp(self):
        super().setUp()
        self.group = make_gn_group()
        self.box = make_object("Box", group=self.group)
        self.preset = make_preset()
        self.scene = bpy.context.scene

    @property
    def col(self): return self.preset.collections[0]

    def obj_entry(self, name="Box"):
        return next(o for o in self.col.objects if o.name == name)

    def letters_and_numbers(self, letters=("A", "B", "C"), numbers=("1", "2", "3"), mark="\\"):
        override(self.scene.batch_stl_global_nodegroups, "Word", "STRING", *(mark + v for v in letters))
        override(self.preset.nodegroups, "Count", "INT", *(mark + v for v in numbers))

    def assertPaths(self, expected):
        paths = planned_paths(self.preset)
        self.assertEqual(sorted(paths), sorted(expected))
        self.assertEqual(len(paths), len(set(paths)), "a path is written twice")
        self.assertCountsMatch()

    def assertCountsMatch(self):
        """The variant count (product of pool sizes without anchors) equals the variants generated, and every variant's
        values are all active when it is walked again."""
        live = ce.targets.live_collection_names(bpy.context.view_layer)
        for *_rest, ovrs in ce.targets.iter_export_objects(self.scene, self.preset, live):
            variants = list(ce.engine.generate_named_combinations(ovrs))
            self.assertEqual(ce.engine.count_override_combinations(ovrs), len(variants))
            for combo, _tags, _dirs in variants:
                active = ce.engine.walk_override_branch(ovrs, combo)[0]
                self.assertTrue(all(id(inp) in active for _ovr, inp in combo))


class CombinationTests(EngineTestCase):
    def test_no_overrides_one_file(self):
        self.assertPaths(["Box.stl"])

    def test_values_multiply(self):
        self.letters_and_numbers()
        self.assertPaths([f"{l}/{n}/Box.stl" for l in "ABC" for n in "123"])

    def test_inputs_sharing_a_value_still_multiply(self):
        override(self.scene.batch_stl_global_nodegroups, "Count", "INT", "\\1", "\\2")
        override(self.preset.nodegroups, "Size", "FLOAT", "\\1", "\\2")
        self.assertPaths(["1/1/Box.stl", "1/2/Box.stl", "2/1/Box.stl", "2/2/Box.stl"])

    def test_sweeps(self):
        override(self.preset.nodegroups, "Size", "FLOAT", "\\0:0.5:3")
        override(self.preset.nodegroups, "Mode", "MENU", "_*")
        self.assertPaths([f"{s}/Box_{m}.stl" for s in ("0", "0.5", "1") for m in "ABC"])

    def test_tags_follow_hierarchy_order(self):
        override(self.scene.batch_stl_global_nodegroups, "Word", "STRING", "_A")
        self.col.use_tag, self.col.tag = True, "v2"
        override(self.col.nodegroups, "Count", "INT", "_1")
        self.obj_entry().tag = "lid"
        self.assertPaths(["Box_A_v2_1_lid.stl"])

    def test_more_specific_level_replaces_the_value(self):
        make_object("Box2", group=self.group)
        ce.targets.sync_collection_objects(self.col)
        override(self.preset.nodegroups, "Size", "FLOAT", "\\1", "\\2")
        override(self.obj_entry().nodegroups, "Size", "FLOAT", "\\3")
        self.assertPaths(["3/Box.stl", "1/Box2.stl", "2/Box2.stl"])

    def test_overrides_skip_objects_without_the_group(self):
        make_object("Plain")
        ce.targets.sync_collection_objects(self.col)
        override(self.preset.nodegroups, "Size", "FLOAT", "\\1", "\\2")
        self.assertPaths(["1/Box.stl", "2/Box.stl", "Plain.stl"])

    def test_plain_values_clash(self):
        override(self.preset.nodegroups, "Size", "FLOAT", "1", "2")
        clashes = ce.targets.find_export_clashes(self.scene, self.preset, ce.targets.live_collection_names(bpy.context.view_layer), self.tmp)
        self.assertEqual(len(clashes), 1)

    def test_prefix_and_sub_folders(self):
        self.preset.preset_prefix, self.col.sub_path, self.obj_entry().sub_path = "out", "parts", "box"
        override(self.preset.nodegroups, "Count", "INT", "\\1")
        self.assertPaths(["out/1/parts/box/Box.stl"])


class BranchMergeTests(EngineTestCase):
    """The Branch Merging examples of the README: letters A B C x numbers 1 2 3 upstream."""

    def block(self, naming, *values):
        ng = add_group(self.col.nodegroups, naming=naming)
        add_input(add_node(ng), "Size", "FLOAT", *values)
        return ng

    def test_block_merges_into_one_letter(self):
        self.letters_and_numbers()
        self.block("\\B", "1\\X", "2\\Y")
        self.assertPaths([f"{l}/{n}/Box.stl" for l in "AC" for n in "123"] + [f"B/{n}/{x}/Box.stl" for n in "123" for x in "XY"])

    def test_block_chain(self):
        self.letters_and_numbers()
        self.block("\\C\\2", "1\\X", "2\\Y")
        expected = [f"{l}/{n}/Box.stl" for l in "ABC" for n in "123" if (l, n) != ("C", "2")]
        self.assertPaths(expected + ["C/2/X/Box.stl", "C/2/Y/Box.stl"])

    def test_block_merges_into_every_number(self):
        self.letters_and_numbers()
        self.block("\\2", "1\\X", "2\\Y")
        expected = [f"{l}/{n}/Box.stl" for l in "ABC" for n in "13"]
        self.assertPaths(expected + [f"{l}/2/{x}/Box.stl" for l in "ABC" for x in "XY"])

    def test_value_merges_through_its_typed_folder(self):
        override(self.scene.batch_stl_global_nodegroups, "Word", "STRING", "\\A", "\\B", "\\C")
        override(self.preset.nodegroups, "Count", "INT", "\\B\\1", "\\B\\2")
        self.assertPaths(["A/Box.stl", "B/1/Box.stl", "B/2/Box.stl", "C/Box.stl"])

    def test_value_merges_through_its_typed_token(self):
        override(self.scene.batch_stl_global_nodegroups, "Word", "STRING", "_A", "_B", "_C")
        override(self.preset.nodegroups, "Count", "INT", "_B_1", "_B_2")
        self.assertPaths(["Box_A.stl", "Box_B_1.stl", "Box_B_2.stl", "Box_C.stl"])

    def test_token_chain_merges_into_one_branch_of_a_branch(self):
        override(self.scene.batch_stl_global_nodegroups, "Word", "STRING", "_A", "_B")
        override(self.preset.nodegroups, "Count", "INT", "_1", "_2")
        override(self.col.nodegroups, "Size", "FLOAT", "_B_2_15")
        self.assertPaths(["Box_A_1.stl", "Box_A_2.stl", "Box_B_1.stl", "Box_B_2_15.stl"])

    def test_dead_block_never_applies(self):
        # X and Y only exist under A, so a block merging into C/X never applies
        override(self.scene.batch_stl_global_nodegroups, "Word", "STRING", "\\A", "\\B", "\\C")
        add_input(add_node(add_group(self.preset.nodegroups, naming="\\A")), "Size", "FLOAT", "1\\X", "2\\Y")
        dead = add_group(self.col.nodegroups, naming="\\C\\X")
        add_input(add_node(dead), "Count", "INT", "\\5", "\\6")
        self.assertPaths(["A/X/Box.stl", "A/Y/Box.stl", "B/Box.stl", "C/Box.stl"])

        scope, _ctx = ce.props.upstream_scope(dead)
        _ctx, res = scope.enter_block(dead, frozenset(), dead)
        self.assertTrue(ce.scope.scope_dead(res), "the UI does not flag the dead block")

    def test_live_merge_is_predicted(self):
        self.letters_and_numbers()
        ng = self.block("\\C\\2", "1\\X")
        scope, _ctx = ce.props.upstream_scope(ng)
        _ctx, res = scope.enter_block(ng, frozenset(), ng)
        self.assertFalse(ce.scope.scope_dead(res))
        self.assertEqual(res["dir"][0], ["C", "2"])

    def test_block_search_offers_names_under_the_typed_branch(self):
        self.letters_and_numbers()
        ng = add_group(self.col.nodegroups)
        offers = [text for text, _desc in ce.props.search_block_naming(ng, bpy.context, "\\C\\")]
        for n in "123": self.assertIn(f"\\C\\{n}\\", offers)
        self.assertFalse(any(text.startswith(("\\C\\A", "\\C\\B")) for text in offers))

    def test_value_search_offers_chains_with_the_value(self):
        self.letters_and_numbers(numbers=("1", "2"))
        val = override(self.col.nodegroups, "Size", "FLOAT", "\\15").values[0]
        offers = [text for text, _desc in ce.props.search_value_expr(val, bpy.context, "\\B\\")]
        for text in ("\\B\\1\\15", "\\B\\1\\", "\\B\\2\\15", "\\B\\2\\"): self.assertIn(text, offers)

    # Note: an inherited block keeps its folder only while one of its inputs survives resolve_overrides. Whether its
    # folder should also go when a lower level overrides every input is a design question, so it is not tested here.


class FastCountTests(unittest.TestCase):
    """Without anchors every combination is a variant: the count is a product and generation skips the pruning walks.
    Forcing the pruning walks on random anchor-free stacks must not drop or change any variant."""

    def random_stack(self, rnd):
        overrides = []
        for level in ("GLOBAL", "PRESET", "COLLECTION", "OBJECT"):
            for o in range(rnd.randint(0, 2)):
                inputs = []
                for i in range(rnd.randint(1, 2)):
                    name = f"{level}{o}{i}"
                    base = types.SimpleNamespace(input_name=name, override_type="INT")
                    for v in rnd.sample(range(1, 5), rnd.randint(1, 3)):
                        # Typed names unique to the input: they are new folders / tokens, never anchors
                        dir_t = rnd.choice((None, "*", "*_mm", f"n{name}/*"))
                        tag_t = rnd.choice((None, "*", f"t{name}_*"))
                        val = types.SimpleNamespace(value_string=str(v), value_menu="", use_sweep=False, use_dir=dir_t is not None,
                                                    dir_tag=dir_t or "", use_tag=tag_t is not None, tag=tag_t or "")
                        inputs.append(ce.engine.MockInput(base, val, is_temp=True))
                sub = rnd.choice(("", "", "blk"))
                overrides.append(ce.engine.MockOverride('MODIFIER', None, "<Modifier Interface>", inputs, level, sub, "", "", "", (level, o)))
        return overrides

    def test_product_count_matches_pruned_generation(self):
        rnd, checked = random.Random(1234), 0
        for trial in range(40):
            stack = self.random_stack(rnd)
            known = ce.engine._known_branch_dirs(stack)
            if ce.engine._has_branch_anchors(stack, known): continue
            checked += 1
            fast = [(t, d) for _c, t, d in ce.engine.generate_named_combinations(stack)]
            with mock.patch.object(ce.engine, "_has_branch_anchors", lambda *_a: True):
                pruned = [(t, d) for _c, t, d in ce.engine.generate_named_combinations(stack)]
            with self.subTest(trial=trial):
                self.assertEqual(ce.engine.count_override_combinations(stack), len(fast))
                self.assertEqual(fast, pruned)
        self.assertGreater(checked, 30)
