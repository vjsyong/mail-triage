"""Lineage / split integrity tests (WP2, FR2).

Intentional clean/attack, counterfactual, reference, source/thread and
near-duplicate leaks must fail lint, and shared personas/policies/templates must
not collapse unrelated lineage groups.
"""
import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3 import build  # noqa: E402
from benchmarks.v3.build import lineage as L  # noqa: E402

_BASE = None


def base_bundle():
    global _BASE
    if _BASE is None:
        _BASE = build.build_dataset(triage_roots=12, workflow_roots=2, seed=1)
    return copy.deepcopy(_BASE)


def _first(cases, pred):
    for case in cases:
        if pred(case):
            return case
    raise AssertionError("no matching case")


def _clone_case(bundle, case, tag, split):
    """Clone a case into a brand-new lineage/scenario with a different split."""
    new_case = copy.deepcopy(case)
    new_case["case_id"] = "case_%s_native" % tag
    new_case["scenario_id"] = "scn_%s" % tag
    new_case["lineage_id"] = "lin_%s" % tag
    new_case["split"] = split
    new_case["gold_id"] = "gold_%s_native" % tag
    new_case["relation"] = {"relation_type": "root", "stable_fields": [],
                            "changing_fields": [], "parent_case_id": None}
    gold = copy.deepcopy(next(g for g in bundle["gold"]
                              if g["gold_id"] == case["gold_id"]))
    gold["gold_id"] = new_case["gold_id"]
    gold["case_id"] = new_case["case_id"]
    scenario = copy.deepcopy(next(s for s in bundle["scenarios"]
                                  if s["scenario_id"] == case["scenario_id"]))
    scenario["scenario_id"] = new_case["scenario_id"]
    scenario["lineage_id"] = new_case["lineage_id"]
    lineage = {"schema_version": "v3.0", "lineage_id": new_case["lineage_id"],
               "root_id": new_case["scenario_id"],
               "members": [new_case["case_id"]],
               "source_message_ids": list(lineage_source_ids(scenario)),
               "relation_type": "root", "partition": split}
    bundle["cases"].append(new_case)
    bundle["gold"].append(gold)
    bundle["scenarios"].append(scenario)
    bundle["lineage"].append(lineage)
    return new_case


def lineage_source_ids(scenario):
    return [m["message_id"] for m in scenario.get("messages", [])]


class CleanBundleTest(unittest.TestCase):
    def test_fresh_bundle_is_clean(self):
        self.assertEqual(build.validate_dataset(base_bundle()), [])

    def test_every_component_is_one_split(self):
        b = base_bundle()
        by_id = {c["case_id"]: c for c in b["cases"]}
        edges, _near = L.case_edges(b["cases"])
        groups = L.connected_components(list(by_id), edges)
        for members in groups.values():
            splits = {by_id[m]["split"] for m in members}
            self.assertEqual(len(splits), 1, members)

    def test_components_do_not_collapse_and_match_lineages(self):
        b = base_bundle()
        edges, _near = L.case_edges(b["cases"])
        groups = L.connected_components([c["case_id"] for c in b["cases"]], edges)
        self.assertGreater(len(groups), 1)
        self.assertEqual(len(groups), len(b["lineage"]))


class SplitLeakTest(unittest.TestCase):
    def test_clean_attack_variant_in_wrong_split_fails(self):
        b = base_bundle()
        variant = _first(b["cases"], lambda c: c["relation"]["relation_type"]
                         == "invariance" and c["relation"].get("parent_case_id"))
        variant["split"] = "calibration"
        problems = build.validate_dataset(b)
        self.assertTrue(any("spans splits" in p for p in problems))
        self.assertTrue(any("partition" in p and "!=" in p for p in problems))

    def test_counterfactual_twin_in_wrong_split_fails(self):
        b = base_bundle()
        twin = _first(b["cases"], lambda c: c["relation"]["relation_type"]
                      == "counterfactual" and c["relation"].get("parent_case_id"))
        twin["split"] = "calibration"
        problems = build.validate_dataset(b)
        self.assertTrue(any("spans splits" in p for p in problems))

    def test_parent_relation_must_match_split(self):
        b = base_bundle()
        child = _first(b["cases"], lambda c: c["relation"].get("parent_case_id"))
        child["split"] = "calibration"
        problems = build.validate_dataset(b)
        self.assertTrue(any("different split" in p for p in problems))


class ReferenceLeakTest(unittest.TestCase):
    def test_unknown_gold_reference_fails(self):
        b = base_bundle()
        b["cases"][0]["gold_id"] = "gold_does_not_exist"
        self.assertTrue(any("unknown gold" in p
                            for p in build.validate_dataset(b)))

    def test_unknown_scenario_and_lineage_fail(self):
        b = base_bundle()
        b["cases"][0]["scenario_id"] = "scn_missing"
        b["cases"][0]["lineage_id"] = "lin_missing"
        problems = build.validate_dataset(b)
        self.assertTrue(any("unknown scenario" in p for p in problems))
        self.assertTrue(any("unknown lineage" in p for p in problems))

    def test_unknown_policy_reference_fails(self):
        b = base_bundle()
        b["cases"][0]["policy_id"] = "policy_missing"
        self.assertTrue(any("unknown policy" in p
                            for p in build.validate_dataset(b)))

    def test_lineage_member_must_exist(self):
        b = base_bundle()
        b["lineage"][0]["members"].append("case_missing")
        self.assertTrue(any("unknown member" in p
                            for p in build.validate_dataset(b)))


class SourceLeakTest(unittest.TestCase):
    def test_shared_source_across_splits_fails(self):
        b = base_bundle()
        scenario = b["cases"][0]["scenario_id"]
        same = [c for c in b["cases"] if c["scenario_id"] == scenario]
        same[1]["split"] = "calibration"
        problems = build.validate_dataset(b)
        self.assertTrue(any("spans splits" in p for p in problems))


class NearDuplicateTest(unittest.TestCase):
    def test_real_detection_ignores_ids(self):
        items = [
            ("case_a", "Please approve the equipment renewal before Friday. "
                       "The total is 1250 and the reference is INV-0101."),
            ("case_b", "Please approve the equipment renewal before Friday. "
                       "The total is 1250 and the reference is INV-0101."),
            ("case_c", "Your monthly newsletter is ready to read online now."),
        ]
        pairs = L.near_duplicate_pairs(items)
        self.assertIn(("case_a", "case_b"), pairs)
        self.assertFalse(any("case_c" in p for p in pairs))

    def test_duplicate_case_in_wrong_split_fails_via_content(self):
        b = base_bundle()
        source = b["cases"][0]
        clone = _clone_case(b, source, "dup9001", "calibration")
        # ids differ, content is identical: the leak must still be caught.
        self.assertNotEqual(clone["case_id"], source["case_id"])
        self.assertNotEqual(clone["lineage_id"], source["lineage_id"])
        problems = build.validate_dataset(b)
        self.assertTrue(any("near-duplicate" in p or "spans splits" in p
                            for p in problems))

    def test_near_duplicate_helper_finds_identical_content(self):
        b = base_bundle()
        source = b["cases"][0]
        clone = _clone_case(b, source, "dup9002", "calibration")
        items = [(c["case_id"], L.case_content(c)) for c in b["cases"]]
        pairs = L.near_duplicate_pairs(items)
        pair = tuple(sorted((source["case_id"], clone["case_id"])))
        self.assertIn(pair, pairs)


class CoverageAxisTest(unittest.TestCase):
    def test_persona_sharing_is_not_a_lineage_edge(self):
        # Two unrelated scenarios share the same persona/policy wording but have
        # genuinely different content; they must stay in separate components.
        cases = [
            {"case_id": "case_one_native", "lineage_id": "lin_one",
             "scenario_id": "scn_one", "persona": "employee_project_coordinator",
             "family": "request_approval", "relation": {"relation_type": "root"},
             "rendered_input": {"profile": "native", "system": "s",
                                "user": "Approve the venue booking before Friday."}},
            {"case_id": "case_two_native", "lineage_id": "lin_two",
             "scenario_id": "scn_two", "persona": "employee_project_coordinator",
             "family": "request_approval", "relation": {"relation_type": "root"},
             "rendered_input": {"profile": "native", "system": "s",
                                "user": "Your train ticket for Tuesday is confirmed."}},
        ]
        edges, near = L.case_edges(cases)
        self.assertEqual(edges, [])
        self.assertEqual(near, [])
        groups = L.connected_components(
            [c["case_id"] for c in cases], edges)
        self.assertEqual(len(groups), 2)

    def test_generic_template_sharing_is_not_a_lineage_edge(self):
        # Same family/template id, different filled content -> still separate.
        cases = [
            {"case_id": "case_a_native", "lineage_id": "lin_a",
             "scenario_id": "scn_a", "family": "receipt_confirmation",
             "relation": {"relation_type": "root"},
             "rendered_input": {"profile": "native", "system": "s",
                                "user": "Receipt INV-0100 for 120.00 to Northwind."}},
            {"case_id": "case_b_native", "lineage_id": "lin_b",
             "scenario_id": "scn_b", "family": "receipt_confirmation",
             "relation": {"relation_type": "root"},
             "rendered_input": {"profile": "native", "system": "s",
                                "user": "Ticket TKT-0444 resolved, nothing needed."}},
        ]
        edges, _ = L.case_edges(cases)
        self.assertEqual(edges, [])


if __name__ == "__main__":
    unittest.main()
