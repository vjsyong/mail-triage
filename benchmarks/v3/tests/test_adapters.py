"""Adapter tests (WP4, FR4).

Covers native raw/parse parity and provenance, the policy card genuinely
reaching the model context (and its hash), TinyJev decision-only credit rules,
the scripted mock's explicit non-baseline status, fusion component provenance,
and the local-endpoint / no-network defaults.
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

from benchmarks.v3 import contracts  # noqa: E402
from benchmarks.v3.adapters import (  # noqa: E402
    AdapterError, FakeAdapter, FusionAdapter, OpenAICompatAdapter,
    TinyJevAdapter, get_adapter, list_adapters)


def view(profile="native", task="decision", user="From: a\n\nbody", policy=None):
    rendered = {"profile": profile, "system": "SYS", "user": user,
                "categories": ["Action", "Promo"],
                "params": {"max_tokens": 4096, "retry_max_tokens": 8192,
                           "json_mode": True, "full": True}}
    if policy:
        rendered["policy"] = policy
    return {"case_id": "case_0001", "task": task, "input_profile": profile,
            "rendered_input": rendered, "policy": policy if profile ==
            "policy_conditioned" else None, "mailbox": None, "tools": None,
            "permissions": None}


VALID = ('{"category": "Action", "needs_reply": true, "confidence": 0.9, '
         '"summary": "s", "reason": "r"}')


class FingerprintTests(unittest.TestCase):
    def test_source_hash_and_mock_flags(self):
        fake = FakeAdapter()
        fp = fake.fingerprint()
        self.assertTrue(fp["adapter_source_sha256"])
        self.assertTrue(fp["adapter_revision"])
        self.assertTrue(fp["mock"])
        self.assertFalse(fp["qualifies_as_baseline"])

    def test_registry(self):
        self.assertIn("offline-fake", list_adapters())
        self.assertIsInstance(get_adapter("offline-fake"), FakeAdapter)
        with self.assertRaises(AdapterError):
            get_adapter("does-not-exist")


class FakeAdapterTests(unittest.TestCase):
    def test_decision_from_scripted_raw(self):
        fake = FakeAdapter(predictions={"case_0001": VALID})
        res = fake.run_case(view())
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["output"]["raw"], VALID)
        self.assertEqual(res["output"]["parsed"]["category"], "Action")
        self.assertTrue(res["capabilities_used"]["prose"])
        self.assertEqual(res["field_provenance"]["summary"], "produced")

    def test_missing_script_is_explicit(self):
        res = FakeAdapter().run_case(view())
        self.assertEqual(res["status"], "missing")


class GenerativeAdapterTests(unittest.TestCase):
    def _adapter(self, responses):
        calls = []

        def transport(payload):
            calls.append(payload)
            return responses[min(len(calls) - 1, len(responses) - 1)]

        adapter = OpenAICompatAdapter(base_url="http://127.0.0.1:8000/v1",
                                      model="m", model_revision="rev", transport=transport)
        return adapter, calls

    def test_raw_parity_and_provenance(self):
        adapter, calls = self._adapter([{"content": "prefix " + VALID + " suffix",
                                         "finish_reason": "stop"}])
        res = adapter.run_case(view())
        self.assertEqual(res["output"]["raw"], "prefix " + VALID + " suffix")
        self.assertEqual(res["output"]["parsed"],
                         contracts.native_output(contracts.parse_native_response(VALID)))
        self.assertEqual(res["field_provenance"]["summary"], "produced")
        self.assertEqual(len(calls), 1)

    def test_retry_on_length_only(self):
        adapter, calls = self._adapter([
            {"content": "partial no json", "finish_reason": "length"},
            {"content": VALID, "finish_reason": "stop"}])
        res = adapter.run_case(view())
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["timings"]["retried"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["max_tokens"], 8192)

    def test_policy_reaches_context_and_hash(self):
        policy = {"policy_id": "p1", "revision": "1", "owner": "o",
                  "categories": [{"name": "Action", "description": "d", "folder": "A"}]}
        adapter, calls = self._adapter([{"content": VALID, "finish_reason": "stop"}])
        res = adapter.run_case(view(profile="policy_conditioned", policy=policy))
        system = calls[0]["messages"][0]["content"]
        self.assertIn("p1", system)
        self.assertTrue(res["output"]["policy_transmitted"])
        from benchmarks.v3.common.hashing import hash_obj
        self.assertEqual(res["output"]["policy_sha256"], hash_obj(policy))

    def test_non_local_endpoint_requires_flag(self):
        with self.assertRaises(AdapterError):
            OpenAICompatAdapter(base_url="http://example.com:8000/v1", model="m",
                                model_revision="r")
        # construct with the explicit flag (no call is made at construction)
        OpenAICompatAdapter(base_url="http://example.com:8000/v1", model="m",
                            model_revision="r", allow_remote=True)

    def test_no_endpoint_and_no_transport_refused(self):
        with self.assertRaises(AdapterError):
            OpenAICompatAdapter(model="m", model_revision="r")

    def test_workflow_budget_exhaustion_is_budget_failure(self):
        from benchmarks.v3.sandbox import Mailbox
        calls = {"n": 0}

        def transport(payload):
            calls["n"] += 1
            return {"content": "", "finish_reason": "tool_calls",
                    "tool_calls": [{"id": "c%d" % calls["n"], "name": "list_folders",
                                    "arguments": "{}"}]}

        adapter = OpenAICompatAdapter(base_url="http://127.0.0.1:8000/v1", model="m",
                                      model_revision="r", transport=transport)
        sandbox = Mailbox({"messages": []}, permissions={}, case_id="c")
        res = adapter.run_case(view(task="workflow", profile="workflow"), sandbox)
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["failure_class"], "budget")


class FakeAgent(object):
    def predict(self, payload, temperature=1.0):
        self.payload = payload
        return {
            "model": {"backend": "fake"},
            "execution": {"model_ms": 1.5},
            "states": [{"id": "request", "answers": {
                "category": {"type": "choice", "choice": "Action",
                             "confidence": 0.8, "probabilities": {"Action": 0.8,
                                                                  "Promo": 0.2}},
                "needs_reply": {"type": "boolean", "p_true": 0.7},
            }}],
        }


class TinyJevTests(unittest.TestCase):
    def test_decision_only_never_claims_prose(self):
        adapter = TinyJevAdapter(agent=FakeAgent(),
                                 categories=["Action", "Promo"])
        res = adapter.run_case(view())
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["output"]["parsed"]["category"], "Action")
        self.assertEqual(res["output"]["parsed"]["needs_reply"], True)
        self.assertNotIn("summary", res["output"]["parsed"])
        self.assertNotIn("reason", res["output"]["parsed"])
        self.assertEqual(res["field_provenance"]["summary"], "missing")
        self.assertEqual(res["field_provenance"]["reason"], "missing")
        self.assertFalse(contracts.has_fabricated_prose(res["field_provenance"]))
        self.assertEqual(res["output"]["tinyjev"]["option_order"], ["Action", "Promo"])
        self.assertFalse(res["output"]["tinyjev"]["synthesized_summary_reason"])

    def test_non_decision_task_is_skipped(self):
        adapter = TinyJevAdapter(agent=FakeAgent())
        res = adapter.run_case(view(task="workflow"))
        self.assertEqual(res["status"], "skipped")


class FusionTests(unittest.TestCase):
    def _prose_transport(self, text):
        return lambda payload: {"content": text, "finish_reason": "stop"}

    def test_composition_provenance(self):
        decision = FakeAdapter(predictions={
            "case_0001": {"category": "Action", "needs_reply": True, "confidence": 0.8}})
        prose = OpenAICompatAdapter(
            base_url="http://127.0.0.1:8000/v1", model="p", model_revision="r",
            transport=self._prose_transport(
                '{"category":"Promo","needs_reply":false,"confidence":0.5,'
                '"summary":"sum","reason":"why"}'))
        fusion = FusionAdapter(decision_adapter=decision, prose_adapter=prose)
        res = fusion.run_case(view())
        self.assertEqual(res["output"]["parsed"]["category"], "Action")
        self.assertEqual(res["output"]["parsed"]["summary"], "sum")
        self.assertEqual(res["field_provenance"]["category"], "produced")
        self.assertEqual(res["field_provenance"]["summary"], "produced")
        self.assertIn("components", res["output"])

    def test_missing_prose_is_missing_not_fabricated(self):
        decision = FakeAdapter(predictions={
            "case_0001": {"category": "Action", "needs_reply": True}})
        fusion = FusionAdapter(decision_adapter=decision, prose_adapter=None)
        res = fusion.run_case(view())
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["field_provenance"]["summary"], "missing")
        self.assertFalse(contracts.has_fabricated_prose(res["field_provenance"]))

    def test_facade_is_labelled_read_only(self):
        fusion = FusionAdapter(facade=lambda v: {
            "category": "Action", "needs_reply": True, "confidence": 0.9,
            "summary": "s", "reason": "r"})
        res = fusion.run_case(view())
        self.assertTrue(res["output"]["read_only_facade"])
        self.assertIn("facade", res["timings"]["label"])


if __name__ == "__main__":
    unittest.main()
