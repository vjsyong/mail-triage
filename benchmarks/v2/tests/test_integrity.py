"""WP1 run-integrity tests."""
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, V2)

from common import identity  # noqa: E402
from harness import run_manager  # noqa: E402


def _manifest(**over):
    fields = dict(
        model_key="testmodel", model_revision="rev1", tokenizer="tok",
        chat_template="tpl", runtime_image="img@sha256:abc", hardware="gpu",
        harness_revision="h2.0", scorer_revision="s2.0",
        corpus_sha256="c" * 64, case_manifest_sha256="m" * 64,
        prompts_sha256="p" * 64, params={"temperature": 0},
        adaptations={"thinking": "off"}, retry_policy={"max": 1},
        fallback_policy={"enabled": False}, cache_state="cold")
    fields.update(over)
    return identity.build_manifest(**fields)


def test_manifest_required_fields():
    try:
        identity.build_manifest(model_key="x")
    except ValueError as exc:
        assert "missing required fields" in str(exc)
        return
    raise AssertionError("expected ValueError")


def test_manifest_hash_deterministic_and_param_sensitive():
    a = _manifest()
    b = _manifest()
    assert a["config_hash"] == b["config_hash"]
    c = _manifest(params={"temperature": 0.7})
    assert a["config_hash"] != c["config_hash"]
    assert a["run_id"] != c["run_id"]


def test_resume_refuses_config_change():
    a = _manifest()
    b = _manifest(params={"temperature": 0.7})
    assert identity.resolve_resume(a, a) == "resume"
    try:
        identity.resolve_resume(b, a)
    except ValueError as exc:
        assert "config mismatch" in str(exc)
        return
    raise AssertionError("expected config mismatch")


def test_attempts_and_coverage():
    tmp = tempfile.mkdtemp()
    m = _manifest()
    run_manager.init_run(m, results=tmp)
    # record two ok attempts and one duplicate case attempt (retry) 
    for i, cid in enumerate(["cls_base_201", "cls_base_202"]):
        run_manager.record_attempt(m["run_id"], "classification", {
            "case_id": cid, "suite": "classification", "model": m["model_key"],
            "run_id": m["run_id"], "attempt": 1, "status": "ok",
            "output": {"parsed": {}}}, results=tmp)
    # retry the first, plus mark one infra error
    run_manager.record_attempt(m["run_id"], "classification", {
        "case_id": "cls_base_201", "suite": "classification", "model": m["model_key"],
        "run_id": m["run_id"], "attempt": 2, "status": "ok",
        "output": {"parsed": {"category": "Action"}}}, results=tmp)
    resolved = run_manager.resolve_attempts(m["run_id"], results=tmp)
    assert resolved[("classification", "cls_base_201")]["attempt"] == 2
    # no actual cases dir override -> real cases; coverage should be incomplete
    cov = run_manager.coverage(m["run_id"], results=tmp)
    assert cov["_totals"]["total"] == 600
    assert cov["_totals"]["missing"] == 598
    assert run_manager.eligible_for_comparison(cov) is False


def test_init_run_rejects_bad_manifest():
    tmp = tempfile.mkdtemp()
    bad = _manifest()
    bad.pop("scorer_revision")
    try:
        run_manager.init_run(bad, results=tmp)
    except Exception as exc:
        assert "scorer_revision" in str(exc) or "required" in str(exc)
        return
    raise AssertionError("expected schema rejection")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
