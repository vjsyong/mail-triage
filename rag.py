"""RAG for Mail Triage: hybrid semantic search over the mail archive.

Pipeline (per the 2026 best-practice research; see README):
  TEI embeddings (Qwen3-Embedding)  ->  sqlite-vec KNN  +  FTS5 BM25
  ->  reciprocal rank fusion (k=60)  ->  optional cross-encoder rerank
Chunking is sentence-packed with a From/To/Date/Subject header on every chunk
(contextual retrieval), ~400 tokens per chunk, no overlap.

The index is built by a resumable pass over the mail folders (Indexer below);
new mail is folded in incrementally. Embeddings never leave the host.
"""
import json
import math
import os
import re
import threading
import time

import requests

import config
import engine
import store

def embed_config():
    """Effective embedding endpoint: UI settings win, blank fields fall back to env."""
    def pick(name, env):
        v = store.get_setting(name)
        return v if v not in (None, "") else env

    protocol = (store.get_setting("embed_protocol") or "tei").lower()
    if protocol == "local":
        # local models are named from the Settings value only; the env default (4B)
        # does not exist as a FastEmbed/ONNX build
        model = store.get_setting("embed_model") or ""
    else:
        model = pick("embed_model", config.EMBED_MODEL) or ""
    return {
        "base": (pick("embed_base_url", config.EMBED_BASE_URL) or "").rstrip("/"),
        "model": model,
        "key": pick("embed_api_key", "") or "",
        "protocol": protocol,
        "timeout": int(store.get_setting("embed_timeout") or config.EMBED_TIMEOUT),
        "query_prefix": store.get_setting("embed_query_prefix") or "",
    }


def rerank_config():
    """Effective reranker endpoint: UI settings win, blank fields fall back to env."""
    def pick(name, env):
        v = store.get_setting(name)
        return v if v not in (None, "") else env

    protocol = (store.get_setting("rerank_protocol") or "tei").lower()
    if protocol == "local":
        model = store.get_setting("rerank_model") or ""
    else:
        model = pick("rerank_model", config.RERANK_MODEL) or ""
    return {
        "base": (pick("rerank_base_url", config.RERANK_BASE_URL) or "").rstrip("/"),
        "model": model,
        "key": pick("rerank_api_key", "") or "",
        "protocol": protocol,
        "timeout": int(store.get_setting("rerank_timeout") or config.RERANK_TIMEOUT),
    }


def excluded_substrings():
    """Case-insensitive folder substrings not indexed by default (Settings -> RAG)."""
    v = store.get_setting("rag_exclude_folders")
    if v is None:
        v = []
    return [str(x).strip().lower() for x in v if str(x).strip()]


def default_folders(all_folders):
    excl = excluded_substrings()
    keep = []
    for f in all_folders:
        fl = f.lower()
        if any(x in fl for x in excl):
            continue
        keep.append(f)
    return keep


CHUNK_CHARS = 1600     # ~400 tokens target per chunk
CHUNK_MAX = 2400       # hard cap; longer single sentences are wrapped
CHUNK_MIN = 300        # merge tiny tails into the previous chunk
EMBED_BATCH = 24       # chunks per embedding request

K_VEC = 100            # KNN candidates
K_FTS = 100            # BM25 candidates
RRF_K = 60             # reciprocal rank fusion constant (original paper value)
CANDIDATES = 50        # fused candidates considered for rerank
RERANK_TOP = 30        # what we send to the cross-encoder


# ---------------------------------------------------------------- embed/rerank clients

LOCAL_EMBED_DEFAULT = "Qwen/Qwen3-Embedding-0.6B"
LOCAL_RERANK_DEFAULT = "jinaai/jina-reranker-v1-turbo-en"

_local_models = {}
_local_lock = threading.Lock()


def _local_cache_dir():
    d = (store.get_setting("local_models_dir")
         or os.environ.get("FASTEMBED_CACHE_PATH")
         or os.path.join(getattr(config, "DATA_DIR", "/data") or "/data", "ragmodels"))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    os.environ.setdefault("FASTEMBED_CACHE_PATH", d)
    return d


def _fix_onnx_symlinks(cache_dir):
    """ONNX Runtime >=1.17 refuses external-data weights whose resolved path escapes
    the model directory; HF snapshots symlink into ../../blobs. Dereference those
    symlinks (idempotent; a few files, copies only when needed)."""
    fixed = 0
    try:
        for root, dirs, files in os.walk(cache_dir):
            if os.sep + "blobs" in root or root.endswith("blobs"):
                continue
            for f in files + dirs:
                fp = os.path.join(root, f)
                if os.path.islink(fp):
                    target = os.path.realpath(fp)
                    if "blobs" in target:
                        try:
                            data = open(target, "rb").read()
                            os.unlink(fp)
                            with open(fp, "wb") as out:
                                out.write(data)
                            fixed += 1
                        except OSError:
                            pass
        if fixed:
            store.log_event("info", "local models: dereferenced %d symlinked file(s) "
                            "for ONNX Runtime" % fixed)
    except Exception:
        pass
    return fixed


def _local_embed(texts, model):
    name = (model or "").strip() or LOCAL_EMBED_DEFAULT
    key = "emb:" + name
    with _local_lock:
        inst = _local_models.get(key)
        if inst is None:
            cache = _local_cache_dir()
            from fastembed import TextEmbedding
            threads = int(store.get_setting("local_embed_threads") or 8)
            try:
                inst = TextEmbedding(name, threads=threads)
            except Exception as exc:
                if "External data path" not in str(exc):
                    raise
                _fix_onnx_symlinks(cache)
                inst = TextEmbedding(name, threads=threads)
            _local_models[key] = inst
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend([list(map(float, v)) for v in inst.embed(list(texts[i:i + EMBED_BATCH]))])
    return out


def _local_rerank(query, texts, model):
    name = (model or "").strip() or LOCAL_RERANK_DEFAULT
    key = "ce:" + name
    with _local_lock:
        inst = _local_models.get(key)
        if inst is None:
            cache = _local_cache_dir()
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            threads = int(store.get_setting("local_embed_threads") or 8)
            try:
                inst = TextCrossEncoder(name, threads=threads)
            except Exception as exc:
                if "External data path" not in str(exc):
                    raise
                _fix_onnx_symlinks(cache)
                inst = TextCrossEncoder(name, threads=threads)
            _local_models[key] = inst
    return [float(s) for s in inst.rerank(query, list(texts))]


def embed(texts, kind="document"):
    """Embed texts via the configured backend. kind='query' prepends the configured
    query instruction prefix (documents stay raw). Protocols: 'tei' (POST /embed),
    'openai' (POST /embeddings) or 'local' (FastEmbed/ONNX on CPU, in-process)."""
    cfg = embed_config()
    payload_texts = list(texts)
    if kind == "query" and cfg["query_prefix"]:
        payload_texts = [cfg["query_prefix"] + t for t in payload_texts]
    if cfg["protocol"] == "local":
        return _local_embed(payload_texts, cfg["model"])
    if not cfg["base"]:
        raise RuntimeError("embedding endpoint not configured - set it in Settings -> RAG "
                           "(or EMBED_BASE_URL in .env)")
    headers = {"Authorization": "Bearer " + cfg["key"]} if cfg["key"] else {}
    out = []
    for i in range(0, len(payload_texts), EMBED_BATCH):
        batch = payload_texts[i:i + EMBED_BATCH]
        if cfg["protocol"] == "openai":
            r = requests.post(cfg["base"] + "/embeddings",
                              json={"model": cfg["model"], "input": batch},
                              headers=headers, timeout=cfg["timeout"])
            r.raise_for_status()
            data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
            out.extend(d.get("embedding") or [] for d in data)
        else:
            r = requests.post(cfg["base"] + "/embed", json={"inputs": batch},
                              headers=headers, timeout=cfg["timeout"])
            r.raise_for_status()
            out.extend(r.json())
    if len(out) != len(payload_texts):
        raise RuntimeError("embedding server returned %d vectors for %d inputs"
                           % (len(out), len(payload_texts)))
    return out


def embed_one(text, kind="query"):
    return embed([text], kind=kind)[0]


def rerank(query, texts, top_n=None):
    """Cross-encoder rerank via the configured endpoint. Returns [{index, score}]
    sorted desc or None. Protocols: 'tei' ({"query","texts"} -> [{index,score}]) or
    'cohere' ({"query","documents","top_n"} -> {"results":[{index,relevance_score}]},
    which covers Cohere/Jina/Infinity-style rerank servers)."""
    cfg = rerank_config()
    if not texts:
        return None
    if cfg["protocol"] == "local":
        scores = _local_rerank(query, list(texts), cfg["model"])
        data = [{"index": i, "score": s} for i, s in enumerate(scores)]
        data.sort(key=lambda d: d.get("score") or 0.0, reverse=True)
        if top_n:
            data = data[:top_n]
        return data
    if not cfg["base"]:
        return None
    headers = {"Authorization": "Bearer " + cfg["key"]} if cfg["key"] else {}
    if cfg["protocol"] == "cohere":
        r = requests.post(cfg["base"] + "/rerank",
                          json={"query": query, "documents": list(texts),
                                "top_n": int(top_n or len(texts))},
                          headers=headers, timeout=cfg["timeout"])
        r.raise_for_status()
        data = [{"index": item.get("index"),
                 "score": item.get("relevance_score", item.get("score", 0.0))}
                for item in (r.json().get("results") or [])]
        data.sort(key=lambda d: d.get("score") or 0.0, reverse=True)
    else:
        r = requests.post(cfg["base"] + "/rerank",
                          json={"query": query, "texts": list(texts)},
                          headers=headers, timeout=cfg["timeout"])
        r.raise_for_status()
        data = r.json()
    if top_n:
        data = data[:top_n]
    return data


# ---------------------------------------------------------------- chunking

_SENT_SPLIT = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])\s+|\n{2,}")


def chunk_text(header, body):
    """Sentence-packed chunks, each prefixed with `header` (contextual retrieval)."""
    body = (body or "").strip()
    if not body:
        return []
    header = (header or "").strip()
    if len(body) + len(header) + 1 <= CHUNK_MAX:
        return [(header + "\n" + body).strip()]

    sentences = [s.strip() for s in _SENT_SPLIT.split(body) if s and s.strip()]
    if not sentences:
        sentences = [body]
    chunks, cur = [], ""
    for s in sentences:
        cand = (cur + " " + s).strip() if cur else s
        if len(cand) > CHUNK_CHARS and cur:
            chunks.append(cur)
            cur = s
        else:
            cur = cand
        while len(cur) > CHUNK_MAX:  # runaway single sentence / huge line
            chunks.append(cur[:CHUNK_MAX].strip())
            cur = cur[CHUNK_MAX:].lstrip()
    if cur:
        if chunks and len(cur) < CHUNK_MIN:
            chunks[-1] = chunks[-1] + " " + cur
        else:
            chunks.append(cur)
    return [(header + "\n" + c).strip() for c in chunks if c.strip()]


def _header_for(row, folder):
    return ("From: %s | To: %s | Date: %s | Folder: %s | Subject: %s"
            % (row.get("from_addr", ""), row.get("to_addr", ""), row.get("date", ""),
               folder, row.get("subject", "")))


def _excerpt(chunk_text_value):
    txt = chunk_text_value or ""
    if txt.startswith("From: ") and "\n" in txt:
        txt = txt.split("\n", 1)[1]
    return re.sub(r"\s+", " ", txt).strip()[:320]


# ---------------------------------------------------------------- index build

def _ensure_dim(dim):
    known = store.meta_get("embed_dim")
    cfg = embed_config()
    if known is None:
        store.ensure_vec_table(dim)
        store.meta_set("embed_dim", int(dim))
        store.meta_set("embed_model", cfg["model"])
        return
    if int(known) != int(dim):
        raise RuntimeError("embedding dimension changed (%s -> %s); rebuild the index "
                           "(dashboard button or python app.py --reindex)"
                           % (known, dim))
    model = store.meta_get("embed_model")
    if model and model != cfg["model"]:
        raise RuntimeError("embedding model changed (%s -> %s); rebuild the index "
                           "(dashboard button or python app.py --reindex) or set the "
                           "model back in Settings -> RAG" % (model, cfg["model"]))


def _index_one(mc, folder, uid, uv):
    """Ensure one message is indexed (message row + chunks + vectors). Idempotent."""
    meta = mc.fetch_meta(uid)
    row = store.get_message_by_uid(folder, uid, uv)
    if row is None and meta.get("msgid"):
        # mail moved between folders: reuse the canonical row instead of duplicating
        row = store.find_message_by_msgid(meta["msgid"])
    if row is None:
        store.insert_message(folder, uid, uv, meta)
        row = store.get_message_by_uid(folder, uid, uv)
    if row is None:
        raise RuntimeError("could not record message %s uid %s" % (folder, uid))
    if store.message_chunk_count(row["id"]) > 0:
        _repair_vectors(row["id"])
        return 0
    full = mc.fetch_full(uid)
    text = full.get("text") or row.get("snippet") or ""
    chunks = chunk_text(_header_for(row, folder), text)
    if not chunks:
        return 0
    # embed BEFORE inserting anything: a failed embed must leave no partial state
    vecs = embed(chunks, "document")
    _ensure_dim(len(vecs[0]))
    ids = store.add_chunks([{"message_id": row["id"], "folder": folder, "uid": uid,
                             "seq": i, "text": c} for i, c in enumerate(chunks)])
    store.add_vectors(list(zip(ids, vecs)))
    return len(ids)


def _repair_vectors(message_id):
    """Backfill vector rows for chunks that exist without vectors (partial runs)."""
    with store.db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, text FROM chunks WHERE message_id=? ORDER BY seq", (message_id,))]
    if not rows:
        return 0
    try:
        with store.db(vec=True) as conn:
            q = ("SELECT rowid FROM vec_chunks WHERE rowid IN (%s)"
                 % ",".join("?" * len(rows)))
            have = {r[0] for r in conn.execute(q, [r["id"] for r in rows])}
    except Exception:
        return 0
    missing = [r for r in rows if r["id"] not in have]
    if not missing:
        return 0
    vecs = embed([r["text"] for r in missing], "document")
    _ensure_dim(len(vecs[0]))
    store.add_vectors([(r["id"], v) for r, v in zip(missing, vecs)])
    store.log_event("info", "indexer: repaired %d missing vector(s) for message %s"
                    % (len(missing), message_id))
    return len(missing)


def index_pass(limit=40):
    """One indexing pass on the ACTIVE backend (lite | legacy)."""
    if (store.get_setting("rag_backend") or "lite").lower() == "lite":
        import rag_lite
        return rag_lite.index_pass(limit=limit)
    return index_pass_legacy(limit=limit)


def index_pass_legacy(limit=40):
    """Index up to `limit` not-yet-chunked messages, resuming folder by folder."""
    if not store.get_setting("index_enabled", True):
        return {"processed": 0, "folders_done": 0, "folders_total": 0, "remaining": 0,
                "summary": "indexing disabled"}
    mc = engine.MailClient().connect()
    try:
        all_folders = mc.folders()
        wanted = store.get_setting("index_folders") or default_folders(sorted(all_folders))
        wanted = [f for f in wanted if f in all_folders]
        processed = 0
        consecutive_errors = 0
        for folder in wanted:
            if processed >= limit:
                break
            st = store.index_state_get(folder) or {}
            uv = mc.select(folder)
            if st.get("uidvalidity") and int(st["uidvalidity"]) != int(uv):
                store.delete_chunks_folder(folder)
                st = {}
                store.log_event("info", "indexer: %s UIDVALIDITY changed - re-indexing" % folder)
            last = int(st.get("last_uid") or 0)
            if last:
                uids = [u for u in mc.search("UID", "%d:*" % (last + 1)) if u > last]
            else:
                uids = mc.search("ALL")
            if not uids:
                store.index_state_put(folder, uv, last, status="done")
                continue
            exhausted = True
            for uid in uids:
                if processed >= limit:
                    exhausted = False
                    break
                try:
                    _index_one(mc, folder, uid, uv)
                    processed += 1
                    consecutive_errors = 0
                except Exception as exc:
                    consecutive_errors += 1
                    store.log_event("error", "indexer: %s uid %s failed: %r" % (folder, uid, exc))
                    if consecutive_errors >= 5:
                        raise RuntimeError("5 consecutive indexing failures - pausing this pass")
                    continue
                store.index_state_touch(folder, uv, uid)
            if exhausted:
                store.index_state_put(folder, uv, uids[-1], status="done")
        done = sum(1 for r in store.index_overview(wanted) if r.get("status") == "done")
        summary = ("indexed %d message(s) this pass - %d/%d folders complete"
                   % (processed, done, len(wanted)))
        if processed:
            store.log_event("debug", "indexer: " + summary)
        return {"processed": processed, "folders_done": done, "folders_total": len(wanted),
                "remaining": max(0, len(wanted) - done), "summary": summary}
    finally:
        mc.close()


def rebuild():
    """Clear the ACTIVE backend's index. Mail itself is untouched."""
    if (store.get_setting("rag_backend") or "lite").lower() == "lite":
        store.clear_rag2()
        store.log_event("info", "index rebuild: cleared the lite index (chunks2, vectors, state)")
        return
    store.clear_rag()
    store.log_event("info", "index rebuild: cleared chunks, vectors and folder state")


def index_stats():
    if (store.get_setting("rag_backend") or "lite").lower() == "lite":
        import rag_lite
        return rag_lite.index_stats()
    with store.db() as conn:
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        msgs = conn.execute("SELECT COUNT(DISTINCT message_id) FROM chunks").fetchone()[0]
    return {"chunks": chunks, "messages": msgs}


def index_pass_active(limit=40):
    """Alias for index_pass (kept for callers that read clearer this way)."""
    return index_pass(limit=limit)


class Indexer(threading.Thread):
    """Background indexer thread: continuous backfill when triggered, incremental
    refresh on a timer otherwise. State dict mirrors Worker's for the UI."""

    def __init__(self):
        super().__init__(daemon=True, name="triage-indexer")
        self.force = threading.Event()
        self.rebuild_next = False
        self.stop_flag = threading.Event()
        self.state = {"running": False, "last_ok": 0, "last_error": None,
                      "progress": "", "remaining": None, "started": 0}

    def trigger(self, rebuild=False):
        if rebuild:
            self.rebuild_next = True
        self.force.set()

    def run(self):
        store.init_db()
        self.stop_flag.wait(6)  # let the UI come up
        while not self.stop_flag.is_set():
            if self.force.is_set():
                self.force.clear()
                self._run(continuous=True)
                continue
            # idle: incremental refresh on the configured cadence (default 10 min)
            try:
                idle_min = max(1, int(store.get_setting("index_refresh_minutes") or 10))
            except (TypeError, ValueError):
                idle_min = 10
            self.stop_flag.wait(idle_min * 60)
            if self.force.is_set() or self.stop_flag.is_set():
                continue
            if store.get_setting("index_enabled", True):
                self._run(continuous=False)

    def _run(self, continuous=True):
        self.state["running"] = True
        self.state["started"] = int(time.time())
        try:
            cfg = embed_config()
            if cfg["protocol"] != "local" and not cfg["base"]:
                self.state["progress"] = "embedding endpoint not configured (Settings -> RAG)"
                return
            if self.rebuild_next:
                self.rebuild_next = False
                rebuild()
            calls = 0
            while not self.stop_flag.is_set():
                if not store.get_setting("index_enabled", True):
                    self.state["progress"] = "indexing disabled in settings"
                    break
                res = index_pass_active(limit=40 if continuous else 60)
                self.state["progress"] = res["summary"]
                self.state["remaining"] = res["remaining"]
                calls += 1
                if not continuous or res["remaining"] == 0 or res["processed"] == 0:
                    break
                if calls > 400:  # safety valve
                    break
            self.state["last_ok"] = int(time.time())
            self.state["last_error"] = None
        except Exception as exc:
            self.state["last_error"] = repr(exc)
            store.log_event("error", "indexer failed: %r" % exc)
        finally:
            self.state["running"] = False


# ---------------------------------------------------------------- search

def fts_query(q):
    terms = re.findall(r"[\w@.\-+#]+", q or "", re.UNICODE)
    terms = [t for t in terms if len(t) >= 2]
    if not terms:
        return '"%s"' % (q or "").replace('"', " ")
    return " OR ".join('"%s"' % t.replace('"', '""') for t in terms)


def search(query, k=8, folder=None, since=None, rerank_on=None, mode="hybrid"):
    """Hybrid semantic search -> list of matching messages.

    Dispatches on the `rag_backend` setting: 'lite' (default, rag_lite module) or
    'legacy' (the original pipeline below). Same result contract either way.
    mode: 'hybrid' (default), 'vector', 'fts' (diagnostics/evals).
    Returns {"ok": True, "results": [{message_id, folder, uid, from_addr, to_addr,
    subject, date, excerpt, score}], "meta": {...}} or {"ok": False, "error": ...}.
    """
    if (store.get_setting("rag_backend") or "lite").lower() == "lite":
        import rag_lite
        return rag_lite.search(query, k=k, folder=folder, since=since,
                               rerank_on=rerank_on, mode=mode)
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "empty query"}
    if store.chunk_count() == 0:
        return {"ok": False, "error": "the search index is empty - run the indexer first"}
    t0 = time.time()
    fused = {}
    notes = []
    if mode in ("hybrid", "vector"):
        try:
            qv = embed_one(q, kind="query")
            _ensure_dim(len(qv))
            for rank, (cid, _dist) in enumerate(store.vec_search(qv, K_VEC)):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank)
        except Exception as exc:
            if mode == "vector":
                return {"ok": False, "error": "vector search failed: %r" % exc}
            notes.append("vector channel unavailable (%r)" % exc)
    if mode in ("hybrid", "fts"):
        try:
            for rank, (cid, _score) in enumerate(store.fts_search(fts_query(q), K_FTS)):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank)
        except Exception as exc:
            notes.append("keyword channel failed (%r)" % exc)
    if not fused:
        return {"ok": True, "results": [], "meta": {"query": q, "note": "; ".join(notes)}}

    top_ids = sorted(fused, key=fused.get, reverse=True)[:CANDIDATES]
    chunks = store.chunks_by_ids(top_ids)
    msgs = store.messages_by_ids([c["message_id"] for c in chunks])
    since_epoch = None
    if since:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", (since or "").strip())
        if m:
            since_epoch = time.mktime((int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                       0, 0, 0, 0, 0, -1))
    rows = []
    for c in chunks:
        msg = msgs.get(c["message_id"])
        if msg is None:
            continue
        if folder and c["folder"] != folder:
            continue
        if since_epoch is not None and (msg.get("processed_at") or 0) < since_epoch:
            # use the mail's own date when parseable, else the scan time
            ts = None
            try:
                import email.utils
                dt = email.utils.parsedate_to_datetime(msg.get("date") or "")
                ts = dt.timestamp()
            except Exception:
                ts = None
            if ts is None or ts < since_epoch:
                continue
        rows.append({"chunk_id": c["id"], "message_id": msg["id"], "folder": c["folder"],
                     "uid": c["uid"], "from_addr": msg.get("from_addr"),
                     "to_addr": msg.get("to_addr"), "subject": msg.get("subject"),
                     "date": msg.get("date"), "excerpt": _excerpt(c["text"]),
                     "score": fused.get(c["id"], 0.0)})
    rows.sort(key=lambda r: r["score"], reverse=True)

    want_rerank = bool(store.get_setting("rerank_enabled", True)) \
        if rerank_on is None else rerank_on
    if want_rerank and len(rows) > 1:
        try:
            head = rows[:RERANK_TOP]
            chunk_texts = {c["id"]: c["text"] for c in chunks}
            rr = rerank(q, [(chunk_texts.get(r["chunk_id"]) or r["excerpt"])[:2400]
                            for r in head])
            if rr:
                by_index = {item["index"]: item.get("score", 0.0) for item in rr}
                for i, r in enumerate(head):
                    r["rerank_score"] = by_index.get(i)
                head.sort(key=lambda r: r.get("rerank_score")
                          if r.get("rerank_score") is not None else float("-inf"),
                          reverse=True)
                rows = head + rows[RERANK_TOP:]
        except Exception as exc:
            notes.append("rerank unavailable (%r)" % exc)

    # one result per message, keep the best-scored chunk
    seen, results = set(), []
    for r in rows:
        if r["message_id"] in seen:
            continue
        seen.add(r["message_id"])
        results.append(r)
        if len(results) >= max(1, int(k)):
            break
    return {"ok": True, "results": results,
            "meta": {"query": q, "ms": int((time.time() - t0) * 1000),
                     "channels": mode, "note": "; ".join(notes)}}
