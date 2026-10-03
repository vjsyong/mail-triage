"""Runner tests (WP4, FR7): identity, resume, resources, offline flow.

Covers the offline end-to-end flow through the scripted mock, immutable exact
fingerprints, gold isolation from the adapter, unsupported-capability skips,
resume refusal on every identity change, duplicate/stale attempt rejection, the
preserved failure denominator, and honest CPU resource qualification.
"""
import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3 import contracts  # noqa: E402
from benchmarks.v3.common import identity  # noqa: E402
from benchmarks.v3.adapters import FakeAdapter, TinyJevAdapter  # noqa: E402
from benchmarks.v3.runner import (  # noqa: E402
    RunnerError, collect_resource_evidence, load_run, run_dataset, save_run)
from benchmarks.v3.schema import validate_run_manifest  # noqa: E402

VALID = ('{"category": "Action", "needs_reply": true, "confidence": 0.9, '
         '"summary": "s", "reason": "r"}')


def _case(cid, task, profile, extra=None):
    case = {
        "schema_version": "v3.0", "case_id": cid, "scenario_id": "sc_" + cid,
        "lineage_id": "ln_" + cid, "task": task, "input_profile": profile,
        "policy_id": "default", "split": "development", "gold_id": "g_" + cid,
        "rendered_input": {"profile": profile, "system": "SYS", "user": "USER " + cid,
                           "categories": ["Action", "Promo"],
                           "params": {"max_tokens": 4096, "retry_max_tokens": 8192,
                                      "json_mode": True, "full": True}},
    }
    if extra:
        case.update(extra)
    return case


def _gold(cid):
    return {"schema_version": "v3.0", "gold_id": "g_" + cid, "case_id": cid,
            "review_status": "draft", "human_seal": False, "observable": {},
            "answer": {"category": "Action"}}


def _bundle(review_status="sealed"):
    workflow = _case("case_0003", "workflow", "workflow", extra={
        "mailbox": {"folders": ["INBOX", "Receipts"],
                    "messages": [{"message_id": "m1", "folder": "INBOX",
                                  "subject": "Bill", "body": "invoice",
                                  "from_addr": "b@x.com"}]},
        "tools": ["move_message", "read_message"],
        "permissions": {"move": "auto"}})
    return {
        "schema_version": "v3.0", "dataset_id": "dev_min",
        "cases": [_case("case_0001", "decision", "native"),
                  _case("case_0002", "full_response", "policy_conditioned"),
                  workflow],
        "gold": [_gold("case_0001"), _gold("case_0002"), _gold("case_0003")],
        "scenarios": [], "lineage": [], "provenance": [],
        "policies": [{"schema_version": "v3.0", "policy_id": "default",
                      "revision": "3.0", "owner": "o",
                      "categories": [{"name": "Action", "description": "d",
                                      "folder": "Action"}],
                      "filing": {"default_folder": "Action", "mode": "suggest"},
                      "permissions": {"allow_move": True, "allow_send": False,
                                      "allow_rule_create": False,
                                      "require_approval": False}}],
        "metadata": {"review_status": review_status},
    }


def _adapter(**over):
    kwargs = dict(
        predictions={"case_0001": VALID,
                     "case_0002": {"category": "Promo", "needs_reply": False,
                                   "confidence": 0.5, "summary": "sum",
                                   "reason": "why"}},
        workflow_script={"case_0003": [{"tool": "move_message",
                                        "args": {"message_id": "m1",
                                                 "target_folder": "Receipts"}}]},
        workflow_replies={"case_0003": "done"})
    kwargs.update(over)
    return FakeAdapter(**kwargs)


class SpyAdapter(FakeAdapter):
    capabilities = {"decision": True, "prose": True, "tools": True,
                    "native_parse": True}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.views = []

    def run_case(self, view, sandbox=None):
        self.views.append(copy.deepcopy(view))
        return super().run_case(view, sandbox)


class NoToolsAdapter(FakeAdapter):
    capabilities = {"decision": True, "prose": True, "tools": False,
                    "native_parse": True}


class OfflineFlowTests(unittest.TestCase):
    def test_complete_flow_and_valid_attempts(self):
        run = run_dataset(_bundle(), _adapter(), allow_draft=True)
        self.assertEqual(len(run["attempts"]), 3)
        statuses = {a["case_id"]: a["status"] for a in run["attempts"]}
        self.assertEqual(statuses, {"case_0001": "ok", "case_0002": "ok",
                                    "case_0003": "ok"})
        wf = next(a for a in run["attempts"] if a["case_id"] == "case_0003")
        self.assertEqual(len(wf["tool_events"]), 1)
        self.assertEqual(wf["tool_events"][0]["tool"], "move_message")
        dec = next(a for a in run["attempts"] if a["case_id"] == "case_0001")
        self.assertEqual(dec["output"]["raw"], VALID)
        from benchmarks.v3.schema import validate_artifact
        for attempt in run["attempts"]:
            self.assertEqual(validate_artifact("attempt", attempt), [])

    def test_manifest_fingerprints_present(self):
        run = run_dataset(_bundle(), _adapter(), allow_draft=True)
        manifest = run["manifest"]
        validate_run_manifest(manifest)  # raises on any missing fingerprint
        for name in identity.REQUIRED_NONEMPTY_FIELDS:
            self.assertTrue(manifest[name], name)

    def test_gold_never_reaches_adapter(self):
        spy = SpyAdapter(predictions={"case_0001": VALID,
                                      "case_0002": VALID},
                         workflow_script={}, workflow_replies={})
        run_dataset(_bundle(), spy, allow_draft=True)
        for view in spy.views:
            self.assertNotIn("gold_id", view)
            self.assertNotIn("gold", view)
            self.assertNotIn("answer", view)

    def test_gold_leakage_is_rejected(self):
        bundle = _bundle()
        bundle["gold"][0]["hidden_evidence"] = ["the secret codeword"]
        bundle["cases"][0]["rendered_input"]["user"] = "USER contains the secret codeword"
        with self.assertRaises(contracts.GoldLeakageError):
            run_dataset(bundle, _adapter(), allow_draft=True)

    def test_unsupported_capability_recorded_as_skip(self):
        run = run_dataset(_bundle(), NoToolsAdapter(
            predictions={"case_0001": VALID, "case_0002": VALID}),
            allow_draft=True)
        wf = next(a for a in run["attempts"] if a["case_id"] == "case_0003")
        self.assertEqual(wf["status"], "skipped")
        self.assertTrue(run["metadata"]["skips"])
        self.assertIn("skipped", run["metadata"]["failure_counts"])

    def test_draft_dataset_requires_allow_draft(self):
        with self.assertRaises(RunnerError):
            run_dataset(_bundle("draft"), _adapter())
        run = run_dataset(_bundle("draft"), _adapter(), allow_draft=True)
        self.assertEqual(run["metadata"]["dataset_review_status"], "draft")

    def test_sealed_dataset_with_contradictory_gold_refused(self):
        bundle = _bundle("sealed")
        bundle["gold"][0]["human_seal"] = True  # seal=true on a draft gold
        with self.assertRaises(RunnerError):
            run_dataset(bundle, _adapter(), allow_draft=True)


class ResumeTests(unittest.TestCase):
    def test_same_identity_resumes_without_reexecution(self):
        bundle = _bundle()
        first = run_dataset(bundle, _adapter(), allow_draft=True)
        second = run_dataset(bundle, _adapter(), allow_draft=True, resume=first)
        self.assertEqual(second["metadata"]["executed_case_count"], 0)
        self.assertEqual(second["metadata"]["resumed_from"], first["manifest"]["run_id"])
        self.assertEqual(len(second["attempts"]), len(first["attempts"]))

    def test_failure_denominator_preserved(self):
        # one case has no scripted prediction -> a real failure.
        adapter = _adapter(predictions={"case_0001": VALID})
        first = run_dataset(_bundle(), adapter, allow_draft=True)
        failed = sum(1 for a in first["attempts"] if a["status"] != "ok")
        self.assertGreaterEqual(failed, 1)
        second = run_dataset(_bundle(), _adapter(predictions={"case_0001": VALID}),
                             allow_draft=True, resume=first)
        self.assertEqual(second["metadata"]["executed_case_count"], 0)
        still_failed = sum(1 for a in second["attempts"] if a["status"] != "ok")
        self.assertEqual(still_failed, failed)

    def _assert_blocks(self, bundle, adapter, first, **kwargs):
        with self.assertRaises(identity.IdentityError):
            run_dataset(bundle, adapter, allow_draft=True, resume=first, **kwargs)

    def test_changed_data_blocks_resume(self):
        first = run_dataset(_bundle(), _adapter(), allow_draft=True)
        changed = _bundle()
        changed["cases"][0]["rendered_input"]["user"] = "DIFFERENT"
        self._assert_blocks(changed, _adapter(), first)

    def test_changed_model_blocks_resume(self):
        first = run_dataset(_bundle(), _adapter(), allow_draft=True)
        self._assert_blocks(_bundle(), _adapter(model_revision="other"), first)

    def test_changed_scorer_blocks_resume(self):
        first = run_dataset(_bundle(), _adapter(), allow_draft=True)
        self._assert_blocks(_bundle(), _adapter(), first, scorer_revision="v9")

    def test_changed_calibrator_blocks_resume(self):
        first = run_dataset(_bundle(), _adapter(), allow_draft=True)
        self._assert_blocks(_bundle(), _adapter(), first, calibrator_revision="cal-9")

    def test_changed_runtime_blocks_resume(self):
        first = run_dataset(_bundle(), _adapter(), allow_draft=True)
        self._assert_blocks(_bundle(), _adapter(), first,
                            runtime_config={"threads": 2})

    def test_changed_subset_blocks_resume(self):
        first = run_dataset(_bundle(), _adapter(), allow_draft=True)
        self._assert_blocks(_bundle(), _adapter(), first,
                            requested_case_ids=["case_0001"])

    def test_duplicate_attempts_rejected(self):
        first = run_dataset(_bundle(), _adapter(), allow_draft=True)
        dup = copy.deepcopy(first["attempts"][0])
        dup["attempt"] = 2
        first["attempts"].append(dup)
        with self.assertRaises(RunnerError):
            run_dataset(_bundle(), _adapter(), allow_draft=True, resume=first)

    def test_stale_manifest_rejected(self):
        first = run_dataset(_bundle(), _adapter(), allow_draft=True)
        tampered = copy.deepcopy(first)
        tampered["manifest"]["config_hash"] = "0" * 64
        with self.assertRaises(identity.IdentityError):
            run_dataset(_bundle(), _adapter(), allow_draft=True, resume=tampered)


class ResourceTests(unittest.TestCase):
    def test_missing_limits_are_unqualified(self):
        record = collect_resource_evidence(probe={})
        self.assertFalse(record["cpu_qualified"])
        self.assertFalse(record["enforced_limits_verified"])
        self.assertTrue(record["notes"])

    def test_verified_limits_qualify(self):
        probe = {
            "/sys/fs/cgroup/cpuset.cpus.effective": "0-3",
            "/sys/fs/cgroup/memory.max": "8589934592",
            "/sys/fs/cgroup/cpu.max": "400000 100000",
            "affinity": [0, 1, 2, 3],
            "process_tree_rss_bytes": 123456789,
            "/proc/cpuinfo": "model name\t: Test CPU",
        }
        record = collect_resource_evidence(probe=probe)
        self.assertTrue(record["cpu_qualified"])
        self.assertEqual(record["threads"], 4)
        self.assertEqual(record["memory"]["limit_bytes"], 8589934592)

    def test_external_warm_endpoint_ineligible(self):
        probe = {"/sys/fs/cgroup/cpuset.cpus.effective": "0-3",
                 "/sys/fs/cgroup/memory.max": "8589934592"}
        record = collect_resource_evidence(probe=probe,
                                           endpoint_class="external_warm")
        self.assertFalse(record["cpu_qualified"])


class PersistenceTests(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        run = run_dataset(_bundle(), _adapter(), allow_draft=True)
        with tempfile.TemporaryDirectory() as tmp:
            target = save_run(run, tmp)
            loaded = load_run(target)
            self.assertEqual(loaded["manifest"]["run_id"], run["manifest"]["run_id"])
            self.assertEqual(len(loaded["attempts"]), len(run["attempts"]))
            self.assertEqual(loaded["metadata"]["adapter_id"], "offline-fake")

    def test_run_dataset_can_write_out_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = run_dataset(_bundle(), _adapter(), allow_draft=True, out_dir=tmp)
            run_id = run["manifest"]["run_id"]
            self.assertTrue(os.path.exists(os.path.join(tmp, run_id, "manifest.json")))
            loaded = load_run(os.path.join(tmp, run_id))
            self.assertEqual(len(loaded["attempts"]), 3)


if __name__ == "__main__":
    unittest.main()
