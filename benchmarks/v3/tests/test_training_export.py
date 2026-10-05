"""AC2/M2/M3: explicit role/domain, hard fail-closed gating, contamination."""
import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.training import export as E  # noqa: E402
from benchmarks.v3.training import samples as S  # noqa: E402
from benchmarks.v3.training.taxonomies import load_taxonomy  # noqa: E402


def _fixture():
    fix = S.load_workflow_scenarios()
    return copy.deepcopy(fix["sources"]), copy.deepcopy(fix["dialogues"])


class DomainTests(unittest.TestCase):
    def test_domain_hashes_distinct_and_seed_sensitive(self):
        hs = [E.domain_spec(d, 7)["domain_sha256"]
              for d in ("training", "development", "evaluation")]
        self.assertEqual(len(set(hs)), 3)
        self.assertNotEqual(E.domain_spec("training", 1)["domain_sha256"],
                            E.domain_spec("training", 2)["domain_sha256"])


class FailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tax = load_taxonomy()

    def _export(self, sources, dialogues, domain="training", roles=("training",)):
        return E.export_sources(sources, dialogues, self.tax, domain=domain,
                                seed=7, allowed_roles=roles)

    def test_valid_fixture_has_no_rejections_and_pairs(self):
        s, d = _fixture()
        res = self._export(s, d)
        self.assertEqual(res["rejected"], [])
        self.assertEqual(len(res["pairs"]), len(s))
        self.assertEqual(E.assert_cross_task_linkage(res), [])

    def test_missing_role_rejected(self):
        s, d = _fixture()
        del s[0]["role"]
        res = self._export(s, d)
        self.assertTrue(any(r["reason"] == "missing_role" for r in res["rejected"]))

    def test_missing_domain_rejected(self):
        s, d = _fixture()
        del s[0]["domain"]
        res = self._export(s, d)
        self.assertTrue(any(r["reason"] == "missing_domain" for r in res["rejected"]))

    def test_disallowed_split_rejected_even_if_allowed(self):
        s, d = _fixture()
        s[0]["split"] = "calibration"
        # role/domain are valid and allowed, split must still fail closed
        res = self._export(s, d, roles=("training",))
        self.assertTrue(any(r["reason"].startswith("disallowed_split")
                            for r in res["rejected"]), res["rejected"])

    def test_real_mail_rejected(self):
        s, d = _fixture()
        s[0]["source_kind"] = "real_mail"
        res = self._export(s, d)
        self.assertTrue(any(r["reason"] == "real_mail_material"
                            for r in res["rejected"]))

    def test_ambiguous_or_unobservable_rejected(self):
        s, d = _fixture()
        s[0]["intent"]["observable"] = "ambiguous"
        s[0]["intent"]["category"] = None
        res = self._export(s, d)
        self.assertTrue(any(r["reason"] == "unobservable_decision_gold"
                            for r in res["rejected"]))

    def test_mixed_domain_rejected(self):
        s, d = _fixture()
        s[0]["domain"] = "development"
        # dialogue still training domain
        res = self._export(s, d)
        self.assertTrue(any("mixed_generation_domain" in r["reason"]
                            for r in res["rejected"]))

    def test_role_not_allowed_rejected(self):
        s, d = _fixture()
        res = self._export(s, d, roles=("development",))
        self.assertTrue(any(r["reason"].startswith("role_not_allowed")
                            for r in res["rejected"]))

    def test_no_dialogue_rejected(self):
        s, d = _fixture()
        res = self._export(s, d[:1])
        self.assertTrue(any(r["reason"] == "no_dialogue" for r in res["rejected"]))

    def test_dialogue_lineage_mismatch_rejected(self):
        s, d = _fixture()
        d[0]["lineage_id"] = "lin_wrong"
        res = self._export(s, d)
        self.assertTrue(any(r["reason"] == "dialogue_lineage_mismatch"
                            for r in res["rejected"]))


class ContaminationTests(unittest.TestCase):
    def setUp(self):
        self.tax = load_taxonomy()

    def test_shared_source_across_domains_flagged(self):
        s, d = _fixture()
        train = E.export_sources(s, d, self.tax, domain="training", seed=7,
                                 allowed_roles=("training",))
        # same records relabelled development (and same source id/text)
        s2 = copy.deepcopy(s)
        d2 = copy.deepcopy(d)
        for x in s2:
            x["role"] = "development"
            x["domain"] = "development"
        for x in d2:
            x["role"] = "development"
            x["domain"] = "development"
        dev = E.export_sources(s2, d2, self.tax, domain="development", seed=8,
                               allowed_roles=("development",))
        problems = E.contamination_report([train, dev])
        self.assertTrue(any("source_id" in p or "email text" in p or "lineage" in p
                            for p in problems), problems)

    def test_disjoint_domains_clean(self):
        s, d = _fixture()
        devfix = S.load_dev_workflow_scenarios()
        train = E.export_sources(s, d, self.tax, domain="training", seed=7,
                                 allowed_roles=("training",))
        dev = E.export_sources(copy.deepcopy(devfix["sources"]),
                               copy.deepcopy(devfix["dialogues"]), self.tax,
                               domain="development", seed=8,
                               allowed_roles=("development",))
        self.assertEqual(E.contamination_report([train, dev]), [])

    def test_cross_task_lineage_asserted_on_export(self):
        s, d = _fixture()
        res = E.export_sources(s, d, self.tax, domain="training", seed=7,
                               allowed_roles=("training",))
        by = {}
        for ex in res["accepted"]:
            by.setdefault(ex["source_id"], {})[ex["task"]] = ex["lineage_id"]
        for sid, tasks in by.items():
            self.assertEqual(tasks.get("decision"), tasks.get("workflow"), sid)


if __name__ == "__main__":
    unittest.main()
