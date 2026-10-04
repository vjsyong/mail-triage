"""Unit tests for the Enron style-transfer thin slice.

All offline: a temporary synthetic maildir stands in for the read-only corpus,
and guards are exercised directly. Zero model calls.
"""
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

from benchmarks.v3.build import enron_slice as es  # noqa: E402
from benchmarks.v3.build.world import World  # noqa: E402


def _write_msg(path, from_, to, subject, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = ("From: %s\r\nTo: %s\r\nSubject: %s\r\n"
           "Date: Mon, 3 Mar 2025 10:15:00 -0600\r\n"
           "Content-Type: text/plain; charset=us-ascii\r\n\r\n%s"
           % (from_, to, subject, body))
    with open(path, "wb") as fh:
        fh.write(raw.encode("utf-8"))


def _body(text):
    return text + " " + ("lorem ipsum " * 12)


class TestSampling(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="enron-")
        # 6 eligible, distinct users/subjects
        for i, (user, folder, subj) in enumerate([
                ("u1", "sent", "quarterly planning meeting"),
                ("u2", "sent_items", "gas trading desk update"),
                ("u3", "inbox", "invoice approval needed"),
                ("u4", "sent", "travel logistics for houston"),
                ("u5", "inbox", "calendar scheduling request"),
                ("u6", "sent", "pipeline capacity follow up")]):
            _write_msg(os.path.join(self.root, user, folder, "1."),
                       "a@x.com", "b@y.com", subj, _body("Body text number %d." % i))
        # ineligible ones
        _write_msg(os.path.join(self.root, "bad1", "sent", "1."),
                   "a@x.com", "b@y.com", "short", "too short")
        _write_msg(os.path.join(self.root, "bad2", "sent", "1."),
                   "a@x.com", "b@y.com", "quoted",
                   _body("Hi") + "\n> quote one\n> quote two\n")
        _write_msg(os.path.join(self.root, "bad3", "sent", "1."),
                   "a@x.com", "b@y.com", "fwd", _body("Forwarded by Someone"))
        _write_msg(os.path.join(self.root, "bad4", "sent", "1."),
                   "a@x.com", "b@y.com", "VIAGRA now", _body("click here now"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_filters_and_min_users(self):
        refs = es.sample_references(self.root, seed=1, n=5, min_users=5)
        users = {r["user"] for r in refs}
        self.assertEqual(len(refs), 5)
        self.assertGreaterEqual(len(users), 5)
        self.assertNotIn("bad1", users)
        self.assertNotIn("bad2", users)

    def test_determinism(self):
        a = es.sample_references(self.root, seed=7, n=4, min_users=4)
        b = es.sample_references(self.root, seed=7, n=4, min_users=4)
        self.assertEqual([r["message_id"] for r in a], [r["message_id"] for r in b])
        self.assertEqual([r["sha256"] for r in a], [r["sha256"] for r in b])

    def test_insufficient_users_raises(self):
        with self.assertRaises(es.BuildError):
            es.sample_references(self.root, seed=1, n=8, min_users=8)


class TestProvenance(unittest.TestCase):
    def test_sha_matches_file(self):
        root = tempfile.mkdtemp(prefix="enron-")
        try:
            path = os.path.join(root, "u1", "sent", "1.")
            _write_msg(path, "a@x.com", "b@y.com", "subject here", _body("hello"))
            ref = es.sample_references(root, seed=2, n=1, min_users=1)[0]
            import hashlib
            with open(path, "rb") as fh:
                self.assertEqual(ref["sha256"], hashlib.sha256(fh.read()).hexdigest())
            rec = es.provenance_record(ref)
            self.assertEqual(rec["user"], "u1")
            self.assertEqual(rec["folder"], "sent")
            self.assertIn("message_id", rec)
        finally:
            shutil.rmtree(root, ignore_errors=True)


def _ref():
    parsed = {
        "subject": "Trading update",
        "from_name": "Kenneth Lay", "from_email": "kenneth.lay@enron.com",
        "to_name": "Jeff Skilling", "to_email": "jeff.skilling@enron.com",
        "cc": [],
        "body": ("Ken,\n\nPlease review the numbers for the western desk and let "
                 "me know if anything looks off before the close. The gas book is "
                 "well within limits and nothing needs escalation today.\n\n"
                 "Regards,\nKenneth Lay\n555-867-5309\n"),
    }
    return {"id": "ref01", "user": "lay-k", "folder": "sent", "message_id": "lay-k/sent/1.",
            "sha256": "0" * 64, "bytes": 10, "parsed": parsed}


def _world_ref_emails():
    w = World.build()
    p = w.people[0]
    q = next(x for x in w.people if x["org_id"] != p["org_id"])
    return w, p, q


class TestGuards(unittest.TestCase):
    def test_pii_guard_rejects_name_email_phone(self):
        w, p, q = _world_ref_emails()
        ref = _ref()
        allowed = ["3 March 2025"]
        for bad in ("Kenneth Lay", "kenneth.lay@enron.com", "555-867-5309"):
            gen = {"subject": "New note", "body": "Hello, this mentions %s here." % bad,
                   "from_email": p["email"], "to_email": q["email"]}
            problems, stats = es.guard(ref, gen, w, allowed)
            self.assertTrue(any("PII leak" in x for x in problems), bad)
            self.assertTrue(stats["pii_hits"])

    def test_copy_8gram_detected(self):
        w, p, q = _world_ref_emails()
        ref = _ref()
        copied = ("Please review the numbers for the western desk and let me know "
                  "if anything looks off before the close.")
        gen = {"subject": "New", "body": copied + " Extra words to pad this out a bit "
               "so the body is long enough to pass the length band check for tests.",
               "from_email": p["email"], "to_email": q["email"]}
        problems, stats = es.guard(ref, gen, w, ["3 March 2025"])
        self.assertTrue(any("copy" in x or "verbatim" in x for x in problems))

    def test_generic_greeting_allowed(self):
        w, p, q = _world_ref_emails()
        ref = _ref()
        gen = {"subject": "Fresh subject",
               "body": ("Dear colleague,\n\nWe should align on the delivery window "
                        "for the northern contract and confirm the new owner before "
                        "Friday so the team can proceed without delay.\n\nRegards,\n"
                        + p["full"]),
               "from_email": p["email"], "to_email": q["email"]}
        problems, stats = es.guard(ref, gen, w, ["3 March 2025"])
        self.assertFalse(any("copy" in x or "verbatim" in x for x in problems))
        self.assertEqual(stats["pii_hits"], [])

    def test_world_identity_required(self):
        w, p, q = _world_ref_emails()
        ref = _ref()
        gen = {"subject": "New", "body": "A sufficiently long new message body that "
               "is entirely original and clears the word count band easily here.",
               "from_email": "x@notaworld.example", "to_email": q["email"]}
        problems, _ = es.guard(ref, gen, w, ["3 March 2025"])
        self.assertTrue(any("not a world entity" in x for x in problems))

    def test_hygiene_and_claim_checks(self):
        w, p, q = _world_ref_emails()
        ref = _ref()
        gen = {"subject": "New", "body": "Hello!! Please find attached the file and "
               "I have cc'd the wider team on this note for their awareness now.",
               "from_email": p["email"], "to_email": q["email"]}
        problems, _ = es.guard(ref, gen, w, ["3 March 2025"])
        self.assertTrue(any("doubled" in x for x in problems))
        self.assertTrue(any("claim" in x for x in problems))


class TestPromptLeakage(unittest.TestCase):
    def test_prompt_contains_only_style_reference(self):
        w, p, q = _world_ref_emails()
        ref = _ref()
        msgs = es.build_prompt(ref, p, q, "meeting", "3 March 2025", "8 March 2025")
        joined = msgs[0]["content"] + msgs[1]["content"]
        self.assertIn(ref["parsed"]["body"], joined)          # reference present
        self.assertIn("untrusted", msgs[1]["content"])         # flagged untrusted
        for leak in ("jeff.skilling@enron.com", "555-867-5309"):
            self.assertNotIn(leak, msgs[0]["content"])         # system has no source
        # Only the one reference body; no other corpus message text.
        self.assertEqual(joined.count(ref["parsed"]["body"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
