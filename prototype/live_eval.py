#!/usr/bin/env python3
"""Evaluate the LIVE production stack (through the running app services) on a
labelled query file. Read-only against triage.db; TEI reachable on the host.

    cd ~/mail-triage && .venv/bin/python ../mail-triage-rag/prototype/live_eval.py --qfile tests/eval_queries.json --tag live_48

Requires env (defaults in the script): EMBED_BASE_URL, RERANK_BASE_URL, DATA_DIR.
"""
import argparse, json, os, statistics, sys, time

PROD = os.environ.get("PROD_REPO", "/home/xrim/mail-triage")
sys.path.insert(0, PROD)
os.environ.setdefault("DATA_DIR", os.path.join(PROD, "data"))
os.environ.setdefault("EMBED_BASE_URL", "http://127.0.0.1:8041")
os.environ.setdefault("RERANK_BASE_URL", "http://127.0.0.1:8042")

import store  # noqa: E402
import rag    # noqa: E402

MODES = [
    ("fts", dict(mode="fts", rerank_on=False)),
    ("vector", dict(mode="vector", rerank_on=False)),
    ("hybrid", dict(mode="hybrid", rerank_on=False)),
    ("hybrid+rerank", dict(mode="hybrid", rerank_on=True)),
]


def rank_of(results, expect):
    for i, r in enumerate(results):
        s = (r.get("subject") or "").lower()
        for e in expect:
            if e.lower() in s:
                return i + 1
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qfile", default="tests/eval_queries.json")
    ap.add_argument("--tag", default="live")
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()
    queries = json.load(open(args.qfile))
    print("LIVE stack eval | %d queries | index chunks: %d" % (len(queries), store.chunk_count()))
    out = {"stack": "live/TEI-4B+v2m3", "query_file": args.qfile, "chunks": store.chunk_count()}
    for name, kw in MODES:
        hits1 = hits5 = hits10 = 0
        rr, lat, fails, rows = 0.0, [], [], []
        for item in queries:
            t0 = time.perf_counter()
            try:
                res = rag.search(item["q"], k=args.k, **kw)
            except Exception as exc:
                res = {"ok": False, "error": repr(exc), "results": []}
            ms = (time.perf_counter() - t0) * 1000
            lat.append(ms)
            results = res.get("results") or []
            rank = rank_of(results, item["expect"])
            if rank == 1: hits1 += 1
            if rank and rank <= 5: hits5 += 1
            if rank and rank <= args.k: hits10 += 1
            rr += (1.0 / rank) if rank else 0.0
            if not rank: fails.append(item["q"])
            rows.append({"q": item["q"], "rank": rank, "ms": round(ms, 1),
                         "note": (res.get("meta") or {}).get("note", "")})
        n = max(1, len(queries))
        ls = sorted(lat)
        m = {"R@1": round(hits1/n*100, 1), "R@5": round(hits5/n*100, 1),
             "R@10": round(hits10/n*100, 1), "MRR": round(rr/n, 3),
             "not_found": len(fails), "ms_mean": round(statistics.mean(lat), 1),
             "ms_p50": round(ls[len(ls)//2], 1), "ms_p95": round(ls[int(len(ls)*0.95)], 1)}
        out[name] = {"metrics": m, "misses": fails[:10], "rows": rows}
        print("%-15s R@1 %5.1f%%  R@5 %5.1f%%  R@10 %5.1f%%  MRR %.3f  nf %2d  ms p50 %5.0f" %
              (name, m["R@1"], m["R@5"], m["R@10"], m["MRR"], m["not_found"], m["ms_p50"]))
    tag = args.tag
    dest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench", "live_%s.json" % tag)
    json.dump(out, open(dest, "w"), indent=1)
    print("wrote", dest)


if __name__ == "__main__":
    main()
