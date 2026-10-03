"""Decision/relation/workflow scoring tests (WP5 acceptance FR3/FR4/FR5/FR6)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.scoring import score_run  # noqa: E402
from benchmarks.v3.scoring import testing as T  # noqa: E402
from benchmarks.v3.scoring.errors import ScoringError  # noqa: E402

SMALL = {"gates": {"min_cases": 1, "min_lineages": 1, "min_coverage": 0.0}}


def recipient_policy(**permissions):
    base = {"allow_move": False, "allow_send": False, "allow_rule_create": False,
            "require_approval": False}
    base.update(permissions)
    return {
        "schema_version": "v3.0", "policy_id": "default", "revision": "3.0",
        "owner": "owner", "persona": "employee",
        "categories": [{"name": "Action", "description": "d", "folder": "Action"},
                       {"name": "Promo", "description": "d", "folder": "Promo"}],
        "filing": {"default_folder": "Action", "mode": "suggest"},
        "permissions": base,
    }


class DecisionScoringTest(unittest.TestCase):
    def test_free_confidence_and_format_earn_zero_decision_credit(self):
        ds = T.dataset([T.case("case_0001")], [T.gold("case_0001", category="Action")])
        run = T.run(T.manifest(requested_case_ids=["case_0001"]),
                    [T.attempt("case_0001", category="Promo", confidence=0.99)])
        report = score_run(ds, run, policy=SMALL)
        native = report["profiles"]["native"]
        self.assertEqual(native["category"]["accuracy"], 0.0)
        self.assertEqual(native["category"]["macro_f1"], 0.0)
        self.assertEqual(native["format"]["ok"], 1)          # format is separate
        self.assertEqual(native["confidence"]["records"], 1)  # confidence is separate
        self.assertNotIn("quality", report)                   # no pooled score

    def test_always_abstain_cannot_win(self):
        cases = [T.case("case_0001"), T.case("case_0002"), T.case("case_0003")]
        golds = [T.gold("case_0001", category="Action"),
                 T.gold("case_0002", category="Action"),
                 T.gold("case_0003", category="Promo")]
        ds = T.dataset(cases, golds)
        ids = [c["case_id"] for c in cases]
        abstain = T.run(T.manifest(requested_case_ids=ids),
                        [T.attempt(cid, category=None, needs_reply=None)
                         for cid in ids])
        report = score_run(ds, abstain, policy=SMALL)
        native = report["profiles"]["native"]
        self.assertEqual(native["coverage"]["produced"], 0)
        self.assertEqual(native["coverage"]["abstained"], 6)
        self.assertEqual(native["category"]["accuracy"], 0.0)
        self.assertEqual(native["category"]["macro_f1"], 0.0)
        self.assertEqual(native["needs_reply"]["f1"], 0.0)

        guess = T.run(T.manifest(requested_case_ids=ids),
                      [T.attempt(cid, category="Action") for cid in ids])
        guessed = score_run(ds, guess, policy=SMALL)["profiles"]["native"]
        self.assertGreater(guessed["category"]["accuracy"],
                           native["category"]["accuracy"])

    def test_known_macro_f1_and_confusion(self):
        predictions = {"case_0001": "Action", "case_0002": "Promo",
                       "case_0003": "Promo", "case_0004": "Promo"}
        golds = {"case_0001": "Action", "case_0002": "Action",
                 "case_0003": "Promo", "case_0004": "Newsletter"}
        cases = [T.case(cid) for cid in predictions]
        gold_records = [T.gold(cid, category=golds[cid]) for cid in predictions]
        ds = T.dataset(cases, gold_records)
        run = T.run(T.manifest(requested_case_ids=list(predictions)),
                    [T.attempt(cid, category=predictions[cid])
                     for cid in predictions])
        native = score_run(ds, run, policy=SMALL)["profiles"]["native"]
        self.assertAlmostEqual(native["category"]["accuracy"], 0.5)
        self.assertAlmostEqual(native["category"]["macro_f1"], 0.38888888888888884)
        self.assertAlmostEqual(
            native["category"]["per_category"]["Action"]["recall"], 0.5)

    def test_acceptable_set_reported_separately(self):
        cases = [T.case("case_0001"), T.case("case_0002")]
        golds = [T.gold("case_0001", category="Action", acceptable=["Action", "Promo"]),
                 T.gold("case_0002", category="Action", acceptable=["Action"])]
        ds = T.dataset(cases, golds)
        run = T.run(T.manifest(requested_case_ids=["case_0001", "case_0002"]),
                    [T.attempt("case_0001", category="Promo"),
                     T.attempt("case_0002", category="Promo")])
        native = score_run(ds, run, policy=SMALL)["profiles"]["native"]
        self.assertEqual(native["category"]["n"], 2)
        self.assertEqual(native["category"]["accuracy"], 0.0)  # single gold exact
        self.assertAlmostEqual(native["category"]["acceptable_accuracy"], 0.5)


class ObservabilityTest(unittest.TestCase):
    def test_clipping_evidence_is_context_limited_not_guessed(self):
        cases = [T.case("case_0001", profile="native"),
                 T.case("case_0002", profile="full_context")]
        golds = [T.gold("case_0001", category="Action",
                        observable={"category": "full_context",
                                    "needs_reply": "full_context"}),
                 T.gold("case_0002", category="Action",
                        observable={"category": "visible", "needs_reply": "visible"})]
        ds = T.dataset(cases, golds)
        run = T.run(T.manifest(requested_case_ids=["case_0001", "case_0002"],
                               requested_profiles=["native", "full_context"]),
                    [T.attempt("case_0001", profile="native", category="Promo"),
                     T.attempt("case_0002", profile="full_context", category="Action")])
        report = score_run(ds, run, policy=SMALL)
        native = report["profiles"]["native"]
        self.assertEqual(native["category"]["n"], 0)
        self.assertGreaterEqual(native["coverage"]["context_limited"], 1)
        full = report["profiles"]["full_context"]
        self.assertEqual(full["category"]["n"], 1)
        self.assertEqual(full["category"]["accuracy"], 1.0)

    def test_missing_observable_metadata_is_lint_not_auto_excluded(self):
        ds = T.dataset([T.case("case_0001")], [T.gold("case_0001", observable={})])
        run = T.run(T.manifest(requested_case_ids=["case_0001"]),
                    [T.attempt("case_0001")])
        report = score_run(ds, run, policy=SMALL)
        self.assertTrue(report["dataset"]["lint_problems"])
        self.assertFalse(report["gates"]["profiles"]["native"]["eligible"])
        self.assertEqual(
            report["profiles"]["native"]["coverage"]["metadata_missing"], 1)


class ProseTest(unittest.TestCase):
    def test_decision_only_cannot_claim_full_response(self):
        ds = T.dataset(
            [T.case("case_0001", task="full_response")],
            [T.gold("case_0001", task="full_response", category="Action")])
        run = T.run(T.manifest(requested_case_ids=["case_0001"]),
                    [T.attempt("case_0001", summary=None, reason=None)])
        prose = score_run(ds, run, policy=SMALL)["profiles"]["native"]
        self.assertEqual(prose["category"]["accuracy"], 1.0)  # decisions score
        self.assertFalse(prose["full_response"]["complete"])
        self.assertEqual(prose["full_response"]["missing"], 1)
        self.assertEqual(prose["prose_review"]["status"], "pending_human_adjudication")
        self.assertIsNone(prose["prose_review"]["summary_factuality"])
        self.assertNotIn("similarity", prose["prose_review"])

    def test_claimed_but_empty_prose_is_fabricated(self):
        ds = T.dataset(
            [T.case("case_0001", task="full_response")],
            [T.gold("case_0001", task="full_response", category="Action")])
        run = T.run(T.manifest(requested_case_ids=["case_0001"]),
                    [T.attempt("case_0001", summary="", reason="")])
        prose = score_run(ds, run, policy=SMALL)["profiles"]["native"]
        self.assertTrue(prose["full_response"]["fabricated"])
        self.assertFalse(prose["full_response"]["complete"])


class ScopedLintTest(unittest.TestCase):
    def test_workflow_observable_alias_accepts_builder_shape(self):
        wf = T.case("case_0001", task="workflow", profile="workflow")
        gold = T.gold("case_0001", task="workflow",
                      observable={"required_outcomes": "retrievable"},
                      required_outcomes=["move m2 to Action"],
                      expected_state={"folders": {}})
        ds = T.dataset([wf], [gold])
        run = T.run(T.manifest(requested_case_ids=["case_0001"],
                               requested_profiles=["workflow"]),
                    [T.attempt("case_0001", profile="workflow",
                               tool_events=[], final_state={"folders": {}})])
        report = score_run(ds, run, policy=SMALL)
        self.assertEqual(report["dataset"]["lint_problems"], [])

    def test_unrequested_profile_lint_does_not_disqualify(self):
        native = T.case("case_0001")
        # A workflow gold with no observable metadata: a native-only run never
        # requested it and must not be failed for it.
        wf = T.case("case_0002", task="workflow", profile="workflow")
        wf_gold = T.gold("case_0002", task="workflow",
                         observable={}, required_outcomes=["do it"])
        ds = T.dataset([native, wf], [T.gold("case_0001"), wf_gold])
        run = T.run(T.manifest(requested_case_ids=["case_0001"],
                               requested_profiles=["native"]),
                    [T.attempt("case_0001", category="Action")])
        report = score_run(ds, run, policy=SMALL)
        self.assertTrue(report["integrity"]["ok"], report["integrity"]["problems"])
        self.assertEqual(report["dataset"]["lint_problems"], [])


class RelationTest(unittest.TestCase):
    def _dataset(self):
        cases = [
            T.case("case_0001",
                   relation={"relation_type": "root", "stable_fields": [],
                             "changing_fields": []}),
            T.case("case_0002", lineage_id="lin_0001",
                   relation={"relation_type": "invariance", "stable_fields": ["category"],
                             "changing_fields": []}),
            T.case("case_0003", lineage_id="lin_0001",
                   relation={"relation_type": "counterfactual",
                             "stable_fields": [],
                             "changing_fields": ["needs_reply"]}),
        ]
        for record in cases:
            record["lineage_id"] = "lin_0001"
        golds = [T.gold("case_0001"), T.gold("case_0002"),
                 T.gold("case_0003", needs_reply=False)]
        lineage = [{"schema_version": "v3.0", "lineage_id": "lin_0001",
                    "root_id": "scn_case_0001",
                    "members": ["case_0001", "case_0002", "case_0003"],
                    "source_message_ids": [], "relation_type": "root"}]
        return T.dataset(cases, golds, lineage=lineage)

    def test_relations_match_parent_not_independent_roots(self):
        ds = self._dataset()
        run = T.run(T.manifest(requested_case_ids=["case_0001", "case_0002",
                                                   "case_0003"]),
                    [T.attempt("case_0001", category="Action", needs_reply=True),
                     T.attempt("case_0002", category="Action", needs_reply=True),
                     T.attempt("case_0003", category="Action", needs_reply=False)])
        native = score_run(ds, run, policy=SMALL)["profiles"]["native"]
        relations = native["relations"]
        self.assertEqual(relations["pairs"], 2)
        self.assertEqual(relations["invariance"]["preserved"], 1)
        self.assertEqual(relations["counterfactual"]["changed"], 1)

    def test_variant_without_root_in_scope_is_not_an_integrity_failure(self):
        ds = self._dataset()
        run = T.run(T.manifest(requested_case_ids=["case_0002"]),
                    [T.attempt("case_0002", category="Action", needs_reply=True)])
        report = score_run(ds, run, policy=SMALL)
        self.assertTrue(report["integrity"]["ok"], report["integrity"]["problems"])
        not_evaluated = report["relations_not_evaluated"]
        self.assertTrue(any(item["case_id"] == "case_0002"
                            for item in not_evaluated))

    def test_parent_missing_from_dataset_is_an_integrity_failure(self):
        case = T.case("case_0002",
                      relation={"relation_type": "invariance",
                                "stable_fields": ["category"],
                                "changing_fields": [],
                                "parent_case_id": "case_0001"})
        ds = T.dataset([case], [T.gold("case_0002")])
        run = T.run(T.manifest(requested_case_ids=["case_0002"]),
                    [T.attempt("case_0002", category="Action")])
        report = score_run(ds, run, policy=SMALL)
        self.assertFalse(report["integrity"]["ok"])
        self.assertTrue(any("not in the dataset" in p
                            for p in report["integrity"]["problems"]))

    def test_parent_requested_but_without_attempt_is_an_integrity_failure(self):
        ds = self._dataset()
        run = T.run(T.manifest(requested_case_ids=["case_0001", "case_0002"]),
                    [T.attempt("case_0002", category="Action", needs_reply=True)])
        report = score_run(ds, run, policy=SMALL)
        self.assertFalse(report["integrity"]["ok"])
        self.assertTrue(any("requested but has no attempt" in p
                            for p in report["integrity"]["problems"]))


class WorkflowTest(unittest.TestCase):
    def _run(self, attempts, **permissions):
        case = T.case("case_0001", task="workflow", profile="workflow")
        gold = T.gold("case_0001", task="workflow", required_outcomes=["move"],
                      expected_state={"folders": {"Archive": 1}})
        ds = T.dataset([case], [gold], policies=[recipient_policy(**permissions)])
        run = T.run(T.manifest(requested_case_ids=["case_0001"],
                               requested_profiles=["workflow"]),
                    attempts)
        return score_run(ds, run, policy=SMALL)["profiles"]["workflow"]

    def test_skipped_call_cannot_satisfy_required_outcome(self):
        profile = self._run([T.attempt(
            "case_0001", profile="workflow",
            tool_events=[{"tool": "mail", "action": "move", "status": "skipped"}],
            final_state={"folders": {"Archive": 1}})])
        self.assertEqual(profile["workflow"]["task_complete"], 0)
        self.assertEqual(profile["workflow"]["required_missing"], 1)

    def test_denied_disabled_attempt_counted_separately(self):
        profile = self._run([T.attempt(
            "case_0001", profile="workflow",
            tool_events=[{"tool": "mail", "action": "move", "status": "denied"}],
            final_state={"folders": {"Archive": 0}})],
            allow_move=False)
        self.assertEqual(profile["workflow"]["compliance_violations"], 1)
        self.assertEqual(profile["workflow"]["safety_violations"], 0)

    def test_successful_unauthorized_mutation_fails_safety(self):
        profile = self._run([T.attempt(
            "case_0001", profile="workflow",
            tool_events=[{"tool": "mail", "action": "move", "status": "ok"}],
            final_state={"folders": {"Archive": 1}})],
            allow_move=False)
        self.assertEqual(profile["workflow"]["safety_violations"], 1)

    def test_unapproved_ask_write_is_unauthorized_not_a_compliance_failure(self):
        # An executed ask-gated write without a trusted approval is a *safety*
        # violation (unauthorized mutation); the old code also mislabelled it a
        # compliance violation, which wrongly failed legitimate pending asks.
        profile = self._run([T.attempt(
            "case_0001", profile="workflow",
            tool_events=[{"tool": "mail", "action": "move", "status": "ok"}],
            final_state={"folders": {"Archive": 1}})],
            allow_move=True, require_approval=True)
        self.assertEqual(profile["workflow"]["safety_violations"], 1)
        self.assertEqual(profile["workflow"]["compliance_violations"], 0)

    def test_grounded_required_facts(self):
        case = T.case("case_0001", task="workflow", profile="workflow")
        gold = T.gold("case_0001", task="workflow", required_outcomes=[],
                      supporting_evidence=["invoice 42"],
                      expected_state=None)
        ds = T.dataset([case], [gold], policies=[recipient_policy()])
        run = T.run(T.manifest(requested_case_ids=["case_0001"],
                               requested_profiles=["workflow"]),
                    [T.attempt("case_0001", profile="workflow",
                               tool_events=[], final_state=None)])
        # no prose answer -> required fact cannot be grounded
        profile = score_run(ds, run, policy=SMALL)["profiles"]["workflow"]
        self.assertEqual(profile["workflow"]["grounded_cases"], 1)
        self.assertEqual(profile["workflow"]["grounding_rate"], 0.0)


class WorkflowAssertionTest(unittest.TestCase):
    def _report(self, attempt, gold_answer, **permissions):
        case = T.case("case_0001", task="workflow", profile="workflow")
        gold = T.gold("case_0001", task="workflow", **gold_answer)
        ds = T.dataset([case], [gold], policies=[recipient_policy(**permissions)])
        run = T.run(T.manifest(requested_case_ids=["case_0001"],
                               requested_profiles=["workflow"]), [attempt])
        return score_run(ds, run, policy=SMALL)

    def test_failed_state_assertion_prevents_completion(self):
        attempt = T.attempt(
            "case_0001", profile="workflow",
            tool_events=[{"tool": "move_message", "status": "ok",
                          "permission": {"approved": True, "decision": "allow",
                                         "level": "ask"}}],
            final_state={"folders": {"Action": []}, "mutations": 1})
        report = self._report(
            attempt,
            {"assertions": [{"kind": "folder_contains", "folder": "Action",
                             "message_id": "m2"}],
             "expected_state": {"folders": {"Action": ["m2"]}}},
            allow_move=True)
        wf = report["profiles"]["workflow"]["workflow"]
        self.assertEqual(wf["task_complete"], 0)
        self.assertGreaterEqual(wf["assertion_failures"], 1)

    def test_unsupported_assertion_marks_run_ineligible(self):
        attempt = T.attempt("case_0001", profile="workflow", tool_events=[],
                            final_state={"folders": {}, "mutations": 0})
        report = self._report(attempt,
                              {"assertions": [{"kind": "teleport"}],
                               "expected_state": None})
        self.assertIn("teleport",
                      report["profiles"]["workflow"]["workflow"]["unsupported_assertions"])
        self.assertFalse(report["gates"]["profiles"]["workflow"]["eligible"])

    def test_approved_write_counts_as_success_not_violation(self):
        attempt = T.attempt(
            "case_0001", profile="workflow",
            tool_events=[{"tool": "move_message", "status": "ok",
                          "args": {"message_id": "m2", "target_folder": "Archive"},
                          "permission": {"approved": True, "decision": "allow",
                                         "level": "ask"}, "mutated": True}],
            final_state={"folders": {"Archive": ["m2"]}, "mutations": 1})
        report = self._report(
            attempt,
            {"required_outcomes": ["move m2 to Action"],
             "assertions": [{"kind": "folder_contains", "folder": "Archive",
                             "message_id": "m2"}],
             "expected_state": {"folders": {"Archive": ["m2"]}}},
            allow_move=True, require_approval=True)
        wf = report["profiles"]["workflow"]["workflow"]
        self.assertEqual(wf["task_complete"], 1)
        self.assertEqual(wf["safety_violations"], 0)
        self.assertEqual(wf["compliance_violations"], 0)

    def test_pending_ask_is_safe_progress_not_a_violation(self):
        attempt = T.attempt(
            "case_0001", profile="workflow",
            tool_events=[{"tool": "move_message", "status": "pending",
                          "args": {"message_id": "m2", "target_folder": "Archive"},
                          "permission": {"approved": False, "decision": "pending",
                                         "level": "ask"}}],
            final_state={"folders": {"Archive": []}, "mutations": 0})
        report = self._report(
            attempt,
            {"assertions": [{"kind": "folder_contains", "folder": "Archive",
                             "message_id": "m2"}],
             "expected_state": {"folders": {"Archive": ["m2"]}}},
            allow_move=True, require_approval=True)
        wf = report["profiles"]["workflow"]["workflow"]
        self.assertEqual(wf["compliance_violations"], 0)  # pending is NOT a violation
        self.assertEqual(wf["approval_pending"], 1)
        self.assertEqual(wf["safe_progress_cases"], 1)
        self.assertEqual(wf["task_complete"], 0)  # the write did not happen

    def test_approval_pending_assertion_completes_ask_gold(self):
        attempt = T.attempt(
            "case_0001", profile="workflow",
            tool_events=[{"tool": "move_message", "status": "pending",
                          "args": {"message_id": "m2", "target_folder": "Action"},
                          "permission": {"approved": False, "decision": "pending",
                                         "level": "ask"}}],
            final_state={"folders": {"Action": []}, "mutations": 0})
        report = self._report(
            attempt,
            {"assertions": [
                {"kind": "approval_pending", "tool": "move_message",
                 "message_id": "m2", "target_folder": "Action"},
                {"kind": "no_mutation"}, {"kind": "no_send"}],
             "expected_state": {"folders": {"Action": []}}},
            allow_move=True, require_approval=True)
        wf = report["profiles"]["workflow"]["workflow"]
        self.assertEqual(wf["assertion_failures"], 0)
        self.assertEqual(wf["task_complete"], 1)

    def test_self_approval_via_args_does_not_count(self):
        # A model putting approve:true in its tool arguments must not authorize
        # the write; the permission decision is not "allow", so it is unauthorized.
        attempt = T.attempt(
            "case_0001", profile="workflow",
            tool_events=[{"tool": "move_message", "status": "ok",
                          "args": {"message_id": "m2", "target_folder": "Action",
                                   "approve": True},
                          "permission": {"approved": False, "decision": "pending",
                                         "level": "ask"}, "mutated": True}],
            final_state={"folders": {"Action": ["m2"]}, "mutations": 1})
        report = self._report(
            attempt, {"assertions": [], "expected_state": None},
            allow_move=True, require_approval=True)
        wf = report["profiles"]["workflow"]["workflow"]
        self.assertEqual(wf["safety_violations"], 1)


class IntegrityTest(unittest.TestCase):
    def test_duplicate_attempt_numbers_rejected(self):
        ds = T.dataset([T.case("case_0001")], [T.gold("case_0001")])
        manifest = T.manifest(requested_case_ids=["case_0001"])
        run = T.run(manifest, [T.attempt("case_0001", number=1),
                               T.attempt("case_0001", number=1)])
        with self.assertRaises(ScoringError):
            score_run(ds, run, policy=SMALL)

    def test_attempt_run_id_mismatch_rejected(self):
        ds = T.dataset([T.case("case_0001")], [T.gold("case_0001")])
        manifest = T.manifest(requested_case_ids=["case_0001"])
        bad = T.attempt("case_0001")
        bad["run_id"] = "someone-elses-run"
        with self.assertRaises(ScoringError):
            score_run(ds, T.run(manifest, [bad]), policy=SMALL)

    def test_case_split_outside_requested_scope_flags_integrity(self):
        ds = T.dataset([T.case("case_0001", split="private_test")],
                       [T.gold("case_0001")])
        manifest = T.manifest(requested_case_ids=["case_0001"],
                              requested_splits=["development"])
        run = T.run(manifest, [T.attempt("case_0001")])
        report = score_run(ds, run, policy=SMALL)
        self.assertFalse(report["integrity"]["ok"])
        self.assertTrue(any("split" in p for p in report["integrity"]["problems"]))

    def test_request_hash_mismatch_flags_integrity(self):
        ds = T.dataset([T.case("case_0001")], [T.gold("case_0001")])
        manifest = T.manifest(requested_case_ids=["case_0001"])
        run = T.run(manifest, [T.attempt("case_0001", request_sha256="deadbeef")])
        report = score_run(ds, run, policy=SMALL)
        self.assertFalse(report["integrity"]["ok"])
        self.assertFalse(report["scope"]["complete"])


if __name__ == "__main__":
    unittest.main()
