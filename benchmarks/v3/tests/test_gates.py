"""Qualification gate tests (WP5 FR8)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.scoring import gates, score_run  # noqa: E402
from benchmarks.v3.scoring import testing as T  # noqa: E402


def bundle(*, n=20, split="private_test", review_status="reviewed",
           correct=True, real_mail=False, abstain=False):
    cases = []
    golds = []
    attempts = []
    ids = []
    for i in range(n):
        cid = "case_%04d" % i
        ids.append(cid)
        cases.append(T.case(cid, split=split, lineage_id="lin_%02d" % (i % 10)))
        gold = T.gold(cid, category="Action", review_status=review_status)
        if real_mail:
            gold.update({"source": "real_mail", "review_status": "sealed",
                         "human_seal": True, "reviewer": "human", "authorized": False})
        golds.append(gold)
        if abstain:
            attempts.append(T.attempt(cid, category=None, needs_reply=None))
        else:
            attempts.append(T.attempt(
                cid, category="Action" if correct else "Promo",
                needs_reply=True, confidence=0.9))
    ds = T.dataset(cases, golds, metadata={"review_status": review_status})
    manifest = T.manifest(requested_case_ids=ids, requested_splits=[split],
                          requested_profiles=["native"])
    return ds, T.run(manifest, attempts)


class ProfileGateTest(unittest.TestCase):
    def test_reviewed_test_scope_is_final_qualified(self):
        report = score_run(*bundle())
        native = report["gates"]["profiles"]["native"]
        self.assertTrue(native["eligible"], native["reasons"])
        self.assertTrue(report["gates"]["final_test_qualified"])

    def test_draft_test_scope_never_final_qualified(self):
        report = score_run(*bundle(review_status="draft"))
        self.assertFalse(report["gates"]["final_test_qualified"])
        self.assertFalse(report["gates"]["profiles"]["native"]["eligible"])
        self.assertTrue(any("reviewed" in reason
                            for reason in report["gates"]["profiles"]["native"]["reasons"]))

    def test_draft_development_is_exploratory(self):
        report = score_run(*bundle(split="development", review_status="draft"))
        self.assertFalse(report["gates"]["final_test_qualified"])
        self.assertTrue(report["gates"]["exploratory_development"])

    def test_always_abstain_fails_coverage(self):
        report = score_run(*bundle(abstain=True))
        native = report["gates"]["profiles"]["native"]
        self.assertFalse(native["eligible"])
        self.assertTrue(any("coverage" in reason for reason in native["reasons"]),
                        native["reasons"])

    def test_wrong_decisions_fail_quality_not_format(self):
        report = score_run(*bundle(correct=False))
        native = report["gates"]["profiles"]["native"]
        self.assertFalse(native["eligible"])
        self.assertTrue(any("macro-F1" in reason or "reply F1" in reason
                            for reason in native["reasons"]), native["reasons"])
        self.assertNotIn("quality", report)

    def test_real_mail_needs_authorization(self):
        report = score_run(*bundle(real_mail=True))
        native = report["gates"]["profiles"]["native"]
        self.assertFalse(native["eligible"])
        self.assertTrue(any("real-mail" in reason for reason in native["reasons"]),
                        native["reasons"])


class WorkflowGateTest(unittest.TestCase):
    GATE_POLICY = {"gates": {"min_cases": 1, "min_lineages": 1}}

    def _policy_card(self, allow_move):
        return {
            "schema_version": "v3.0", "policy_id": "default", "revision": "3.0",
            "owner": "o", "categories": [{"name": "Action", "description": "d",
                                          "folder": "Action"}],
            "filing": {"default_folder": "Action", "mode": "suggest"},
            "permissions": {"allow_move": allow_move, "allow_send": False,
                            "allow_rule_create": False, "require_approval": False},
        }

    def _report(self, allow_move, status="ok"):
        case = T.case("case_0001", task="workflow", profile="workflow",
                      split="private_test")
        gold = T.gold("case_0001", task="workflow", required_outcomes=["move"],
                      expected_state={"folders": {"Archive": 1}})
        ds = T.dataset([case], [gold], policies=[self._policy_card(allow_move)],
                       metadata={"review_status": "reviewed"})
        manifest = T.manifest(requested_case_ids=["case_0001"],
                              requested_splits=["private_test"],
                              requested_profiles=["workflow"])
        run = T.run(manifest, [T.attempt(
            "case_0001", profile="workflow",
            tool_events=[{"tool": "mail", "action": "move", "status": status}],
            final_state={"folders": {"Archive": 1}})])
        return score_run(ds, run, policy=self.GATE_POLICY)

    def test_completed_safe_workflow_is_eligible(self):
        report = self._report(allow_move=True)
        self.assertTrue(report["gates"]["profiles"]["workflow"]["eligible"],
                        report["gates"]["profiles"]["workflow"]["reasons"])

    def test_unauthorized_workflow_mutation_is_ineligible(self):
        report = self._report(allow_move=False)
        reasons = report["gates"]["profiles"]["workflow"]["reasons"]
        self.assertFalse(report["gates"]["profiles"]["workflow"]["eligible"])
        self.assertTrue(any("safety" in reason for reason in reasons), reasons)


class CpuQualificationTest(unittest.TestCase):
    def setUp(self):
        from benchmarks.v3.scoring import default_policy
        self.policy = default_policy()

    def test_config_only_numbers_never_qualify(self):
        result = gates.qualify_cpu(
            {"state": "configured", "metrics": {"cores": 4, "ram_gib": 8}}, self.policy)
        self.assertFalse(result["cpu_qualified"])

    def test_verified_receipt_qualifies(self):
        result = gates.qualify_cpu(
            {"state": "measured", "hardware_receipt": {
                "verified": True, "cpu_model": "Ryzen 7", "shared": False}},
            self.policy)
        self.assertTrue(result["cpu_qualified"], result["reasons"])

    def test_shared_or_warm_infrastructure_does_not_qualify(self):
        result = gates.qualify_cpu(
            {"hardware_receipt": {"verified": True, "cpu_model": "X",
                                  "warm_endpoint": True}}, self.policy)
        self.assertFalse(result["cpu_qualified"])

    def test_report_without_receipt_is_cpu_ineligible(self):
        report = score_run(*bundle())
        self.assertFalse(report["gates"]["deployment"]["cpu_qualified"])
        self.assertEqual(report["gates"]["deployment"]["state"], "not_measured")


class ComparisonGateTest(unittest.TestCase):
    def setUp(self):
        from benchmarks.v3.scoring import default_policy
        self.policy = default_policy()

    def test_incomplete_or_cluster_poor_comparison_not_estimable(self):
        comparison = {"scope": {"complete": False, "n_roots": 3}}
        result = gates.qualify_comparison(comparison, self.policy)
        self.assertFalse(result["eligible"])
        self.assertFalse(result["estimable"])

    def _runs(self, mock=False, source="pinned", cpu=False):
        return {"baseline": {"mock": mock, "model_identity_source": source},
                "candidate": {"mock": mock, "model_identity_source": source,
                              "cpu_qualified": cpu}}

    def test_adequate_comparison_eligible(self):
        comparison = {"scope": {"complete": True, "n_roots": 12},
                      "runs": self._runs(), "profiles": {},
                      "dataset": {"review_status": "reviewed"}}
        result = gates.qualify_comparison(comparison, self.policy)
        self.assertTrue(result["eligible"])
        self.assertTrue(result["quality_eligible"])
        self.assertTrue(result["estimable"])

    def test_mock_or_draft_or_unverified_comparison_not_qualified(self):
        comparison = {"scope": {"complete": True, "n_roots": 12},
                      "runs": self._runs(mock=True), "profiles": {},
                      "dataset": {"review_status": "reviewed"}}
        result = gates.qualify_comparison(comparison, self.policy)
        self.assertFalse(result["eligible"])
        self.assertFalse(result["quality_eligible"])
        self.assertTrue(result["estimable"])  # exploratory statistics still usable
        self.assertTrue(result["exploratory"])
        self.assertIn("mock", " ".join(result["reasons"]))

        unseen = {"scope": {"complete": True, "n_roots": 12},
                  "runs": self._runs(source="unverified"), "profiles": {},
                  "dataset": {"review_status": "reviewed"}}
        self.assertFalse(gates.qualify_comparison(unseen, self.policy)["eligible"])

        draft = {"scope": {"complete": True, "n_roots": 12},
                 "runs": self._runs(), "profiles": {},
                 "dataset": {"review_status": "draft"}}
        self.assertFalse(gates.qualify_comparison(draft, self.policy)["eligible"])

    def test_quality_eligible_but_deployment_separate(self):
        comparison = {"scope": {"complete": True, "n_roots": 12},
                      "runs": self._runs(cpu=False), "profiles": {},
                      "dataset": {"review_status": "reviewed"}}
        result = gates.qualify_comparison(comparison, self.policy)
        self.assertTrue(result["quality_eligible"])
        self.assertFalse(result["deployment_eligible"])
        self.assertTrue(result["reasons_by_dimension"]["deployment"])


if __name__ == "__main__":
    unittest.main()
