#!/usr/bin/env python3
"""Run retrieval configurations against eval query sets; emit metrics + diagnostics.

    python prototype/eval.py --tag phase1
    python prototype/eval.py --configs C-06b-minilm,D-06b-nor --qfile bench/eval_extra.json
"""
import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import raglite as R  # noqa: E402

ROOT = R.ROOT

CONFIGS = {
    # name: (chunk set, embedder, reranker, channels)
    "FTS-raw":            ("raw",   "06b", None,       ("fts",)),
    "VEC-raw-06b":        ("raw",   "06b", None,       ("vec",)),
    "A-4b-v2m3":          ("raw",   "4b",  "v2m3",     ("fts", "vec")),
    "A-4b-nor":           ("raw",   "4b",  None,       ("fts", "vec")),
    "B-06b-v2m3":         ("raw",   "06b", "v2m3",     ("fts", "vec")),
    "D-06b-nor":          ("clean", "06b", None,       ("fts", "vec")),
    "C-06b-minilm":       ("clean", "06b", "minilm",   ("fts", "vec")),
    "C-06b-minilm12":     ("clean", "06b", "minilm12", ("fts", "vec")),
    "C-06b-bge-base":     ("clean", "06b", "bge-base", ("fts", "vec")),
    "C-06b-jina-turbo":   ("clean", "06b", "jina-turbo", ("fts", "vec")),
    "C-06b-v2m3":         ("clean", "06b", "v2m3",     ("fts", "vec")),
    "C-4b-v2m3":          ("clean", "4b",  "v2m3",     ("fts", "vec")),
}


def rank_of(results, expect):
    for i, r in enumerate(results):
        s = (r.get("subject") or "").lower()
        for e in expect:
            if e.lower() in s:
                return i + 1
    return None


def run(qfile, k=10):
    queries = json.load(open(qfile))
    print("configs run against %s (%d queries)" % (qfile, len(queries)))
    out = {}
    for name, (cset, embed, rerank, channels) in CONFIGS.items():
        hits1 = hits5 = hits10 = 0
        rr, lat, fails = 0.0, [], []
        diag_rows = []
        t_all = time.perf_counter()
        for item in queries:
            t0 = time.perf_counter()
            try:
                res = R.search(item["q"], k=k, cset=cset, embed=embed, rerank=rerank,
                               channels=channels, diag=True)
            except Exception as exc:
                res = {"ok": False, "error": repr(exc), "results": []}
            ms = (time.perf_counter() - t0) * 1000
            lat.append(ms)
            results = res.get("results") or []
            rank = rank_of(results, item["expect"])
            if rank == 1:
                hits1 += 1
            if rank and rank <= 5:
                hits5 += 1
            if rank and rank <= k:
                hits10 += 1
            if rank:
                rr += 1.0 / rank
            else:
                fails.append(item["q"])
            stage = {k: (res.get("meta") or {}).get(k) for k in
                     ("ms_meta", "ms_fts", "ms_vec", "ms_rerank")}
            diag_rows.append({
                "q": item["q"], "rank": rank, "ms": round(ms, 1), "stage_ms": stage,
                "note": (res.get("meta") or {}).get("note", ""),
                "top": [{"subject": (r.get("subject") or "")[:70],
                         "message_id": r.get("message_id"),
                         "bm25_rank": r.get("bm25_rank"), "vec_rank": r.get("vector_rank"),
                         "rrf": r.get("rrf_score"), "rerank": r.get("reranker_score"),
                         "thread_size": r.get("thread_size"),
                         "from": r.get("from_addr")} for r in results[:5]],
            })
        n = max(1, len(queries))
        lat_sorted = sorted(lat)
        metrics = {
            "R@1": round(hits1 / n * 100, 1), "R@5": round(hits5 / n * 100, 1),
            "R@10": round(hits10 / n * 100, 1), "MRR": round(rr / n, 3),
            "not_found": len(fails),
            "ms_mean": round(statistics.mean(lat), 1),
            "ms_p50": round(lat_sorted[len(lat_sorted) // 2], 1),
            "ms_p95": round(lat_sorted[int(len(lat_sorted) * 0.95)], 1),
            "total_s": round(time.perf_counter() - t_all, 1),
        }
        out[name] = {"query_file": qfile, "config": {"cset": cset, "embed": embed,
                                                     "rerank": rerank,
                                                     "channels": list(channels)},
                     "metrics": metrics, "misses": fails[:10],
                     "rows": [{"q": d["q"], "rank": d["rank"], "ms": d["ms"]}
                              for d in diag_rows]}
        print("%-18s R@1 %5.1f%%  R@5 %5.1f%%  R@10 %5.1f%%  MRR %.3f  "
              "notfound %2d  ms p50 %5.0f p95 %5.0f" %
              (name, metrics["R@1"], metrics["R@5"], metrics["R@10"], metrics["MRR"],
               metrics["not_found"], metrics["ms_p50"], metrics["ms_p95"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="phase1")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--qfile", default=os.path.join(ROOT, "tests", "eval_queries.json"))
    ap.add_argument("--configs", default="")
    args = ap.parse_args()
    keep = [c.strip() for c in args.configs.split(",") if c.strip()]
    if keep:
        global CONFIGS
        CONFIGS = {k: v for k, v in CONFIGS.items() if k in keep}
    bench = os.path.join(ROOT, "prototype", "bench")
    os.makedirs(bench, exist_ok=True)
    out = run(args.qfile, k=args.k)
    outfile = os.path.join(bench, "results_%s.json" % args.tag)
    json.dump({"results": out, "index": R.index_stats()}, open(outfile, "w"), indent=1)
    print("wrote", outfile)


if __name__ == "__main__":
    main()
