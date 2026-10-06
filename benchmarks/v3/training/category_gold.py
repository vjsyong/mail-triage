"""Visible category-evidence semantics for the SFT slice (S1 correction).

A ``category`` label is only valid when the **classifier-visible email text**
justifies it against the *configured public definition*.  This catches the two
S1 contradictions:

* an ``Action`` mail (definition: "asks the owner to do something or reply") that
  is actually an automated no-action notice, and
* a ``Receipt`` mail (definition: "a purchase, order or payment completed") that
  is only a shipping notice.

Evidence phrases are authored per variant (``category_evidence``) or fall back to
``DEFAULT_EVIDENCE`` for the category.  Quoted-only evidence does not count, and
an ``Action`` whose only action-ish text is negated is rejected.
"""
from __future__ import annotations

from .reply_gold import _unquoted_occurrence

DEFAULT_EVIDENCE = {
    "Action": ("please review", "review the", "please approve", "approve the",
               "please sign", "submit", "complete the review", "please complete"),
    "Receipt": ("payment received", "purchase completed", "order completed",
                "payment confirmation", "payment has been received", "receipt for",
                "has been paid", "successfully paid"),
    "Billing": ("invoice", "bill", "amount", "overdue", "owed", "payment",
                "balance due"),
    "Promo": ("sale", "offer", "discount", "newsletter", "unsubscribe",
              "promotion", "digest"),
    "Security": ("sign-in", "login", "log in", "password", "security alert",
                 "verification code", "new device"),
    "Personal": ("personal", "friend", "note", "dinner", "weekend", "birthday",
                 "congratulations"),
}

# Phrases that negate an action request for the Action category.
ACTION_NEGATIONS = ("no action is required", "no action needed", "nothing to do",
                    "no further action", "for your information",
                    "automated notification", "do not act", "no action required")


def has_category_evidence(text, category, evidence=None):
    phrases = tuple(evidence) if evidence else DEFAULT_EVIDENCE.get(category, ())
    return any(_unquoted_occurrence(text, p) for p in phrases)


def category_problems(text, category, evidence=None):
    """Problems where the visible text does not justify ``category``."""
    problems = []
    if not has_category_evidence(text, category, evidence):
        problems.append("category_evidence_not_visible")
    if category == "Action":
        for neg in ACTION_NEGATIONS:
            if _unquoted_occurrence(text, neg):
                problems.append("action_negated:%s" % neg)
    return problems
