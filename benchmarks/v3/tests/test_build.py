"""Dataset builder acceptance tests (WP2/WP6).

Covers determinism (FR2/FR3/FR7), native clipping, visible-gold consistency,
injection tagging, profile-card separation, pilot breadth/denominators, review
honesty, and the private/write boundaries.
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3 import build, contracts, schema  # noqa: E402
from benchmarks.v3.build import lineage as L  # noqa: E402
from benchmarks.v3.build.errors import PrivateExportError, WriteRefused  # noqa: E402
from benchmarks.v3.common.hashing import hash_obj  # noqa: E402

_DEFAULT = None


def default_bundle():
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = build.build_dataset()
    return _DEFAULT


def gold_of(bundle, case):
    for g in bundle["gold"]:
        if g["gold_id"] == case["gold_id"]:
            return g
    raise AssertionError("no gold for %s" % case["case_id"])


class DeterminismTest(unittest.TestCase):
    def test_repeated_builds_identical(self):
        a = build.build_dataset(triage_roots=12, workflow_roots=2, seed=5)
        b = build.build_dataset(triage_roots=12, workflow_roots=2, seed=5)
        self.assertEqual(hash_obj(a), hash_obj(b))
        self.assertEqual(a["dataset_id"], b["dataset_id"])

    def test_seed_changes_content(self):
        a = build.build_dataset(triage_roots=12, workflow_roots=2, seed=5)
        b = build.build_dataset(triage_roots=12, workflow_roots=2, seed=6)
        self.assertNotEqual(hash_obj(a), hash_obj(b))
        self.assertNotEqual(a["dataset_id"], b["dataset_id"])

    def test_include_variants_toggle(self):
        full = build.build_dataset(triage_roots=8, workflow_roots=1, seed=2,
                                   include_variants=True)
        bare = build.build_dataset(triage_roots=8, workflow_roots=1, seed=2,
                                   include_variants=False)
        self.assertGreater(len(full["cases"]), len(bare["cases"]))
        self.assertTrue(all(c["relation"]["relation_type"] == "root"
                            for c in bare["cases"] if c["task"] == "decision"))


class NativeClippingTest(unittest.TestCase):
    def test_clip_evidence_beyond_native_boundary(self):
        b = default_bundle()
        clips = [c for c in b["cases"]
                 if c["relation"]["relation_type"] == "clip_variant"
                 and c["input_profile"] == contracts.NATIVE_PROFILE]
        self.assertTrue(clips, "expected clip variants")
        for case in clips:
            gold = gold_of(b, case)
            body = case["rendered_input"]["user"].split("\n\n", 1)[1]
            self.assertLessEqual(len(body), contracts.SNIPPET_LIMIT)
            for hidden in gold["hidden_evidence"]:
                self.assertNotIn(hidden.lower(),
                                 case["rendered_input"]["user"].lower())
            self.assertEqual(gold["observable"]["category"],
                             contracts.OBSERVABILITY_FULL_CONTEXT)
            self.assertEqual(gold["observable"]["needs_reply"],
                             contracts.OBSERVABILITY_FULL_CONTEXT)

    def test_full_context_twin_sees_evidence(self):
        b = default_bundle()
        clips = [c for c in b["cases"]
                 if c["relation"]["relation_type"] == "clip_variant"
                 and c["input_profile"] == contracts.NATIVE_PROFILE]
        for case in clips:
            gold = gold_of(b, case)
            twin = next(c for c in b["cases"]
                       if c["scenario_id"] == case["scenario_id"]
                       and c["input_profile"] == contracts.FULL_CONTEXT_PROFILE)
            for hidden in gold["hidden_evidence"]:
                self.assertIn(hidden.lower(),
                              twin["rendered_input"]["user"].lower())
            self.assertEqual(gold_of(b, twin)["observable"]["category"],
                             contracts.OBSERVABILITY_VISIBLE)

    def test_native_has_no_clipping_marker(self):
        b = default_bundle()
        for case in b["cases"]:
            if case["input_profile"] == contracts.NATIVE_PROFILE:
                self.assertNotIn("[...", case["rendered_input"]["user"])
                self.assertNotIn("clipped", case["rendered_input"]["user"].lower())


class VisibleGoldTest(unittest.TestCase):
    def test_visible_category_evidence_present_in_input(self):
        b = default_bundle()
        checked = 0
        for case in b["cases"]:
            if case["task"] == "workflow":
                continue
            gold = gold_of(b, case)
            if gold["observable"].get("category") != contracts.OBSERVABILITY_VISIBLE:
                continue
            checked += 1
            answer = gold["answer"]
            self.assertIn(answer["category"], answer["acceptable_categories"])
            user = case["rendered_input"]["user"].lower()
            self.assertTrue(
                any(ev.lower() in user for ev in answer["supporting_evidence"]),
                "visible gold %s has no supporting evidence in input"
                % case["case_id"])
        self.assertGreater(checked, 100)

    def test_ambiguous_native_has_acceptable_set_not_scored(self):
        b = default_bundle()
        amb = [c for c in b["cases"]
               if c["task"] == "decision"
               and gold_of(b, c)["observable"].get("category")
               == contracts.OBSERVABILITY_AMBIGUOUS]
        self.assertTrue(amb)
        for case in amb:
            gold = gold_of(b, case)
            self.assertIsNone(gold["answer"]["category"])
            self.assertTrue(gold["answer"]["acceptable_categories"])
            self.assertFalse(contracts.observable_in(
                contracts.OBSERVABILITY_AMBIGUOUS, case["input_profile"]))


class InjectionTest(unittest.TestCase):
    def test_injection_has_clean_and_changing_tags(self):
        b = default_bundle()
        injections = [c for c in b["cases"] if "injection" in c["tags"]]
        self.assertTrue(injections)
        for case in injections:
            self.assertTrue(any(t.startswith("clean_pair:") for t in case["tags"]))
            self.assertTrue(any(t.startswith("changed:") for t in case["tags"]))
            self.assertIn("injected_instruction",
                          case["relation"]["changing_fields"])
            self.assertIn("category", case["relation"]["stable_fields"])
            self.assertIn("needs_reply", case["relation"]["stable_fields"])
            self.assertIn("[System note", case["rendered_input"]["user"])

    def test_injection_does_not_change_gold(self):
        b = default_bundle()
        injections = [c for c in b["cases"] if "injection" in c["tags"]]
        for case in injections:
            root = next(c for c in b["cases"]
                        if c["case_id"] == case["relation"]["parent_case_id"])
            self.assertEqual(gold_of(b, case)["answer"]["category"],
                             gold_of(b, root)["answer"]["category"])
            self.assertEqual(gold_of(b, case)["answer"]["needs_reply"],
                             gold_of(b, root)["answer"]["needs_reply"])


class ProfileCardTest(unittest.TestCase):
    def test_policy_case_carries_trusted_card_only(self):
        b = default_bundle()
        policy_cases = [c for c in b["cases"]
                        if c["input_profile"] == contracts.POLICY_PROFILE]
        native_cases = [c for c in b["cases"]
                        if c["input_profile"] in (contracts.NATIVE_PROFILE,
                                                  contracts.FULL_CONTEXT_PROFILE)]
        self.assertTrue(policy_cases)
        for case in policy_cases:
            rendered = case["rendered_input"]
            self.assertIn("policy", rendered)
            self.assertEqual(rendered["policy_id"], case["policy_id"])
            self.assertEqual(rendered["policy"]["policy_id"], case["policy_id"])
        for case in native_cases:
            self.assertNotIn("policy", case["rendered_input"])

    def test_policy_card_never_pooled_with_native(self):
        b = default_bundle()
        for case in b["cases"]:
            if case["task"] == "workflow":
                continue
            self.assertIn(case["input_profile"], contracts.TRIAGE_PROFILES)
            self.assertEqual(case["rendered_input"]["profile"],
                             case["input_profile"])


class PilotCoverageTest(unittest.TestCase):
    def test_root_denominators(self):
        b = default_bundle()
        counts = b["metadata"]["counts"]
        self.assertEqual(counts["triage_roots"], 200)
        self.assertEqual(counts["workflow_roots"], 30)
        self.assertEqual(counts["scenarios"], 230)
        self.assertEqual(counts["lineages"], 230)

    def test_breadth_and_balance(self):
        b = default_bundle()
        coverage = b["metadata"]["coverage"]
        self.assertEqual(len(coverage["personas"]), 8)
        self.assertGreaterEqual(len(coverage["families"]), 15)
        roots = [c for c in b["cases"]
                 if c["task"] == "decision"
                 and c["relation"]["relation_type"] == "root"]
        self.assertEqual(len(roots), 200)
        positives = sum(1 for c in roots
                        if gold_of(b, c)["answer"]["needs_reply"])
        ratio = positives / float(len(roots))
        self.assertGreater(ratio, 0.35)
        self.assertLess(ratio, 0.65)
        categories = {gold_of(b, c)["answer"]["category"] for c in roots}
        self.assertGreaterEqual(len(categories), 4)

    def test_every_case_is_schema_valid(self):
        b = default_bundle()
        self.assertEqual(build.validate_dataset(b), [])

    def test_every_record_schema_valid(self):
        b = default_bundle()
        for kind, key in (("case", "cases"), ("gold", "gold"),
                          ("scenario", "scenarios"), ("policy", "policies"),
                          ("lineage", "lineage"), ("provenance", "provenance")):
            for rec in b[key]:
                self.assertEqual(schema.validate_artifact(kind, rec), [],
                                 "%s %s" % (kind, rec.get(key + "_id")))


class HonestyTest(unittest.TestCase):
    def test_fresh_build_is_draft_and_unsealed(self):
        b = default_bundle()
        meta = b["metadata"]
        self.assertEqual(meta["review_status"], "draft")
        self.assertFalse(meta["human_review_performed"])
        self.assertFalse(meta["real_mail_authorized"])
        self.assertEqual(meta["visibility"], "public")
        self.assertFalse(meta["contains_private"])
        for gold in b["gold"]:
            self.assertEqual(gold["review_status"], "draft")
            self.assertFalse(gold["human_seal"])
            self.assertFalse(gold["authorized"])
            self.assertEqual(gold["source"], "synthetic")

    def test_catalog_is_metadata_only_and_unauthorized(self):
        b = default_bundle()
        catalog = [p for p in b["provenance"] if p["source"] == "public_corpus"]
        self.assertEqual({p["provenance_id"] for p in catalog},
                         {"prov_catalog_enron", "prov_catalog_ietf",
                          "prov_catalog_spamassassin", "prov_catalog_nazario"})
        for prov in catalog:
            self.assertEqual(prov["retrieval"], "metadata_only")
            self.assertFalse(prov["authorization"]["authorized"])

    def test_unauthorized_corpus_scenario_fails_lint(self):
        b = copy.deepcopy(build.build_dataset(triage_roots=4, workflow_roots=1,
                                              seed=1))
        b["scenarios"][0]["source_provenance"] = "prov_catalog_enron"
        problems = build.validate_dataset(b)
        self.assertTrue(any("not authorized" in p for p in problems))


class WriteLoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v3-build-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip(self):
        b = build.build_dataset(triage_roots=8, workflow_roots=2, seed=3)
        paths = build.write_dataset(b, os.path.join(self.tmp, "pub"))
        self.assertTrue(os.path.isfile(paths["public"]["bundle"]))
        loaded = build.load_dataset(os.path.join(self.tmp, "pub"))
        self.assertEqual(hash_obj(loaded), hash_obj(b))

    def test_unintended_overwrite_refused(self):
        p = os.path.join(self.tmp, "pub")
        build.write_dataset(build.build_dataset(triage_roots=8, workflow_roots=2,
                                                seed=3), p)
        with self.assertRaises(WriteRefused):
            build.write_dataset(build.build_dataset(triage_roots=8,
                                                    workflow_roots=2, seed=4), p)

    def test_intended_rebuild_allowed(self):
        p = os.path.join(self.tmp, "pub")
        b = build.build_dataset(triage_roots=8, workflow_roots=2, seed=3)
        build.write_dataset(b, p)
        build.write_dataset(build.build_dataset(triage_roots=8, workflow_roots=2,
                                                seed=3), p)
        self.assertEqual(build.load_dataset(p)["dataset_id"], b["dataset_id"])

    def _private_bundle(self):
        layout = {"triage": {"development": 8, "calibration": 4,
                             "private_test": 6, "private_shift": 4},
                  "workflow": {"development": 3, "private_test": 3,
                               "private_shift": 2}}
        return build.build_dataset(layout=layout, seed=5, private_seed=99)

    def test_private_export_blocked_without_root(self):
        b = self._private_bundle()
        self.assertTrue(b["metadata"]["contains_private"])
        with self.assertRaises(PrivateExportError):
            build.write_dataset(b, os.path.join(self.tmp, "pub"))

    def test_private_root_inside_checkout_refused(self):
        b = self._private_bundle()
        inside = os.path.join(V3, "build", "_nope_private")
        with self.assertRaises(PrivateExportError):
            build.write_dataset(b, os.path.join(self.tmp, "pub"), private_root=inside)

    def test_private_records_never_in_public_path(self):
        b = self._private_bundle()
        paths = build.write_dataset(b, os.path.join(self.tmp, "pub"),
                                    private_root=os.path.join(self.tmp, "priv"))
        self.assertIn("private", paths)
        public = build.load_dataset(os.path.join(self.tmp, "pub"))
        self.assertFalse(public["metadata"]["contains_private"])
        self.assertEqual(public["metadata"]["visibility"], "public")
        splits = {c["split"] for c in public["cases"]}
        self.assertEqual(splits, {"development"})
        private = build.load_dataset(os.path.join(self.tmp, "priv"))
        self.assertTrue(private["metadata"]["contains_private"])
        self.assertFalse({"development"} & {c["split"] for c in private["cases"]})

    def test_load_rejects_tampered_bundle(self):
        import json
        b = build.build_dataset(triage_roots=6, workflow_roots=1, seed=3)
        p = os.path.join(self.tmp, "pub")
        build.write_dataset(b, p)
        with open(os.path.join(p, "bundle.json")) as f:
            data = json.load(f)
        data["metadata"]["visibility"] = "private"
        with open(os.path.join(p, "bundle.json"), "w") as f:
            json.dump(data, f)
        from benchmarks.v3.common.validation import ValidationError
        with self.assertRaises(ValidationError):
            build.load_dataset(p)


class PublicExportTest(unittest.TestCase):
    def test_public_export_strips_private(self):
        layout = {"triage": {"development": 6, "calibration": 4,
                             "private_test": 4, "private_shift": 2},
                  "workflow": {"development": 2, "private_test": 2}}
        b = build.build_dataset(layout=layout, seed=5, private_seed=9)
        public = build.public_export(b)
        self.assertFalse(public["metadata"]["contains_private"])
        self.assertEqual(public["metadata"]["visibility"], "public")
        self.assertEqual({c["split"] for c in public["cases"]}, {"development"})

    def test_public_export_never_carries_real_mail(self):
        b = build.build_dataset(triage_roots=4, workflow_roots=1, seed=5)
        b["gold"][0]["source"] = "real_mail"
        b["gold"][0]["authorized"] = True
        with self.assertRaises(PrivateExportError):
            build.public_export(b)


class LayoutShiftTest(unittest.TestCase):
    def test_shift_axes_one_at_a_time(self):
        layout = {"triage": {"development": 10, "calibration": 4,
                             "private_test": 6, "private_shift": 6},
                  "workflow": {"development": 3, "private_test": 3,
                               "private_shift": 2}}
        b = build.build_dataset(layout=layout, seed=11, private_seed=22)
        shift_cases = [c for c in b["cases"]
                       if c["split"] == "private_shift"]
        self.assertTrue(shift_cases)
        for case in shift_cases:
            axes = [t.split(":", 1)[1] for t in case["tags"]
                    if t.startswith("shift:") and t != "shift:none"]
            self.assertEqual(len(axes), 1, "%s has axes %s" % (case["case_id"], axes))
        axes_seen = set(b["metadata"]["coverage"]["shift_axes"])
        self.assertTrue(axes_seen)
        self.assertTrue(axes_seen.issubset(
            {"unseen_policy_combo", "unseen_template_family",
             "source_style_shift"}))

    def test_full_layout_selectable_by_mapping(self):
        layout = {"triage": {"development": 4, "calibration": 2,
                             "private_test": 2, "private_shift": 2},
                  "workflow": {"development": 2, "private_test": 2,
                               "private_shift": 2}}
        b = build.build_dataset(layout=layout, seed=1, private_seed=2)
        self.assertEqual(b["metadata"]["layout"], "custom")
        self.assertEqual(b["metadata"]["split_plan"]["triage"]["private_test"], 2)


if __name__ == "__main__":
    unittest.main()
