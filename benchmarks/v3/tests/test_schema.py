"""Schema, artifact-validation and review-gate tests (WP1)."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3 import schema  # noqa: E402
from benchmarks.v3 import contracts  # noqa: E402
from benchmarks.v3.common import identity  # noqa: E402


def _scenario():
    return {
        "schema_version": "v3.0", "scenario_id": "scn_0001",
        "lineage_id": "lin_0001",
        "recipient": {"persona": "employee", "policy_id": "default"},
        "frozen_time": "2025-09-01T09:00:00Z",
        "messages": [{"message_id": "msg_1", "from_addr": "a@b.c",
                      "to_addr": "o@x.y", "subject": "S",
                      "date": "2025-09-01", "snippet": "hi"}],
        "relevant_facts": ["fact"], "source_provenance": "prov_0001",
    }


def _case():
    return {
        "schema_version": "v3.0", "case_id": "case_0001",
        "scenario_id": "scn_0001", "lineage_id": "lin_0001", "task": "decision",
        "input_profile": "native", "policy_id": "default", "split": "development",
        "gold_id": "gold_0001",
        "rendered_input": {"profile": "native", "system": "s", "user": "u"},
    }


def _lineage():
    return {
        "schema_version": "v3.0", "lineage_id": "lin_0001",
        "root_id": "scn_0001", "members": ["case_0001"],
        "source_message_ids": ["msg_1"], "relation_type": "root",
    }


def _provenance():
    return {
        "schema_version": "v3.0", "provenance_id": "prov_0001",
        "source": "synthetic", "source_release": "v3-synth-1",
        "license": "CC0", "retrieval": "generated",
        "content_sha256": "a" * 64,
        "authorization": {"authorized": True, "authorized_by": "bench-owner"},
    }


def _gold():
    g = schema.new_gold("case_0001", "gold_0001",
                        observable={"category": "visible", "needs_reply": "visible"})
    g["answer"] = {"category": "Action", "acceptable_categories": ["Action"],
                   "needs_reply": True, "required_outcomes": [],
                   "forbidden_outcomes": [], "supporting_evidence": []}
    return g


def _attempt():
    return {
        "schema_version": "v3.0", "run_id": "run-1", "case_id": "case_0001",
        "attempt": 1, "adapter_id": "fake-native", "status": "ok",
        "output": {"raw": "{}", "parsed": {}},
        "field_provenance": {"category": "produced", "needs_reply": "produced",
                             "confidence": "missing", "summary": "missing",
                             "reason": "missing"},
    }


def _manifest():
    return identity.build_manifest(
        dataset_id="v3_dev", dataset_sha256="a" * 64,
        case_manifest_sha256="b" * 64, prompt_revision="native-v3.0",
        prompt_sha256="c" * 64, model_key="minicpm5-2b", model_revision="rev-1",
        model_artifact_sha256="", adapter_id="fake-native",
        adapter_revision="1.0", scorer_revision="3.0", policy_revision="3.0",
        policy_sha256="d" * 64, engine_contract_sha256="e" * 64,
        calibrator_revision="none",
        generation_config={"temperature": 0, "calibrator": "none"},
        runtime_config={"cores": 4, "ram_gib": 8},
        requested_case_ids=["case_0001"], requested_splits=["development"],
        requested_profiles=["native"])


VALID = {
    "scenario": _scenario, "case": _case, "lineage": _lineage,
    "gold": _gold, "provenance": _provenance, "attempt": _attempt,
    "run_manifest": _manifest,
}


class ValidArtifactsTest(unittest.TestCase):
    def test_all_kinds_present_and_valid(self):
        self.assertTrue(set(VALID) <= set(schema.ARTIFACT_KINDS))
        for kind, factory in VALID.items():
            with self.subTest(kind=kind):
                self.assertEqual(schema.validate_artifact(kind, factory()), [])

    def test_default_policy_valid(self):
        path = os.path.join(V3, "policy", "default.json")
        with open(path) as f:
            policy = json.load(f)
        self.assertEqual(schema.validate_artifact("policy", policy), [])
        self.assertEqual(policy["schema_version"], schema.SCHEMA_VERSION)

    def test_unknown_kind(self):
        with self.assertRaises(KeyError):
            schema.validate_artifact("nope", {})


class MalformedArtifactsTest(unittest.TestCase):
    def test_wrong_schema_version_rejected(self):
        bad = _case()
        bad["schema_version"] = "v2.0"
        self.assertTrue(schema.validate_artifact("case", bad))

    def test_missing_required_field_rejected(self):
        for kind, key in (("scenario", "scenario_id"), ("case", "gold_id"),
                          ("gold", "review_status"), ("attempt", "run_id"),
                          ("lineage", "members")):
            with self.subTest(kind=kind):
                bad = VALID[kind]()
                del bad[key]
                self.assertTrue(schema.validate_artifact(kind, bad))

    def test_run_manifest_missing_fingerprint_rejected(self):
        bad = _manifest()
        del bad["dataset_sha256"]
        errs = schema.validate_artifact("run_manifest", bad)
        self.assertTrue(any("dataset_sha256" in e for e in errs))
        # identity violations are surfaced even when the JSON shape is intact
        bad2 = _manifest()
        bad2["engine_contract_sha256"] = ""
        self.assertTrue(any("engine_contract_sha256" in e
                            for e in schema.validate_artifact("run_manifest", bad2)))

    def test_observability_enum_enforced(self):
        bad = _gold()
        bad["observable"] = {"category": "guessed"}
        errs = schema.validate_artifact("gold", bad)
        self.assertTrue(any("observable" in e for e in errs))

    def test_wrong_shaped_observable_returns_errors(self):
        for bad_shape in (["visible"], "visible", 5):
            with self.subTest(shape=type(bad_shape).__name__):
                bad = _gold()
                bad["observable"] = bad_shape
                errs = schema.validate_artifact("gold", bad)  # must not raise
                self.assertTrue(any("observable" in e for e in errs))

    def test_wrong_shaped_field_provenance_returns_errors(self):
        for bad_shape in (["produced"], "produced", 5):
            with self.subTest(shape=type(bad_shape).__name__):
                bad = _attempt()
                bad["field_provenance"] = bad_shape
                errs = schema.validate_artifact("attempt", bad)  # must not raise
                self.assertTrue(any("field_provenance" in e for e in errs))

    def test_workflow_profile_accepted(self):
        self.assertIn("workflow", schema.PROFILES)
        self.assertEqual(set(schema.PROFILES), set(contracts.BENCHMARK_PROFILES))
        case = _case()
        case["task"] = "workflow"
        case["input_profile"] = "workflow"
        case["rendered_input"] = {"profile": "workflow", "system": "s", "user": "u"}
        self.assertEqual(schema.validate_artifact("case", case), [])
        att = _attempt()
        att["profile"] = "workflow"
        self.assertEqual(schema.validate_artifact("attempt", att), [])

    def test_field_provenance_enum_enforced(self):
        bad = _attempt()
        bad["field_provenance"] = {"category": "invented"}
        errs = schema.validate_artifact("attempt", bad)
        self.assertTrue(any("field_provenance" in e for e in errs))

    def test_validate_or_raise(self):
        bad = _scenario()
        del bad["messages"]
        with self.assertRaises(schema.ValidationError):
            schema.validate_artifact_or_raise("scenario", bad)


class ReviewGateTest(unittest.TestCase):
    def test_new_gold_is_honestly_draft(self):
        g = schema.new_gold("case_0001", "gold_0001")
        self.assertEqual(g["review_status"], "draft")
        self.assertFalse(g["human_seal"])
        self.assertIsNone(g["reviewer"])
        self.assertFalse(g["authorized"])

    def test_draft_cannot_be_born_sealed(self):
        g = schema.new_gold("case_0001", "gold_0001",
                            review_status="sealed", human_seal=True)
        self.assertEqual(g["review_status"], "draft")
        self.assertFalse(g["human_seal"])

    def test_seal_requires_reviewer(self):
        with self.assertRaises(schema.SealError):
            schema.seal_gold(schema.new_gold("c", "g"), reviewer="")

    def test_real_mail_requires_authorization(self):
        g = schema.new_gold("c", "g", source="real_mail")
        self.assertFalse(schema.can_seal(g))
        with self.assertRaises(schema.SealError):
            schema.seal_gold(g, reviewer="human-1")
        sealed = schema.seal_gold(g, reviewer="human-1", authorized=True)
        self.assertEqual(sealed["review_status"], "sealed")
        self.assertTrue(sealed["human_seal"])

    def test_synthetic_seal_with_reviewer(self):
        g = schema.new_gold("c", "g", source="synthetic")
        self.assertTrue(schema.can_seal(dict(g, reviewer="human-1")))
        sealed = schema.seal_gold(g, reviewer="human-1")
        self.assertTrue(sealed["human_seal"])

    def test_assert_draft_honest(self):
        with self.assertRaises(schema.SealError):
            schema.assert_draft_honest({"review_status": "sealed",
                                        "human_seal": False, "reviewer": None})

    def test_draft_cannot_carry_seal(self):
        g = _gold()
        g["review_status"] = "draft"
        g["human_seal"] = True
        g["reviewer"] = "name"
        with self.assertRaises(schema.SealError):
            schema.assert_draft_honest(g)
        self.assertTrue(any("human_seal" in e
                            for e in schema.validate_artifact("gold", g)))

    def test_sealed_requires_nonblank_reviewer(self):
        g = _gold()
        g["review_status"] = "sealed"
        g["human_seal"] = True
        g["reviewer"] = "   "
        with self.assertRaises(schema.SealError):
            schema.assert_draft_honest(g)
        self.assertFalse(schema.can_seal(dict(g, reviewer="   ")))
        self.assertFalse(schema.can_seal(dict(g, reviewer=None)))

    def test_real_mail_sealed_requires_authorization_in_validation(self):
        g = _gold()
        g["source"] = "real_mail"
        g["review_status"] = "sealed"
        g["human_seal"] = True
        g["reviewer"] = "human-1"
        g["authorized"] = False
        errs = schema.validate_artifact("gold", g)
        self.assertTrue(any("authorized" in e for e in errs))
        with self.assertRaises(schema.SealError):
            schema.assert_draft_honest(g)

    def test_new_gold_defaults_authorized_false_and_serializable(self):
        g = schema.new_gold("case_0001", "gold_0001")
        self.assertFalse(g["authorized"])
        self.assertEqual(schema.validate_artifact("gold", g), [])


if __name__ == "__main__":
    unittest.main()
