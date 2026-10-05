"""AC1: configurable taxonomy -- name-independent resolution and projection."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.training import taxonomies as T  # noqa: E402


class TaxonomyResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tax = T.load_taxonomy()

    def test_visible_by_semantic_intent(self):
        got = T.resolve_semantics(self.tax, "invoice_due", "policy_conditioned")
        self.assertEqual(got["observable"], T.VISIBLE)
        self.assertEqual(got["category"], "Billing")

    def test_ambiguous_multiple_intents(self):
        got = T.resolve_semantics(self.tax, "action_and_billing",
                                  "policy_conditioned")
        self.assertEqual(got["observable"], T.AMBIGUOUS)
        self.assertIsNone(got["category"])
        self.assertEqual(sorted(got["acceptable"]), ["Action", "Billing"])

    def test_taxonomy_gap(self):
        got = T.resolve_semantics(self.tax, "uncovered_purpose",
                                  "policy_conditioned")
        self.assertEqual(got["observable"], T.UNAVAILABLE)
        self.assertEqual(got["reason"], "taxonomy_gap")

    def test_native_hides_definition_when_not_self_evident(self):
        got = T.resolve_semantics(self.tax, "task_request", "native",
                                  by_name=False)
        self.assertEqual(got["observable"], T.AMBIGUOUS)
        self.assertEqual(got["reason"], "requires_definition")

    def test_unknown_label_taxonomy_resolves_identically(self):
        """Renaming every display name must not change any resolution."""
        renamed = T.rename(self.tax, {c["id"]: "ZZ_%s" % c["id"]
                                      for c in self.tax["categories"]})
        for family in self.tax["families"]:
            base = T.resolve_semantics(self.tax, family, "policy_conditioned")
            renamed_res = T.resolve_semantics(renamed, family,
                                              "policy_conditioned")
            self.assertEqual(base["observable"], renamed_res["observable"],
                             family)
            if base["observable"] == T.VISIBLE:
                self.assertIsNotNone(renamed_res["category"], family)
            else:
                self.assertIsNone(renamed_res["category"], family)

    def test_resolution_is_keyed_by_id_not_name(self):
        renamed = T.rename(self.tax, {"c_billing": "Totally Unrelated Word"})
        got = T.resolve_semantics(renamed, "invoice_due", "policy_conditioned")
        self.assertEqual(got["category"], "Totally Unrelated Word")


class TaxonomyTransformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tax = T.load_taxonomy()

    def test_reorder_preserves_resolution_and_semantics(self):
        order = [c["id"] for c in reversed(self.tax["categories"])]
        out = T.reorder(self.tax, order)
        self.assertEqual([c["id"] for c in out["categories"]], order)
        self.assertEqual(T.resolve_semantics(out, "invoice_due",
                                             "policy_conditioned")["category"],
                         "Billing")

    def test_remap_folders_changes_folder_only(self):
        out = T.remap_folders(self.tax, {"c_billing": "Money"})
        cat = T.category_by_id(out, "c_billing")
        self.assertEqual(cat["folder"], "Money")
        self.assertEqual(T.resolve_semantics(out, "invoice_due",
                                             "policy_conditioned")["category"],
                         "Billing")

    def test_hidden_facts_cannot_reach_public_prompt(self):
        public = T.public_projection(self.tax)
        T.assert_public_clean(public)
        prompt = T.render_classifier_prompt(public, owner="Owner")
        for hidden in ("covers", "roles", "authoring_notes"):
            self.assertNotIn(hidden, prompt)
        for cat in self.tax["categories"]:
            for role in cat.get("roles") or []:
                self.assertNotIn('"%s"' % role, prompt)
        self.assertNotIn("authoring-only", prompt)

    def test_public_projection_drops_hidden_keys(self):
        public = T.public_projection(self.tax)
        for cat in public["categories"]:
            self.assertEqual(T.hidden_category_keys(cat), [])

    def test_assert_public_clean_rejects_a_leak(self):
        public = T.public_projection(self.tax)
        public["categories"][0]["covers"] = ["action_request"]
        with self.assertRaises(T.TaxonomyError):
            T.assert_public_clean(public)

    def test_definition_text_is_name_independent(self):
        public_a = T.public_projection(self.tax)
        renamed = T.rename(self.tax, {c["id"]: "N%s" % c["id"]
                                      for c in self.tax["categories"]})
        public_b = T.public_projection(renamed)
        defs_a = [c["definition"] for c in public_a["categories"]]
        defs_b = [c["definition"] for c in public_b["categories"]]
        self.assertEqual(defs_a, defs_b)


class CrossTaskLineageTests(unittest.TestCase):
    def test_same_email_classifier_and_workflow_share_source(self):
        classifier = {"case_id": "c1", "task": "decision"}
        workflow = {"case_id": "w1", "task": "workflow"}
        lin = T.cross_task_lineage("src42", [classifier, workflow])
        self.assertEqual(classifier["source_id"], "src42")
        self.assertEqual(workflow["lineage_id"], "lin_src42")
        self.assertEqual(classifier["lineage_id"], workflow["lineage_id"])
        self.assertEqual(lin["tasks"], ["decision", "workflow"])

    def test_single_task_lineage_rejected(self):
        with self.assertRaises(T.TaxonomyError):
            T.cross_task_lineage("src", [{"case_id": "c1", "task": "decision"}])


if __name__ == "__main__":
    unittest.main()
