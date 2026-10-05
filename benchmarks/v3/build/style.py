"""Corpus-guided style application (WP2).

The committed ``fixtures/style.json`` holds aggregate shapes mined offline from
the reference corpora (greetings, sign-offs, subject prefixes, length bands,
quoting rate) with provenance. Runtime never reads the corpora. Style is applied
as wording variety (greeting opener) and as a reserved shift profile, never as
verbatim real content.
"""
import re

from . import recipes

_GREETING_RE = re.compile(
    r"^(hi|hello|hey|dear|good morning|good afternoon)\b(.*)$", re.I)


def _data():
    return recipes.style()


def _titlecase(word):
    return " ".join(part.capitalize() for part in word.split())


def greeting_pool(profile="standard"):
    """Greeting openers for a style profile (disjoint standard vs shift)."""
    words = list(_data().get("greetings") or ["hi", "hello", "dear"])
    if profile == "shift":
        pool = words[2:]
        return pool or ["dear"]
    return words[:2] or words


def closings():
    return list(_data().get("closings") or ["thanks", "regards"])


def subject_prefixes():
    return [p for p in (_data().get("subject_prefixes") or ["re:", "[tag]"])]


def provenance():
    return dict(_data().get("provenance") or {})


def restyle_greeting(body, stream, profile="standard"):
    """Rewrite the leading greeting opener using the corpus-derived pool."""
    lines = body.split("\n")
    for i, line in enumerate(lines[:3]):
        match = _GREETING_RE.match(line)
        if match:
            opener = _titlecase(stream.pick(greeting_pool(profile)))
            lines[i] = opener + match.group(2)
            return "\n".join(lines)
    return body
