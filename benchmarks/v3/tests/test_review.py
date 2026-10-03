"""Review worksheet / import / seal gate tests (WP6).

Fresh records are draft and unsealed; review requires imported named human
judgements; disagreement blocks; sealing demands reviewed items and an explicit
reviewer; real-mail import is de-identified and separately authorized.
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3 import build, schema  # noqa: E402
from benchmarks.v3.build.errors import (ImportAuthorizationError,  # noqa: E402
                                        ReviewError)

_BASE = None


def base_bundle():
    global _BASE
    if _BASE is None:
        _BASE = build.build_dataset(triage_roots=4, workflow_roots=1, seed=7)
    return copy.deepcopy(_BASE)


class WorksheetTest(unittest.TestCase):
    def test_worksheet_is_fresh_draft_and_unsealed(self):
        b = base_bundle()
        ws = build.build_review_worksheet(b)
        self.assertEqual(ws["review_status"], "draft")
        self.assertFalse(ws["human_seal"])
        self.assertTrue(ws["items"])
        for item in ws["items"]:
            self.assertEqual(item["review_status"], "draft")
            self.assertFalse(item["human_seal"])
        self.assertEqual(build.validate_review_worksheet(ws), [])

    def test_worksheet_scoping(self):
        b = base_bundle()
        ws = build.build_review_worksheet(b, splits=["development"])
        self.assertTrue(ws["items"])


class ImportTest(unittest.TestCase):
    def test_unanimous_accept_marks_reviewed(self):
        b = base_bundle()
        ws = build.build_review_worksheet(b)
        judges = {i["gold_id"]: {"decision": "accept", "reviewer": "alice"}
                  for i in ws["items"]}
        result = build.import_review(b, ws, judges)
        self.assertEqual(result["review_status"], "reviewed")
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["unreviewed"], [])
        for gold in result["bundle"]["gold"]:
            self.assertEqual(gold["review_status"], "reviewed")
            self.assertFalse(gold["human_seal"])

    def test_reviewer_required(self):
        b = base_bundle()
        ws = build.build_review_worksheet(b)
        judges = {ws["items"][0]["gold_id"]: {"decision": "accept"}}
        with self.assertRaises(ReviewError):
            build.import_review(b, ws, judges)

    def test_double_review_agreement(self):
        b = base_bundle()
        ws = build.build_review_worksheet(b)
        judges = []
        for item in ws["items"]:
            judges.append({"gold_id": item["gold_id"], "decision": "accept",
                           "reviewer": "alice"})
            judges.append({"gold_id": item["gold_id"], "decision": "accept",
                           "reviewer": "bob"})
        result = build.import_review(b, ws, judges)
        self.assertEqual(result["agreement"]["multi_reviewed"], len(ws["items"]))
        self.assertEqual(result["agreement"]["agreement"], 1.0)
        self.assertEqual(result["conflicts"], [])

    def test_disagreement_is_a_conflict_and_stays_draft(self):
        b = base_bundle()
        ws = build.build_review_worksheet(b)
        gid = ws["items"][0]["gold_id"]
        judges = {gid: [{"decision": "accept", "reviewer": "alice"},
                        {"decision": "reject", "reviewer": "bob"}]}
        result = build.import_review(b, ws, judges)
        self.assertTrue(result["conflicts"])
        self.assertEqual(result["review_status"], "draft")
        gold = next(g for g in result["bundle"]["gold"] if g["gold_id"] == gid)
        self.assertEqual(gold["review_status"], "draft")

    def test_rejection_leaves_draft(self):
        b = base_bundle()
        ws = build.build_review_worksheet(b)
        judges = {i["gold_id"]: {"decision": "reject", "reviewer": "alice"}
                  for i in ws["items"]}
        result = build.import_review(b, ws, judges)
        self.assertEqual(len(result["rejected"]), len(ws["items"]))
        self.assertTrue(all(g["review_status"] == "draft"
                            for g in result["bundle"]["gold"]))


class SealTest(unittest.TestCase):
    def test_seal_demands_reviewed_items(self):
        b = base_bundle()
        with self.assertRaises(ReviewError):
            build.seal_bundle(b, reviewer="alice")

    def test_seal_demands_named_reviewer(self):
        b = base_bundle()
        ws = build.build_review_worksheet(b)
        result = build.import_review(b, ws, {i["gold_id"]: {
            "decision": "accept", "reviewer": "alice"} for i in ws["items"]})
        with self.assertRaises(ReviewError):
            build.seal_bundle(result["bundle"], reviewer="   ")

    def test_seal_after_review(self):
        b = base_bundle()
        ws = build.build_review_worksheet(b)
        result = build.import_review(b, ws, {i["gold_id"]: {
            "decision": "accept", "reviewer": "alice"} for i in ws["items"]})
        sealed = build.seal_bundle(result["bundle"], reviewer="alice")
        self.assertEqual(sealed["metadata"]["review_status"], "sealed")
        for gold in sealed["gold"]:
            self.assertEqual(gold["review_status"], "sealed")
            self.assertTrue(gold["human_seal"])
            self.assertEqual(gold["reviewer"], "alice")
        self.assertEqual(build.validate_dataset(sealed), [])

    def test_annotation_conflict_prevents_seal(self):
        b = base_bundle()
        ws = build.build_review_worksheet(b)
        result = build.import_review(b, ws, {i["gold_id"]: {
            "decision": "accept", "reviewer": "alice"} for i in ws["items"]})
        conflict = [{"gold_id": ws["items"][0]["gold_id"],
                     "decisions": ["accept", "reject"]}]
        with self.assertRaises(ReviewError):
            build.seal_bundle(result["bundle"], reviewer="alice",
                              conflicts=conflict)

    def test_fresh_build_has_no_seal(self):
        b = base_bundle()
        self.assertFalse(any(g["human_seal"] for g in b["gold"]))
        self.assertEqual(b["metadata"]["review_status"], "draft")


class WorksheetExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v3-review-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_load_roundtrip(self):
        b = base_bundle()
        ws = build.build_review_worksheet(b)
        path = os.path.join(self.tmp, "ws.json")
        build.write_review_worksheet(ws, path)
        loaded = build.load_review_worksheet(path)
        self.assertEqual(loaded["dataset_id"], ws["dataset_id"])
        self.assertEqual(build.validate_review_worksheet(loaded), [])

    def test_export_refuses_dishonest_worksheet(self):
        b = base_bundle()
        ws = build.build_review_worksheet(b)
        ws["items"][0]["human_seal"] = True
        with self.assertRaises(ReviewError):
            build.write_review_worksheet(ws, os.path.join(self.tmp, "ws.json"))


class RealMailTest(unittest.TestCase):
    def _real_bundle(self):
        b = base_bundle()
        b = copy.deepcopy(b)
        b["gold"][0]["source"] = "real_mail"
        return b

    def test_real_import_without_authorization_rejected(self):
        b = self._real_bundle()
        ws = build.build_review_worksheet(b)
        gid = ws["items"][0]["gold_id"]
        with self.assertRaises(ImportAuthorizationError):
            build.import_review(b, ws, {gid: {"decision": "accept",
                                              "reviewer": "alice"}})

    def test_real_import_with_authorization_accepted(self):
        b = self._real_bundle()
        ws = build.build_review_worksheet(b)
        gid = ws["items"][0]["gold_id"]
        auth = {"consent": {"consent_ref": "consent-1"},
                "authorization_ref": "auth-1", "authorized_by": "owner"}
        result = build.import_review(b, ws, {gid: {"decision": "accept",
                                                   "reviewer": "alice"}},
                                     authorization=auth)
        self.assertIn(gid, result["reviewed"])

    def test_real_seal_requires_authorization(self):
        gold = schema.new_gold("c", "g", source="real_mail")
        with self.assertRaises(schema.SealError):
            schema.seal_gold(gold, "alice")

    def test_validate_real_import_rejects_raw_mail(self):
        with self.assertRaises(ReviewError):
            build.validate_real_import(
                [{"gold_id": "g", "decision": "accept", "reviewer": "a",
                  "body": "raw message body"}],
                authorization={"consent": {"consent_ref": "c"},
                               "authorization_ref": "a", "authorized_by": "o"})

    def test_validate_real_import_requires_authorization(self):
        with self.assertRaises(ImportAuthorizationError):
            build.validate_real_import(
                [{"gold_id": "g", "decision": "accept", "reviewer": "a"}],
                authorization=None)

    def test_validate_real_import_accepts_deidentified(self):
        rows = [{"gold_id": "g", "decision": "accept", "reviewer": "a"}]
        auth = {"consent": {"consent_ref": "c"}, "authorization_ref": "a",
                "authorized_by": "o"}
        out = build.validate_real_import(rows, authorization=auth)
        self.assertEqual(out[0]["gold_id"], "g")


if __name__ == "__main__":
    unittest.main()
