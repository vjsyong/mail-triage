"""Integrated CLI tests (WP2/WP4/WP5/WP7).

These run the **actual** build/scoring packages through the CLI (no mocks, no
raw-JSON fallbacks): build -> validate -> run offline-fake -> score -> compare
-> review -> export/import, plus the fail-closed guarantees (invalid bundle,
rejected import destination, unverified model identity, external endpoint).
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
from benchmarks.v3 import build as B  # noqa: E402

VALID = ('{"category": "Action", "needs_reply": true, "confidence": 0.9, '
         '"summary": "s", "reason": "r"}')


class _Integrated(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pilot_dir = os.path.join(cls.tmp.name, "pilot")
        bundle = B.build_dataset(triage_roots=6, workflow_roots=2, layout="pilot")
        B.write_dataset(bundle, cls.pilot_dir)
        cls.bundle = B.load_dataset(cls.pilot_dir)
        # an honest constant mock (never derived from gold)
        cls.predictions = {c["case_id"]: VALID for c in cls.bundle["cases"]
                           if c["task"] != "workflow"}
        cls.runs_dir = os.path.join(cls.tmp.name, "runs")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

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


class ParserTests(_Integrated):
    def test_help_lists_commands(self):
        text = cli.build_parser().format_help()
        for command in ("build", "validate", "run", "score", "compare",
                        "review", "export", "import"):
            self.assertIn(command, text)

    def test_no_command_returns_usage(self):
        code, out, _ = self._run([])
        self.assertEqual(code, 2)
        self.assertIn("usage", out.lower())


class ValidateTests(_Integrated):
    def test_valid_written_dataset(self):
        code, out, _ = self._run(["validate", self.pilot_dir, "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["valid"])

    def test_invalid_bundle_fails_closed(self):
        bad = os.path.join(self.tmp.name, "bad")
        os.makedirs(bad)
        with open(os.path.join(bad, "bundle.json"), "w") as f:
            json.dump({"schema_version": "v3.0", "dataset_id": "x", "cases": []}, f)
        code, out, _ = self._run(["validate", bad, "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["valid"])


class RunTests(_Integrated):
    def _pred_path(self):
        return self._write_json("pred.json", self.predictions)

    def test_offline_fake_run(self):
        pred = self._pred_path()
        code, out, _ = self._run([
            "run", self.pilot_dir, "--adapter", "offline-fake",
            "--out", self.runs_dir, "--predictions", pred, "--allow-draft",
            "--json"])
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["cases"], len(self.bundle["cases"]))
        from benchmarks.v3.runner import load_run
        loaded = load_run(self.runs_dir)
        self.assertEqual(len(loaded["attempts"]), len(self.bundle["cases"]))

    def test_preflight_is_informative(self):
        pred = self._pred_path()
        code, out, _ = self._run([
            "run", self.pilot_dir, "--adapter", "offline-fake",
            "--out", os.path.join(self.tmp.name, "runs2"), "--predictions", pred,
            "--allow-draft"])
        self.assertEqual(code, 0)
        self.assertIn("preflight", out)
        self.assertIn("model_identity_source", out)

    def test_draft_requires_allow_draft(self):
        pred = self._pred_path()
        out_dir = os.path.join(self.tmp.name, "runs3")
        code, _, err = self._run([
            "run", self.pilot_dir, "--out", out_dir, "--predictions", pred])
        self.assertEqual(code, 1)
        self.assertIn("allow-draft", err)
        code, _, _ = self._run([
            "run", self.pilot_dir, "--out", out_dir, "--predictions", pred,
            "--allow-draft"])
        self.assertEqual(code, 0)

    def test_unverified_model_identity_refused(self):
        out_dir = os.path.join(self.tmp.name, "runs-tiny")
        code, _, err = self._run([
            "run", self.pilot_dir, "--adapter", "tinyjev-decision",
            "--out", out_dir, "--allow-draft"])
        self.assertEqual(code, 1)
        self.assertIn("identity", err.lower())

    def test_external_endpoint_requires_remote_flag(self):
        out_dir = os.path.join(self.tmp.name, "runs4")
        code, _, err = self._run([
            "run", self.pilot_dir, "--adapter", "generative-openai",
            "--endpoint", "http://example.com:8000/v1", "--out", out_dir,
            "--allow-draft"])
        self.assertEqual(code, 1)
        self.assertIn("remote", err.lower())

    def test_loopback_endpoint_is_external_warm(self):
        args = cli.build_parser().parse_args([
            "run", "d", "--adapter", "generative-openai",
            "--endpoint", "http://127.0.0.1:8042/v1", "--out", "o",
            "--model", "m", "--model-revision", "r"])
        self.assertEqual(cli._endpoint_class(args), "external_warm")


class ScoreCompareTests(_Integrated):
    def _run_pilot(self, prefix):
        pred = self._write_json(prefix + "-pred.json", self.predictions)
        out_dir = os.path.join(self.tmp.name, prefix + "-runs")
        code, _, _ = self._run([
            "run", self.pilot_dir, "--adapter", "offline-fake",
            "--out", out_dir, "--predictions", pred, "--allow-draft"])
        self.assertEqual(code, 0)
        from benchmarks.v3.runner import load_run
        run_dir = load_run(out_dir)  # single-run dir
        return out_dir, run_dir

    def test_score_and_compare(self):
        out_dir, _ = self._run_pilot("score")
        code, out, err = self._run(["score", self.pilot_dir, out_dir, "--json"])
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertIn("integrity", report)
        self.assertIn("gates", report)

        # compare two independent runs
        out2, _ = self._run_pilot("score2")
        code, out, err = self._run(["compare", self.pilot_dir, out_dir, out2,
                                    "--json"])
        self.assertEqual(code, 0, err)
        self.assertIn("scope", json.loads(out))


class CalibrationTests(_Integrated):
    def test_fit_calibrator_from_calibration_split(self):
        layout = {"triage": {"development": 3, "calibration": 3}, "workflow": {}}
        bundle = B.build_dataset(triage_roots=0, workflow_roots=0, layout=layout,
                                 private_seed=11)
        public_dir = os.path.join(self.tmp.name, "cal")
        private_dir = os.path.join(self.tmp.name, "cal-private")
        B.write_dataset(bundle, public_dir, private_root=private_dir)
        # calibration lives only in the private subset
        pred = self._write_json("cal-pred.json", {
            c["case_id"]: VALID for c in bundle["cases"] if c["task"] != "workflow"})
        runs = os.path.join(self.tmp.name, "cal-runs")
        code, _, err = self._run([
            "run", private_dir, "--adapter", "offline-fake", "--out", runs,
            "--predictions", pred, "--allow-draft"])
        self.assertEqual(code, 0, err)
        art = os.path.join(self.tmp.name, "calibrator.json")
        code, out, err = self._run(["score", private_dir, runs,
                                    "--fit-calibrator", art, "--json"])
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(art))
        artifact = json.load(open(art))
        self.assertTrue(artifact.get("revision") or artifact.get("artifact_sha256"))
        self.assertEqual(artifact.get("fit_split"), "calibration")


class ReviewTests(_Integrated):
    def test_export_and_import_worksheet(self):
        worksheet = os.path.join(self.tmp.name, "worksheet.json")
        code, out, _ = self._run([
            "review", self.pilot_dir, "--export-worksheet", worksheet, "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(worksheet))
        ws = json.load(open(worksheet))
        self.assertEqual(ws["review_status"], "draft")
        self.assertFalse(ws["human_seal"])

        # import accept judgements for every item (named reviewer required)
        judgements = [{"gold_id": i["gold_id"], "decision": "accept",
                       "reviewer": "A. Reviewer"} for i in ws["items"]]
        jpath = self._write_json("judgements.json", judgements)
        out_dir = os.path.join(self.tmp.name, "reviewed")
        code, out, err = self._run([
            "review", self.pilot_dir, "--worksheet", worksheet,
            "--import", jpath, "--out", out_dir, "--json"])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(len(result["reviewed"]), len(ws["items"]))
        self.assertEqual(result["review_status"], "reviewed")

    def test_import_without_reviewer_fails_closed(self):
        worksheet = os.path.join(self.tmp.name, "worksheet2.json")
        self._run(["review", self.pilot_dir, "--export-worksheet", worksheet])
        ws = json.load(open(worksheet))
        judgements = [{"gold_id": ws["items"][0]["gold_id"], "decision": "accept"}]
        jpath = self._write_json("judgements2.json", judgements)
        code, _, err = self._run([
            "review", self.pilot_dir, "--worksheet", worksheet, "--import", jpath])
        self.assertEqual(code, 1)
        self.assertIn("reviewer", err.lower())

    def test_seal_requires_reviewer(self):
        code, _, err = self._run([
            "review", self.pilot_dir, "--seal", "--out",
            os.path.join(self.tmp.name, "sealed")])
        self.assertEqual(code, 1)


class ExportImportTests(_Integrated):
    def test_export_and_import(self):
        out = os.path.join(self.tmp.name, "copy")
        code, _, _ = self._run(["export", self.pilot_dir, out, "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(out, "bundle.json")))
        out2 = os.path.join(self.tmp.name, "copy2")
        code, _, _ = self._run(["import", out, out2, "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(out2, "bundle.json")))

    def test_import_invalid_does_not_create_destination(self):
        bad = os.path.join(self.tmp.name, "bad2")
        os.makedirs(bad)
        with open(os.path.join(bad, "bundle.json"), "w") as f:
            json.dump({"schema_version": "v3.0", "dataset_id": "x", "cases": []}, f)
        dest = os.path.join(self.tmp.name, "never")
        code, _, _ = self._run(["import", bad, dest])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(dest))


if __name__ == "__main__":
    unittest.main()
