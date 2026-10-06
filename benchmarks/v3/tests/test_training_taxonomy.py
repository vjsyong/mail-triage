"""AC1: configurable taxonomy -- name-independent resolution and public prompts."""
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

    def test_taxonomy_gap(self):
        got = T.resolve_semantics(self.tax, "uncovered_purpose",
                                  "policy_conditioned")
        self.assertEqual(got["observable"], T.UNAVAILABLE)
        self.assertEqual(got["reason"], "taxonomy_gap")

    def test_unknown_label_taxonomy_resolves_identically(self):
        renamed = T.rename(self.tax, {c["id"]: "ZZ_%s" % c["id"]
                                      for c in self.tax["categories"]})
        for family in self.tax["families"]:
            base = T.resolve_semantics(self.tax, family, "policy_conditioned")
            ren = T.resolve_semantics(renamed, family, "policy_conditioned")
            self.assertEqual(base["observable"], ren["observable"], family)
            if base["observable"] == T.VISIBLE:
                self.assertIsNotNone(ren["category"], family)
                # direct check: the renamed result is the opaque label, not None
                self.assertTrue(ren["category"].startswith("ZZ_"), family)
            else:
                self.assertIsNone(ren["category"], family)

    def test_resolution_is_keyed_by_id_not_name(self):
        renamed = T.rename(self.tax, {"c_billing": "Totally Unrelated Word"})
        self.assertEqual(T.resolve_semantics(renamed, "invoice_due",
                                             "policy_conditioned")["category"],
                         "Totally Unrelated Word")


class TaxonomyTransformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tax = T.load_taxonomy()

    def test_reorder_and_remap_preserve_resolution(self):
        order = [c["id"] for c in reversed(self.tax["categories"])]
        out = T.remap_folders(T.reorder(self.tax, order), {"c_billing": "Money"})
        self.assertEqual(T.category_by_id(out, "c_billing")["folder"], "Money")
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

    def test_public_projection_drops_hidden_keys(self):
        for cat in T.public_projection(self.tax)["categories"]:
            self.assertEqual(T.hidden_category_keys(cat), [])

    def test_assert_public_clean_rejects_a_leak(self):
        public = T.public_projection(self.tax)
        public["categories"][0]["covers"] = ["action_request"]
        with self.assertRaises(T.TaxonomyError):
            T.assert_public_clean(public)

    def test_same_taxonomy_block_in_classifier_and_workflow(self):
        public = T.public_projection(self.tax)
        block = T.taxonomy_block(public)
        clf = T.render_classifier_prompt(public)
        wf = T.render_workflow_prompt(public)
        self.assertIn(block, clf)
        self.assertIn(block, wf)

    def test_definition_change_is_visible_and_resolution_stable(self):
        changed = T._copy(self.tax)
        for c in changed["categories"]:
            if c["id"] == "c_billing":
                c["definition"] = "A changed public definition for bills."
        block = T.taxonomy_block(T.public_projection(changed))
        self.assertIn("A changed public definition for bills.", block)
        self.assertEqual(T.resolve_semantics(changed, "invoice_due",
                                             "policy_conditioned")["category"],
                         "Billing")

    def test_internal_ids_never_appear_model_facing(self):
        """S2: the stable internal ids stay private; only names+definitions show."""
        public = T.public_projection(self.tax)
        clf = T.render_classifier_prompt(public)
        wf = T.render_workflow_prompt(public)
        for cat in self.tax["categories"]:
            for text in (clf, wf):
                self.assertNotIn(cat["id"], text)
                self.assertNotIn("(%s)" % cat["id"], text)
        for opaque in ("c_billing", "c_action", "c_receipt", "c_promo",
                       "c_personal", "c_security"):
            self.assertNotIn(opaque, clf)
            self.assertNotIn(opaque, wf)
        # opaque rename still shows only the new display label + definition
        renamed = T.rename(self.tax, {c["id"]: "Label_%d" % i
                                      for i, c in enumerate(self.tax["categories"])})
        renamed_clf = T.render_classifier_prompt(T.public_projection(renamed))
        self.assertIn("Label_", renamed_clf)
        for opaque in ("c_billing", "c_action"):
            self.assertNotIn(opaque, renamed_clf)
        self.assertNotIn("c_billing", renamed_clf)

    def test_public_prompt_view_has_only_name_and_definition(self):
        view = T.public_prompt_view(T.public_projection(self.tax))
        for c in view["categories"]:
            self.assertEqual(set(c), {"name", "definition"})

    def test_definition_text_is_name_independent(self):
        a = T.public_projection(self.tax)
        renamed = T.rename(self.tax, {c["id"]: "N%s" % c["id"]
                                      for c in self.tax["categories"]})
        b = T.public_projection(renamed)
        self.assertEqual([c["definition"] for c in a["categories"]],
                         [c["definition"] for c in b["categories"]])


class CrossTaskLineageTests(unittest.TestCase):
    def test_same_email_classifier_and_workflow_share_source(self):
        classifier = {"case_id": "c1", "task": "decision"}
        workflow = {"case_id": "w1", "task": "workflow"}
        lin = T.cross_task_lineage("src42", [classifier, workflow])
        self.assertEqual(classifier["source_id"], "src42")
        self.assertEqual(workflow["lineage_id"], "lin_src42")
        self.assertEqual(lin["tasks"], ["decision", "workflow"])

    def test_single_task_lineage_rejected(self):
        with self.assertRaises(T.TaxonomyError):
            T.cross_task_lineage("src", [{"case_id": "c1", "task": "decision"}])


if __name__ == "__main__":
    unittest.main()
