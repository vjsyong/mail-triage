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


class RoundTripTests(unittest.TestCase):
    def test_nested_rule_args_bools_arrays_multiline_round_trip(self):
        args = {
            "name": "File shopmail",
            "conditions": [{"field": "from_addr", "op": "contains",
                            "value": "shopmail.com"}],
            "actions": [{"type": "move", "folder": "Promo"}],
            "enabled": True,
            "priority": 3,
            "rationale": "line one\nline two <x> & y",
        }
        xml = X.render_tool_call("propose_rule", args)
        self.assertIn("<![CDATA[line one", xml)
        parsed = X.parse_tool_calls("<function name=\"propose_rule\">%s</function>"
                                    % xml.split(">", 1)[1])
        self.assertEqual(parsed[0]["name"], "propose_rule")
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
        self.assertIn("<function name=\"list_rules\">", text)
        self.assertIn("<tool_response>", text)
        self.assertIn("</tool_response>", text)
        self.assertIn("<think>", text)
        # the tool role was not dropped (upstream chat-only patch would drop it)
        self.assertIn('{"ok": true}', text)

    def test_multiline_cdata_param_round_trips(self):
        args = {"body": "first\nsecond & third <tag>"}
        xml = X.render_tool_call("draft_reply", args)
        parsed = X.parse_tool_calls("<function name=\"draft_reply\">%s</function>"
                                    % xml.split(">", 1)[1])
        self.assertEqual(parsed[0]["arguments"], args)


class MaskTests(unittest.TestCase):
    def test_assistant_only_mask_covers_calls_and_finals(self):
        msgs = [
            M.message("system", "s"),
            M.message("user", "u"),
            M.assistant_message("thinking", tool_calls=[
                {"id": "c1", "name": "move_message", "arguments": "{}"}]),
            M.message("tool", "{}", tool_call_id="c1"),
            M.assistant_message("final"),
        ]
        self.assertEqual(M.mask_list(msgs), [False, False, True, False, True])

    def test_bad_student_turn_is_masked(self):
        msgs = [
            M.message("system", "s"),
            M.message("user", "u"),
            M.assistant_message("bad", rejected=True),
            M.message("tool", "{}", tool_call_id="c1"),
            M.assistant_message("good"),
        ]
        masked = M.apply_mask(msgs, bad_indices=[2])
        self.assertEqual(M.mask_list(masked), [False, False, False, False, True])

    def test_apply_mask_never_supervises_inputs(self):
        msgs = [M.message("system", "s", supervised=True),
                M.message("user", "u", supervised=True),
                M.assistant_message("a")]
        masked = M.apply_mask(msgs)
        self.assertEqual(M.mask_list(masked), [False, False, True])

    def test_native_conversion_json_encodes_nested_args(self):
        msgs = [M.message("system", "s"), M.message("user", "u"),
                M.assistant_message("go", think="reason", tool_calls=[
                    {"id": "c1", "name": "propose_rule",
                     "arguments": '{"conditions": [{"field": "a"}]}'}])]
        native = M.to_native_messages(msgs)
        self.assertIn("<think>", native[2]["content"])
        args = native[2]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(args["conditions"], '[{"field":"a"}]')


class DecisionExampleTests(unittest.TestCase):
    def test_decision_example_omits_confidence(self):
        case = {
            "case_id": "c1", "gold_id": "g1", "input_profile": "native",
            "split": "development",
            "rendered_input": {"system": "sys", "user": "Subject: Hi\n\nbody"},
        }
        gold = {"gold_id": "g1", "observable": {"category": "visible"},
                "answer": {"category": "Action", "needs_reply": True,
                           "acceptable_categories": ["Action"]},
                "hidden_evidence": []}
        ex = S.build_decision_example(case, gold, domain="training")
        self.assertNotIn("confidence", ex["decision"])
        self.assertEqual(ex["decision"]["category"], "Action")
        self.assertTrue(ex["messages"][-1]["supervised"])

    def test_workflow_example_injection_is_not_obeyed(self):
        scenarios = S.load_workflow_scenarios()["scenarios"]
        injection = next(s for s in scenarios if s["id"] == "wf_injection")
        ex = S.build_workflow_example(injection, domain="training")
        final = ex["trace"]["state_final"]
        self.assertEqual(final["folders"]["Promo"], ["n1"])
        self.assertEqual(final["folders"]["INBOX"], [])
        self.assertEqual(final["rule_count"], 0)


if __name__ == "__main__":
    unittest.main()
