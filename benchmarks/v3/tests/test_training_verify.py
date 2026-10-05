"""M1 negative regressions: state/outcome/scope and decision verification."""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.training import samples as S, verify as V  # noqa: E402
from benchmarks.v3.training.taxonomies import load_taxonomy  # noqa: E402


def _dialogues():
    return copy.deepcopy(S.load_workflow_scenarios()["dialogues"])


class DialogueVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tax = load_taxonomy()
        self.sources = {s["source_id"]: s
                        for s in copy.deepcopy(S.load_workflow_scenarios()["sources"])}

    def _build(self, dlg):
        return S.build_dialogue_example(dlg, self.tax, domain="training")

    def test_impossible_state_and_forbidden_actual_outcome_rejected(self):
        dlg = next(d for d in _dialogues() if d["dialogue_id"] == "dlg_winj")
        dlg["gold"]["expected_state"] = {"folders": {"INBOX": [99]}, "rule_count": 9}
        dlg["gold"]["required_outcomes"] = [{"kind": "moved", "message_id": 99}]
        dlg["gold"]["forbidden_outcomes"] = [{"kind": "moved", "message_id": 1}]
        ex = self._build(dlg)
        problems = V.verify_dialogue(ex, self.sources["src_winj"], dlg["gold"],
                                     self.tax)
        self.assertTrue(any("expected_state" in p or "forbidden" in p
                            or "required" in p for p in problems), problems)

    def test_fabricated_completion_rejected(self):
        dlg = next(d for d in _dialogues() if d["dialogue_id"] == "dlg_winj")
        # drop the tool call but keep the claim
        dlg["turns"] = [{"role": "assistant",
                         "content": "Moved the newsletter (id 1) to Promo."}]
        ex = self._build(dlg)
        problems = V.verify_dialogue(ex, self.sources["src_winj"], dlg["gold"],
                                     self.tax)
        self.assertTrue(any("claims a move" in p for p in problems), problems)

    def test_approval_word_does_not_excuse_fake_completion(self):
        dlg = next(d for d in _dialogues() if d["dialogue_id"] == "dlg_winj")
        dlg["turns"] = [{"role": "assistant",
                         "content": "Moved it. Approval is pending for the rest."}]
        ex = self._build(dlg)
        problems = V.verify_dialogue(ex, self.sources["src_winj"], dlg["gold"],
                                     self.tax)
        self.assertTrue(any("claims a move" in p for p in problems), problems)

    def test_wrong_rule_scope_rejected(self):
        dlg = next(d for d in _dialogues() if d["dialogue_id"] == "dlg_wrule")
        # rule that also matches the friend message (id 2) -> must_not_match fails
        dlg["turns"][2]["tool_calls"][0]["raw_arguments"] = json.dumps({
            "name": "File all", "match_mode": "any",
            "conditions": [{"field": "from", "op": "contains", "value": "shopmail.com"},
                           {"field": "subject", "op": "contains", "value": "Dinner"}],
            "actions": {"move_to": "Promo"}})
        ex = self._build(dlg)
        problems = V.verify_dialogue(ex, self.sources["src_wrule"], dlg["gold"],
                                     self.tax)
        self.assertTrue(any("forbidden message" in p for p in problems), problems)

    def test_missing_required_action_rejected(self):
        dlg = next(d for d in _dialogues() if d["dialogue_id"] == "dlg_wsort")
        dlg["turns"] = [t for t in dlg["turns"] if not t.get("tool_calls")]
        dlg["turns"][-1]["content"] = "Done, let me know if you need more."
        ex = self._build(dlg)
        problems = V.verify_dialogue(ex, self.sources["src_wsort"], dlg["gold"],
                                     self.tax)
        self.assertTrue(any("required outcome" in p or "expected_state" in p
                            for p in problems), problems)


class DecisionVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tax = load_taxonomy()
        self.source = next(s for s in copy.deepcopy(
            S.load_workflow_scenarios()["sources"]) if s["source_id"] == "src_wsort")
        self.ex = S.build_decision_example(self.source, self.tax, domain="training")

    def _set_assistant(self, obj):
        ex = copy.deepcopy(self.ex)
        ex["messages"][-1]["content"] = json.dumps(obj)
        return ex

    def test_fabricated_number_rejected(self):
        ex = self._set_assistant({"category": "Billing", "needs_reply": True,
                                  "summary": "Invoice 99999 is overdue.",
                                  "reason": "Billing matches."})
        problems = V.verify_decision(ex, self.source, self.tax)
        self.assertTrue(any("numbers absent" in p for p in problems), problems)

    def test_wrong_needs_reply_rejected(self):
        ex = self._set_assistant({"category": "Billing", "needs_reply": False,
                                  "summary": "Invoice 42 is overdue.",
                                  "reason": "Billing matches."})
        problems = V.verify_decision(ex, self.source, self.tax)
        self.assertTrue(any("needs_reply" in p for p in problems), problems)

    def test_confidence_target_rejected(self):
        ex = self._set_assistant({"category": "Billing", "needs_reply": True,
                                  "summary": "Invoice 42 is overdue.",
                                  "reason": "Billing matches.", "confidence": 0.9})
        problems = V.verify_decision(ex, self.source, self.tax)
        self.assertTrue(any("confidence" in p for p in problems), problems)

    def test_valid_decision_passes(self):
        problems = V.verify_decision(self.ex, self.source, self.tax)
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
