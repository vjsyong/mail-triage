"""Calibration, probability semantics and selective risk tests (WP5 FR6)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.scoring import (  # noqa: E402
    CalibrationError, ProbabilityError, apply_calibrator, confidence_field,
    default_policy, fit_calibrator, reliability, selective_risk,
    validate_probability,
)
from benchmarks.v3.scoring import testing as T  # noqa: E402


class ProbabilitySemanticsTest(unittest.TestCase):
    def setUp(self):
        self.policy = default_policy()

    def test_valid_probabilities_pass(self):
        self.assertEqual(validate_probability(0.0, self.policy, "category_confidence"), 0.0)
        self.assertEqual(validate_probability(1, self.policy, "category_confidence"), 1.0)
        self.assertIsNone(validate_probability(None, self.policy, "category_confidence"))

    def test_nonfinite_and_out_of_range_rejected(self):
        for bad in (float("nan"), float("inf"), -0.1, 1.1, "0.5", True):
            with self.subTest(value=bad):
                with self.assertRaises(ProbabilityError):
                    validate_probability(bad, self.policy, "category_confidence")

    def test_confidence_is_not_a_reply_probability(self):
        self.assertEqual(confidence_field(self.policy, "category_confidence"),
                         "confidence")
        with self.assertRaises(ProbabilityError):
            confidence_field(self.policy, "reply_probability")


class CalibratorTest(unittest.TestCase):
    def _bundle(self):
        cases = [T.case("case_0001", split="calibration"),
                 T.case("case_0002", split="development")]
        golds = [T.gold("case_0001", category="Action"),
                 T.gold("case_0002", category="Action")]
        ds = T.dataset(cases, golds)
        manifest = T.manifest(requested_case_ids=["case_0001", "case_0002"],
                              requested_splits=["calibration", "development"])
        run = T.run(manifest, [T.attempt("case_0001", category="Action",
                                         confidence=0.9),
                               T.attempt("case_0002", category="Promo",
                                         confidence=0.9)])
        return ds, run

    def test_fit_rejects_non_calibration_split(self):
        ds, run = self._bundle()
        with self.assertRaises(CalibrationError):
            fit_calibrator(ds, run, split="private_test")

    def test_fit_uses_calibration_split_only(self):
        ds, run = self._bundle()
        artifact = fit_calibrator(ds, run)
        self.assertEqual(artifact["fit_split"], "calibration")
        self.assertEqual(artifact["source_case_ids"], ["case_0001"])
        self.assertTrue(artifact["artifact_sha256"])
        self.assertTrue(artifact["revision"].startswith("cal3.0-"))

    def test_fit_is_deterministic(self):
        ds, run = self._bundle()
        self.assertEqual(fit_calibrator(ds, run)["artifact_sha256"],
                         fit_calibrator(ds, run)["artifact_sha256"])

    def test_unauthorized_real_mail_not_used(self):
        ds, run = self._bundle()
        ds["gold"][0]["source"] = "real_mail"
        ds["gold"][0]["authorized"] = False
        with self.assertRaises(CalibrationError):
            fit_calibrator(ds, run)

    def test_apply_calibrator_uses_bin_accuracy(self):
        artifact = {"bins": [{"n": 5, "accuracy": 0.25}, {"n": 5, "accuracy": 0.75}],
                    "threshold": 0.6}
        self.assertEqual(apply_calibrator(artifact, 0.1), 0.25)
        self.assertEqual(apply_calibrator(artifact, 0.9), 0.75)
        self.assertIsNone(apply_calibrator(artifact, None))


class ReliabilityTest(unittest.TestCase):
    def test_no_confidence_is_not_estimable(self):
        records = [{"confidence": None, "correct": True}]
        table = reliability(records, 10)
        self.assertEqual(table["n"], 0)
        self.assertIsNone(table["ece"])
        selective = selective_risk(records, 0.6)
        self.assertEqual(selective["coverage"], 0.0)

    def test_ece_and_selective_risk(self):
        records = [{"confidence": 0.9, "correct": True},
                   {"confidence": 0.9, "correct": True},
                   {"confidence": 0.9, "correct": False},
                   {"confidence": 0.1, "correct": False}]
        table = reliability(records, 10)
        self.assertGreater(table["ece"], 0)
        selective = selective_risk(records, 0.5)
        self.assertAlmostEqual(selective["coverage"], 0.75)
        self.assertAlmostEqual(selective["accepted_error_rate"], 1 / 3.0)

    def test_always_defer_has_zero_coverage(self):
        records = [{"confidence": 0.2, "correct": True}]
        selective = selective_risk(records, 0.99)
        self.assertEqual(selective["coverage"], 0.0)
        self.assertIsNone(selective["accepted_error_rate"])


if __name__ == "__main__":
    unittest.main()
