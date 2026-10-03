"""Run-identity and resume tests (WP1, FR7).

Every fingerprint must be non-empty; a null/absent hash cannot support a
resume; any change to dataset, prompt, weights, requested subset, scorer,
runtime or calibrator must block resume.
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

from benchmarks.v3.common import identity  # noqa: E402


def _fields(**over):
    f = dict(
        dataset_id="v3_dev",
        dataset_sha256="a" * 64,
        case_manifest_sha256="b" * 64,
        prompt_revision="native-v3.0",
        prompt_sha256="c" * 64,
        model_key="minicpm5-2b",
        model_revision="rev-1",
        model_artifact_sha256="",
        adapter_id="fake-native",
        adapter_revision="1.0",
        scorer_revision="3.0",
        policy_revision="3.0",
        policy_sha256="d" * 64,
        engine_contract_sha256="e" * 64,
        calibrator_revision="none",
        generation_config={"temperature": 0, "calibrator": "none"},
        runtime_config={"cores": 4, "ram_gib": 8},
        requested_case_ids=["case_0001", "case_0002"],
        requested_splits=["development"],
        requested_profiles=["native"],
    )
    f.update(over)
    return f


class BuildManifestTests(unittest.TestCase):
    def test_stable_and_order_independent(self):
        a = identity.build_manifest(**_fields())
        b = identity.build_manifest(**_fields())
        self.assertEqual(a["config_hash"], b["config_hash"])
        self.assertEqual(a["run_id"], b["run_id"])
        # nested dict insertion order must not matter (canonical hashing)
        reordered = _fields(generation_config={"calibrator": "none", "temperature": 0})
        self.assertEqual(a["config_hash"], identity.build_manifest(**reordered)["config_hash"])

    def test_model_artifact_alternative(self):
        m = identity.build_manifest(**_fields(model_revision="",
                                              model_artifact_sha256="f" * 64))
        self.assertEqual(m["model_key"], "minicpm5-2b")
        with self.assertRaises(identity.IdentityError):
            identity.build_manifest(**_fields(model_revision="",
                                              model_artifact_sha256=""))

    def test_missing_required_fields_rejected(self):
        with self.assertRaises(identity.IdentityError):
            identity.build_manifest(**_fields(dataset_sha256=None))
        with self.assertRaises(identity.IdentityError):
            identity.build_manifest(**_fields(scorer_revision=""))
        with self.assertRaises(identity.IdentityError):
            identity.build_manifest(**_fields(requested_case_ids=[]))
        with self.assertRaises(identity.IdentityError):
            identity.build_manifest(**_fields(runtime_config=None))

    def test_validate_identity_reports_errors(self):
        errs = identity.validate_identity(_fields(dataset_id=""))
        self.assertTrue(any("dataset_id" in e for e in errs))

    def test_scope_entries_must_be_nonempty_strings(self):
        for bad in (["case_0001", None], ["case_0001", ""], ["case_0001", "   "],
                    [None]):
            with self.subTest(bad=bad):
                with self.assertRaises(identity.IdentityError):
                    identity.build_manifest(**_fields(requested_case_ids=bad))

    def test_original_model_identity_is_hashed(self):
        slashed = identity.build_manifest(**_fields(model_key="openbmb/MiniCPM5-2B"))
        hyphen = identity.build_manifest(**_fields(model_key="openbmb-MiniCPM5-2B"))
        # sanitised only for run_id; the hashed identity keeps the original value
        self.assertNotEqual(slashed["config_hash"], hyphen["config_hash"])
        self.assertEqual(slashed["model_key"], "openbmb/MiniCPM5-2B")
        self.assertNotIn("/", slashed["run_id"])

    def test_malicious_model_key_yields_safe_run_id(self):
        m = identity.build_manifest(**_fields(model_key="../../etc/passwd"))
        self.assertNotIn("/", m["run_id"])
        self.assertNotIn("..", m["run_id"])
        self.assertEqual(m["model_key"], "../../etc/passwd")
        self.assertTrue(m["run_id"].split("-")[-2:])
        # path components never escape: safe() maps it to a simple slug
        self.assertEqual(identity.safe_run_component("../../etc/passwd"), "etc-passwd")
        self.assertEqual(identity.safe_run_component(".."), "model")
        self.assertEqual(identity.safe_run_component(""), "model")


class ResumeTests(unittest.TestCase):
    def test_same_identity_resumes(self):
        a = identity.build_manifest(**_fields())
        self.assertEqual(identity.resolve_resume(a, a), "resume")
        self.assertEqual(identity.resolve_resume(a, None), "fresh")

    def test_changes_block_resume(self):
        base = identity.build_manifest(**_fields())
        changes = {
            "corpus": {"dataset_sha256": "0" * 64},
            "prompt": {"prompt_sha256": "0" * 64},
            "weights": {"model_revision": "rev-2"},
            "subset": {"requested_case_ids": ["case_0001"]},
            "split": {"requested_splits": ["private_test"]},
            "scorer": {"scorer_revision": "3.1"},
            "runtime": {"runtime_config": {"cores": 8, "ram_gib": 8}},
            "calibrator": {"generation_config": {"temperature": 0,
                                                 "calibrator": "platt-v1"}},
        }
        for name, over in changes.items():
            with self.subTest(change=name):
                other = identity.build_manifest(**_fields(**over))
                self.assertNotEqual(base["config_hash"], other["config_hash"])
                with self.assertRaises(identity.IdentityError):
                    identity.resolve_resume(other, base)

    def test_null_hash_cannot_resume(self):
        a = identity.build_manifest(**_fields())
        with self.assertRaises(identity.IdentityError):
            identity.resolve_resume(a, {"run_id": "x", "config_hash": None})
        with self.assertRaises(identity.IdentityError):
            identity.resolve_resume(a, {"run_id": "x"})

    def test_changed_body_with_copied_hash_rejected(self):
        a = identity.build_manifest(**_fields())
        tampered = dict(a)
        tampered["prompt_sha256"] = "0" * 64  # body changed, old hash/run_id kept
        with self.assertRaises(identity.IdentityError):
            identity.resolve_resume(tampered, tampered)
        with self.assertRaises(identity.IdentityError):
            identity.resolve_resume(a, tampered)

    def test_invalid_old_manifest_with_matching_hash_rejected(self):
        a = identity.build_manifest(**_fields())
        broken = dict(a)
        broken["dataset_sha256"] = ""  # identity invalid, hash untouched
        with self.assertRaises(identity.IdentityError):
            identity.resolve_resume(a, broken)

    def test_tampered_run_id_rejected(self):
        a = identity.build_manifest(**_fields())
        wrong = dict(a)
        wrong["run_id"] = "someone-elses-run"
        with self.assertRaises(identity.IdentityError):
            identity.resolve_resume(a, wrong)

    def test_tampered_requested_hash_rejected(self):
        a = identity.build_manifest(**_fields())
        b = dict(a)
        b["config_hash"] = "f" * 64  # requested manifest stale hash
        with self.assertRaises(identity.IdentityError):
            identity.resolve_resume(b, a)


if __name__ == "__main__":
    unittest.main()
