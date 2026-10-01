#!/usr/bin/env python3
"""Construct an extra labelled eval set from the corpus covering query types the
48-query semantic set under-tests: exact identifiers, sender queries,
date-restricted queries, subject lookups, thread questions, mixed metadata queries.

Ground truth = a unique lowercase fragment of the target message's subject (same
matching rule as tests/retrieval_eval.py). Only locally generated from mail the
user already owns - do not publish. Requires: build.py load (messages_light).
"""
import collections
import datetime
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import raglite as R  # noqa: E402

OUT = os.path.join(R.ROOT, "prototype", "bench", "eval_extra.json")
META = os.path.join(R.ROOT, "prototype", "bench", "eval_extra_meta.json")

TOKEN_RE = re.compile(r"\b[A-Z]{2,6}[-# ]?\d{4,10}\b")          # INV-39281, D100334310
DIGIT_RE = re.compile(r"\b\d{6,12}\b")                          # long refs
WORD_RE = re.compile(r"[a-z]{7,}")


def main():
    if not os.path.exists(R.DB_PATH):
        print("run build.py load first"); return 1
    with R.db(vec=False) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, from_addr, subject, date, date_ts, snippet, thread_key FROM messages_light")]
    src = sqlite3.connect("file:%s?mode=ro" % R.LIVE_DB, uri=True)
    live_ids = {r[0] for r in src.execute("SELECT DISTINCT message_id FROM chunks")}
    corpus_ids = {r["id"] for r in rows}
    print("corpus %d messages; %d present in the live index" % (len(rows), len(live_ids & corpus_ids)))

    now = datetime.datetime.now()
    prev_m0 = (now.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    prev_m1 = now.replace(day=1)
    prev_lo, prev_hi = int(prev_m0.timestamp()), int(prev_m1.timestamp())

    tok2ids, word2ids, sender2ids = collections.defaultdict(set), collections.defaultdict(set), collections.defaultdict(set)
    for r in rows:
        blob = (r["subject"] or "") + "\n" + (r["snippet"] or "")
        for t in set(TOKEN_RE.findall(blob)) | set(DIGIT_RE.findall(blob)):
            tok2ids[t].add(r["id"])
        for w in set(WORD_RE.findall((r["subject"] or "").lower())):
            word2ids[w].add(r["id"])
        sender2ids[r["from_addr"].lower()].add(r["id"])

    def subj_frag(r, n=34):
        s = re.sub(r"^(re|fwd|fw)\s*:\s*", "", (r["subject"] or "").strip(), flags=re.I)
        return s[:n].lower().strip()

    qs, seen = [], set()

    def add(q, r, typ):
        frag = subj_frag(r)
        if not frag or len(frag) < 12 or frag in seen or r["id"] not in live_ids:
            return
        seen.add(frag)
        qs.append({"q": q, "expect": [frag], "type": typ})

    # 1) exact identifiers (unique in corpus + live)
    for tok, ids in sorted(tok2ids.items(), key=lambda kv: -len(kv[0])):
        if len(qs) >= 6:
            break
        if len(ids) == 1:
            r = next(x for x in rows if x["id"] == next(iter(ids)))
            add("find the email that mentions %s" % tok, r, "identifier")
    # 2) sender-specific (sender has exactly one wanted message)
    for s, ids in sorted(sender2ids.items()):
        if len([q for q in qs if q["type"] == "sender"]) >= 6:
            break
        if len(ids) == 1 and s:
            r = next(x for x in rows if x["id"] == next(iter(ids)))
            add("emails from %s" % s, r, "sender")
    # 3) date-restricted (message in the previous calendar month, unique-ish word)
    for r in rows:
        if len([q for q in qs if q["type"] == "date"]) >= 5:
            break
        if not (prev_lo <= (r["date_ts"] or 0) < prev_hi):
            continue
        ws = [w for w in WORD_RE.findall((r["subject"] or "").lower())
              if len(word2ids[w]) == 1]
        if ws:
            add("what came in about %s last month" % ws[0], r, "date")
    # 4) subject lookup (near-exact)
    for r in sorted(rows, key=lambda x: x["id"]):
        if len([q for q in qs if q["type"] == "subject"]) >= 6:
            break
        base = R.norm_subject(r["subject"])
        if len(base) >= 18 and len(r["subject"]) >= 24:
            add("email titled “%s”" % R.norm_subject(r["subject"])[:44], r, "subject")
    # 5) thread questions (clusters with >=3 messages)
    clusters = collections.defaultdict(list)
    for r in rows:
        clusters[r["thread_key"]].append(r)
    for tk, members in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        if len([q for q in qs if q["type"] == "thread"]) >= 6:
            break
        if len(members) >= 3:
            base = R.norm_subject(members[0]["subject"])
            r = min(members, key=lambda m: m["id"])
            if len(base) >= 15 and r["id"] in live_ids:
                frag = base[:34].strip()
                if frag and len(frag) >= 12 and frag not in seen:
                    seen.add(frag)
                    qs.append({"q": "what was discussed in the thread about %s" % base[:60],
                               "expect": [frag], "type": "thread"})
    # 6) mixed metadata + semantics (sender + topic word)
    for s, ids in sorted(sender2ids.items()):
        if len([q for q in qs if q["type"] == "mixed"]) >= 5:
            break
        if len(ids) == 1:
            r = next(x for x in rows if x["id"] == next(iter(ids)))
            ws = [w for w in WORD_RE.findall((r["subject"] or "").lower())
                  if len(word2ids[w]) <= 2]
            if ws:
                add("what did %s send me about %s" % (s, ws[0]), r, "mixed")

    json.dump(qs, open(OUT, "w"), ensure_ascii=False, indent=1)
    counts = collections.Counter(q["type"] for q in qs)
    json.dump({"counts": dict(counts), "total": len(qs),
               "note": "constructed locally from the user's mailbox; do not publish"},
              open(META, "w"), indent=1)
    print("wrote %s: %d queries %s" % (OUT, len(qs), dict(counts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
