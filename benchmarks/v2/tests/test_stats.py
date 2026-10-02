"""WP5 statistics tests."""
import os
import sys

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, V2)

from harness import stats  # noqa: E402


def _rows(base, n=40, fam=4):
    rows = []
    for i in range(n):
        rows.append({"id": "c%03d" % i, "family": "fam%d" % (i % fam),
                     "quality": base, "failures": []})
    return rows


def test_paired_detects_improvement():
    a = _rows(0.5)
    b = _rows(0.9)
    out = stats.paired_compare(a, b, B=500)
    assert out["verdict"] == "better"
    assert out["mean_diff"] > 0.3
    assert out["ci_low"] > 0


def test_paired_inconclusive_when_equal():
    a = _rows(0.8)
    b = _rows(0.8)
    out = stats.paired_compare(a, b, B=500)
    assert out["verdict"] == "inconclusive"


def test_noninferiority_margin():
    a = _rows(0.80)
    b = _rows(0.78)  # slightly worse
    ni = stats.noninferiority(a, b, margin=0.05, B=500)
    assert ni["noninferior"] is True
    strict = stats.noninferiority(a, b, margin=0.005, B=500)
    assert strict["noninferior"] is False


def test_stability_detects_variance():
    r1 = [{"id": "c1", "quality": 1.0, "failures": []},
          {"id": "c2", "quality": 1.0, "failures": []}]
    r2 = [{"id": "c1", "quality": 1.0, "failures": []},
          {"id": "c2", "quality": 0.0, "failures": [{"kind": "wrong_category"}]}]
    r3 = [{"id": "c1", "quality": 1.0, "failures": []},
          {"id": "c2", "quality": 1.0, "failures": []}]
    out = stats.stability_report([r1, r2, r3])
    assert out["quality_variance_rate"] == 0.5
    assert out["failure_kind_variance_rate"] == 0.5


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
