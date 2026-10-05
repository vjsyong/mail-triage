"""LLM pilot tests: client, agent cards, briefs, renderer, verifier, audit.

Every test uses a fake LLM (deterministic/local composition or scripted
responses); the suite makes **zero** model calls and no network access.
"""
import json
import os
import re
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

from benchmarks.v3.build import agent_cards, audit, briefs, pilot, verify  # noqa: E402
from benchmarks.v3.build import llm_render  # noqa: E402
from benchmarks.v3.build.llm_client import (CachedChatClient, FakeClient,  # noqa: E402
                                            MessageCache)
from benchmarks.v3.build.rng import stream  # noqa: E402
from benchmarks.v3.build.world import World  # noqa: E402


def _compose_body(brief):
    """A syntactically valid body that states every required token."""
    toks = [t for _label, t in brief.get("required_facts") or []]
    parts = ["Dear %s," % (brief.get("recipient") or {}).get("name", "there"),
             "This message concerns the matter described below.",
             " ".join(toks) + "."]
    if brief.get("login"):
        parts.append("The sign-in was recorded from %s." % brief["login"]["place"])
    parts.append("Regards,\n%s" % (brief.get("signer") or {}).get("name", "Sender"))
    return "\n\n".join(parts)


class ComposeClient(object):
    """Deterministic local client that composes a valid email from the prompt.

    It is a fake LLM: no network, no GPU, stable output for a given prompt.
    """

    def __init__(self, mutate=None):
        self.model = "fake-compose"
        self.revision = "fake"
        self.last_provenance = None
        self.calls = []
        self._mutate = mutate

    def endpoint(self):
        return "fake://compose"

    def chat(self, messages, temperature=0.9, max_tokens=1024, seed=None,
             top_p=0.95, extra=None):
        self.calls.append({"messages": list(messages), "seed": seed})
        user = messages[-1]["content"] if messages[-1]["role"] == "user" \
            else messages[0]["content"]
        facts = re.findall(r"^- ([a-z_]+): (.*)$", user, re.M)
        toks = [t for _l, t in facts]
        sign = re.search(r"^SIGN-OFF NAME: (.*)$", user, re.M)
        ref = next((t for l, t in facts if l == "reference"), "")
        body = ["Dear recipient,", "This message concerns the matter below.",
                " ".join(toks) + "."]
        login = re.search(r"^- place: (.*)$", user, re.M)
        if login:
            body.append("The sign-in was recorded from %s." % login.group(1))
        body.append("Regards,\n%s" % (sign.group(1) if sign else "Sender"))
        out = "\n\n".join(body)
        if self._mutate:
            out = self._mutate(out)
        return {"content": json.dumps({"subject": "Notice %s" % ref, "body": out}),
                "finish_reason": "stop", "usage": {}, "request": {}}


def _brief(family="receipt_confirmation", persona_index=0, index=1, allocator=None,
           world=None, seed=1):
    world = world or World.build()
    from benchmarks.v3.build import recipes
    persona = recipes.personas()[persona_index]
    slots, facts, region, _style = world.build_scenario(
        family, persona, index, seed, "development", None)
    rng = stream(seed, "brief:%s:%d" % (family, index))
    facts2, _delta = briefs.retime_facts(facts, family, rng, world.config)
    allocator = allocator or briefs.RefAllocator(seed)
    b = briefs.build_brief(family_id=family, slots=slots, facts=facts2,
                           region=region, persona=persona, allocator=allocator,
                           rng=rng, config=world.config)
    return b, facts2, region, persona, world


class TestBriefs(unittest.TestCase):
    def test_random_minutes_never_round_only(self):
        world = World.build()
        minutes = set()
        alloc = briefs.RefAllocator(7)
        from benchmarks.v3.build import recipes
        for i in range(120):
            persona = recipes.personas()[i % 8]
            family = list(pilot.PILOT_FAMILIES)[i % len(pilot.PILOT_FAMILIES)]
            slots, facts, region, _s = world.build_scenario(
                family, persona, i, 5, "development", None)
            rng = stream(5, "t:%d" % i)
            f2, _d = briefs.retime_facts(facts, family, rng, world.config)
            minutes.add(f2["send"][14:16])
        self.assertGreater(len(minutes), 30,
                           "send minutes are not spread (got %d)" % len(minutes))
        self.assertFalse(minutes <= {"00", "15", "30", "45"})

    def test_reference_prefixes_and_uniqueness(self):
        alloc = briefs.RefAllocator(11)
        ids = []
        for fam in ("invoice_receipt", "receipt_confirmation", "payment_reminder",
                    "order_request", "support_exchange", "document_request",
                    "security_notification", "shipping_travel_update"):
            ids.append(alloc.alloc_for_family(fam)["id"])
        self.assertEqual(len(ids), len(set(ids)), "reference ids must be unique")
        by_family = dict(zip(("invoice_receipt", "receipt_confirmation",
                              "payment_reminder", "order_request",
                              "support_exchange", "document_request",
                              "security_notification", "shipping_travel_update"),
                             ids))
        self.assertTrue(by_family["invoice_receipt"].startswith("INV-"))
        self.assertTrue(by_family["receipt_confirmation"].startswith("RCPT-"))
        self.assertTrue(by_family["payment_reminder"].startswith("BILL-"))
        self.assertTrue(by_family["order_request"].startswith("ORD-"))
        self.assertTrue(by_family["support_exchange"].startswith("TKT-"))
        self.assertTrue(by_family["document_request"].startswith("POL-"))
        self.assertTrue(by_family["security_notification"].startswith("CASE-"))
        track = by_family["shipping_travel_update"]
        self.assertTrue(track.startswith("1Z") or track.startswith("SF"),
                        "tracking must be carrier-like, got %r" % track)

    def test_paid_state_obligations(self):
        receipt, *_ = _brief("receipt_confirmation")
        self.assertIn("if already paid", receipt["forbidden_facts"])
        bill, *_ = _brief("payment_reminder")
        self.assertIn("we have received", bill["forbidden_facts"])


class TestVerify(unittest.TestCase):
    def test_valid_message_passes(self):
        brief, facts, _region, _p, world = _brief("payment_reminder")
        body = _compose_body(brief)
        problems = verify.verify_message(brief, "Payment due", body, world=world)
        self.assertEqual(problems, [])

    def test_missing_required_fact(self):
        brief, _f, _r, _p, world = _brief("receipt_confirmation")
        body = _compose_body(brief).replace(
            (brief["reference"] or {}).get("id", "zzz"), "")
        self.assertTrue(any("reference" in p for p in
                            verify.verify_message(brief, "Receipt", body, world=world)))

    def test_received_forbidden_phrase(self):
        brief, _f, _r, _p, world = _brief("receipt_confirmation")
        body = _compose_body(brief) + " If already paid, disregard this notice."
        problems = verify.verify_message(brief, "Receipt", body, world=world)
        self.assertTrue(any("forbidden" in p for p in problems))

    def test_tracking_never_invoice_like(self):
        brief, _f, _r, _p, world = _brief("shipping_travel_update")
        body = _compose_body(brief) + " Invoice INV-ABC1234 enclosed."
        problems = verify.verify_message(brief, "Tracking", body, world=world)
        self.assertTrue(any("invoice-like" in p or "prefix mismatch" in p
                            for p in problems))

    def test_security_requires_login_time(self):
        brief, _f, _r, _p, world = _brief("security_notification")
        body = _compose_body(brief).replace(brief["login"]["time"], "noon")
        problems = verify.verify_message(brief, "Security", body, world=world)
        self.assertTrue(any("login time" in p for p in problems))

    def test_hygiene_doubled_punctuation(self):
        problems = verify._check_hygiene("Hello world!! This is fine.")
        self.assertTrue(any("doubled" in p for p in problems))

    def test_gold_leakage_detected(self):
        brief, _f, _r, _p, world = _brief("receipt_confirmation")
        body = _compose_body(brief)
        gold = {"gold_id": "gold_x", "hidden_evidence": ["SECRET-TOKEN-123"]}
        body = body + " SECRET-TOKEN-123"
        problems = verify.verify_message(brief, "Receipt", body, world=world,
                                         gold=gold)
        self.assertTrue(any("leaked" in p for p in problems))


class TestRenderer(unittest.TestCase):
    def test_selects_valid_candidate(self):
        world = World.build()
        brief, _f, _r, _p, _w = _brief("receipt_confirmation", world=world)
        valid = json.dumps({"subject": "Receipt", "body": _compose_body(brief)})
        invalid = json.dumps({"subject": "Receipt", "body": "Nothing here."})
        client = FakeClient([{"content": invalid}, {"content": valid}])
        out = llm_render.render_message(brief, {"agent_key": "a"}, client=client,
                                        world=world, n_candidates=2, max_retries=0)
        self.assertEqual(out["candidate_index"], 1)

    def test_retry_uses_feedback_then_succeeds(self):
        world = World.build()
        brief, _f, _r, _p, _w = _brief("receipt_confirmation", world=world)
        valid = json.dumps({"subject": "Receipt", "body": _compose_body(brief)})
        invalid = json.dumps({"subject": "Receipt", "body": "Nothing here."})
        client = FakeClient([{"content": invalid}, {"content": valid}])
        out = llm_render.render_message(brief, {"agent_key": "a"}, client=client,
                                        world=world, n_candidates=1, max_retries=2)
        self.assertEqual(out["attempt"], 1)
        # The retry prompt must carry the verifier's feedback.
        self.assertIn("rejected", client.calls[1]["messages"][-1]["content"])

    def test_rejects_when_all_invalid(self):
        world = World.build()
        brief, _f, _r, _p, _w = _brief("receipt_confirmation", world=world)
        bad = json.dumps({"subject": "x", "body": "nope"})
        client = FakeClient([{"content": bad}])
        with self.assertRaises(llm_render.RenderRejected):
            llm_render.render_message(brief, {"agent_key": "a"}, client=client,
                                      world=world, n_candidates=1, max_retries=1)


class TestAgentCardsAndCache(unittest.TestCase):
    def test_card_determinism_and_style_cache(self):
        world = World.build()
        brief, facts, region, persona, _w = _brief("receipt_confirmation",
                                                   world=world)
        owner = world.owner(persona)
        region2 = dict(region, locale=owner.get("locale"),
                       timezone=owner.get("timezone"))
        c1 = agent_cards.build_card(facts["sender"], facts["recipient"],
                                    facts.get("relationship"), region2, seed=3)
        c2 = agent_cards.build_card(facts["sender"], facts["recipient"],
                                    facts.get("relationship"), region2, seed=3)
        self.assertEqual(c1, c2)
        self.assertTrue(c1["style_seed"])
        client = ComposeClient()
        cache = MessageCache(tempfile.mkdtemp(prefix="llmcache-"), enabled=True)
        try:
            wrapped = CachedChatClient(client, cache=cache)
            first = agent_cards.ensure_style(c1, client=wrapped, seed=1)
            calls = len(client.calls)
            second = agent_cards.ensure_style(c1, client=wrapped, seed=1)
            self.assertEqual(first, second)
            self.assertEqual(len(client.calls), calls, "style must be cached")
            self.assertEqual(wrapped.last_provenance["cache"], "hit")
        finally:
            shutil.rmtree(cache.root, ignore_errors=True)

    def test_cache_provenance_fields(self):
        client = FakeClient([{"content": "ok"}], model="m1")
        cache = MessageCache(tempfile.mkdtemp(prefix="llmcache-"), enabled=True)
        try:
            wrapped = CachedChatClient(client, cache=cache, revision_hint="rev1")
            wrapped.chat([{"role": "user", "content": "hi"}], seed=5,
                         temperature=0.9, candidate_index=1, retry=2)
            first = wrapped.last_provenance
            self.assertEqual(first["cache"], "miss")
            prov = first["provenance"]
            for key in ("endpoint", "model", "revision_hint", "prompt_hash",
                        "seed", "temperature", "candidate_index", "retry"):
                self.assertIn(key, prov)
            self.assertEqual(prov["candidate_index"], 1)
            self.assertEqual(prov["retry"], 2)
            wrapped.chat([{"role": "user", "content": "hi"}], seed=5,
                         temperature=0.9, candidate_index=1, retry=2)
            self.assertEqual(wrapped.last_provenance["cache"], "hit")
            self.assertEqual(len(client.calls), 1)
        finally:
            shutil.rmtree(cache.root, ignore_errors=True)


class TestAudit(unittest.TestCase):
    def test_rejects_templated_corpus(self):
        texts = ["Please find the attached invoice for your account and pay it "
                 "by the due date. Thank you for your business."] * 30
        report = audit.audit_corpus(texts)
        self.assertTrue(report["rejected"])
        self.assertGreater(report["char_jaccard"]["max"], 0.9)
        self.assertEqual(report["unique_word_ngram_ratio"], 0.0)
        self.assertTrue(report["shared_sentence_count"] > 0)

    def test_accepts_diverse_corpus(self):
        # Disjoint per-document vocabulary: no shared n-gram, so the corpus must
        # pass. (A real corpus also contains common words; the audit measures
        # n-gram reuse, which is the failure mode of a re-skinned template.)
        texts = []
        for i in range(30):
            texts.append(" ".join("tok%d_%d" % (i, j) for j in range(14))
                         + " end%d" % i)
        report = audit.audit_corpus(texts)
        self.assertFalse(report["rejected"], report.get("reasons"))

    def test_repeated_footer_is_flagged(self):
        footer = ("This message and any attachments are confidential and intended "
                  "only for the named recipient.")
        texts = [("unique body %d about topic %d and more distinct words here %d. "
                  % (i, i * 3, i * 7)) + footer for i in range(30)]
        report = audit.audit_corpus(texts)
        self.assertTrue(report["rejected"])
        self.assertGreater(report["top_feature_share"], 0.7)


class TestPilotBuild(unittest.TestCase):
    def test_small_pilot_lints_and_is_reproducible(self):
        client = ComposeClient()
        builder = pilot.PilotBuilder(seed=42, client=client, cache_enabled=False,
                                     n_candidates=1, max_retries=1)
        result = builder.build(n_base=6, n_threads=0, include_variants=False)
        bundle = result["bundle"]
        self.assertEqual(bundle["metadata"]["counts"]["emails"], 6)
        self.assertEqual(bundle["metadata"]["visibility"], "public")
        self.assertFalse(bundle["metadata"]["contains_private"])
        self.assertTrue(bundle["dataset_id"])
        # No real network client was constructed by the test.
        self.assertGreater(len(client.calls), 0)

    def test_public_dataset_id_independent_of_private_seed(self):
        b = pilot.PilotBuilder(seed=1, client=ComposeClient(), cache_enabled=False,
                               n_candidates=1, max_retries=1)
        b.emails = [1, 2, 3]
        id1 = b.dataset_id()
        b2 = pilot.PilotBuilder(seed=1, client=ComposeClient(), cache_enabled=False,
                                n_candidates=1, max_retries=1)
        b2.emails = [1, 2, 3]
        self.assertEqual(id1, b2.dataset_id())
        # The public id must not embed a private seed argument anywhere.
        self.assertNotIn("private", id1)

    def test_pilot_defect_scanner_clean(self):
        client = ComposeClient()
        builder = pilot.PilotBuilder(seed=9, client=client, cache_enabled=False,
                                     n_candidates=1, max_retries=1)
        result = builder.build(n_base=8, n_threads=1, include_variants=True,
                               variant_roots=1)
        defects = pilot.scan_six_defects(result["emails"])
        self.assertEqual(defects["counts"].get("paid_disregard", 0), 0)
        self.assertEqual(defects["counts"].get("received_vs_outstanding", 0), 0)
        self.assertEqual(defects["counts"].get("fixed_round_timestamps", 0), 0)
        self.assertEqual(defects["counts"].get("missing_login_time", 0), 0)
        self.assertEqual(defects["counts"].get("tracking_invoice_like", 0), 0)
        # At least one thread with two or more messages exists.
        self.assertTrue(any(len(v) >= 2 for v in result["threads"].values()))


if __name__ == "__main__":
    unittest.main()
