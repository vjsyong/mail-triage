"""AC4/B1: per-turn trace, context reconstruction, malformed-call masking."""
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
from benchmarks.v3.training.messages import to_native_messages  # noqa: E402
from benchmarks.v3.training.taxonomies import load_taxonomy  # noqa: E402


def _dialogue_by_id(did):
    for d in S.load_workflow_scenarios()["dialogues"]:
        if d["dialogue_id"] == did:
            return d
    raise AssertionError(did)


class TraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tax = load_taxonomy()
        cls.ex = S.build_dialogue_example(_dialogue_by_id("dlg_wsort"), cls.tax,
                                          domain="training")

    def test_context_reconstruction_matches_recorded_input(self):
        trace = self.ex["trace"]
        for i, turn in enumerate(trace["turns"]):
            self.assertEqual(TR.reconstruct_context(trace, i),
                             turn["model_visible_input"])
        self.assertEqual(len(trace["turns"][0]["model_visible_input"]), 2)

    def test_complete_turn_evidence(self):
        tool_turn = next(t for t in self.ex["trace"]["turns"] if t["tool_calls"])
        self.assertTrue(tool_turn["tool_calls"][0]["raw_arguments"])
        self.assertTrue(tool_turn["events"])
        self.assertIsNotNone(tool_turn["state_snapshot"])
        self.assertIsNotNone(tool_turn["state_after"])

    def test_usage_aggregated(self):
        usage = self.ex["trace"]["usage"]
        per_turn = sum(t["usage"]["total_tokens"] for t in self.ex["trace"]["turns"])
        self.assertEqual(usage["total_tokens"], per_turn)

    def test_identities_recorded(self):
        ids = self.ex["trace"]["identities"]
        for key in ("model", "prompt_sha256", "schema_sha256", "tools_sha256",
                    "template_sha256"):
            self.assertTrue(ids[key])


class MalformedCallTests(unittest.TestCase):
    def _bad(self):
        return {
            "dialogue_id": "dlg_bad", "source_id": "src_bad",
            "lineage_id": "lin_bad", "role": "training", "domain": "training",
            "request": "Move the message.",
            "permissions": {"move": "auto"},
            "mailbox": [{"id": 1, "uid": 1, "folder": "INBOX",
                         "from_addr": "a@b.com", "to_addr": "o@x.com",
                         "subject": "Hi", "date": "2025-09-01", "body": "hello"}],
            "turns": [
                {"role": "assistant", "content": "moving",
                 "tool_calls": [{"name": "move_message",
                                 "raw_arguments": "{not valid json"}]},
                {"role": "assistant", "content": "sorry, retrying"}],
            "gold": {"expected_state": {"folders": {"INBOX": [1]}},
                     "required_outcomes": [], "forbidden_outcomes": [],
                     "assertions": []},
            "source_email": {"from_addr": "a@b.com", "to_addr": "o@x.com",
                             "subject": "Hi", "body": "hello"},
        }

    def test_malformed_rejected_before_execution(self):
        ex = S.build_dialogue_example(self._bad(), load_taxonomy(), domain="training")
        trace = ex["trace"]
        self.assertTrue(trace["turns"][0]["rejected"])
        self.assertEqual(trace["turns"][0]["finish_reason"], "rejected")
        self.assertEqual(trace["turns"][0]["tool_results"][0]["rejected"],
                         "malformed_arguments")
        self.assertEqual(trace["state_final"]["folders"]["INBOX"], [1])

    def test_bad_turn_masked_and_propagated_to_native(self):
        ex = S.build_dialogue_example(self._bad(), load_taxonomy(), domain="training")
        bad = [m for m in ex["messages"]
               if m["role"] == "assistant" and "moving" in m.get("content", "")]
        self.assertTrue(bad)
        self.assertFalse(bad[0]["supervised"])
        native = to_native_messages(ex["messages"])
        bad_native = [m for m in native if m["role"] == "assistant"
                      and "moving" in m.get("content", "")]
        self.assertEqual(bad_native[0]["supervised"], False)


if __name__ == "__main__":
    unittest.main()
