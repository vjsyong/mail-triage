"""AC5: native serialization, round-trip and per-turn loss masks."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.training import messages as M  # noqa: E402
from benchmarks.v3.training import minicpm as X  # noqa: E402
from benchmarks.v3.training import samples as S  # noqa: E402
from benchmarks.v3.training.taxonomies import load_taxonomy  # noqa: E402


class RoundTripTests(unittest.TestCase):
    def test_nested_rule_args_bools_arrays_multiline_round_trip(self):
        args = {
            "name": "File shopmail",
            "match_mode": "all",
            "conditions": [{"field": "from", "op": "contains",
                            "value": "shopmail.com"}],
            "actions": {"move_to": "Promo", "mark_read": True},
            "enabled": True, "priority": 3,
            "rationale": "line one\nline two <x> & y",
        }
        xml = X.render_tool_call("propose_rule", args)
        self.assertIn("<![CDATA[line one", xml)
        parsed = X.parse_tool_calls('<function name="propose_rule">%s</function>'
                                    % xml.split(">", 1)[1])
        self.assertEqual(parsed[0]["arguments"], args)

    def test_template_keeps_tool_role_and_think_markers(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "calling",
             "tool_calls": [{"function": {"name": "list_rules", "arguments": {}}}]},
            {"role": "tool", "content": '{"ok": true}', "tool_call_id": "c1"},
            {"role": "assistant", "content": "done"},
        ]
        text = X.render_messages(msgs, tools=[{"type": "function",
                                               "function": {"name": "list_rules"}}])
        self.assertIn("<|im_start|>system", text)
        self.assertIn("<tools>", text)
        self.assertIn('<function name="list_rules">', text)
        self.assertIn("<tool_response>", text)
        self.assertIn("<think>", text)

    def test_multiline_cdata_param_round_trips(self):
        args = {"body": "first\nsecond & third <tag>"}
        xml = X.render_tool_call("draft_reply", args)
        parsed = X.parse_tool_calls('<function name="draft_reply">%s</function>'
                                    % xml.split(">", 1)[1])
        self.assertEqual(parsed[0]["arguments"], args)


class MaskTests(unittest.TestCase):
    def test_assistant_only_mask_covers_calls_and_finals(self):
        msgs = [
            M.message("system", "s"), M.message("user", "u"),
            M.assistant_message("thinking", tool_calls=[
                {"id": "c1", "name": "move_message", "arguments": "{}"}]),
            M.message("tool", "{}", tool_call_id="c1"),
            M.assistant_message("final"),
        ]
        self.assertEqual(M.mask_list(msgs), [False, False, True, False, True])

    def test_bad_student_turn_is_masked(self):
        msgs = [
            M.message("system", "s"), M.message("user", "u"),
            M.assistant_message("bad", rejected=True),
            M.message("tool", "{}", tool_call_id="c1"),
            M.assistant_message("good"),
        ]
        masked = M.apply_mask(msgs, bad_indices=[2])
        self.assertEqual(M.mask_list(masked), [False, False, False, False, True])

    def test_supervision_preserved_through_native_conversion(self):
        msgs = [M.message("system", "s"), M.message("user", "u"),
                M.assistant_message("bad", rejected=True),
                M.assistant_message("good")]
        native = M.to_native_messages(msgs)
        self.assertEqual([m.get("supervised") for m in native
                          if m["role"] == "assistant"], [False, True])


class InjectionDialogueTests(unittest.TestCase):
    def test_injection_body_is_not_obeyed(self):
        tax = load_taxonomy()
        dlg = next(d for d in S.load_workflow_scenarios()["dialogues"]
                   if d["dialogue_id"] == "dlg_winj")
        ex = S.build_dialogue_example(dlg, tax, domain="training")
        final = ex["trace"]["state_final"]
        self.assertEqual(final["folders"]["Promo"], [1])
        self.assertEqual(final["folders"]["INBOX"], [])
        self.assertEqual(final["rule_count"], 0)


if __name__ == "__main__":
    unittest.main()
