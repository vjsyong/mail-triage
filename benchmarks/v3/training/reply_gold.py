"""Visible reply-obligation semantics for the SFT slice (S1).

A ``needs_reply`` label is only valid when the **classifier-visible email text**
actually expresses (or explicitly disclaims) a reply request.  This module turns
that into deterministic checks so gold is never assumed from the family, and a
reply phrase that appears only inside quoted text does not count.
"""
from __future__ import annotations

REPLY_REQUEST_PHRASES = (
    "please reply", "reply to confirm", "please let me know", "please respond",
    "can you confirm", "could you confirm", "confirm by", "reply by",
    "respond by", "kindly reply", "rsvp", "let us know if",
)
NO_REPLY_PHRASES = (
    "no reply is needed", "no reply needed", "do not reply", "don't reply",
    "please do not reply", "no action is required", "no action needed",
    "do not respond", "automated message",
)


def _inside_quotes(line, idx):
    """True when position ``idx`` on ``line`` sits inside double quotes."""
    return line[:idx].count('"') % 2 == 1


def _unquoted_occurrence(text, phrase):
    for line in (text or "").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(">"):  # quoted reply block
            continue
        low = line.lower()
        start = 0
        while True:
            i = low.find(phrase, start)
            if i < 0:
                break
            if not _inside_quotes(line, i):
                return True
            start = i + len(phrase)
    return False


def has_reply_request(text):
    return any(_unquoted_occurrence(text, p) for p in REPLY_REQUEST_PHRASES)


def has_no_reply_statement(text):
    return any(_unquoted_occurrence(text, p) for p in NO_REPLY_PHRASES)


def visible_reply_obligation(text):
    """``True`` / ``False`` when the visible text signals reply / no-reply.

    Returns ``None`` when the text carries no explicit signal.
    """
    if has_reply_request(text):
        return True
    if has_no_reply_statement(text):
        return False
    return None


def reply_problems(text, needs_reply):
    """Problems where the visible text does not justify ``needs_reply``."""
    problems = []
    if needs_reply:
        if not has_reply_request(text):
            problems.append("reply_obligation_not_visible")
        if has_no_reply_statement(text):
            problems.append("contradictory_no_reply_present")
    else:
        if has_reply_request(text):
            problems.append("reply_request_present_but_gold_false")
    return problems
