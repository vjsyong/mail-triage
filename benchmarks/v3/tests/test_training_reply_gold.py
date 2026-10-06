"""S1: visible reply-gold -- a label must be justified by the visible email."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3, os.path.join(ROOT, "training", "mail")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import briefs as B  # noqa: E402
from benchmarks.v3.training import export as E, reply_gold as RG  # noqa: E402
from benchmarks.v3.training.taxonomies import load_taxonomy  # noqa: E402

OLD_INVOICE = ("Invoice 1187 is now overdue. Please be advised the amount of "
               "$299.00 is outstanding.")
REPLY_INVOICE = ("Invoice 1187 is now overdue. Please reply to confirm when you "
                 "will pay.")
NOREPLY_INVOICE = ("Invoice 1187 is now overdue. Pay securely via the online "
                   "portal. No reply is needed.")


class ReplyGoldTests(unittest.TestCase):
    def test_old_invoice_text_cannot_pass_needs_reply_true(self):
        self.assertIn("reply_obligation_not_visible",
                      RG.reply_problems(OLD_INVOICE, True))
        self.assertEqual(RG.reply_problems(OLD_INVOICE, False), [])
        self.assertEqual(RG.visible_reply_obligation(OLD_INVOICE), None)

    def test_reply_phrase_justifies_true_and_contradicts_false(self):
        self.assertEqual(RG.reply_problems(REPLY_INVOICE, True), [])
        self.assertIn("reply_request_present_but_gold_false",
                      RG.reply_problems(REPLY_INVOICE, False))

    def test_no_reply_statement_justifies_false(self):
        self.assertEqual(RG.visible_reply_obligation(NOREPLY_INVOICE), False)
        self.assertEqual(RG.reply_problems(NOREPLY_INVOICE, False), [])

    def test_quoted_only_reply_phrase_does_not_count(self):
        quoted = "> Please reply to confirm\n\nAutomated notice. No reply is needed."
        self.assertFalse(RG.has_reply_request(quoted))
        self.assertIn("reply_obligation_not_visible", RG.reply_problems(quoted, True))

    def test_contradictory_no_reply_rejected(self):
        text = "Please reply to confirm. No reply is needed."
        self.assertIn("contradictory_no_reply_present",
                      RG.reply_problems(text, True))


class BriefTests(unittest.TestCase):
    def test_same_family_has_reply_and_no_reply_variants(self):
        briefs = B.build_briefs(11, per_family=2)
        by_family = {}
        for b in briefs:
            by_family.setdefault(b["family"], set()).add(b["needs_reply"])
        # not a family->bool shortcut
        for fam in ("invoice_due", "task_request", "friend_note"):
            self.assertEqual(by_family[fam], {True, False}, fam)
        # contrasting: a non-Action family with reply True, an Action with False
        self.assertIn(False, by_family["task_request"])   # Action / no reply
        self.assertIn(True, by_family["friend_note"])     # personal / reply
        self.assertIn(False, by_family["receipt"])
        self.assertIn(False, by_family["newsletter"])
        self.assertIn(False, by_family["login_alert"])

    def test_counterfactual_pair_shares_pair_id(self):
        briefs = [b for b in B.build_briefs(11, per_family=2)
                  if b["family"] == "invoice_due"]
        self.assertEqual(len({b["pair_id"] for b in briefs}), 1)
        self.assertEqual({b["needs_reply"] for b in briefs}, {True, False})

    def test_every_brief_fact_encodes_reply_obligation(self):
        for b in B.build_briefs(22, per_family=2):
            text = " ".join(b["facts"])
            if b["needs_reply"]:
                self.assertTrue(RG.has_reply_request(text), b["brief_id"])
            else:
                self.assertFalse(RG.has_reply_request(text), b["brief_id"])


class ExportSourceVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tax = load_taxonomy()

    def _source(self, body, needs_reply, action="none", sid="src_t"):
        return {"source_id": sid, "lineage_id": "lin_ev_t", "role": "training",
                "domain": "training", "family": "invoice_due", "action": action,
                "intent": {"category": "Billing", "needs_reply": needs_reply,
                           "observable": "visible"},
                "email": {"from_addr": "billing@acme.com", "to_addr": "o@x.com",
                          "subject": "Invoice 1187", "date": "2025-09-01",
                          "body": body}}

    def test_verify_source_accepts_visible_reply(self):
        self.assertEqual(E.verify_source(self._source(REPLY_INVOICE, True),
                                         self.tax), [])
        self.assertEqual(E.verify_source(self._source(NOREPLY_INVOICE, False),
                                         self.tax), [])

    def test_verify_source_rejects_invisible_reply(self):
        problems = E.verify_source(self._source(OLD_INVOICE, True), self.tax)
        self.assertTrue(any("reply_gold:reply_obligation_not_visible" in p
                            for p in problems), problems)

    def test_export_rejects_invisible_reply_source(self):
        res = E.export_sources([self._source(OLD_INVOICE, True)],
                               [], self.tax, domain="training", seed=1)
        self.assertEqual(res["accepted"], [])
        self.assertTrue(res["rejected"][0]["reason"].startswith(
            "source_verification_failed"), res["rejected"])


if __name__ == "__main__":
    unittest.main()
