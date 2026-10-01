"""RAG-Lite prototype: CPU-friendly hybrid retrieval for the mail archive.

Standalone research prototype for the mail-triage RAG evaluation (branch
rag-lite-eval). It reads the live triage.db READ-ONLY and builds its own
prototype store with *versioned* chunk sets + vector tables so nothing in
production is touched or invalidated.

Pipeline:
    query -> query understanding (sender/date/exact hints)
          -> metadata prefilter (SQL on messages_light)
          -> FTS5 BM25 (field-weighted on the clean set)  --\\
          -> sqlite-vec KNN                                --/ RRF (k=60)
          -> small CPU cross-encoder reranker (optional)
          -> dedupe per message -> top-k -> thread expansion (diagnostics)

Chunk sets:
    raw   = the CURRENT production representation (From/To/Date/Subject header
            + full body including quoted history) - baseline for A/B.
    clean = email-aware representation: quoted history stripped, header kept
            for context; fielded FTS (subject / sender / body with weights).

Models:
    embed 06b  = Qwen/Qwen3-Embedding-0.6B via FastEmbed (ONNX, CPU), 1024-dim
    embed 4b   = Qwen/Qwen3-Embedding-4B via the LIVE TEI on :8041 (GPU control)
    rerank ... = fastembed CPU cross-encoders, or v2m3 via LIVE TEI :8042
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "prototype", "data")
DB_PATH = os.path.join(DATA_DIR, "raglite.db")
MODEL_CACHE = os.path.join(ROOT, "prototype", "model_cache")
LIVE_DB = os.environ.get("LIVE_DB", "/home/xrim/mail-triage/data/triage.db")
TEI_EMBED = os.environ.get("TEI_EMBED", "http://127.0.0.1:8041")
TEI_RERANK = os.environ.get("TEI_RERANK", "http://127.0.0.1:8042")
TEI_EMBED_06B = os.environ.get("TEI_EMBED_06B", "http://127.0.0.1:8043")  # scratch TEI for building 0.6B vectors fast

QUERY_PREFIX = ("Instruct: Given a search query, retrieve relevant email messages "
                "from the user's mailbox\nQuery: ")

EXCLUDE_FOLDERS = ["junk", "deleted", "trash", "sync issues", "calendar", "contacts",
                   "journal", "conversation history", "outbox", "rss feeds"]

# mirror rag.py chunking so the raw set is a faithful baseline
CHUNK_CHARS = 1600
CHUNK_MAX = 2400
CHUNK_MIN = 300

RRF_K = 60
K_FTS = 50        # per user spec: 30-50 lexical candidates
K_VEC_FETCH = 300  # KNN candidates fetched before message-level filtering
K_VEC = 50         # vector candidates after filtering
TOP_FUSE = 20      # candidates entering the reranker (user spec: 15-20)
FTS_WEIGHTS_CLEAN = (4.0, 2.0, 1.0)  # subject / sender / body


# ------------------------------------------------------------------ helpers

def mem_rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def _epoch(y, m, d, h=0):
    return int(time.mktime((y, m, d, h, 0, 0, 0, 0, -1)))


# ------------------------------------------------------------------ text handling

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
    """Split an email body into (new_content, quoted_tail, method).

    Heuristics, first hit wins:
      1. explicit quote markers (On ... wrote:, Outlook From:/Sent: block,
         Original Message, forwarded banners, CN equivalents, long underscores)
      2. a trailing block of >=3 consecutive '>' quoted lines
    Anything shorter than 2 lines of signal is left alone (inline quoting).
    """
    text = text or ""
    cut = None
    method = ""
    for i, pat in enumerate(_QUOTE_MARKERS):
        m = pat.search(text)
        if m and m.start() > 0:
            cut, method = m.start(), "marker:%d" % i
            break
    if cut is None:
        # '>' quoted block: find first line-start '>' with >= 3 quoted lines after
        lines = text.split("\n")
        for idx in range(1, len(lines)):
            if lines[idx].startswith(">"):
                tail = lines[idx:]
                if sum(1 for l in tail if l.startswith(">")) >= 3:
                    cut, method = len("\n".join(lines[:idx])), "quoted-block"
                break
    if cut is None or cut < 40:          # nothing meaningful was cut
        return text.strip(), "", method or "none"
    new, quoted = text[:cut].strip(), text[cut:].strip()
    if len(new) < 40:                    # the "new" part is a stub; keep full text
        return text.strip(), "", "none"
    return new, quoted, method


def norm_subject(s):
    s = (s or "").strip().lower()
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"^(re|fwd?|aw|sv|回复|答复)\s*(\[\d+\])?\s*[:\]\uff1a]\s*", "", s)
    s = re.sub(r"^\s*\[[^\]]{1,40}\]\s*", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _counterpart(from_addr, to_addr):
    f = (from_addr or "").lower()
    if "seanyong" in f or "ust.hk" in f:
        return (to_addr or "").lower()
    return f


def thread_key(subject, from_addr, to_addr):
    raw = "t:%s|%s" % (norm_subject(subject), _counterpart(from_addr, to_addr))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:14]


_SENT_SPLIT = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])\s+|\n{2,}")


def chunk_pack(text):
    """Same packing policy as rag.py chunk_text (minus the header)."""
    body = (text or "").strip()
    if not body:
        return []
    if len(body) <= CHUNK_MAX:
        return [body]
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
        while len(cur) > CHUNK_MAX:
            chunks.append(cur[:CHUNK_MAX].strip())
            cur = cur[CHUNK_MAX:].lstrip()
    if cur:
        if chunks and len(cur) < CHUNK_MIN:
            chunks[-1] += " " + cur
        else:
            chunks.append(cur)
    return [c for c in chunks if c.strip()]


def header_line(row):
    return ("From: %s | To: %s | Date: %s | Subject: %s"
            % (row["from_addr"], row["to_addr"], row["date"], row["subject"]))


# ------------------------------------------------------------------ sqlite plumbing

def db(path=DB_PATH, vec=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    if vec:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS messages_light (
            id INTEGER PRIMARY KEY,
            msgid TEXT DEFAULT '', from_addr TEXT DEFAULT '', to_addr TEXT DEFAULT '',
            subject TEXT DEFAULT '', date TEXT DEFAULT '', date_ts INTEGER DEFAULT 0,
            folder TEXT DEFAULT '', snippet TEXT DEFAULT '',
            clean_body TEXT DEFAULT '', quoted TEXT DEFAULT '', quote_method TEXT DEFAULT 'none',
            thread_key TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_ml_date ON messages_light(date_ts);
        CREATE INDEX IF NOT EXISTS idx_ml_from ON messages_light(from_addr);
        CREATE INDEX IF NOT EXISTS idx_ml_thread ON messages_light(thread_key);
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cset TEXT NOT NULL, message_id INTEGER NOT NULL, seq INTEGER NOT NULL DEFAULT 0,
            node TEXT NOT NULL DEFAULT 'new', text TEXT NOT NULL DEFAULT '',
            subject TEXT DEFAULT '', sender TEXT DEFAULT '', created INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_msg ON chunks(message_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_set ON chunks(cset, message_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_raw USING fts5(text);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_clean
            USING fts5(subject, sender, body);
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_raw_06b
            USING vec0(embedding float[1024] distance_metric=cosine);
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_clean_06b
            USING vec0(embedding float[1024] distance_metric=cosine);
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_raw_4b
            USING vec0(embedding float[2560] distance_metric=cosine);
        CREATE TABLE IF NOT EXISTS index_meta (k TEXT PRIMARY KEY, v TEXT);
        """)
        c.commit()


def meta_get(k, default=None):
    with db(vec=False) as c:
        r = c.execute("SELECT v FROM index_meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(k, v):
    with db(vec=False) as c:
        c.execute("INSERT INTO index_meta(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        c.commit()


# ------------------------------------------------------------------ corpus load

def load_messages():
    """Copy wanted mail from the live DB into messages_light (read-only source)."""
    src = sqlite3.connect("file:%s?mode=ro" % LIVE_DB, uri=True)
    src.row_factory = sqlite3.Row
    where = " AND ".join(["lower(folder) NOT LIKE '%'||?||'%'"] * len(EXCLUDE_FOLDERS))
    rows = src.execute(
        "SELECT id, msgid, from_addr, to_addr, subject, date, "
        "COALESCE(NULLIF(date_ts,0), NULLIF(sort_ts,0), processed_at, 0) AS ts, "
        "folder, snippet FROM messages WHERE snippet != '' AND " + where,
        EXCLUDE_FOLDERS).fetchall()
    out = []
    for r in rows:
        new, quoted, method = strip_quoted(r["snippet"])
        out.append((r["id"], r["msgid"], r["from_addr"], r["to_addr"], r["subject"],
                    r["date"], int(r["ts"] or 0), r["folder"], r["snippet"],
                    new, quoted, method, thread_key(r["subject"], r["from_addr"], r["to_addr"])))
    with db(vec=False) as c:
        c.execute("DELETE FROM messages_light")
        c.executemany("INSERT INTO messages_light VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
        c.commit()
    return len(out)


# ------------------------------------------------------------------ embedders / rerankers

_fastembed_models = {}


FASTEMBED_THREADS = int(os.environ.get("RAGLITE_THREADS", "8"))


def fastembed_embedder(name="Qwen/Qwen3-Embedding-0.6B"):
    global _fastembed_models
    key = "emb:" + name
    if key not in _fastembed_models:
        os.environ.setdefault("FASTEMBED_CACHE_PATH", MODEL_CACHE)
        from fastembed import TextEmbedding
        t0 = time.time()
        _fastembed_models[key] = (TextEmbedding(name, threads=FASTEMBED_THREADS),
                                  time.time() - t0)
    return _fastembed_models[key]


def tei_embed(texts, batch=32, base=None):
    import urllib.request
    out = []
    base = base or TEI_EMBED
    for i in range(0, len(texts), batch):
        req = urllib.request.Request(
            base + "/embed",
            data=json.dumps({"inputs": list(texts[i:i + batch])}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out.extend(json.loads(r.read()))
    return out


def embed_docs(texts, embed="06b", batch=64):
    """Returns list of float lists.
    embed: '06b' (fastembed ONNX CPU) | '4b' (TEI :8041) | '06btei' (scratch TEI :8043)"""
    if embed == "4b":
        return tei_embed(texts, batch=batch)
    if embed == "06btei":
        return tei_embed(texts, batch=batch, base=TEI_EMBED_06B)
    m, _ = fastembed_embedder()
    vecs = []
    for i in range(0, len(texts), batch):
        vecs.extend([list(map(float, v)) for v in m.embed(list(texts[i:i + batch]))])
    return vecs


def embed_query(q, embed="06b"):
    return embed_docs([QUERY_PREFIX + q], embed=embed)[0]


def rerank_scores(name, query, passages):
    """name: none | v2m3 (live TEI) | minilm | bge-base | jina-turbo (fastembed CPU)."""
    if not name or name == "none" or not passages:
        return None
    if name == "v2m3":
        import urllib.request
        req = urllib.request.Request(
            TEI_RERANK + "/rerank",
            data=json.dumps({"query": query, "texts": list(passages)}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        out = [0.0] * len(passages)
        for item in data:
            out[int(item["index"])] = float(item.get("score", 0.0))
        return out
    fname = {"minilm": "Xenova/ms-marco-MiniLM-L-6-v2",
             "minilm12": "Xenova/ms-marco-MiniLM-L-12-v2",
             "bge-base": "BAAI/bge-reranker-base",
             "jina-turbo": "jinaai/jina-reranker-v1-turbo-en"}[name]
    key = "ce:" + fname
    if key not in _fastembed_models:
        os.environ.setdefault("FASTEMBED_CACHE_PATH", MODEL_CACHE)
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        t0 = time.time()
        _fastembed_models[key] = (TextCrossEncoder(fname, threads=FASTEMBED_THREADS),
                                  time.time() - t0)
    ce, _ = _fastembed_models[key]
    return [float(s) for s in ce.rerank(query, list(passages))]


def load_time_report():
    return {k: round(v[1], 1) for k, v in _fastembed_models.items()}


# ------------------------------------------------------------------ chunk build

def build_chunks(sets=("raw", "clean"), limit=None):
    stats = {"messages": 0, "raw_chunks": 0, "clean_chunks": 0,
             "quoted_messages": 0, "quoted_deleted_chars": 0, "total_chars": 0}
    with db(vec=True) as c:
        c.execute("DELETE FROM chunks")
        c.execute("DELETE FROM chunks_fts_raw")
        c.execute("DELETE FROM chunks_fts_clean")
        rows = [dict(r) for r in c.execute("SELECT * FROM messages_light ORDER BY id")]
    with db(vec=True) as c:
        for row in rows:
            if limit and stats["messages"] >= limit:
                break
            stats["messages"] += 1
            stats["total_chars"] += len(row["snippet"])
            if row["quoted"]:
                stats["quoted_messages"] += 1
                stats["quoted_deleted_chars"] += len(row["quoted"])
            hdr = header_line(row)
            if "raw" in sets:
                for seq, body in enumerate(chunk_pack(row["snippet"])):
                    text = (hdr + "\n" + body).strip()
                    cid = c.execute(
                        "INSERT INTO chunks (cset, message_id, seq, node, text, subject, "
                        "sender, created) VALUES ('raw',?,?,?,?,?,?,?)",
                        (row["id"], seq, "full", text, row["subject"],
                         row["from_addr"], 0)).lastrowid
                    c.execute("INSERT INTO chunks_fts_raw (rowid, text) VALUES (?,?)",
                              (cid, text))
                    stats["raw_chunks"] += 1
            if "clean" in sets:
                body_src = row["clean_body"] or row["snippet"]
                node = "new" if row["clean_body"] else "full"
                for seq, body in enumerate(chunk_pack(body_src)):
                    text = (hdr + "\n" + body).strip()
                    cid = c.execute(
                        "INSERT INTO chunks (cset, message_id, seq, node, text, subject, "
                        "sender, created) VALUES ('clean',?,?,?,?,?,?,?)",
                        (row["id"], seq, node, text, row["subject"],
                         row["from_addr"], 0)).lastrowid
                    c.execute(
                        "INSERT INTO chunks_fts_clean (rowid, subject, sender, body) "
                        "VALUES (?,?,?,?)", (cid, row["subject"], row["from_addr"], body))
                    stats["clean_chunks"] += 1
        c.commit()
    return stats


def build_vectors(embed="06b", sets=("raw", "clean"), batch=32, limit=None):
    table = {"raw": "vec_raw_%s" % embed, "clean": "vec_clean_%s" % embed}[sets[0]] \
        if len(sets) == 1 else None
    # embed whichever chunk rows lack a vector in the target table
    tbl = "06b" if embed == "06btei" else embed   # tei-built 0.6B vectors land in the same table
    todo = []
    with db(vec=True) as c:
        for cset in sets:
            t = "vec_%s_%s" % (cset, tbl)
            have = {r[0] for r in c.execute("SELECT rowid FROM %s" % t)}
            q = ("SELECT id, text FROM chunks WHERE cset=? ORDER BY id")
            for r in c.execute(q, (cset,)):
                if r["id"] not in have:
                    todo.append((cset, r["id"], r["text"]))
    if limit:
        todo = todo[:limit]
    t0, done, rss0 = time.time(), 0, mem_rss_mb()
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        vecs = embed_docs([p[2] for p in part], embed=embed, batch=batch)
        with db(vec=True) as c:
            for (cset, cid, _), v in zip(part, vecs):
                blob = struct.pack("%df" % len(v), *v)
                c.execute("INSERT INTO vec_%s_%s (rowid, embedding) VALUES (?,?)"
                          % (cset, tbl), (cid, blob))
            c.commit()
        done += len(part)
    dt = time.time() - t0
    return {"embedded": done, "seconds": round(dt, 1),
            "chunks_per_sec": round(done / dt, 2) if dt else None,
            "rss_mb_before": round(rss0, 1), "rss_mb_after": round(mem_rss_mb(), 1),
            "model": embed}


# ------------------------------------------------------------------ query understanding

_STOP = {"the", "a", "an", "my", "me", "i", "we", "you", "mail", "email", "emails",
         "message", "messages", "about", "what", "did", "say", "said", "from", "by",
         "to", "and", "or", "in", "on", "last", "this", "week", "month", "year",
         "was", "were", "is", "are", "it", "that", "which", "when", "who", "find",
         "got", "send", "sent", "sendt", "regarding", "re", "fwd"}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def parse_query(q):
    """Lightweight metadata extraction: senders, date windows, exact tokens."""
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
    if "yesterday" in ql:
        d0 = _epoch(y, mo, now.tm_mday - 1)
        out.update(date_from=d0, date_to=d0 + 86399, date_label="yesterday")
    elif "last week" in ql:
        end = _epoch(y, mo, now.tm_mday) - 86400
        out.update(date_from=end - 6 * 86400, date_to=end + 86399, date_label="last week")
    elif "last month" in ql:
        m0, y0 = (mo - 1, y) if mo > 1 else (12, y - 1)
        m1 = 1 if m0 == 12 else m0 + 1
        y1 = y0 + 1 if m0 == 12 else y0
        out.update(date_from=_epoch(y0, m0, 1), date_to=_epoch(y1, m1, 1) - 1,
                   date_label="last month")
    elif "this month" in ql:
        out.update(date_from=_epoch(y, mo, 1), date_to=_epoch(y, mo, 28) + 4 * 86400,
                   date_label="this month")
    elif "last year" in ql:
        out.update(date_from=_epoch(y - 1, 1, 1), date_to=_epoch(y, 1, 1) - 1,
                   date_label="last year")
    else:
        m = re.search(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\s+(\d{4})\b", ql)
        if m:
            yy, mm = int(m.group(2)), _MONTHS[m.group(1)]
            mm2, yy2 = (mm + 1, yy) if mm < 12 else (1, yy + 1)
            out.update(date_from=_epoch(yy, mm, 1), date_to=_epoch(yy2, mm2, 1) - 1,
                       date_label="%s %d" % (m.group(1), yy))
        else:
            m = re.search(r"\b(20\d\d)-\d\d-\d\d\b", ql)
            if m:
                y0, m0, d0 = map(int, m.group(0).split("-"))
                out.update(date_from=_epoch(y0, m0, d0), date_to=_epoch(y0, m0, d0) + 86399,
                           date_label=m.group(0))
    for m in re.finditer(r'"([^"]{3,80})"', q):
        out["exacts"].append(m.group(1))
    for m in re.finditer(r"\b[A-Z]{2,}[-# ]?\d{2,}\b|\b\d{5,}\b", q):
        out["exacts"].append(m.group(0).strip())
    return out


# ------------------------------------------------------------------ search

def fts_query(q, parse):
    """Quoted OR-join like rag.fts_query, plus any exact tokens verbatim."""
    terms = re.findall(r"[\w@.\-+#]+", q or "", re.UNICODE)
    terms = [t for t in terms if len(t) >= 2]
    special = [t for t in (parse.get("exacts") or []) if t]
    for s in special:
        if s.lower() not in [t.lower() for t in terms]:
            terms.append(s)
    if not terms:
        return '"%s"' % (q or "").replace('"', " ")
    return " OR ".join('"%s"' % t.replace('"', '""') for t in terms)


def _candidate_ids(parse, folder=None):
    """Message ids pre-matching the query's sender/date hints, or None."""
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
    if folder:
        where.append("folder = ?")
        args.append(folder)
    if not where:
        return None
    sql = "SELECT id FROM messages_light WHERE " + " AND ".join(where)
    with db(vec=False) as c:
        return {r[0] for r in c.execute(sql, args)}


def search(q, k=8, cset="clean", embed="06b", rerank=None, folder=None, diag=False,
           channels=("fts", "vec")):
    q = (q or "").strip()
    t0 = time.perf_counter()
    out = {"ok": True, "results": [], "meta": {}}
    if not q:
        return {"ok": False, "error": "empty query"}
    parse = parse_query(q)
    notes = []
    tq = time.perf_counter()
    cand = _candidate_ids(parse, folder=folder)
    if cand is not None and len(cand) == 0:
        notes.append("metadata filters matched no messages - fell back to the full archive")
        cand = None
    t_meta = time.perf_counter() - tq
    cand_clause = ""
    cand_args = []
    if cand is not None:
        cand_clause = " AND message_id IN (%s)" % ",".join("?" * min(len(cand), 900))
        cand_args = list(cand)[:900]

    fts_rank, vec_rank = {}, {}
    t_fts = t_vec = 0.0
    if "fts" in channels:
        t1 = time.perf_counter()
        if cset == "raw":
            sql = ("SELECT f.rowid AS rid FROM chunks_fts_raw f "
                   "JOIN chunks ch ON ch.id = f.rowid "
                   "WHERE chunks_fts_raw MATCH ? AND ch.cset='raw'" + cand_clause +
                   " ORDER BY bm25(chunks_fts_raw) LIMIT %d" % K_FTS)
            args = [fts_query(q, parse)] + cand_args
        else:
            sql = ("SELECT f.rowid AS rid FROM chunks_fts_clean f "
                   "JOIN chunks ch ON ch.id = f.rowid "
                   "WHERE chunks_fts_clean MATCH ? AND ch.cset='clean'" + cand_clause +
                   " ORDER BY bm25(chunks_fts_clean, %s, %s, %s) LIMIT %d"
                   % (FTS_WEIGHTS_CLEAN + (K_FTS,)))
            args = [fts_query(q, parse)] + cand_args
        try:
            with db(vec=True) as c:
                for rank, r in enumerate(c.execute(sql, args)):
                    fts_rank[r["rid"]] = rank + 1
        except Exception as exc:
            notes.append("fts failed: %r" % exc)
        t_fts = time.perf_counter() - t1
    if "vec" in channels:
        t1 = time.perf_counter()
        try:
            qv = embed_query(q, embed=embed)
            blob = struct.pack("%df" % len(qv), *qv)
            with db(vec=True) as c:
                rows = c.execute(
                    "SELECT v.rowid AS rid FROM vec_%s_%s v JOIN chunks ch ON ch.id = v.rowid "
                    "WHERE v.embedding MATCH ? AND k = %d" % (cset, embed, K_VEC_FETCH),
                    (blob,)).fetchall()
                kept = []
                for r in rows:
                    kept.append(r["rid"])
                    if len(kept) >= K_VEC_FETCH:
                        break
            # filter to set + metadata candidates (and to top-K_VEC after filtering)
            with db(vec=True) as c:
                keep2 = []
                for rid in kept:
                    row = c.execute("SELECT cset, message_id FROM chunks WHERE id=?",
                                    (rid,)).fetchone()
                    if not row or row["cset"] != cset:
                        continue
                    if cand is not None and row["message_id"] not in cand:
                        continue
                    keep2.append(rid)
                    if len(keep2) >= K_VEC:
                        break
            for rank, rid in enumerate(keep2):
                vec_rank[rid] = rank + 1
        except Exception as exc:
            notes.append("vector channel failed: %r" % exc)
        t_vec = time.perf_counter() - t1

    fused = {}
    for rid, rank in fts_rank.items():
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
    for rid, rank in vec_rank.items():
        fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
    top_ids = sorted(fused, key=fused.get, reverse=True)[:TOP_FUSE]

    with db(vec=True) as c:
        chunk_rows = {r["id"]: dict(r) for r in c.execute(
            "SELECT * FROM chunks WHERE id IN (%s)" % ",".join("?" * len(top_ids)),
            top_ids)} if top_ids else {}
        msg_ids = list({chunk_rows[i]["message_id"] for i in top_ids if i in chunk_rows})
        msgs = {r["id"]: dict(r) for r in c.execute(
            "SELECT * FROM messages_light WHERE id IN (%s)" % ",".join("?" * len(msg_ids)),
            msg_ids)} if msg_ids else {}

    rows = []
    for rid in top_ids:
        ch = chunk_rows.get(rid)
        if not ch:
            continue
        m = msgs.get(ch["message_id"])
        if not m:
            continue
        rows.append({
            "chunk_id": rid,
            "message_id": m["id"],
            "thread_id": m["thread_key"],
            "folder": m["folder"],
            "from_addr": m["from_addr"],
            "to_addr": m["to_addr"],
            "subject": m["subject"],
            "date": m["date"],
            "date_ts": m["date_ts"],
            "excerpt": re.sub(r"\s+", " ", ch["text"])[:320],
            "score": fused[rid],
            "bm25_rank": fts_rank.get(rid),
            "vector_rank": vec_rank.get(rid),
            "rrf_score": round(fused[rid], 6),
            "node": ch["node"],
        })

    t_rr = 0.0
    if rerank:
        t1 = time.perf_counter()
        passages = [(chunk_rows[x["chunk_id"]]["text"])[:2000] for x in rows]
        try:
            scores = rerank_scores(rerank, q, passages)
            if scores:
                for x, s in zip(rows, scores):
                    x["reranker_score"] = round(float(s), 4)
                rows.sort(key=lambda x: x.get("reranker_score", float("-inf")),
                          reverse=True)
        except Exception as exc:
            notes.append("rerank failed: %r" % exc)
        t_rr = time.perf_counter() - t1

    seen, results = set(), []
    for x in rows:
        if x["message_id"] in seen:
            continue
        seen.add(x["message_id"])
        results.append(x)
        if len(results) >= max(1, int(k)):
            break

    # thread expansion (context, diagnostics-level)
    if diag:
        with db(vec=False) as c:
            for x in results:
                nb = [dict(r) for r in c.execute(
                    "SELECT id, subject, date, from_addr, folder FROM messages_light "
                    "WHERE thread_key=? AND id != ? ORDER BY ABS(date_ts - ?) LIMIT 3",
                    (x["thread_id"], x["message_id"], x["date_ts"]))]
                x["thread_more"] = nb
                x["thread_size"] = 1 + len(nb)

    if not diag:
        for x in results:
            x.pop("rrf_score", None)
            x.pop("bm25_rank", None)
            x.pop("vector_rank", None)
    out["results"] = results
    out["meta"] = {
        "query": q, "ms": round((time.perf_counter() - t0) * 1000),
        "ms_meta": round(t_meta * 1000, 1), "ms_fts": round(t_fts * 1000, 1),
        "ms_vec": round(t_vec * 1000, 1), "ms_rerank": round(t_rr * 1000, 1),
        "channels": list(channels), "chunk_set": cset, "embed": embed,
        "rerank": rerank or "none", "parse": {k: parse[k] for k in
                                              ("senders", "date_label", "exacts")},
        "note": "; ".join(notes),
    }
    return out


def index_stats():
    with db(vec=False) as c:
        rows = c.execute("SELECT cset, COUNT(*) n, COUNT(DISTINCT message_id) m "
                         "FROM chunks GROUP BY cset").fetchall()
        stats = {r["cset"]: {"chunks": r["n"], "messages": r["m"]} for r in rows}
        stats["messages_light"] = c.execute(
            "SELECT COUNT(*) FROM messages_light").fetchone()[0]
    for t in ("vec_raw_06b", "vec_clean_06b", "vec_raw_4b"):
        try:
            with db(vec=True) as c:
                stats[t] = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        except Exception:
            stats[t] = -1
    return stats
