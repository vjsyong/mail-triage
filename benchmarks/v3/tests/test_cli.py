"""CLI tests (WP4/WP7 plumbing).

Covers command discovery, JSON validate/run, the offline fake complete flow via
the CLI, draft gating, the non-local endpoint refusal, and informative preflight
output.  Commands owned by not-yet-integrated packages (build/scoring/review)
must fail with a clear message rather than a stack trace.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3 import cli  # noqa: E402


def _case(cid, task, profile, extra=None):
    case = {
        "schema_version": "v3.0", "case_id": cid, "scenario_id": "sc_" + cid,
        "lineage_id": "ln_" + cid, "task": task, "input_profile": profile,
        "policy_id": "default", "split": "development", "gold_id": "g_" + cid,
        "rendered_input": {"profile": profile, "system": "SYS",
                           "user": "USER " + cid, "categories": ["Action"],
                           "params": {"max_tokens": 4096, "json_mode": True}},
    }
    if extra:
        case.update(extra)
    return case


def _bundle(review_status="sealed"):
    return {
        "schema_version": "v3.0", "dataset_id": "dev_min",
        "cases": [_case("case_0001", "decision", "native"),
                  _case("case_0002", "workflow", "workflow", extra={
                      "mailbox": {"messages": [{"message_id": "m1",
                                                "folder": "INBOX"}]},
                      "tools": ["move_message"],
                      "permissions": {"move": "auto"}})],
        "gold": [{"schema_version": "v3.0", "gold_id": "g_%s" % c,
                  "case_id": "case_000" + str(i + 1), "review_status": "draft",
                  "human_seal": False, "observable": {}, "answer": {}}
                 for i, c in enumerate(("1", "2"))],
        "scenarios": [], "policies": [], "lineage": [], "provenance": [],
        "metadata": {"review_status": review_status},
    }


class _CLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = os.path.join(self.tmp.name, "bundle.json")
        with open(self.dataset_path, "w") as f:
            json.dump(_bundle(), f)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _write_json(self, name, value):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            json.dump(value, f)
        return path


class ParserTests(_CLI):
    def test_help_lists_commands(self):
        text = cli.build_parser().format_help()
        for command in ("build", "validate", "run", "score", "compare",
                        "review", "export", "import"):
            self.assertIn(command, text)

    def test_no_command_returns_usage(self):
        code, out, _ = self._run([])
        self.assertEqual(code, 2)
        self.assertIn("usage", out.lower())


class ValidateTests(_CLI):
    def test_valid_dataset(self):
        code, out, _ = self._run(["validate", self.dataset_path, "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["valid"])

    def test_invalid_dataset(self):
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w") as f:
            json.dump({"schema_version": "v3.0", "dataset_id": "x", "cases": []}, f)
        code, out, _ = self._run(["validate", bad, "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["valid"])


class RunTests(_CLI):
    def test_offline_fake_run(self):
        pred = self._write_json("pred.json", {
            "case_0001": '{"category":"Action","needs_reply":true,'
                         '"confidence":0.9,"summary":"s","reason":"r"}'})
        wf = self._write_json("wf.json", {
            "case_0002": [{"tool": "move_message",
                           "args": {"message_id": "m1",
                                    "target_folder": "Receipts"}}]})
        out_dir = os.path.join(self.tmp.name, "runs")
        code, out, _ = self._run([
            "run", self.dataset_path, "--adapter", "offline-fake",
            "--out", out_dir, "--predictions", pred, "--workflow-script", wf,
            "--json"])
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["cases"], 2)
        from benchmarks.v3.runner import load_run
        loaded = load_run(out_dir)
        self.assertEqual(len(loaded["attempts"]), 2)

    def test_preflight_is_informative(self):
        pred = self._write_json("pred.json", {"case_0001": "{}"})
        out_dir = os.path.join(self.tmp.name, "runs2")
        code, out, _ = self._run([
            "run", self.dataset_path, "--adapter", "offline-fake",
            "--out", out_dir, "--predictions", pred])
        self.assertEqual(code, 0)
        self.assertIn("preflight", out)
        self.assertIn("offline-fake", out)

    def test_draft_requires_allow_draft(self):
        draft = os.path.join(self.tmp.name, "draft.json")
        with open(draft, "w") as f:
            json.dump(_bundle("draft"), f)
        out_dir = os.path.join(self.tmp.name, "runs3")
        code, _, err = self._run(["run", draft, "--out", out_dir])
        self.assertEqual(code, 1)
        self.assertIn("allow-draft", err)
        code, _, _ = self._run(["run", draft, "--out", out_dir, "--allow-draft"])
        self.assertEqual(code, 0)

    def test_non_local_endpoint_requires_remote_flag(self):
        out_dir = os.path.join(self.tmp.name, "runs4")
        code, _, err = self._run([
            "run", self.dataset_path, "--adapter", "generative-openai",
            "--endpoint", "http://example.com:8000/v1", "--out", out_dir])
        self.assertEqual(code, 1)
        self.assertIn("remote", err.lower())


class UnintegratedPackageTests(_CLI):
    def test_build_reports_missing_package(self):
        code, _, err = self._run(["build", "--out", os.path.join(self.tmp.name, "d")])
        self.assertEqual(code, 1)
        self.assertIn("build package", err)

    def test_review_reports_missing_package(self):
        code, _, err = self._run(["review", self.dataset_path, "--export",
                                  os.path.join(self.tmp.name, "r.json")])
        self.assertEqual(code, 1)
        self.assertIn("build package", err)


if __name__ == "__main__":
    unittest.main()
