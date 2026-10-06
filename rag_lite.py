"""RAG-Lite backend (v2): CPU-friendly hybrid retrieval for the mail archive.

Selected with the `rag_backend` setting ("lite" default, "legacy" = the original
pipeline in rag.py). Design + evidence: docs/rag-lite-design.md / rag-lite-report.md
in the rag-lite-eval branch; this module is the production port of that prototype.

Pipeline: query understanding (sender/date/exact hints) -> metadata prefilter on
`messages` -> FTS5 BM25 (fielded: subject 4.0 / sender 2.0 / body 1.0) + sqlite-vec
KNN -> RRF (k=60) -> top-20 -> cross-encoder -> top k. Chunks are "clean": quoted
history is stripped before indexing (falling back to the full text for stub bodies).

Tables: chunks2 / chunks2_fts / vec_chunks2 / index2_state (see store.py). The legacy
tables stay untouched for instant rollback (flip `rag_backend`).
"""
import hashlib
import json
import re
import time

import config
import store

RRF_K = 60
K_FTS = 50
K_VEC_FETCH = 300
K_VEC = 50
TOP_FUSE = 20
FTS_WEIGHTS = (4.0, 2.0, 1.0)

_STOP = {"the", "a", "an", "my", "me", "i", "we", "you", "mail", "email", "emails",
         "message", "messages", "about", "what", "did", "say", "said", "from", "by",
         "to", "and", "or", "in", "on", "last", "this", "week", "month", "year",
         "was", "were", "is", "are", "it", "that", "which", "when", "who", "find",
         "got", "send", "sent", "regarding", "re", "fwd"}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


# ---------------------------------------------------------------- text handling

_QUOTE_MARKERS = [
    re.compile(r"\nOn .{4,160}wrote:\s*\n", re.I),
    re.compile(r"\n-{2,}\s*Original Message\s*-{2,}", re.I),
    re.compile(r"\n-{2,}\s*Original message\s*-{2,}", re.I),
    re.compile(r"\nBegin forwarded message:", re.I),
    re.compile(r"\nFrom: .{2,200}\n(?:Sent|Date): .{2,120}\n", re.I),   # Outlook quote header
    re.compile(r"\n发件人[:：]", re.I),
    re.compile(r"\n在.{2,80}写道[:：]", re.I),
    re.compile(r"\n_{10,}\n"),                                          # common separator
]


def strip_quoted(text):
    """Split an email body into (new_content, quoted_tail). Conservative: only cuts
    on explicit quote markers (On ... wrote:, Outlook From:/Sent: blocks, forwarded
    banners, CN equivalents, long separators) or a >=3-line trailing '>' block, and
    only when a meaningful 'new' part remains."""
    text = text or ""
    cut, method = None, ""
    for i, pat in enumerate(_QUOTE_MARKERS):
        m = pat.search(text)
        if m and m.start() > 0:
            cut, method = m.start(), "marker:%d" % i
            break
    if cut is None:
        lines = text.split("\n")
        for idx in range(1, len(lines)):
            if lines[idx].startswith(">"):
                tail = lines[idx:]
                if sum(1 for l in tail if l.startswith(">")) >= 3:
                    cut, method = len("\n".join(lines[:idx])), "quoted-block"
                break
    if cut is None or cut < 40:
        return text.strip(), "", method or "none"
    new, quoted = text[:cut].strip(), text[cut:].strip()
    if len(new) < 40:
        return text.strip(), "", "none"
    return new, quoted, method


def clean_body(full_text):
    """Body for indexing: quote-stripped when that leaves real content."""
    new, quoted, method = strip_quoted(full_text)
    if len(new) < 120:          # short mail / forwarder stubs: keep everything
        return full_text.strip(), "full"
    return new, "new"


def norm_subject(s):
    s = (s or "").strip().lower()
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"^(re|fwd?|aw|sv|回复|答复)\s*(\[\d+\])?\s*[:\]\uff1a]\s*", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def thread_key(subject, from_addr, to_addr):
    f = (from_addr or "").lower()
    me = (config.IMAP_USER or "").lower()
    me_local, _, me_dom = me.partition("@")
    from_is_me = bool(me) and ((me_local and me_local in f) or (me_dom and me_dom in f))
    counterpart = (to_addr or "").lower() if from_is_me else f
    raw = "t:%s|%s" % (norm_subject(subject), counterpart)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:14]


# ---------------------------------------------------------------- query understanding

def parse_query(q):
    """Metadata extraction: sender hints, date windows, exact tokens."""
    ql = (q or "").lower()
    out = {"senders": [], "date_from": None, "date_to": None, "exacts": [], "date_label": ""}
    for m in re.finditer(r"(?:from|by)\s+([a-z][\w.'’-]{2,})(?:\s+([a-z][\w.'’-]{2,}))?", ql):
        for tok in m.groups():
            if tok and tok not in _STOP and len(tok) >= 3:
                out["senders"].append(tok)
    for m in re.finditer(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", ql):
        out["senders"].append(m.group(0))
    now = time.localtime()
    y, mo = now.tm_year, now.tm_mon

    def epoch(yy, mm, dd, h=0):
        return int(time.mktime((yy, mm, dd, h, 0, 0, 0, 0, -1)))

    if "yesterday" in ql:
        d0 = epoch(y, mo, now.tm_mday - 1)
        out.update(date_from=d0, date_to=d0 + 86399, date_label="yesterday")
    elif "last week" in ql:
        end = epoch(y, mo, now.tm_mday) - 86400
        out.update(date_from=end - 6 * 86400, date_to=end + 86399, date_label="last week")
    elif "last month" in ql:
        m0, y0 = (mo - 1, y) if mo > 1 else (12, y - 1)
        m1 = 1 if m0 == 12 else m0 + 1
        y1 = y0 + 1 if m0 == 12 else y0
        out.update(date_from=epoch(y0, m0, 1), date_to=epoch(y1, m1, 1) - 1,
                   date_label="last month")
    elif "this month" in ql:
        out.update(date_from=epoch(y, mo, 1), date_to=epoch(y, mo, 28) + 4 * 86400,
                   date_label="this month")
    elif "last year" in ql:
        out.update(date_from=epoch(y - 1, 1, 1), date_to=epoch(y, 1, 1) - 1,
                   date_label="last year")
    else:
        m = re.search(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\s+(\d{4})\b", ql)
        if m:
            yy, mm = int(m.group(2)), _MONTHS[m.group(1)]
            mm2, yy2 = (mm + 1, yy) if mm < 12 else (1, yy + 1)
            out.update(date_from=epoch(yy, mm, 1), date_to=epoch(yy2, mm2, 1) - 1,
                       date_label="%s %d" % (m.group(1), yy))
        else:
            m = re.search(r"\b(20\d\d)-\d\d-\d\d\b", ql)
            if m:
                y0, m0, d0 = map(int, m.group(0).split("-"))
                out.update(date_from=epoch(y0, m0, d0), date_to=epoch(y0, m0, d0) + 86399,
                           date_label=m.group(0))
    for m in re.finditer(r'"([^"]{3,80})"', q):
        out["exacts"].append(m.group(1))
    for m in re.finditer(r"\b[A-Z]{2,}[-# ]?\d{2,}\b|\b\d{5,}\b", q):
        out["exacts"].append(m.group(0).strip())
    return out


def fts_query(q, parse):
    """Quoted OR-join of terms (+ exact tokens verbatim), like rag.fts_query."""
    terms = re.findall(r"[\w@.\-+#]+", q or "", re.UNICODE)
    terms = [t for t in terms if len(t) >= 2]
    for s in (parse.get("exacts") or []):
        if s and s.lower() not in [t.lower() for t in terms]:
            terms.append(s)
    if not terms:
        return '"%s"' % (q or "").replace('"', " ")
    return " OR ".join('"%s"' % t.replace('"', '""') for t in terms)


def candidate_ids(parse, folder=None, since_epoch=None):
    """Message-id prefilter from sender/date hints (+ folder/since params).
    Returns a set of message ids, or None when there is nothing to prefilter."""
    where, args = [], []
    if parse.get("senders"):
        ors = []
        for s in parse["senders"]:
            ors.append("lower(from_addr) LIKE ?")
            args.append("%" + s + "%")
        where.append("(" + " OR ".join(ors) + ")")
    if parse.get("date_from") is not None:
        where.append("date_ts BETWEEN ? AND ?")
        args += [parse["date_from"], parse["date_to"]]
    if since_epoch is not None:
        where.append("COALESCE(NULLIF(date_ts,0), processed_at, 0) >= ?")
        args.append(since_epoch)
    if folder:
        where.append("folder = ?")
        args.append(folder)
    if not where:
        return None
    sql = "SELECT id FROM messages WHERE " + " AND ".join(where)
    with store.db() as c:
        return {r[0] for r in c.execute(sql, args)}


# ---------------------------------------------------------------- indexing

def ensure_dim(dim):
    """Create/guard the lite vector table (dimension + model recorded in meta)."""
    import rag
    known = store.meta_get("lite_embed_dim")
    cfg = rag.embed_config()
    model = cfg["model"] or ""
    if known is None:
        store.ensure_vec_table2(dim)
        store.meta_set("lite_embed_dim", int(dim))
        store.meta_set("lite_embed_model", model)
        return
    if int(known) != int(dim):
        raise RuntimeError("lite embedding dimension changed (%s -> %s); rebuild the lite "
                           "index (Settings -> rebuild, or python app.py --reindex)"
                           % (known, dim))
    old = store.meta_get("lite_embed_model")
    if old and model and old != model:
        raise RuntimeError("lite embedding model changed (%s -> %s); rebuild the lite index "
                           "(Settings -> rebuild, or python app.py --reindex) or restore the "
                           "model setting" % (old, model))


def _repair_vectors2(message_id):
    """Backfill vec rows for chunks2 rows missing vectors (partial runs)."""
    import rag
    with store.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, text FROM chunks2 WHERE message_id=? ORDER BY seq", (message_id,))]
    if not rows:
        return 0
    try:
        with store.db(vec=True) as c:
            q = "SELECT rowid FROM vec_chunks2 WHERE rowid IN (%s)" % ",".join("?" * len(rows))
            have = {r[0] for r in c.execute(q, [r["id"] for r in rows])}
    except Exception:
        return 0
    missing = [r for r in rows if r["id"] not in have]
    if not missing:
        return 0
    vecs = rag.embed([r["text"] for r in missing], "document")
    ensure_dim(len(vecs[0]))
    store.add_vectors2([(r["id"], v) for r, v in zip(missing, vecs)])
    store.log_event("info", "indexer(lite): repaired %d missing vector(s) for message %s"
                    % (len(missing), message_id))
    return len(missing)


def _index_text(row, folder, raw_text, uid=None):
    """Chunk + embed + store one message's text. Shared by the IMAP path
    (index_one, used by tests/demo) and the cache-driven index pass."""
    import rag
    text = raw_text or row.get("snippet") or ""
    body, node = clean_body(text)
    chunks = rag.chunk_text(rag._header_for(row, folder), body)
    if not chunks:
        return 0
    vecs = rag.embed(chunks, "document")
    ensure_dim(len(vecs[0]))
    ids = store.add_chunks2([{"message_id": row["id"], "folder": folder,
                              "uid": uid or row.get("uid") or 0,
                              "seq": i, "node": node, "text": c,
                              "subject": row.get("subject") or "",
                              "sender": row.get("from_addr") or ""}
                             for i, c in enumerate(chunks)])
    store.add_vectors2(list(zip(ids, vecs)))
    return len(ids)


def index_one(mc, folder, uid, uv):
    """Ensure one message is in the lite index (idempotent). Fetches from IMAP;
    kept for tests, the demo, and one-off repairs. The background pipeline uses
    index_pass, which reads the local body cache instead."""
    import rag
    meta = mc.fetch_meta(uid)
    row = store.get_message_by_uid(folder, uid, uv)
    if row is None and meta.get("msgid"):
        row = store.find_message_by_msgid(meta["msgid"])
    if row is None:
        store.insert_message(folder, uid, uv, meta)
        row = store.get_message_by_uid(folder, uid, uv)
    if row:
        store.capture_thread_headers(row['id'], meta)
    if row is None:
        raise RuntimeError("could not record message %s uid %s" % (folder, uid))
    if store.message_chunk2_count(row["id"]) > 0:
        _repair_vectors2(row["id"])
        return 0
    full = mc.fetch_full(uid)
    text = full.get("text") or row.get("snippet") or ""
    if text and not store.get_message_body(row["id"]):
        store.set_message_body(row["id"], text)
    return _index_text(row, folder, text, uid=uid)


# The index stage can queue this many body-fetch jobs per pass; the fetch
# stage drains them at its own rate.
BODY_ENQUEUE_BATCH = 400


def index_pass(limit=40):
    """Index up to `limit` not-yet-chunked messages from the LOCAL BODY CACHE.

    The fetch stage fills the cache (scan + `fetch.body` jobs); this pass never
    opens IMAP. Messages missing a body are handed to the fetch stage and land
    in a later pass, so a rebuild stays fully decoupled and resumable."""
    import rag
    if not store.get_setting("index_enabled", True):
        return {"processed": 0, "folders_done": 0, "folders_total": 0, "remaining": 0,
                "summary": "indexing disabled"}
    folders = store.get_setting("index_folders") or rag.default_folders(store.distinct_folders())
    if not folders:
        return {"processed": 0, "folders_done": 0, "folders_total": 0, "remaining": 0,
                "summary": "no messages to index"}
    scope = store.meta_get("index_scope")
    scope_set = set(scope) if scope else None
    rows = store.messages_missing_chunks(folders, limit)
    processed = awaiting = 0
    consecutive_errors = 0

    def _need_body(row):
        # only the fetch stage can supply bodies, and only for folders it could
        # resolve on the live mailbox; three terminal failures stop the retries
        return (not store.has_message_body(row["id"])
                and (scope_set is None or row["folder"] in scope_set)
                and store.failed_job_count("fetch.body", row["id"]) < 3)

    pending_bodies = [r for r in rows if _need_body(r)]
    if pending_bodies:
        # widen the body batch so the fetch stage can run at full speed
        for row in store.messages_missing_chunks(folders, BODY_ENQUEUE_BATCH):
            if _need_body(row):
                store.enqueue_job("fetch.body", row["id"])
    for row in rows:
        if not store.has_message_body(row["id"]):
            awaiting += 1
            continue
        body = store.get_message_body(row["id"]) or ""
        try:
            _index_text(row, row["folder"], body, uid=row.get("uid"))
            store.mark_indexed(row["id"])  # even 0 chunks (empty body) is handled
            processed += 1
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            store.log_event("error", "indexer(lite): message %s failed: %r" % (row["id"], exc))
            if consecutive_errors >= 5:
                raise RuntimeError("5 consecutive indexing failures - pausing this pass")
    counts = store.missing_chunks_by_folder(folders)
    done = sum(1 for f in folders if counts.get(f, 0) == 0)
    for f in folders:
        store.index2_state_status(f, "done" if counts.get(f, 0) == 0 else "working")
    remaining = sum(counts.values())
    summary = ("indexed %d message(s) this pass - %d/%d folders complete"
               % (processed, done, len(folders)))
    if awaiting:
        summary += " - %d awaiting body fetch" % awaiting
    if processed:
        store.log_event("debug", "indexer(lite): " + summary)
    return {"processed": processed, "folders_done": done, "folders_total": len(folders),
            "remaining": remaining, "summary": summary}


def index_stats():
    with store.db() as c:
        chunks = c.execute("SELECT COUNT(*) FROM chunks2").fetchone()[0]
        msgs = c.execute("SELECT COUNT(DISTINCT message_id) FROM chunks2").fetchone()[0]
    return {"chunks": chunks, "messages": msgs, "backend": "lite"}


# ---------------------------------------------------------------- search

def search(q, k=8, folder=None, since=None, rerank_on=None, mode="hybrid"):
    """Hybrid lite search -> list of matching messages (rag.search-compatible shape)."""
    import rag
    q = (q or "").strip()
    if not q:
        return {"ok": False, "error": "empty query"}
    if store.chunk2_count() == 0:
        return {"ok": False, "error": "the search index is empty - run the indexer first"}
    t0 = time.time()
    parse = parse_query(q)
    since_epoch = None
    if since:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", (since or "").strip())
        if m:
            since_epoch = int(time.mktime((int(m.group(1)), int(m.group(2)),
                                           int(m.group(3)), 0, 0, 0, 0, 0, -1)))
    notes = []
    cand = candidate_ids(parse, folder=folder, since_epoch=since_epoch)
    if cand is not None and len(cand) == 0:
        if folder or since_epoch is not None:
            # explicit folder/date filters are exact: never fall back to the archive
            return {"ok": True, "results": [],
                    "meta": {"query": q, "channels": mode, "backend": "lite",
                             "note": "no messages match the folder/date filter"}}
        notes.append("sender/date hints matched no messages - fell back to the whole archive")
        cand = None

    fused = {}
    if mode in ("hybrid", "vector"):
        try:
            qv = rag.embed_one(q, kind="query")
            ensure_dim(len(qv))
            rows = store.vec_search2(qv, K_VEC_FETCH)
            kept = []
            if cand is None:
                kept = [rid for rid, _ in rows[:K_VEC]]
            else:
                with store.db() as c:
                    for rid, _ in rows:
                        row = c.execute("SELECT message_id FROM chunks2 WHERE id=?",
                                        (rid,)).fetchone()
                        if not row:
                            continue
                        if row[0] in cand:
                            kept.append(rid)
                        if len(kept) >= K_VEC:
                            break
            for rank, rid in enumerate(kept):
                fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
        except Exception as exc:
            if mode == "vector":
                return {"ok": False, "error": "vector search failed: %r" % exc}
            notes.append("vector channel unavailable (%r)" % exc)
    if mode in ("hybrid", "fts"):
        try:
            for rank, (rid, _score) in enumerate(
                    store.fts_search2(fts_query(q, parse), K_FTS,
                                      weights=FTS_WEIGHTS, cand_ids=cand)):
                fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
        except Exception as exc:
            notes.append("keyword channel failed (%r)" % exc)
    if not fused:
        return {"ok": True, "results": [], "meta": {"query": q, "note": "; ".join(notes)}}

    top_ids = sorted(fused, key=fused.get, reverse=True)[:TOP_FUSE]
    chunks = {c["id"]: c for c in store.chunks2_by_ids(top_ids)}
    msgs = store.messages_by_ids([c["message_id"] for c in chunks.values()])
    rows = []
    for rid in top_ids:
        c = chunks.get(rid)
        if not c:
            continue
        msg = msgs.get(c["message_id"])
        if msg is None:
            continue
        rows.append({"chunk_id": c["id"], "message_id": msg["id"], "folder": c["folder"],
                     "uid": c["uid"], "from_addr": msg.get("from_addr"),
                     "to_addr": msg.get("to_addr"), "subject": msg.get("subject"),
                     "date": msg.get("date"), "excerpt": rag._excerpt(c["text"]),
                     "score": fused.get(c["id"], 0.0), "node": c.get("node")})

    want_rerank = bool(store.get_setting("rerank_enabled", True)) \
        if rerank_on is None else rerank_on
    if want_rerank and len(rows) > 1:
        try:
            chunk_texts = {c["id"]: c["text"] for c in chunks.values()}
            # [:1200] chars: jina-turbo's window is ~512 tokens anyway; measured on
            # the live mailbox 97.9 R@1 @1200/800 chars vs 95.8 @2000, and ~350ms
            # faster per query. Do not "fix" this back to full chunk texts.
            rr = rag.rerank(q, [(chunk_texts.get(r["chunk_id"]) or r["excerpt"])[:1200]
                                for r in rows])
            if rr:
                by_index = {item["index"]: item.get("score", 0.0) for item in rr}
                for i, r in enumerate(rows):
                    r["rerank_score"] = by_index.get(i)
                rows.sort(key=lambda r: r.get("rerank_score")
                          if r.get("rerank_score") is not None else float("-inf"),
                          reverse=True)
        except Exception as exc:
            notes.append("rerank unavailable (%r)" % exc)

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
                     "channels": mode, "backend": "lite",
                     "parse": {k2: parse[k2] for k2 in ("senders", "date_label", "exacts")},
                     "note": "; ".join(notes)}}
