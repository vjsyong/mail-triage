"""MIME-junk salvage helpers, adapted verbatim from ``engine.py``.

The native classifier cleans a stored snippet only when it still looks like raw
MIME (``looks_like_mime_junk``), using ``readable_body``.  Benchmark v3 must
reproduce that path bit-for-bit so native parity is real, not approximated.
These are pure stdlib functions copied from the pinned application; the
equivalence against the live ``engine.py`` source is certified by
``tests/test_contracts.py`` (isolated AST extraction -- no engine import, no
``.env``, no database, no network).
"""
import base64
import html
import quopri
import re

_B64_RUN = re.compile(r"[A-Za-z0-9+/=]{16,}(?:[ \t]+[A-Za-z0-9+/=]{16,})*")
_BOUNDARY_TOKEN = re.compile(r"(?<!\S)-{2,}[=_A-Za-z0-9][\w=._-]{2,}")


def _b64_decode_run(run_text):
    """Decode one base64 run (whitespace-separated tokens joined up); None when it
    does not decode to mostly-printable text."""
    chunk = "".join(run_text.split())
    if len(chunk) < 40:
        return None
    chunk += "=" * (-len(chunk) % 4)
    try:
        dec = base64.b64decode(chunk).decode("utf-8", "replace")
    except Exception:
        return None
    printable = sum(1 for c in dec if c.isprintable() or c in "\r\n\t")
    if dec.strip() and printable >= len(dec) * 0.8:
        return dec
    return None


def _decode_b64_blocks(text):
    """Decode base64 runs wherever they appear."""
    out, buf = [], []

    def flush():
        if not buf:
            return
        dec = _b64_decode_run(" ".join(buf))
        if dec is not None:
            out.append(dec)
        else:
            out.extend(buf)
        buf.clear()

    for ln in text.split("\n"):
        s = ln.strip()
        if len(s) >= 16 and re.fullmatch(r"[A-Za-z0-9+/=]{16,}", s):
            buf.append(s)
        else:
            flush()
            out.append(_B64_RUN.sub(
                lambda m: _b64_decode_run(m.group(0)) or m.group(0), ln))
    flush()
    return "\n".join(out)


def _strip_inline_mime_scaffold(text):
    """Remove MIME scaffolding left inline in salvaged text."""
    text = _BOUNDARY_TOKEN.sub(" ", text)
    text = re.sub(r"(?i)\bContent-Type\s*:\s*[^;\s]+(?:;\s*charset\s*=\s*\"?[\w.-]+\"?)?", " ", text)
    text = re.sub(r"(?i)\bContent-Transfer-Encoding\s*:\s*[\w-]+", " ", text)
    text = re.sub(r"(?i)\bcharset\s*=\s*\"?[\w.-]+\"?", " ", text)
    return text


def readable_body(raw, limit=6000):
    """Readable body text KEEPING paragraph breaks (message viewer + salvage)."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace")
    junk_in = looks_like_mime_junk(text)
    if len(re.findall(r"=[0-9A-Fa-f]{2}", text)) > 20:
        try:
            text = quopri.decodestring(text.encode("latin-1", "replace")).decode("utf-8", "replace")
        except Exception:
            pass
    text = _decode_b64_blocks(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    if junk_in:
        text = _strip_inline_mime_scaffold(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def looks_like_mime_junk(s):
    """True when stored text is raw MIME rather than a decoded body."""
    if not s:
        return True
    head = s[:800]
    if re.search(r"Content-(Type|Transfer-Encoding)\s*:", head):
        return True
    if re.match(r"\s*-{2,}[=_A-Za-z0-9]", s):
        return True
    if re.match(r"(Received|Return-Path|Delivered-To|Authentication-Results|DKIM-Signature)\s*:", head, re.I):
        return True
    if re.search(r"\S{80,}", s) and re.fullmatch(r"[\sA-Za-z0-9+/=]+", s or ""):
        return True
    return False


def clean_snippet(snippet, limit=1500):
    """Reproduce ``LLMClient.classify``'s snippet cleaning.

    The classifier uses the stored snippet unchanged unless it looks like raw
    MIME, in which case it runs ``readable_body`` (limit 1500).
    """
    body = snippet or ""
    if looks_like_mime_junk(body):
        body = readable_body(body, limit=limit)
    return body[:limit]
