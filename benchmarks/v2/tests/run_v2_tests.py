#!/usr/bin/env python3
"""Dependency-free v2 test runner.

Runs every ``test_*`` function in the ``tests`` directory, prints a summary,
and exits non-zero on the first failure class.  Also collectible by pytest.
"""
import importlib
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, V2)
sys.path.insert(0, HERE)

MODULES = ["test_scoring", "test_integrity", "test_lint", "test_stats",
           "test_runner_offline", "test_end_to_end", "test_plugin_data"]


def main():
    total = failed = 0
    failures = []
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
        except ImportError as exc:
            print("skip %s (%s)" % (name, exc))
            continue
        tests = [k for k in dir(mod) if k.startswith("test_") and callable(getattr(mod, k))]
        for t in tests:
            total += 1
            try:
                getattr(mod, t)()
            except Exception:
                failed += 1
                failures.append((name, t, traceback.format_exc()))
    for name, t, tb in failures:
        print("FAIL %s.%s\n%s" % (name, t, tb))
    print("\n%d tests, %d failed" % (total, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
