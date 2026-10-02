"""End-to-end offline test: oracle satisfies every case; flawed is caught."""
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, V2)

from harness import mock_run  # noqa: E402
from scoring import score as scorer  # noqa: E402


def test_oracle_scores_perfect():
    tmp = tempfile.mkdtemp()
    m = mock_run.build_run("oracle", "oracle-mock", tmp)
    rep, per_case = scorer.score_run(m["run_id"], results=tmp)
    assert rep["coverage"]["_totals"]["complete"] is True
    assert rep["quality"]["overall_scored_only"] == 100.0
    assert rep["cost_index"]["value"] == 100.0
    assert not rep["failures"]["by_kind"], rep["failures"]["by_kind"][:10]
    assert rep["coverage"]["_totals"]["total"] == 600


def test_flawed_is_caught_and_absent_not_credited():
    tmp = tempfile.mkdtemp()
    m = mock_run.build_run("flawed", "flawed-mock", tmp)
    # delete one attempt to simulate a missing case -> incomplete
    path = os.path.join(tmp, m["run_id"], "attempts.jsonl")
    lines = open(path).read().splitlines()
    open(path, "w").write("\n".join(lines[1:]) + "\n")
    rep, _ = scorer.score_run(m["run_id"], results=tmp)
    assert rep["coverage"]["_totals"]["complete"] is False
    assert rep["quality"]["overall_fixed_denominator"] < 100.0
    assert rep["failures"]["total_model_failures"] > 0


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
