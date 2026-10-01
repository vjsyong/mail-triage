#!/usr/bin/env python3
"""Retrieval quality evaluation for the mail search index (real stack).

Runs against the live triage.db + TEI servers (no mocks) and compares retrieval
modes on a labelled query set:
    fts-only | vector-only | hybrid (RRF) | hybrid + cross-encoder rerank
Metrics: recall@1 / @5 / @10 and MRR per mode.

Query file: tests/eval_queries.json
    [{"q": "natural language query", "expect": ["substring of the subject", ...]}]
A result counts as a hit when its subject (case-insensitive) contains any of the
`expect` substrings. Rank is the position of the first hit.

Usage (from ~/mail-triage):
    EMBED_BASE_URL=http://127.0.0.1:8041 RERANK_BASE_URL=http://127.0.0.1:8042 \
        .venv/bin/python tests/retrieval_eval.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT)

import store  # noqa: E402
import rag  # noqa: E402

MODES = [
    ("fts", dict(mode="fts", rerank_on=False)),
    ("vector", dict(mode="vector", rerank_on=False)),
    ("hybrid", dict(mode="hybrid", rerank_on=False)),
    ("hybrid+rerank", dict(mode="hybrid", rerank_on=True)),
]

K = 10


def rank_of(results, expect):
    for i, r in enumerate(results):
        s = (r.get("subject") or "").lower()
        for e in expect:
            if e.lower() in s:
                return i + 1
    return None


def main():
    qfile = os.path.join(HERE, "eval_queries.json")
    if not os.path.exists(qfile):
        print("no %s yet" % qfile)
        return 1
    queries = json.load(open(qfile))
    stats = rag.index_stats()
    print("queries: %d | backend: %s | chunks in index: %d"
          % (len(queries), stats.get("backend", "?"), stats.get("chunks", 0)))
    print("%-15s %7s %7s %8s %7s %7s" % ("mode", "R@1", "R@5", "R@10", "MRR", "ms/q"))
    for name, kw in MODES:
        hits1 = hits5 = hits10 = 0
        rr = 0.0
        t0 = time.time()
        fails = []
        for item in queries:
            try:
                res = rag.search(item["q"], k=K, **kw)
            except Exception as exc:
                res = {"ok": False, "error": repr(exc)}
            results = res.get("results") or []
            rank = rank_of(results, item["expect"])
            if rank == 1:
                hits1 += 1
            if rank and rank <= 5:
                hits5 += 1
            if rank and rank <= K:
                hits10 += 1
            if rank:
                rr += 1.0 / rank
            else:
                fails.append(item["q"])
        n = max(1, len(queries))
        print("%-15s %6.1f%% %6.1f%% %7.1f%% %7.3f %6.0f"
              % (name, hits1 / n * 100, hits5 / n * 100, hits10 / n * 100,
                 rr / n, (time.time() - t0) / n * 1000))
        if fails and name == "hybrid+rerank":
            print("  misses (%s):" % len(fails))
            for f in fails[:12]:
                print("   -", f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
