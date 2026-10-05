"""AC4: per-turn trace persistence, context reconstruction, malformed rejection."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.training import samples as S  # noqa: E402
from benchmarks.v3.training import trace as TR  # noqa: E402


class TraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scenarios = S.load_workflow_scenarios()["scenarios"]
        cls.sort = next(s for s in scenarios if s["id"] == "wf_sort_ambiguous")

    def test_context_reconstruction_matches_recorded_input(self):
        ex = S.build_workflow_example(self.sort, domain="training")
        trace = ex["trace"]
        for i, turn in enumerate(trace["turns"]):
            self.assertEqual(TR.reconstruct_context(trace, i),
                             turn["model_visible_input"])
        # the first turn sees only system+request; the final turn sees more.
        self.assertEqual(len(trace["turns"][0]["model_visible_input"]), 2)
        self.assertGreater(len(trace["turns"][-1]["model_visible_input"]), 2)

    def test_input_grows_only_by_known_turns(self):
        ex = S.build_workflow_example(self.sort, domain="training")
        trace = ex["trace"]
        first = trace["turns"][0]["model_visible_input"]
        self.assertEqual([m["role"] for m in first], ["system", "user"])

    def test_complete_turn_evidence_recorded(self):
        ex = S.build_workflow_example(self.sort, domain="training")
        trace = ex["trace"]
        tool_turn = next(t for t in trace["turns"] if t["tool_calls"])
        self.assertTrue(tool_turn["tool_calls"][0]["raw_arguments"])
        self.assertTrue(tool_turn["tool_results"][0]["raw_arguments"])
        self.assertTrue(tool_turn["events"])
        self.assertIsNotNone(tool_turn["state_snapshot"])
        self.assertIsNotNone(tool_turn["state_after"])
        self.assertTrue(tool_turn["finish_reason"])

    def test_usage_is_aggregated(self):
        ex = S.build_workflow_example(self.sort, domain="training")
        usage = ex["trace"]["usage"]
        per_turn = sum(t["usage"]["total_tokens"]
                       for t in ex["trace"]["turns"])
        self.assertEqual(usage["total_tokens"], per_turn)

    def test_identities_recorded(self):
        ex = S.build_workflow_example(self.sort, domain="training")
        ids = ex["trace"]["identities"]
        for key in ("model", "prompt_sha256", "schema_sha256", "tools_sha256",
                    "template_sha256"):
            self.assertIn(key, ids)
            self.assertTrue(ids[key])


class MalformedCallTraceTests(unittest.TestCase):
    def _scenario_with_bad_call(self):
        return {
            "id": "wf_bad", "source_id": "src_bad",
            "request": "Move the message.",
            "permissions": {"allow_move": True, "require_approval": False},
            "mailbox": {"folders": ["INBOX", "Action"], "messages": [
                {"message_id": "m1", "folder": "INBOX",
                 "from_addr": "a@b.com", "to_addr": "o@x.com",
                 "subject": "Hi", "date": "2025-09-01", "body": "hello"}],
                "rules": []},
            "turns": [
                {"role": "assistant", "content": "moving",
                 "tool_calls": [{"name": "move_message",
                                 "raw_arguments": "{not valid json"}]},
                {"role": "assistant", "content": "sorry, retrying"},
            ],
            "gold": {"required_outcomes": ["moved m1"], "forbidden_outcomes": [],
                     "expected_state": {"folders": {"INBOX": ["m1"]}}},
        }

    def test_malformed_args_rejected_before_execution(self):
        ex = S.build_workflow_example(self._scenario_with_bad_call(),
                                      domain="training")
        trace = ex["trace"]
        bad_turn = trace["turns"][0]
        self.assertTrue(bad_turn["rejected"])
        self.assertEqual(bad_turn["finish_reason"], "rejected")
        self.assertEqual(bad_turn["tool_results"][0]["rejected"],
                         "malformed_arguments")
        # nothing executed; mailbox unchanged
        self.assertEqual(trace["state_final"]["folders"]["INBOX"], ["m1"])
        self.assertEqual(trace["state_final"]["folders"]["Action"], [])

    def test_bad_student_turn_is_masked(self):
        ex = S.build_workflow_example(self._scenario_with_bad_call(),
                                      domain="training")
        # the assistant turn that emitted the malformed call is not supervised
        malformed = [m for m in ex["messages"]
                     if m["role"] == "assistant" and "moving" in m.get("content", "")]
        self.assertTrue(malformed)
        self.assertFalse(malformed[0]["supervised"])


if __name__ == "__main__":
    unittest.main()
