"""Benchmark v3 tests (stdlib ``unittest``; no network, no live DB).

Run from the repo root:

    .venv/bin/python -m unittest discover -s benchmarks/v3/tests -v
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_V3 = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_ROOT, _V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)
