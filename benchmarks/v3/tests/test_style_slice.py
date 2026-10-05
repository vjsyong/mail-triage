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

from benchmarks.v3.build import style_slice as es  # noqa: E402
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
        refs = es.sample_references(source="enron", seed=1, n=5, min_groups=5, enron_root=self.root)
        users = {r["user"] for r in refs}
        self.assertEqual(len(refs), 5)
        self.assertGreaterEqual(len(users), 5)
        self.assertNotIn("bad1", users)
        self.assertNotIn("bad2", users)

    def test_determinism(self):
        a = es.sample_references(source="enron", seed=7, n=4, min_groups=4, enron_root=self.root)
        b = es.sample_references(source="enron", seed=7, n=4, min_groups=4, enron_root=self.root)
        self.assertEqual([r["message_id"] for r in a], [r["message_id"] for r in b])
        self.assertEqual([r["sha256"] for r in a], [r["sha256"] for r in b])

    def test_insufficient_users_raises(self):
        with self.assertRaises(es.BuildError):
            es.sample_references(source="enron", seed=1, n=8, min_groups=8, enron_root=self.root)


class TestProvenance(unittest.TestCase):
    def test_sha_matches_file(self):
        root = tempfile.mkdtemp(prefix="enron-")
        try:
            path = os.path.join(root, "u1", "sent", "1.")
            _write_msg(path, "a@x.com", "b@y.com", "subject here", _body("hello"))
            ref = es.sample_references(source="enron", seed=2, n=1, min_groups=1, enron_root=root)[0]
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
            problems, stats = es.guard(ref, gen, w, allowed, pii_guard=True)
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


    def test_pii_guard_off_by_default(self):
        w, p, q = _world_ref_emails()
        ref = _ref()
        gen = {"subject": "New note",
               "body": "Hello, this mentions Kenneth Lay here.",
               "from_email": p["email"], "to_email": q["email"]}
        probs, stats = es.guard(ref, gen, w, ["3 March 2025"])   # default off
        self.assertFalse(any("PII leak" in x for x in probs))
        self.assertTrue(stats["pii_hits"])                       # still reported
        probs2, _ = es.guard(ref, gen, w, ["3 March 2025"], pii_guard=True)
        self.assertTrue(any("PII leak" in x for x in probs2))


def _mbox(messages):
    out = []
    for (from_, to, subject, body) in messages:
        out.append("From sender@x.com Mon Mar  3 10:15:00 2025")
        out.append("From: %s" % from_)
        out.append("To: %s" % to)
        out.append("Subject: %s" % subject)
        out.append("Date: Mon, 3 Mar 2025 10:15:00 -0600")
        out.append("Content-Type: text/plain; charset=us-ascii")
        out.append("")
        out.append(body)
        out.append("")           # blank line before the next envelope
    return "\n".join(out).encode("utf-8")


class TestMboxSplitter(unittest.TestCase):
    def test_splits_real_envelopes_only(self):
        body1 = _body("first message") + "\nFrom the desk of Bob\n"
        body2 = _body("second message")
        data = _mbox([("a@x.com", "b@y.com", "one", body1),
                      ("c@x.com", "d@y.com", "two", body2)])
        chunks = es._split_mbox(data)
        self.assertEqual(len(chunks), 2)
        self.assertIn(b"From the desk of Bob", chunks[0])   # body From not split
        self.assertIn(b"second message", chunks[1])

    def test_no_envelope_returns_empty(self):
        self.assertEqual(es._split_mbox(b"just some text\nno envelope\n"), [])


class TestIetfSampling(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ietf-")
        for lst in ("oauth", "dmarc"):
            d = os.path.join(self.root, lst)
            os.makedirs(d, exist_ok=True)
            data = _mbox([("%s-a@x.com" % lst, "b@y.com", "%s alpha topic" % lst,
                           _body("list %s message one" % lst)),
                          ("%s-b@x.com" % lst, "b@y.com", "%s beta topic" % lst,
                           _body("list %s message two" % lst))])
            with open(os.path.join(d, "2020-01.mail"), "wb") as fh:
                fh.write(data)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_ietf_spread_and_determinism(self):
        refs = es.sample_references(source="ietf", seed=3, n=2, ietf_root=self.root)
        self.assertEqual(len(refs), 2)
        self.assertEqual(len({r["group"] for r in refs}), 2)
        again = es.sample_references(source="ietf", seed=3, n=2, ietf_root=self.root)
        self.assertEqual([r["sha256"] for r in refs], [r["sha256"] for r in again])


class TestSpamSampling(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="spam-")
        for group in ("easy_ham", "hard_ham"):
            d = os.path.join(self.root, group)
            os.makedirs(d, exist_ok=True)
            _write_msg(os.path.join(d, "00001.abc"), "a@x.com", "b@y.com",
                       "%s topic" % group, _body("ham body %s" % group))
        with open(os.path.join(self.root, "easy_ham", "cmds"), "w") as fh:
            fh.write("not a message at all")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_spam_balanced_and_skips_cmds(self):
        refs = es.sample_references(source="spamassassin", seed=4, n=2,
                                    spam_root=self.root)
        self.assertEqual({r["group"] for r in refs}, {"easy_ham", "hard_ham"})
        self.assertFalse(any(r["message_id"].endswith("cmds") for r in refs))

    def test_mix_plan(self):
        ietf = tempfile.mkdtemp(prefix="mix-ietf-")
        try:
            for lst in ("oauth", "dmarc"):
                d = os.path.join(ietf, lst)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "m.mail"), "wb") as fh:
                    fh.write(_mbox([("%s1@x.com" % lst, "b@y.com", "%s one" % lst,
                                     _body("one")),
                                    ("%s2@x.com" % lst, "b@y.com", "%s two" % lst,
                                     _body("two"))]))
            spam = tempfile.mkdtemp(prefix="mix-spam-")
            try:
                for group in ("easy_ham", "hard_ham"):
                    d = os.path.join(spam, group)
                    os.makedirs(d, exist_ok=True)
                    for j in (1, 2):
                        _write_msg(os.path.join(d, "0000%d.x" % j), "a@x.com",
                                   "b@y.com", "%s %d" % (group, j),
                                   _body("body %s %d" % (group, j)))
                refs = es.sample_references(source="mix", seed=5, n=8,
                                            ietf_root=ietf, spam_root=spam)
                self.assertEqual(len(refs), 8)
                self.assertEqual(sum(1 for r in refs if r["source"] == "ietf"), 4)
                self.assertEqual(sum(1 for r in refs if r["group"] == "easy_ham"), 2)
                self.assertEqual(sum(1 for r in refs if r["group"] == "hard_ham"), 2)
            finally:
                shutil.rmtree(spam, ignore_errors=True)
        finally:
            shutil.rmtree(ietf, ignore_errors=True)


class TestNewsletter(unittest.TestCase):
    def setUp(self):
        self.spam = tempfile.mkdtemp(prefix="nl-spam-")
        self.enron = tempfile.mkdtemp(prefix="nl-enron-")
        hard = os.path.join(self.spam, "hard_ham")
        easy = os.path.join(self.spam, "easy_ham")
        os.makedirs(hard, exist_ok=True)
        os.makedirs(easy, exist_ok=True)
        # hard_ham: one signal, one non-signal (must be excluded)
        _write_msg(os.path.join(hard, "00001.a"), "news@x.com", "b@y.com",
                   "Monthly Newsletter - March", _body("unsubscribe from this issue"))
        _write_msg(os.path.join(hard, "00002.b"), "a@x.com", "b@y.com",
                   "lunch today", _body("are we still on for lunch"))
        # easy_ham: digest marker via list tag
        _write_msg(os.path.join(easy, "00001.c"), "list@x.com", "b@y.com",
                   "[dev] digest volume 3", _body("weekly digest of the list"))
        # enron: inbox bulletin (kept) and sent duplicate (excluded: inbox-only)
        _write_msg(os.path.join(self.enron, "u1", "inbox", "1."), "n@x.com",
                   "b@y.com", "Weekly Update", _body("weekly update bulletin"))
        _write_msg(os.path.join(self.enron, "u1", "sent", "1."), "n@x.com",
                   "b@y.com", "Weekly Update", _body("weekly update bulletin"))

    def tearDown(self):
        shutil.rmtree(self.spam, ignore_errors=True)
        shutil.rmtree(self.enron, ignore_errors=True)

    def test_signals_per_source(self):
        news = {"subject": "Monthly Newsletter", "body": "... unsubscribe ...",
                "from_name": "", "from_email": "", "to_name": "", "to_email": "", "cc": []}
        plain = {"subject": "lunch", "body": "are we still on", "from_name": "",
                 "from_email": "", "to_name": "", "to_email": "", "cc": []}
        self.assertTrue(es._newsletter_signal(news, "hard_ham"))
        self.assertFalse(es._newsletter_signal(plain, "hard_ham"))
        self.assertTrue(es._newsletter_signal({"subject": "[dev] digest", "body": "x",
                                               "from_name": "", "from_email": "",
                                               "to_name": "", "to_email": "", "cc": []},
                                              "easy_ham"))
        self.assertTrue(es._newsletter_signal(news, "enron"))
        self.assertFalse(es._newsletter_signal(plain, "enron"))

    def test_newsletter_sampling_filters_and_scope(self):
        refs = es.sample_references(source="mix", family="newsletter", seed=9, n=3,
                                    min_groups=3, enron_root=self.enron,
                                    spam_root=self.spam)
        groups = {r["group"] for r in refs}
        self.assertEqual(groups, {"hard_ham", "easy_ham", "enron"})
        self.assertTrue(all(r["register"] == "newsletter" for r in refs))
        # the non-signal hard_ham and the sent-folder enron message are excluded
        self.assertFalse(any("00002" in r["message_id"] for r in refs))
        self.assertTrue(all("/inbox/" in r["message_id"] or r["source"] != "enron"
                            for r in refs))

    def test_register_aware_prompt(self):
        w, p, q = _world_ref_emails()
        ref = _ref()
        nl = es.build_prompt(ref, p, q, "monthly_digest", "3 March 2025",
                             "8 March 2025", register="newsletter")
        self.assertIn("NEWSLETTER", nl[0]["content"])
        self.assertIn("monthly digest", nl[1]["content"].lower())
        gen = es.build_prompt(ref, p, q, "meeting", "3 March 2025", "8 March 2025")
        self.assertNotIn("NEWSLETTER", gen[0]["content"])

    def test_newsletter_word_band(self):
        w, p, q = _world_ref_emails()
        ref = _ref()
        words = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet "
                 "kilo lima mike november oscar papa quebec romeo sierra tango")
        body = " ".join((words.split() * 12)[:220])
        gen = {"subject": "Issue", "body": body, "from_email": p["email"],
               "to_email": q["email"]}
        nl, _ = es.guard(ref, gen, w, ["3 March 2025"], register="newsletter")
        gen_probs, _ = es.guard(ref, gen, w, ["3 March 2025"], register="general")
        self.assertFalse(any("band" in x for x in nl))
        self.assertTrue(any("band" in x for x in gen_probs))

    def test_newsletter_sender_is_role_mailbox(self):
        w = World.build()
        from benchmarks.v3.build.rng import stream
        s = es.pick_newsletter_sender(w, stream(1, "nl"))
        self.assertIn(s["email"].split("@")[0], es.NEWS_ROLES)
        self.assertIn(s["domain"], w.domains)


class TestDateNormalization(unittest.TestCase):
    def test_us_format_equals_eu_allowed(self):
        for text in ("Meet on June 1, 2026 please.",
                     "Meet on June 1 2026 please.",
                     "Meet on 01 June 2026 please.",
                     "Meet on 1st June 2026 please.",
                     "Meet on 2026-06-01 please.",
                     "Meet on 1 June 2026 please."):
            self.assertEqual(es._dates_consistent(text, ["1 June 2026"]), [], text)

    def test_wrong_full_date_rejected(self):
        allowed = ["1 June 2026", "6 June 2026"]
        self.assertTrue(es._dates_consistent("See you on 5 June 2026.", allowed))
        self.assertTrue(es._dates_consistent("See you on June 7, 2026.", allowed))
        self.assertTrue(es._dates_consistent("See you on 1 July 2026.", allowed))
        self.assertTrue(es._dates_consistent("See you on 1 June 2027.", allowed))

    def test_month_year_consistency(self):
        allowed = ["1 June 2026", "10 June 2026"]
        self.assertEqual(es._dates_consistent("Sometime in June 2026.", allowed), [])
        self.assertTrue(es._dates_consistent("Sometime in July 2026.", allowed))
        self.assertTrue(es._dates_consistent("Sometime in June 2027.", allowed))

    def test_times_amounts_ids_numbers_not_dates(self):
        allowed = ["1 June 2026"]
        for text in ("Login at 13:47 today.", "Total $310.75 due.",
                     "Reference INV-23F5GNW.", "Account 12345.",
                     "Send 2500 units.", "The number 2026 alone."):
            self.assertEqual(es._dates_consistent(text, allowed), [], text)

    def test_guard_accepts_us_date(self):
        w, p, q = _world_ref_emails()
        ref = _ref()
        body = ("Join us on June 1, 2026 for the monthly session. " + " ".join(
            ["alpha bravo charlie delta echo foxtrot golf hotel india juliet"] * 4))
        gen = {"subject": "Digest", "body": body, "from_email": p["email"],
               "to_email": q["email"]}
        probs, _ = es.guard(ref, gen, w, ["1 June 2026"], register="newsletter")
        self.assertFalse(any("match the provided dates" in x for x in probs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
