#!/usr/bin/env python3
"""Offline corpus style miner (read-only, aggregate only).

Samples a bounded number of messages from the downloaded reference corpora and
derives AGGREGATE STYLE STATISTICS ONLY -- greeting shapes, sign-off shapes,
subject prefixes, body-length bands and quoting conventions. It never copies a
body, name, address or domain. Its output is the committed
``benchmarks/v3/build/fixtures/style.json``; the runtime and the test suite do
not require the corpora.

Usage:
    python benchmarks/v3/build/tools/mine_corpora.py \
        --corpus-root /home/xrim/datasets/email-corpora \
        --out benchmarks/v3/build/fixtures/style.json --per-corpus 150
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

GREETING = re.compile(r"^(hi|hello|hey|dear|good morning|good afternoon|"
                      r"to whom it may concern)\b[\s,:-]*", re.I)
CLOSING = re.compile(r"^(thanks|thank you|many thanks|regards|kind regards|"
                     r"best regards|best|cheers|sincerely|yours|speak soon|"
                     r"talk soon)\b[\s,.-]*", re.I)
SUBJECT_PREFIX = re.compile(r"^\s*((?:re|fwd|fw)\s*:\s*|\[[^\]]{1,20}\]\s*)", re.I)


def _sha256(path, cap=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        remaining = cap
        while remaining > 0:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def _first_nonempty(lines):
    for line in lines:
        if line.strip():
            return line.strip()
    return ""


def _split_body(lines):
    """Drop the leading header block, mbox marker and quoted lines."""
    body = list(lines)
    if body and body[0].startswith("From "):
        body = body[1:]
    out = []
    in_headers = True
    for line in body:
        if in_headers and re.match(r"^[A-Za-z][A-Za-z-]*:\s", line):
            continue
        if in_headers and not line.strip():
            in_headers = False
            continue
        in_headers = False
        out.append(line)
    return [l for l in out if not l.startswith(">")]


def _first_body_line(lines):
    for line in _split_body(lines):
        if line.strip():
            return line.strip()
    return ""


def _last_body_line(lines):
    for line in reversed(_split_body(lines)):
        if line.strip() and line.strip() not in ("--", "-- "):
            return line.strip()
    return ""


def _last_nonempty(lines):
    for line in reversed(lines):
        if line.strip() and line.strip() not in ("--", "-- "):
            return line.strip()
    return ""


def _iter_files(root, limit):
    count = 0
    for base, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.startswith(".") or name.endswith((".tar.gz", ".sha256", ".md")):
                continue
            yield os.path.join(base, name)
            count += 1
            if count >= limit * 40:
                return


def _sample_enron(root, per):
    out = []
    for path in _iter_files(root, per * 4):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                lines = f.read(20000).splitlines()
        except OSError:
            continue
        if lines:
            out.append(lines)
        if len(out) >= per:
            break
    return out


def _sample_mbox(root, per):
    out = []
    for path in _iter_files(root, per):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                block = []
                started = False
                for line in f:
                    if line.startswith("From ") and started:
                        if block:
                            out.append(block)
                            block = []
                            if len(out) >= per:
                                return out
                    elif started or line.startswith("From "):
                        started = True
                        block.append(line.rstrip("\n"))
        except OSError:
            continue
    return out[:per]


def analyse(samples):
    greetings = {}
    closings = {}
    prefixes = {}
    lengths = []
    quoted = 0
    for lines in samples:
        if not lines:
            continue
        head = _first_body_line(lines)
        m = GREETING.match(head)
        if m:
            greetings[m.group(1).lower()] = greetings.get(m.group(1).lower(), 0) + 1
        tail = _last_body_line(lines)
        m = CLOSING.match(tail)
        if m:
            closings[m.group(1).lower()] = closings.get(m.group(1).lower(), 0) + 1
        subject = ""
        for line in lines[:40]:
            if line.lower().startswith("subject:"):
                subject = line.split(":", 1)[1]
                break
        m = SUBJECT_PREFIX.match(subject)
        if m:
            key = m.group(1).strip().lower()
            key = key if not key.startswith("[") else "[tag]"
            prefixes[key] = prefixes.get(key, 0) + 1
        words = len(re.findall(r"\S+", "\n".join(lines)))
        lengths.append(words)
        if any(l.startswith(">") for l in lines):
            quoted += 1
    lengths.sort()
    total = max(1, len(samples))
    return {
        "sampled_messages": len(samples),
        "greetings": greetings,
        "closings": closings,
        "subject_prefixes": prefixes,
        "quoted_rate": round(quoted / total, 3),
        "body_words": {
            "p25": lengths[len(lengths) // 4] if lengths else 0,
            "median": lengths[len(lengths) // 2] if lengths else 0,
            "p75": lengths[(3 * len(lengths)) // 4] if lengths else 0,
        },
    }


def top(counts, n):
    return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-root", default="/home/xrim/datasets/email-corpora")
    parser.add_argument("--out", required=True)
    parser.add_argument("--per-corpus", type=int, default=150)
    args = parser.parse_args(argv)

    corpora = {
        "enron": _sample_enron(os.path.join(args.corpus_root, "enron"), args.per_corpus),
        "ietf": _sample_mbox(os.path.join(args.corpus_root, "ietf"), args.per_corpus),
        "spamassassin": _sample_enron(
            os.path.join(args.corpus_root, "spamassassin"), args.per_corpus),
        "nazario": _sample_mbox(os.path.join(args.corpus_root, "nazario"), args.per_corpus),
    }
    per = {name: analyse(rows) for name, rows in corpora.items()}
    combined = analyse([r for rows in corpora.values() for r in rows])
    checksums = os.path.join(args.corpus_root, "CHECKSUMS.sha256")
    provenance = {
        "source_root": args.corpus_root,
        "checksums_sha256": _sha256(checksums) if os.path.isfile(checksums) else None,
        "per_corpus": per,
        "method": "aggregate counts of greeting/sign-off/subject shapes, length bands and quoting rate; no verbatim text, names, addresses or domains are retained",
        "license_note": "Enron (public record, redacted), IETF (IETF Trust), SpamAssassin (Apache project), Nazario (CC-BY-4.0); aggregate statistics only",
    }
    fixture = {
        "note": "Corpus-guided aggregate style. Derived offline by tools/mine_corpora.py; committed so runtime/tests never read the corpora. No verbatim real-data content.",
        "provenance": provenance,
        "greetings": top(combined["greetings"], 6) or ["hi", "hello", "dear"],
        "closings": top(combined["closings"], 6) or ["thanks", "regards", "best"],
        "subject_prefixes": top(combined["subject_prefixes"], 4) or ["re", "fwd"],
        "quoted_rate": combined["quoted_rate"],
        "body_words": combined["body_words"],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(fixture, f, indent=2, sort_keys=True)
        f.write("\n")
    sys.stderr.write("wrote %s (%d sampled)\n" % (args.out, combined["sampled_messages"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
