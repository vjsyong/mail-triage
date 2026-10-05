"""System One decision-only adapter tests (WP4).

Covers the five ``systemone:<key>`` heads with **zero model calls** (fakes only):
registration + capability declarations, the decision-only credit rules
(prose never fabricated), per-case option order and declared meanings, the
semantic-vs-wire hashes, lazy/cached loading, the explicit ``unavailable``
failure for a missing dependency, and CLI ``list-adapters`` / ``run`` wiring.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3 import cli  # noqa: E402
from benchmarks.v3 import contracts  # noqa: E402
from benchmarks.v3 import build as B  # noqa: E402
from benchmarks.v3.adapters import (  # noqa: E402
    AdapterError, get_adapter, list_adapters)
from benchmarks.v3.adapters import systemone  # noqa: E402

SYSTEMONE_IDS = ["systemone:%s" % k for k in systemone.ADAPTER_KEYS]


def view(profile="native", task="decision", user="From: a\n\nbody", policy=None):
    rendered = {"profile": profile, "system": "SYS", "user": user,
                "owner": "owner@example.com",
                "categories": ["Action", "Promo"],
                "params": {"max_tokens": 4096, "retry_max_tokens": 8192,
                           "json_mode": True, "full": True}}
    if policy:
        rendered["policy"] = policy
    return {"case_id": "case_0001", "task": task, "input_profile": profile,
            "rendered_input": rendered,
            "policy": policy if profile == "policy_conditioned" else None,
            "mailbox": None, "tools": None, "permissions": None}


class FakeHead(object):
    """A scripted decision head: records calls, never loads a model."""

    def __init__(self):
        self.loads = 0
        self.calls = []

    def load(self):
        self.loads += 1

    def close(self):
        pass

    def predict(self, user, categories, descriptions, owner):
        self.calls.append({"user": user, "categories": list(categories),
                           "descriptions": dict(descriptions), "owner": owner})
        cat = categories[0] if categories else "Action"
        return {
            "category": cat,
            "p_category": 0.8,
            "p_needs_reply": 0.7,
            "raw": {"answers": {"category": {"choice": cat,
                                              "probabilities": {cat: 0.8,
                                                                "Promo": 0.2}},
                                 "needs_reply": {"noul": 0.7}}},
            "request": {"state": user, "questions": {"category": "choice"}},
            "category_probabilities": {cat: 0.8, "Promo": 0.2},
            "needs_reply_probabilities": {"yes": 0.7, "no": 0.3},
        }


class RegistryTests(unittest.TestCase):
    def test_all_ids_registered(self):
        listed = list_adapters()
        for adapter_id in SYSTEMONE_IDS:
            self.assertIn(adapter_id, listed)
            adapter = get_adapter(adapter_id)
            self.assertEqual(adapter.adapter_id, adapter_id)
            self.assertTrue(adapter.capabilities["decision"])
            self.assertFalse(adapter.capabilities["prose"])
            self.assertFalse(adapter.capabilities["tools"])
            self.assertFalse(adapter.mock)
            self.assertTrue(adapter.qualifies_as_baseline)

    def test_unknown_systemone_key_refused(self):
        with self.assertRaises(AdapterError):
            get_adapter("systemone:nope")


class DecisionOnlyTests(unittest.TestCase):
    def _adapter(self, head=None):
        return systemone.SystemOneGlinerAdapter(head=head or FakeHead())

    def test_never_claims_prose(self):
        adapter = self._adapter()
        res = adapter.run_case(view())
        self.assertEqual(res["status"], "ok")
        parsed = res["output"]["parsed"]
        self.assertEqual(parsed["category"], "Action")
        self.assertEqual(parsed["needs_reply"], True)
        self.assertAlmostEqual(parsed["confidence"],
                               min(0.8, max(0.7, 0.3)), places=3)
        self.assertNotIn("summary", parsed)
        self.assertNotIn("reason", parsed)
        self.assertEqual(res["field_provenance"]["summary"], "missing")
        self.assertEqual(res["field_provenance"]["reason"], "missing")
        self.assertEqual(res["field_provenance"]["confidence"], "derived")
        self.assertFalse(contracts.has_fabricated_prose(res["field_provenance"]))
        self.assertFalse(res["output"]["systemone"]["synthesized_summary_reason"])

    def test_option_order_and_raw_are_model_answer(self):
        adapter = self._adapter()
        res = adapter.run_case(view())
        self.assertEqual(res["output"]["systemone"]["option_order"],
                         ["Action", "Promo"])
        # raw is the actual model answer (a JSON object of the head's answer)
        self.assertIn("answers", res["output"]["raw"])
        self.assertNotIn("\"questions\"", res["output"]["raw"])
        self.assertTrue(res["output"]["wire_sha256"])
        self.assertIn("confidence_meaning", res["output"]["systemone"])

    def test_request_sha256_is_semantic_rendered_hash(self):
        adapter = self._adapter()
        v = view()
        res = adapter.run_case(v)
        self.assertEqual(res["request_sha256"],
                         contracts.rendered_input_hash(v["rendered_input"]))

    def test_non_decision_task_is_skipped(self):
        adapter = self._adapter()
        res = adapter.run_case(view(task="workflow"))
        self.assertEqual(res["status"], "skipped")
        self.assertIn("decision-only", res["output"]["error"])

    def test_head_is_cached_not_reloaded(self):
        created = {"n": 0}
        head = FakeHead()

        def factory(key, **kwargs):
            created["n"] += 1
            return head

        adapter = self._adapter()
        adapter._head = None
        with mock.patch.object(systemone, "build_head", factory):
            adapter.run_case(view())
            adapter.run_case(view())
        self.assertEqual(created["n"], 1)
        self.assertEqual(head.loads, 1)

    def test_missing_dependency_is_explicit_unavailable(self):
        def boom(*args, **kwargs):
            raise ImportError("No module named 'gliner2'")

        adapter = self._adapter()
        adapter._head = None
        with mock.patch.object(systemone, "build_head", boom):
            res = adapter.run_case(view())
        self.assertEqual(res["status"], "error")
        self.assertIn("unavailable", res["output"]["error"])
        self.assertEqual(res["failure_class"], "infrastructure")
        self.assertFalse(res["capabilities_used"].get("prose"))
        self.assertFalse(contracts.has_fabricated_prose(res["field_provenance"]))

    def test_malformed_head_answer_is_model_failure(self):
        class BadHead(FakeHead):
            def predict(self, user, categories, descriptions, owner):
                return {"category": None, "p_category": 0.5,
                        "p_needs_reply": 0.5, "raw": {"x": 1}, "request": {}}

        res = self._adapter(BadHead()).run_case(view())
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["failure_class"], "model")

    def test_policy_descriptions_reach_the_head(self):
        policy = {"policy_id": "p1", "owner": "pol@example.com",
                  "categories": [{"name": "Action", "description": "POLICY-DESC"},
                                 {"name": "Promo", "description": "promo-d"}]}
        head = FakeHead()
        adapter = self._adapter(head)
        adapter.run_case(view(profile="policy_conditioned", policy=policy))
        call = head.calls[0]
        self.assertEqual(call["descriptions"]["Action"], "POLICY-DESC")
        self.assertEqual(call["owner"], "owner@example.com")  # rendered wins


class CliWiringTests(unittest.TestCase):
    def _run(self, argv, patch_head=None):
        out, err = io.StringIO(), io.StringIO()
        ctx = mock.patch.object(systemone, "build_head", patch_head) \
            if patch_head else mock.patch.object(systemone, "build_head",
                                                 lambda *a, **k: FakeHead())
        with ctx, redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_list_adapters_command(self):
        code, out, _ = self._run(["list-adapters", "--json"])
        self.assertEqual(code, 0)
        ids = [a["adapter_id"] for a in json.loads(out)["adapters"]]
        for adapter_id in SYSTEMONE_IDS:
            self.assertIn(adapter_id, ids)
        row = next(a for a in json.loads(out)["adapters"]
                   if a["adapter_id"] == "systemone:kev")
        self.assertEqual(row["capabilities"]["decision"], True)
        self.assertEqual(row["capabilities"]["tools"], False)
        self.assertFalse(row["mock"])

    def test_run_systemone_with_fake_head(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            pilot = os.path.join(tmp.name, "pilot")
            bundle = B.build_dataset(triage_roots=4, workflow_roots=1,
                                     layout="pilot")
            B.write_dataset(bundle, pilot)
            decision_cases = [c for c in bundle["cases"]
                              if c["task"] == "decision"]
            runs = os.path.join(tmp.name, "runs")
            code, out, err = self._run([
                "run", pilot, "--adapter", "systemone:gliner", "--out", runs,
                "--allow-draft", "--json"])
            self.assertEqual(code, 0, err)
            summary = json.loads(out)
            self.assertEqual(summary["cases"], len(bundle["cases"]))
            # workflow cases are skipped, every decision case is ok
            self.assertEqual(summary["ok"], len(decision_cases))
            from benchmarks.v3.runner import load_run
            loaded = load_run(runs)
            self.assertTrue(loaded["manifest"]["qualifies_as_baseline"])
            self.assertFalse(loaded["manifest"]["mock"])
        finally:
            tmp.cleanup()

    def test_nanojev_defaults_to_cuda_device(self):
        args = cli.build_parser().parse_args([
            "run", "d", "--adapter", "systemone:nanojev", "--out", "o"])
        adapter = cli._adapter_from_args(args)
        self.assertEqual(adapter.device, "cuda:0")
        coarse = cli.build_parser().parse_args([
            "run", "d", "--adapter", "systemone:laya", "--out", "o"])
        self.assertEqual(cli._adapter_from_args(coarse).device, "cpu")


if __name__ == "__main__":
    unittest.main()
