"""S1 correction: visible category evidence vs the configured definitions."""
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
from benchmarks.v3.training import category_gold as CG  # noqa: E402
from benchmarks.v3.training import export as E, reply_gold as RG  # noqa: E402
from benchmarks.v3.training.taxonomies import load_taxonomy  # noqa: E402

# The exact old contradictory texts the orchestrator flagged.
OLD_ACTION = ("Automated notification: the document is ready in the shared "
              "drive. No action is required.")
SHIPPING_ONLY = "Order 4471 has shipped. Tracking number XY123. Thanks for your order."
COMPLETED_PAYMENT = ("Your online purchase completed and the payment received. "
                     "Thank you for your order.")
REAL_WORK_REQUEST = ("Please review the document in the shared drive and complete "
                     "the review in the portal. No reply is needed.")


class CategoryEvidenceTests(unittest.TestCase):
    def test_old_no_action_text_is_not_action(self):
        problems = CG.category_problems(OLD_ACTION, "Action")
        self.assertIn("category_evidence_not_visible", problems)
        self.assertTrue(any(p.startswith("action_negated") for p in problems), problems)

    def test_shipping_only_text_is_not_a_receipt(self):
        problems = CG.category_problems(SHIPPING_ONLY, "Receipt")
        self.assertIn("category_evidence_not_visible", problems)

    def test_completed_payment_text_is_a_receipt(self):
        self.assertEqual(CG.category_problems(COMPLETED_PAYMENT, "Receipt"), [])

    def test_real_work_request_is_action_without_email_reply(self):
        self.assertEqual(CG.category_problems(REAL_WORK_REQUEST, "Action"), [])
        self.assertEqual(RG.reply_problems(REAL_WORK_REQUEST, False), [])
        # labelling it needs_reply=True is rejected: no visible reply request
        self.assertIn("reply_obligation_not_visible",
                      RG.reply_problems(REAL_WORK_REQUEST, True))


class BriefCoherenceTests(unittest.TestCase):
    def test_every_brief_variant_is_visibly_coherent(self):
        for brief in B.build_briefs(11, per_family=2):
            text = " ".join(brief["facts"])
            phrase = brief.get("reply_phrase") or brief.get("no_reply_phrase") or ""
            text = text + " " + phrase
            self.assertEqual(
                CG.category_problems(text, brief["category"],
                                     brief["category_evidence"]), [],
                brief["brief_id"])
            self.assertEqual(RG.reply_problems(text, brief["needs_reply"]), [],
                             brief["brief_id"])

    def test_action_no_reply_variant_is_a_real_work_request(self):
        v1 = [b for b in B.build_briefs(11, per_family=2)
              if b["family"] == "task_request" and not b["needs_reply"]][0]
        self.assertEqual(v1["category"], "Action")
        self.assertNotIn("automated", v1["purpose"].lower())
        self.assertTrue(any("review" in f.lower() for f in v1["facts"]), v1["facts"])
        self.assertEqual(CG.category_problems(
            " ".join(v1["facts"]) + " No reply is needed", "Action",
            v1["category_evidence"]), [])

    def test_receipt_variant_requires_completed_payment(self):
        for b in B.build_briefs(11, per_family=2):
            if b["family"] != "receipt":
                continue
            text = " ".join(b["facts"])
            self.assertEqual(CG.category_problems(text, "Receipt",
                                                  b["category_evidence"]), [],
                             b["brief_id"])
            self.assertTrue(
                any(k in text.lower() for k in ("payment received", "purchase completed")),
                b["facts"])


class ExportSourceCategoryTests(unittest.TestCase):
    def setUp(self):
        self.tax = load_taxonomy()

    def _source(self, text, category, needs_reply, action="none"):
        return {"source_id": "src_x", "lineage_id": "lin_ev_x", "role": "training",
                "domain": "training", "action": action,
                "intent": {"category": category, "needs_reply": needs_reply,
                           "observable": "visible"},
                "email": {"from_addr": "a@b.com", "to_addr": "o@x.com",
                          "subject": "s", "date": "2025-09-01", "body": text}}

    def test_export_rejects_old_action_contradiction(self):
        res = E.export_sources([self._source(OLD_ACTION, "Action", False)],
                               [], self.tax, domain="training", seed=1)
        self.assertEqual(res["accepted"], [])
        self.assertTrue(res["rejected"][0]["reason"].startswith(
            "source_verification_failed"), res["rejected"])
        self.assertIn("category_gold", res["rejected"][0]["reason"])

    def test_export_rejects_shipping_only_receipt(self):
        res = E.export_sources([self._source(SHIPPING_ONLY, "Receipt", False)],
                               [], self.tax, domain="training", seed=1)
        self.assertEqual(res["accepted"], [])
        self.assertIn("category_gold", res["rejected"][0]["reason"])

    def test_export_accepts_real_work_request(self):
        res = E.export_sources([self._source(REAL_WORK_REQUEST, "Action", False)],
                               [], self.tax, domain="training", seed=1)
        self.assertEqual(res["rejected"], [])


if __name__ == "__main__":
    unittest.main()
