#!/usr/bin/env python3
"""Phase 6 measurement: model load times, CPU/GPU latencies, memory, storage size.

    python prototype/measure.py            # all measurements -> bench/measurements.json
"""
import json
import os
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import raglite as R  # noqa: E402

OUT = os.path.join(R.ROOT, "prototype", "bench", "measurements.json")


def http_ms(url, payload, n=3):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
        ts.append((time.perf_counter() - t0) * 1000)
    return round(statistics.median(ts), 1)


def main():
    out = {}

    # ---- storage sizes (prototype tables) ----
    with R.db(vec=True) as c:
        def q(sql):
            return c.execute(sql).fetchone()[0]
        sizes = {
            "messages_light_text_bytes": q("SELECT COALESCE(SUM(LENGTH(snippet)+LENGTH(clean_body)+LENGTH(quoted)),0) FROM messages_light"),
            "chunks_raw_text_bytes": q("SELECT COALESCE(SUM(LENGTH(text)),0) FROM chunks WHERE cset='raw'"),
            "chunks_clean_text_bytes": q("SELECT COALESCE(SUM(LENGTH(text)),0) FROM chunks WHERE cset='clean'"),
            "vec_raw_06b_bytes": q("SELECT COUNT(*)*1024*4 FROM vec_raw_06b"),
            "vec_clean_06b_bytes": q("SELECT COUNT(*)*1024*4 FROM vec_clean_06b"),
            "vec_raw_4b_bytes": q("SELECT COUNT(*)*2560*4 FROM vec_raw_4b"),
        }
    out["storage_bytes"] = sizes
    out["storage_mb"] = {k: round(v / 1e6, 1) for k, v in sizes.items()}
    try:
        out["db_file_mb"] = round(os.path.getsize(R.DB_PATH) / 1e6, 1)
    except OSError:
        pass

    # ---- CPU embedding: load, throughput, per-query ----
    os.environ.setdefault("FASTEMBED_CACHE_PATH", R.MODEL_CACHE)
    from fastembed import TextEmbedding
    t0 = time.time()
    emb = TextEmbedding("Qwen/Qwen3-Embedding-0.6B", threads=32)
    out["fastembed_load_s"] = round(time.time() - t0, 1)
    out["rss_after_load_mb"] = round(R.mem_rss_mb(), 1)
    with R.db(vec=False) as c:
        chunks = [r[0] for r in c.execute(
            "SELECT text FROM chunks WHERE cset='clean' AND id > 100 LIMIT 32")]
    t0 = time.perf_counter()
    list(emb.embed(chunks))
    dt = time.perf_counter() - t0
    out["cpu_index_chunks_per_s"] = round(32 / dt, 2)
    out["cpu_index_seconds_per_email"] = round(2.6 / (32 / dt), 2)  # avg ~2.6 chunks/msg
    out["rss_after_embed_mb"] = round(R.mem_rss_mb(), 1)
    q = ["Instruct: Given a search query, retrieve relevant email messages from "
         "the user's mailbox\nQuery: what did the library say about recycling"]
    ts = []
    for _ in range(5):
        t0 = time.perf_counter()
        list(emb.embed(q))
        ts.append((time.perf_counter() - t0) * 1000)
    out["cpu_query_embed_ms_median"] = round(statistics.median(ts), 1)
    del emb

    # ---- rerankers: load + 20-passage latency ----
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    passages = ["From: a@b | Subject: s | " + ("paragraph text about budgets and "
                "invoices. " * 20)] * 20
    out["rerankers"] = {}
    for name in ["Xenova/ms-marco-MiniLM-L-6-v2", "Xenova/ms-marco-MiniLM-L-12-v2",
                 "BAAI/bge-reranker-base", "jinaai/jina-reranker-v1-turbo-en"]:
        t0 = time.time()
        ce = TextCrossEncoder(name, threads=32)
        load = time.time() - t0
        t0 = time.perf_counter()
        list(ce.rerank("invoices and budgets", passages))
        dt = time.perf_counter() - t0
        out["rerankers"][name] = {"load_s": round(load, 1),
                                  "ms_20_passages": round(dt * 1000, 1),
                                  "ms_per_pair": round(dt * 1000 / 20, 1)}
        del ce
    # v2-m3 control via TEI (GPU)
    out["rerankers"]["v2m3-TEI-GPU"] = {
        "ms_20_passages": http_ms(R.TEI_RERANK + "/rerank",
                                  {"query": "invoices and budgets", "texts": passages})}
    # 4B embed latency (GPU control), one batch of 8
    out["tei_4b_ms_batch8"] = http_ms(R.TEI_EMBED + "/embed",
                                      {"inputs": ["text"] * 8}, n=3)
    out["tei_06b_ms_batch8"] = http_ms(R.TEI_EMBED_06B + "/embed",
                                       {"inputs": ["text"] * 8}, n=3)

    out["peak_rss_mb"] = round(R.mem_rss_mb(), 1)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
