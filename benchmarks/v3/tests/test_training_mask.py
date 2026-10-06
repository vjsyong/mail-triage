"""B1: the ACTUAL training mask path (`lora_common.build_supervised`).

Uses a lightweight reference tokenizer (char offsets) so the real
``supervised_spans``/``build_supervised`` code is exercised offline; a cached
real-tokenizer smoke is run separately by the pipeline.  A rejected assistant
segment must receive only -100; a successful call/final must be non-empty;
system/user/tool tokens stay masked.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3, os.path.join(ROOT, "training", "mail")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import lora_common as LC  # noqa: E402
from benchmarks.v3.training import minicpm, samples as S  # noqa: E402
from benchmarks.v3.training.taxonomies import load_taxonomy  # noqa: E402


class RefTokenizer(object):
    """Char-level stand-in with the released template (char == one token)."""

    def apply_chat_template(self, messages, tools=None, tokenize=False,
                            add_generation_prompt=False, **kw):
        return minicpm.render_messages(messages, tools=tools or None,
                                       add_generation_prompt=add_generation_prompt)

    def __call__(self, text, return_offsets_mapping=False,
                 add_special_tokens=False, **kw):
        return {"input_ids": list(range(len(text))),
                "offset_mapping": [(i, i + 1) for i in range(len(text))]}


def _malformed():
    return {
        "dialogue_id": "dlg_bad", "source_id": "src_bad", "lineage_id": "lin_bad",
        "role": "training", "domain": "training", "request": "Move the message.",
        "permissions": {"move": "auto"},
        "mailbox": [{"id": 1, "uid": 1, "folder": "INBOX", "from_addr": "a@b.com",
                     "to_addr": "o@x.com", "subject": "Hi", "date": "2025-09-01",
                     "body": "hello"}],
        "turns": [
            {"role": "assistant", "content": "moving",
             "tool_calls": [{"name": "move_message",
                             "raw_arguments": "{not valid json"}]},
            {"role": "assistant", "content": "sorry, retrying"}],
        "gold": {"expected_state": {"folders": {"INBOX": [1]}},
                 "required_outcomes": [], "forbidden_outcomes": [], "assertions": []},
        "source_email": {"from_addr": "a@b.com", "to_addr": "o@x.com",
                         "subject": "Hi", "body": "hello"},
    }


class BuildSupervisedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tok = RefTokenizer()
        cls.tax = load_taxonomy()

    def _built(self, dialogue):
        ex = S.build_dialogue_example(dialogue, self.tax, domain="training")
        native = LC.native_messages(ex)
        full, spans = LC.supervised_spans(self.tok, native, LC.tool_defs(ex))
        built = LC.build_supervised(self.tok, native, LC.tool_defs(ex),
                                    max_len=1_000_000)
        return full, spans, built

    def test_rejected_segment_is_fully_masked(self):
        full, spans, built = self._built(_malformed())
        labels = built["labels"]
        idx = full.index("moving")
        for pos in range(idx, idx + len("moving")):
            self.assertEqual(labels[pos], -100,
                             "rejected assistant segment trained at %d" % pos)
        # the non-rejected final turn is still supervised
        fidx = full.index("sorry, retrying")
        self.assertTrue(any(labels[p] != -100
                            for p in range(fidx, fidx + len("sorry, retrying"))))

    def test_inputs_and_tool_turns_masked(self):
        full, spans, built = self._built(_malformed())
        labels = built["labels"]
        for needle in ("You triage", "Move the message.", "<tool_response>"):
            if needle not in full:
                continue
            idx = full.index(needle)
            self.assertEqual(labels[idx], -100, needle)

    def test_successful_call_and_final_nonempty(self):
        dlg = next(d for d in S.load_workflow_scenarios()["dialogues"]
                   if d["dialogue_id"] == "dlg_wsort")
        full, spans, built = self._built(dlg)
        labels = built["labels"]
        self.assertGreater(built["supervised_tokens"], 0)
        call = full.index('<function name="move_message"')
        self.assertNotEqual(labels[call], -100)
        self.assertTrue(any(labels[p] != -100 for p in range(call,
                        call + len('<function name="move_message">'))))

    def test_no_synthetic_think_targeted_when_thinking_off(self):
        # inference thinking=false yields empty <think></think>; it is inside the
        # supervised assistant segment, which is expected (the template emits it),
        # but the canonical assistant message itself carries no fake think text.
        dlg = next(d for d in S.load_workflow_scenarios()["dialogues"]
                   if d["dialogue_id"] == "dlg_wsort")
        ex = S.build_dialogue_example(dlg, self.tax, domain="training")
        for m in ex["messages"]:
            if m["role"] == "assistant" and m.get("supervised"):
                self.assertNotIn("I think", m.get("content") or "")


if __name__ == "__main__":
    unittest.main()
