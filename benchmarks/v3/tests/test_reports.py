"""Report rendering/writing and comparison-integrity tests (WP5 FR6/FR7)."""
import json
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

from benchmarks.v3.scoring import (  # noqa: E402
    ComparisonError, compare_runs, render_markdown, score_run, write_report,
)
from benchmarks.v3.scoring import testing as T  # noqa: E402

COMPARE_POLICY = {"bootstrap": {"B": 200, "min_lineages": 3}}


def build(n=12, correct=True, model_key="model-a"):
    ids = []
    cases = []
    golds = []
    attempts = []
    for i in range(n):
        cid = "case_%04d" % i
        ids.append(cid)
        cases.append(T.case(cid, lineage_id="lin_%03d" % i))
        golds.append(T.gold(cid, category="Action", review_status="reviewed"))
        attempts.append(T.attempt(cid, category="Action" if correct else "Promo",
                                  needs_reply=True, confidence=0.9))
    ds = T.dataset(cases, golds)
    manifest = T.manifest(model_key=model_key, requested_case_ids=ids,
                          requested_splits=["development"], requested_profiles=["native"])
    return ds, T.run(manifest, attempts)


class ReportRenderTest(unittest.TestCase):
    def setUp(self):
        self.report = score_run(*build())

    def test_dimensions_separate_and_no_quality(self):
        native = self.report["profiles"]["native"]
        for key in ("category", "needs_reply", "format", "confidence",
                    "full_response", "prose_review", "relations"):
            self.assertIn(key, native)
        self.assertNotIn("quality", self.report)

    def test_bootstrap_echoes_single_policy(self):
        boot = self.report["bootstrap"]
        self.assertEqual(boot["B"], 4000)
        self.assertIn("seed", boot)
        self.assertEqual(boot["margin"], 0.03)

    def test_markdown_truthful_absent_states(self):
        ds = T.dataset([T.case("case_0001")], [T.gold("case_0001")])
        run = T.run(T.manifest(requested_case_ids=["case_0001"]),
                    [T.attempt("case_0001", confidence=None)])
        markdown = render_markdown(score_run(ds, run))
        self.assertIn("# Benchmark v3 scoring report", markdown)
        self.assertIn("cpu qualified: False", markdown)
        self.assertIn("eligibility:", markdown)
        # no confidence signal -> reliability n/a, not a fake number
        self.assertIn("ECE=n/a", markdown)

    def test_markdown_shows_integrity_failure(self):
        ds, run = build(n=1)
        run["attempts"][0]["request_sha256"] = "deadbeef"
        report = score_run(ds, run)
        markdown = render_markdown(report)
        self.assertIn("integrity: FAILED", markdown)

    def test_write_json_and_markdown(self):
        tmp = tempfile.mkdtemp(prefix="v3-report-")
        try:
            json_path = os.path.join(tmp, "nested", "report.json")
            md_path = os.path.join(tmp, "report.md")
            write_report(self.report, json_path)
            write_report(self.report, md_path)
            with open(json_path) as handle:
                loaded = json.load(handle)
            self.assertEqual(loaded["run_id"], self.report["run_id"])
            with open(md_path) as handle:
                self.assertIn("# Benchmark v3 scoring report", handle.read())
        finally:
            shutil.rmtree(tmp)


class ComparisonTest(unittest.TestCase):
    def test_compare_rejects_dataset_or_scope_mismatch(self):
        ds, baseline = build()
        _, candidate = build()
        candidate["manifest"]["requested_case_ids"] = ["case_0001"]
        candidate["manifest"]["config_hash"] = "x" * 16
        with self.assertRaises(ComparisonError):
            compare_runs(ds, baseline, candidate, policy=COMPARE_POLICY)

    def test_compare_not_estimable_with_too_few_roots(self):
        ds, baseline = build(n=2, correct=False)
        _, candidate = build(n=2, correct=True, model_key="model-b")
        comparison = compare_runs(ds, baseline, candidate,
                                  policy={"bootstrap": {"min_lineages": 10}})
        metric = comparison["profiles"]["native"]["category_macro_f1"]
        self.assertFalse(metric["estimable"])
        self.assertEqual(metric["verdict"], "not_estimable")
        self.assertFalse(comparison["eligibility"]["estimable"])

    def test_compare_paired_and_protocol_echo(self):
        ds, baseline = build(n=12, correct=False)
        _, candidate = build(n=12, correct=True, model_key="model-b")
        comparison = compare_runs(ds, baseline, candidate, policy=COMPARE_POLICY)
        metric = comparison["profiles"]["native"]["category_macro_f1"]
        self.assertTrue(metric["estimable"])
        self.assertGreater(metric["diff"], 0)
        self.assertTrue(metric["noninferior"])
        self.assertEqual(comparison["protocol"]["B"], 200)
        self.assertEqual(comparison["eligibility"]["eligible"], True)

    def test_compare_deterministic(self):
        ds, baseline = build(n=12, correct=False)
        _, candidate = build(n=12, correct=True, model_key="model-b")
        first = compare_runs(ds, baseline, candidate, policy=COMPARE_POLICY)
        second = compare_runs(ds, baseline, candidate, policy=COMPARE_POLICY)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
