"""Lineage-clustered bootstrap, paired comparison and stability tests (WP5 FR7/8)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.scoring import metrics as M  # noqa: E402
from benchmarks.v3.scoring import stats  # noqa: E402
from benchmarks.v3.scoring.policy import normalize_policy  # noqa: E402


def policy(**bootstrap):
    base = {"B": 200, "min_lineages": 3}
    base.update(bootstrap)
    return normalize_policy({"bootstrap": base})


class BootstrapTest(unittest.TestCase):
    def test_point_is_recomputed_macro_f1_not_accuracy(self):
        clusters = {
            "L1": [("Action", "Action"), ("Action", "Promo")],
            "L2": [("Promo", "Promo"), ("Newsletter", "Promo")],
            "L3": [("Action", "Action")],
        }
        all_items = [it for group in clusters.values() for it in group]
        result = stats.bootstrap_metric(clusters, M.macro_f1, policy())
        self.assertAlmostEqual(result["point"], M.macro_f1(all_items))
        self.assertNotAlmostEqual(result["point"], M.accuracy(all_items))
        self.assertLessEqual(result["ci_low"], result["ci_high"])

    def test_deterministic_same_seed(self):
        clusters = {"L1": [("Action", "Action")], "L2": [("Action", "Promo")],
                    "L3": [("Promo", "Promo")]}
        first = stats.bootstrap_metric(clusters, M.macro_f1, policy(), ("x",))
        second = stats.bootstrap_metric(clusters, M.macro_f1, policy(), ("x",))
        self.assertEqual(first, second)

    def test_clusters_not_rows(self):
        clusters = {"L1": [("Action", "Action")] * 25,
                    "L2": [("Promo", "Promo")], "L3": [("Action", "Action")]}
        result = stats.bootstrap_metric(clusters, M.macro_f1, policy())
        self.assertEqual(result["n_roots"], 3)
        self.assertEqual(result["n_cases"], 27)

    def test_insufficient_clusters_not_estimable(self):
        clusters = {"L1": [("Action", "Action")], "L2": [("Promo", "Promo")]}
        result = stats.bootstrap_metric(clusters, M.macro_f1,
                                        policy(min_lineages=10))
        self.assertFalse(result["estimable"])


class PairedTest(unittest.TestCase):
    def _clusters(self, correct_clusters, total=5):
        clusters = {}
        for i in range(total):
            gold = "Action" if i < correct_clusters else "Promo"
            clusters["L%d" % i] = [("Action", "Action" if i < correct_clusters
                                    else "Promo")]
        return clusters

    def test_paired_better_and_deterministic(self):
        baseline = self._clusters(0)
        candidate = self._clusters(5)
        first = stats.paired_bootstrap(baseline, candidate, M.macro_f1, policy(),
                                       ("native", "category_macro_f1"))
        second = stats.paired_bootstrap(baseline, candidate, M.macro_f1, policy(),
                                        ("native", "category_macro_f1"))
        self.assertEqual(first, second)
        self.assertEqual(first["verdict"], "better")
        self.assertTrue(first["noninferior"])
        self.assertGreater(first["diff"], 0)
        self.assertGreaterEqual(first["ci_low"], first["ci_high"] - 1e6)

    def test_meaningful_bounds(self):
        baseline = self._clusters(1)
        candidate = self._clusters(3)
        result = stats.paired_bootstrap(baseline, candidate, M.macro_f1, policy())
        self.assertLessEqual(result["ci_low"], result["ci_high"])
        self.assertNotEqual(result["ci_low"], result["ci_high"])

    def test_inadequate_or_empty_scope_not_green(self):
        baseline = {"L1": [("Action", "Action")], "L2": [("Promo", "Promo")]}
        candidate = dict(baseline)
        result = stats.paired_bootstrap(baseline, candidate, M.macro_f1,
                                        policy(min_lineages=10))
        self.assertFalse(result["estimable"])
        self.assertFalse(result["noninferior"])
        self.assertEqual(result["verdict"], "not_estimable")

    def test_protocol_echoed(self):
        baseline = self._clusters(1)
        result = stats.paired_bootstrap(baseline, baseline, M.macro_f1,
                                        policy(B=123, alpha=0.1, margin=0.05))
        self.assertEqual(result["B"], 123)
        self.assertEqual(result["alpha"], 0.1)
        self.assertEqual(result["margin"], 0.05)

    def test_paired_resamples_shared_roots_only(self):
        baseline = {"L1": [("Action", "Action")], "L2": [("Promo", "Promo")],
                    "L3": [("Action", "Action")]}
        candidate = {"L1": [("Action", "Action")], "L2": [("Promo", "Promo")],
                     "L3": [("Action", "Action")], "L4": [("Promo", "Promo")]}
        result = stats.paired_bootstrap(baseline, candidate, M.macro_f1, policy())
        self.assertEqual(result["n_roots"], 3)  # L4 is not shared


class StabilityTest(unittest.TestCase):
    def test_variance_and_roots(self):
        repeats = [{"c1": 1, "c2": 1}, {"c1": 1, "c2": 0}]
        result = stats.stability_report(repeats, lineage_of={"c1": "L1", "c2": "L1"})
        self.assertEqual(result["n_cases"], 2)
        self.assertEqual(result["n_roots"], 1)
        self.assertAlmostEqual(result["stability"], 0.5)
        self.assertEqual(len(result["varying_cases"]), 1)


if __name__ == "__main__":
    unittest.main()
