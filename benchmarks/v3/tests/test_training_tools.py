"""AC3: bounded native tool surface -- AST extraction, permissions, rejection."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.sandbox import Mailbox, tool_schemas  # noqa: E402
from benchmarks.v3.training import tools_native as TN  # noqa: E402

MAILBOX = {
    "folders": ["INBOX", "Action"],
    "messages": [
        {"message_id": "m1", "folder": "INBOX", "from_addr": "billing@acme.com",
         "to_addr": "o@x.com", "subject": "Invoice 42 overdue",
         "date": "2025-09-01", "body": "Invoice 42 for 120 is overdue."},
        {"message_id": "m2", "folder": "INBOX", "from_addr": "news@x.com",
         "to_addr": "o@x.com", "subject": "Sale", "date": "2025-09-02",
         "body": "Big sale."},
    ],
    "rules": [],
}


class AstExtractionTests(unittest.TestCase):
    def test_extracted_schemas_match_source_sandbox(self):
        schemas, caps = TN.extract_source_tools()
        source_names = {s["function"]["name"] for s in schemas}
        live_names = {s["function"]["name"] for s in tool_schemas()}
        self.assertEqual(source_names, live_names)
        self.assertEqual(caps, {
            "list_folders": "read", "search_messages": "read",
            "read_message": "read", "list_rules": "read",
            "simulate_rule": "read", "flag_message": "flag",
            "create_folder": "folder", "draft_reply": "draft",
            "move_message": "move", "propose_rule": "rule_create",
            "delete_message": "delete", "send_message": "send"})

    def test_native_surface_is_explicitly_partial(self):
        self.assertEqual(TN.CONTRACT_PARITY, "partial")
        names = [s["function"]["name"] for s in TN.native_tool_schemas()]
        self.assertEqual(names, list(TN.NATIVE_SURFACE))
        for schema in TN.native_tool_schemas():
            self.assertEqual(schema["function"]["x-contract-parity"], "partial")
        # no all-tool parity: the full sandbox has more tools
        self.assertLess(len(TN.NATIVE_SURFACE), len(tool_schemas()))

    def test_unknown_tool_schema_raises(self):
        with self.assertRaises(TN.ToolContractError):
            TN.native_tool_schemas(["not_a_tool"])

    def test_capability_mapping(self):
        self.assertEqual(TN.tool_capability("move_message"), "move")
        self.assertEqual(TN.tool_capability("propose_rule"), "rule_create")


class ArgumentTests(unittest.TestCase):
    def test_malformed_string_rejected_not_empty_dict(self):
        args, err = TN.parse_arguments("{not json")
        self.assertIsNone(args)
        self.assertEqual(err["error"], "malformed_arguments")

    def test_non_object_rejected(self):
        for raw in ("[1,2,3]", '"a string"', "null", ""):
            with self.subTest(raw=raw):
                args, err = TN.parse_arguments(raw)
                self.assertIsNone(args)
                self.assertIsNotNone(err)

    def test_valid_object_parsed(self):
        args, err = TN.parse_arguments('{"message_id": "m1"}')
        self.assertEqual(args, {"message_id": "m1"})
        self.assertIsNone(err)


class ExecutionTests(unittest.TestCase):
    def _mailbox(self, permissions):
        return Mailbox(MAILBOX, permissions=permissions, case_id="t")

    def test_result_only_untrusted_wrapper(self):
        mb = self._mailbox({"allow_move": True})
        res = TN.execute_tool_call(mb, "read_message", '{"message_id": "m1"}')
        wrapped = res["wrapped"]
        self.assertEqual(wrapped["trust"], "untrusted")
        self.assertNotIn("permission", wrapped["payload"])
        self.assertEqual(wrapped["payload"]["tool"], "read_message")
        self.assertIn("result", wrapped["payload"])

    def test_malformed_call_rejected_before_execution(self):
        mb = self._mailbox({"allow_move": True})
        res = TN.execute_tool_call(mb, "move_message", "{bad json")
        self.assertEqual(res["rejected"], "malformed_arguments")
        self.assertIsNone(res["wrapped"])
        self.assertEqual(mb.events, [])  # nothing executed

    def test_dangerous_and_unknown_tools_never_execute(self):
        mb = self._mailbox({"allow_move": True})
        for name in ("send_message", "delete_message"):
            res = TN.execute_tool_call(mb, name, "{}")
            self.assertEqual(res["rejected"], "dangerous_tool")
        self.assertEqual(mb.events, [])
        res = TN.execute_tool_call(mb, "flag_message", "{}")
        self.assertEqual(res["rejected"], "unsupported_tool")
        self.assertEqual(mb.events, [])

    def test_model_cannot_self_approve(self):
        mb = self._mailbox({"allow_move": True, "require_approval": True})
        res = TN.execute_tool_call(
            mb, "move_message",
            '{"message_id": "m1", "target_folder": "Action", "approve": true}')
        self.assertTrue(res["self_approval_stripped"])
        self.assertIn("approve", res["stripped_keys"])
        # ask-gated: stays pending, no mutation
        self.assertEqual(res["event"]["status"], "pending")
        self.assertFalse(res["event"]["mutated"])
        self.assertEqual(mb.final_state()["folders"]["Action"], [])

    def test_off_capability_denied_no_mutation(self):
        mb = self._mailbox({"allow_move": False})
        res = TN.execute_tool_call(
            mb, "move_message", '{"message_id": "m1", "target_folder": "Action"}')
        self.assertEqual(res["event"]["status"], "denied")
        self.assertFalse(res["event"]["mutated"])

    def test_auto_move_mutates(self):
        mb = self._mailbox({"allow_move": True, "require_approval": False})
        res = TN.execute_tool_call(
            mb, "move_message", '{"message_id": "m1", "target_folder": "Action"}')
        self.assertTrue(res["event"]["mutated"])
        self.assertEqual(mb.final_state()["folders"]["Action"], ["m1"])

    def test_propose_rule_is_proposal_only(self):
        mb = self._mailbox({"allow_rule_create": True, "require_approval": False})
        res = TN.execute_tool_call(
            mb, "propose_rule",
            '{"name": "File shopmail", "conditions": [{"field": "from_addr", '
            '"op": "contains", "value": "shopmail"}], "actions": [{"type": '
            '"move", "folder": "Action"}]}')
        self.assertTrue(res["event"]["mutated"])  # proposal recorded
        self.assertEqual(mb.final_state()["rule_count"], 1)
        # but no message moved by the proposal
        self.assertEqual(mb.final_state()["folders"]["Action"], [])

    def test_ask_gated_propose_rule_stays_pending(self):
        mb = self._mailbox({"allow_rule_create": True, "require_approval": True})
        res = TN.execute_tool_call(
            mb, "propose_rule", '{"name": "File shopmail"}')
        self.assertEqual(res["event"]["status"], "pending")
        self.assertFalse(res["event"]["mutated"])
        self.assertEqual(mb.final_state()["rule_count"], 0)


if __name__ == "__main__":
    unittest.main()
