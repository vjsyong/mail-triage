"""AC2: fail-closed export, generation-domain separation, contamination."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.training import export as E  # noqa: E402
from benchmarks.v3.training import samples as S  # noqa: E402


def _case(cid, split="development", user="Subject: Invoice\n\nPlease pay 42.",
          domain=None):
    case = {
        "case_id": cid, "gold_id": "g_%s" % cid, "split": split,
        "input_profile": "native", "scenario_id": "scn_%s" % cid,
        "lineage_id": "lin_%s" % cid,
        "rendered_input": {"system": "sys", "user": user},
    }
    if domain:
        case["generation_domain"] = domain
    return case


def _gold(cid, category="Billing", needs_reply=False, hidden=None, source="synthetic"):
    return {
        "gold_id": "g_%s" % cid, "case_id": cid, "source": source,
        "observable": {"category": "visible" if category else "ambiguous",
                       "needs_reply": "visible"},
        "answer": {"category": category, "needs_reply": needs_reply,
                   "acceptable_categories": [category] if category else []},
        "hidden_evidence": list(hidden or []),
    }


class DomainTests(unittest.TestCase):
    def test_domain_hashes_are_distinct(self):
        specs = [E.domain_spec(d, 7) for d in
                 ("training", "development", "evaluation")]
        hashes = [s["domain_sha256"] for s in specs]
        self.assertEqual(len(set(hashes)), 3)

    def test_domain_hash_is_seed_sensitive(self):
        self.assertNotEqual(E.domain_spec("training", 1)["domain_sha256"],
                            E.domain_spec("training", 2)["domain_sha256"])

    def test_unknown_domain_raises(self):
        with self.assertRaises(E.ExportError):
            E.domain_spec("test", 0)


class FailClosedTests(unittest.TestCase):
    def test_private_calibration_test_rejected(self):
        cases = [_case("d1"), _case("c1", split="calibration"),
                 _case("t1", split="test"), _case("p1", split="private_test")]
        golds = [_gold(c["case_id"]) for c in cases]
        res = E.export_decision_cases(cases, golds, domain="training", seed=1,
                                      allowed_splits=E.TRAINING_ALLOWED)
        self.assertEqual([e["case_id"] for e in res["accepted"]], ["d1"])
        reasons = {r["case_id"]: r["reason"] for r in res["rejected"]}
        self.assertTrue(reasons["c1"].startswith("split_not_allowed"))
        self.assertTrue(reasons["t1"].startswith("split_not_allowed"))
        self.assertTrue(reasons["p1"].startswith("split_not_allowed"))

    def test_ambiguous_and_unavailable_gold_rejected(self):
        cases = [_case("d1"), _case("d2")]
        golds = [_gold("d1", category="Billing"),
                 _gold("d2", category=None)]  # ambiguous
        res = E.export_decision_cases(cases, golds, domain="training", seed=1,
                                      allowed_splits=E.TRAINING_ALLOWED)
        self.assertEqual([e["case_id"] for e in res["accepted"]], ["d1"])
        self.assertEqual(res["rejected"][0]["reason"],
                         "unobservable_decision_gold")

    def test_real_mail_material_rejected(self):
        res = E.export_decision_cases(
            [_case("r1")], [_gold("r1", source="real_mail")],
            domain="training", seed=1, allowed_splits=E.TRAINING_ALLOWED)
        self.assertEqual(res["accepted"], [])
        self.assertEqual(res["rejected"][0]["reason"], "real_mail_material")

    def test_mixed_generation_domain_rejected(self):
        cases = [_case("d1", domain="evaluation")]
        golds = [_gold("d1")]
        res = E.export_decision_cases(cases, golds, domain="training", seed=1,
                                      allowed_splits=E.TRAINING_ALLOWED)
        self.assertEqual(res["accepted"], [])
        self.assertEqual(res["rejected"][0]["reason"], "mixed_generation_domain")

    def test_gold_hidden_evidence_in_input_rejected(self):
        cases = [_case("d1", user="Subject: Invoice\n\nThe overdue invoice is here.")]
        golds = [_gold("d1", hidden=["overdue invoice"])]
        res = E.export_decision_cases(cases, golds, domain="training", seed=1,
                                      allowed_splits=E.TRAINING_ALLOWED)
        self.assertEqual(res["accepted"], [])
        self.assertTrue(res["rejected"][0]["reason"].startswith("contamination"))


class WorkflowExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenarios = S.load_workflow_scenarios()["scenarios"]

    def test_workflow_export_and_verify(self):
        res = E.export_workflow_scenarios(self.scenarios, domain="training", seed=1)
        self.assertEqual(len(res["accepted"]), len(self.scenarios))
        self.assertEqual(res["rejected"], [])

    def test_contamination_report_flags_shared_source(self):
        a = E.export_workflow_scenarios(self.scenarios[:1], domain="training",
                                        seed=1)
        # same scenario exported under a different domain -> shared source_id
        b = E.export_workflow_scenarios(self.scenarios[:1], domain="evaluation",
                                        seed=2)
        problems = E.contamination_report([a, b])
        self.assertTrue(any("source_id" in p or "source text" in p
                            for p in problems), problems)

    def test_contamination_report_clean_for_disjoint_sources(self):
        a = E.export_workflow_scenarios(self.scenarios[:1], domain="training",
                                        seed=1)
        b = E.export_workflow_scenarios(self.scenarios[1:], domain="evaluation",
                                        seed=2)
        self.assertEqual(E.contamination_report([a, b]), [])

    def test_workflow_source_text_included_in_contamination(self):
        a = E.export_workflow_scenarios(self.scenarios, domain="training", seed=1)
        for example in a["accepted"]:
            self.assertIn("workflow_source_text", example)
            self.assertTrue(example["workflow_source_text"])


if __name__ == "__main__":
    unittest.main()
