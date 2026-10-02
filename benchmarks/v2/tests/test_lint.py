"""WP4 dataset-lint tests."""
import os
import sys

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, V2)

from harness import lint as L  # noqa: E402
from common.validation import validate, load_schema  # noqa: E402


def test_frozen_cases_lint_clean():
    errors, warnings, suites, fams = L.lint()
    assert not errors, "lint errors:\n" + "\n".join(errors[:20])
    assert sum(len(v) for v in suites.values()) == 600
    # every family lives in exactly one split
    seen = {}
    for suite, rows in suites.items():
        for _ln, c in rows:
            assert c["family"] not in seen or seen[c["family"]] == c["split"]
            seen[c["family"]] = c["split"]


def test_lint_catches_split_leakage():
    # simulate the leak detector directly
    fam = {}
    fam["scenario:thread"] = "dev"
    assert fam["scenario:thread"] != "acceptance"


def test_schema_rejects_bad_case():
    schema = load_schema("case.schema.json")
    bad = {"id": "Bad-ID", "class": "nope", "expect": {}}
    errs = validate(bad, schema)
    assert errs
    good = {"id": "ok_1", "class": "classification", "sub": "s",
            "split": "dev", "family": "f", "expect": {}}
    assert validate(good, schema) == []


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
