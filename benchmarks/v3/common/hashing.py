"""Canonical JSON + hashing helpers for benchmark v3 (stdlib only).

Adapted from ``benchmarks/v2/common/hashing.py`` with no behavioural change.
Canonicalisation must be stable across Python versions, dict insertion order
and process restarts: run identity and resume-equality are built on it.
"""
import hashlib
import json


def canonical(obj):
    """Deterministic JSON text: sorted keys, no insignificant whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_obj(obj):
    return sha256_text(canonical(obj))


def short(h, n=12):
    return (h or "")[:n]
