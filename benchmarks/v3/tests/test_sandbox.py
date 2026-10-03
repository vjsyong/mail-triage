"""Workflow sandbox tests (WP3, FR5).

Covers: permission normalisation (off/ask/auto and policy booleans); denied and
approval-pending calls never mutate; duplicate moves are safe no-ops with
derived counts; failures are transactional; skipped calls never complete;
read-after-write; dangerous tools are always denied; the event log records tool,
args, status, permission decision, state changes and an executed flag.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.sandbox import Mailbox, normalize_permissions, tool_schemas  # noqa: E402


def fixture():
    return {
        "folders": ["INBOX", "Receipts"],
        "messages": [
            {"message_id": 1, "folder": "INBOX", "from_addr": "a@x.com",
             "subject": "Hello", "body": "hi there", "snippet": "hi there",
             "seen": False},
            {"message_id": "m2", "folder": "INBOX", "from_addr": "b@y.com",
             "subject": "Invoice", "body": "bill", "snippet": "bill"},
            {"message_id": "m3", "folder": "Receipts", "from_addr": "c@z.com",
             "subject": "Old", "body": "old", "snippet": "old", "seen": True},
        ],
        "rules": [{"id": 1, "name": "Newsletter", "enabled": True}],
    }


class PermissionNormalisationTests(unittest.TestCase):
    def test_explicit_levels(self):
        perms = normalize_permissions({"move": "auto", "rule_create": "ask"})
        self.assertEqual(perms["move"], "auto")
        self.assertEqual(perms["rule_create"], "ask")
        self.assertEqual(perms["read"], "auto")

    def test_policy_boolean_block(self):
        perms = normalize_permissions(
            {"allow_move": True, "allow_rule_create": False,
             "allow_send": True, "require_approval": True})
        self.assertEqual(perms["move"], "ask")
        self.assertEqual(perms["rule_create"], "off")
        # send can never be enabled, even if a policy says allow_send.
        self.assertEqual(perms["send"], "off")
        self.assertEqual(perms["delete"], "off")

    def test_auto_when_no_approval(self):
        perms = normalize_permissions(
            {"allow_move": True, "allow_rule_create": True, "require_approval": False})
        self.assertEqual(perms["move"], "auto")
        self.assertEqual(perms["rule_create"], "auto")


class DenialTests(unittest.TestCase):
    def test_move_off_denied_no_mutation(self):
        mb = Mailbox(fixture(), permissions={"move": "off"}, case_id="c")
        before = mb.snapshot()
        res = mb.execute("move_message", {"message_id": 1, "target_folder": "Receipts"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "denied")
        self.assertEqual(mb.snapshot(), before)
        self.assertFalse(mb.events[-1]["executed"])
        self.assertFalse(mb.events[-1]["mutated"])

    def test_delete_and_send_always_denied(self):
        mb = Mailbox(fixture(), permissions={"allow_move": True, "allow_send": True,
                                             "require_approval": False}, case_id="c")
        before = mb.snapshot()
        for tool in ("delete_message", "send_message"):
            res = mb.execute(tool, {"message_id": 1})
            self.assertEqual(res["status"], "denied", tool)
        self.assertEqual(mb.snapshot(), before)

    def test_unknown_tool_error_no_mutation(self):
        mb = Mailbox(fixture(), case_id="c")
        before = mb.snapshot()
        res = mb.execute("frobnicate", {})
        self.assertEqual(res["status"], "error")
        self.assertEqual(mb.snapshot(), before)


class ApprovalTests(unittest.TestCase):
    def test_pending_no_write_then_approved_writes(self):
        mb = Mailbox(fixture(), permissions={"rule_create": "ask"}, case_id="c")
        pending = mb.execute("propose_rule", {"name": "VIP"})
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(mb.snapshot()["proposed_rules"], [])
        self.assertFalse(mb.events[-1]["executed"])
        done = mb.execute("propose_rule", {"name": "VIP"}, approve=True)
        self.assertEqual(done["status"], "ok")
        self.assertEqual(len(mb.snapshot()["proposed_rules"]), 1)


class TransactionTests(unittest.TestCase):
    def test_failed_move_is_transactional(self):
        mb = Mailbox(fixture(), permissions={"move": "auto"}, case_id="c")
        before = mb.snapshot()
        res = mb.execute("move_message", {"message_id": "nope", "target_folder": "Receipts"})
        self.assertEqual(res["status"], "error")
        self.assertEqual(mb.snapshot(), before)

    def test_invalid_arguments(self):
        mb = Mailbox(fixture(), permissions={"move": "auto"}, case_id="c")
        self.assertEqual(mb.execute("move_message", {"message_id": 1})["status"], "error")


class IdempotenceTests(unittest.TestCase):
    def test_duplicate_move_noop_counts_derived(self):
        mb = Mailbox(fixture(), permissions={"move": "auto"}, case_id="c")
        first = mb.execute("move_message", {"message_id": 1, "target_folder": "Receipts"})
        self.assertEqual(first["status"], "ok")
        second = mb.execute("move_message", {"message_id": 1, "target_folder": "Receipts"})
        self.assertEqual(second["status"], "noop")
        self.assertFalse(second["result"].get("error"))
        counts = mb.folder_counts()
        total = sum(v["total"] for v in counts.values())
        self.assertEqual(total, 3)
        self.assertEqual(counts["Receipts"]["total"], 2)
        self.assertEqual(counts["INBOX"]["total"], 1)


class ReadAfterWriteTests(unittest.TestCase):
    def test_read_sees_current_folder(self):
        mb = Mailbox(fixture(), permissions={"move": "auto"}, case_id="c")
        mb.execute("move_message", {"message_id": "m2", "target_folder": "Receipts"})
        read = mb.execute("read_message", {"message_id": "m2"})
        self.assertEqual(read["result"]["folder"], "Receipts")
        found = mb.execute("search_messages", {"folder": "Receipts"})
        ids = [m["message_id"] for m in found["result"]["messages"]]
        self.assertIn("m2", ids)

    def test_flag_update_visible(self):
        mb = Mailbox(fixture(), case_id="c")
        mb.execute("flag_message", {"message_id": 1, "seen": True})
        self.assertTrue(mb.execute("read_message", {"message_id": 1})["result"]["seen"])


class SkipTests(unittest.TestCase):
    def test_skipped_call_does_not_mutate_or_complete(self):
        mb = Mailbox(fixture(), permissions={"move": "auto"}, case_id="c")
        before = mb.snapshot()
        res = mb.execute("move_message", {"message_id": 1, "target_folder": "Receipts"},
                         status="skipped")
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(mb.snapshot(), before)
        event = mb.events[-1]
        self.assertEqual(event["status"], "skipped")
        self.assertFalse(event["executed"])


class EventLogTests(unittest.TestCase):
    def test_event_fields(self):
        mb = Mailbox(fixture(), permissions={"move": "auto"}, case_id="c")
        mb.execute("move_message", {"message_id": 1, "target_folder": "Receipts"})
        event = mb.events[-1]
        for key in ("tool", "args", "status", "permission", "executed", "mutated",
                    "state_changes"):
            self.assertIn(key, event)
        self.assertEqual(event["tool"], "move_message")
        self.assertEqual(event["permission"]["decision"], "allow")
        self.assertTrue(event["executed"])
        self.assertTrue(event["mutated"])
        self.assertTrue(event["state_changes"])

    def test_final_state_scoreable(self):
        mb = Mailbox(fixture(), permissions={"move": "auto"}, case_id="c")
        mb.execute("move_message", {"message_id": 1, "target_folder": "Receipts"})
        state = mb.final_state()
        self.assertIn("folders", state)
        self.assertIn("messages", state)
        self.assertIn("events", state)


class ToolSchemaTests(unittest.TestCase):
    def test_schemas_have_explicit_parameters(self):
        schemas = {s["function"]["name"]: s for s in tool_schemas()}
        for name in ("move_message", "read_message", "propose_rule", "simulate_rule"):
            self.assertIn(name, schemas)
            self.assertEqual(schemas[name]["function"]["parameters"]["type"], "object")
        # dangerous tools are visible so a model can attempt them (and be denied)
        self.assertIn("delete_message", schemas)
        self.assertIn("send_message", schemas)


class FinalStateTests(unittest.TestCase):
    def test_build_shape_state(self):
        mb = Mailbox(fixture(), permissions={"move": "auto"}, case_id="c")
        mb.execute("move_message", {"message_id": "m2", "target_folder": "Receipts"})
        mb.execute("draft_reply", {"message_id": "m2", "instructions": "ack"})
        state = mb.final_state()
        self.assertEqual(state["folders"]["Receipts"], ["m2", "m3"])
        self.assertNotIn("m2", state["folders"]["INBOX"])
        self.assertEqual(state["draft_count"], 1)
        self.assertTrue(any("b@y.com" in r for r in state["draft_recipients"]))
        self.assertEqual(state["rule_count"], 0)

    def test_id_alias_lookup(self):
        mb = Mailbox(fixture(), permissions={"move": "auto"}, case_id="c")
        res = mb.execute("move_message", {"id": "m2", "target_folder": "Receipts"})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(mb.final_state()["folders"]["Receipts"], ["m2", "m3"])

    def test_approval_is_recorded_on_the_event(self):
        mb = Mailbox(fixture(), permissions={"move": "ask"}, case_id="c")
        pending = mb.execute("move_message", {"message_id": "m2",
                                              "target_folder": "Receipts"})
        self.assertEqual(pending["status"], "pending")
        self.assertFalse(mb.events[-1]["permission"]["approved"])
        ok = mb.execute("move_message", {"message_id": "m2",
                                         "target_folder": "Receipts"}, approve=True)
        self.assertEqual(ok["status"], "ok")
        self.assertTrue(mb.events[-1]["permission"]["approved"])


    def test_args_cannot_self_approve(self):
        mb = Mailbox(fixture(), permissions={"move": "ask"}, case_id="c")
        res = mb.execute("move_message", {"message_id": "m2",
                                          "target_folder": "Receipts",
                                          "approve": True})
        self.assertEqual(res["status"], "pending")
        self.assertFalse(mb.events[-1]["permission"]["approved"])
        self.assertEqual(mb.final_state()["folders"]["INBOX"], ["1", "m2"])


if __name__ == "__main__":
    unittest.main()
