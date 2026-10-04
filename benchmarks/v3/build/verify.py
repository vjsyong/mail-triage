"""Deterministic verification of a rendered message (WP6, item 4).

Every candidate the renderer produces must pass this before it can be accepted.
The checks are intentionally mechanical and reuse the world lint
(``plausibility.py``) rather than trusting the model's own claims.  A rendered
message is rejected -- with reasons fed back into the next retry -- when any of
these fail:

* required facts present / forbidden facts absent;
* paid-state consistency (a receipt never carries "if already paid", an
  outstanding request never claims payment was received);
* the document reference prefix matches its document type (a tracking number is
  never invoice-like);
* a security sign-in notice carries the login time (and place);
* every printed date matches a brief fact;
* no domain outside the world;
* no gold leakage into the rendered text;
* no unfounded CC / workstream / history claim;
* sentence hygiene (doubled punctuation, clause injection, lowercase after a
  terminator, duplicated words);
* the world plausibility lint passes for the built message.
"""
import re

from .. import contracts
from . import briefs, plausibility, temporal

PREFIX_FOR_DOC = {
    "receipt": "RCPT", "invoice": "INV", "bill": "BILL", "order": "ORD",
    "tracking": "TRACK", "ticket": "TKT", "case": "CASE", "policy": "POL",
}

_DATE_MONTH = plausibility.MONTH
_DATE_DAY_MONTH = plausibility._DATE_DAY_MONTH
_DATE_MONTH_DAY = plausibility._DATE_MONTH_DAY
_DOMAIN = plausibility._DOMAIN


def build_message(brief, subject, body):
    """The message record for the declared sender/recipient and send time."""
    sender = brief.get("sender") or {}
    recipient = brief.get("recipient") or {}
    return {
        "message_id": "msg_%s" % brief.get("brief_id"),
        "from_addr": sender.get("email") or "",
        "to_addr": recipient.get("email") or "",
        "subject": subject,
        "date": brief["send"]["header"],
        "body": body,
        "snippet": body,
    }


def _allowed_dates(brief):
    allowed = set()
    region = {"date_style": "day_month"}
    for entry in (brief.get("dates") or {}).values():
        allowed.add(plausibility._normalize_date(entry["date"]))
        allowed.add(plausibility._normalize_date(entry["deadline"]))
    allowed.add(plausibility._normalize_date(brief["send"]["date"]))
    return allowed


def _check_dates(text, brief):
    problems = []
    allowed = _allowed_dates(brief)
    for rendered, _weekday in plausibility._extract_dates(text):
        if rendered not in allowed:
            problems.append("printed date %r does not match any brief fact"
                            % rendered)
    return problems


def _check_hygiene(text):
    problems = []
    if plausibility._DOUBLED_PUNCT.search(text):
        problems.append("doubled terminal punctuation")
    if plausibility._lowercase_after_terminator(text):
        problems.append("lowercase word after a sentence terminator")
    opener = plausibility._MID_SENTENCE_OPENER.search(text)
    if opener:
        problems.append("clause opener %r injected mid-sentence" % opener.group(1))
    dup = plausibility._DUPLICATE_WORD.search(text)
    if dup:
        problems.append("duplicated word %r" % dup.group(0))
    return problems


def _check_claims(text, brief):
    problems = []
    low = text.lower()
    facts = brief.get("facts_ref") or {}
    for phrase, key in plausibility._CLAIM_PHRASES:
        if phrase in low and not facts.get(key):
            problems.append("unbacked claim %r (no declared %r fact)" % (phrase, key))
            break
    return problems


def _check_domains(text, world):
    problems = []
    for domain in _DOMAIN.findall(text.lower()):
        if domain not in world.domains:
            problems.append("references domain %r which is not a world entity"
                            % domain)
            break
    return problems


def _check_reference(text, brief):
    problems = []
    ref = brief.get("reference")
    if not ref:
        return problems
    low = text.lower()
    if ref["id"].lower() not in low:
        problems.append("document reference %s is missing" % ref["id"])
    found = briefs.split_by_document_type(text)
    expected = PREFIX_FOR_DOC.get(ref["type"])
    wrong = found - ({expected} if expected else set())
    if wrong:
        problems.append("reference prefix mismatch: document type %r expects %s, "
                        "found %s" % (ref["type"], expected, sorted(wrong)))
    if ref["type"] == "tracking" and (found - {"TRACK"}):
        problems.append("tracking number is invoice-like")
    return problems


def verify_message(brief, subject, body, *, world, gold=None, prior_messages=None):
    """Return a list of problems; ``[]`` means the message is acceptable."""
    problems = []
    subject = subject or ""
    body = body or ""
    text = "%s\n%s" % (subject, body)
    low = text.lower()

    for label, token in brief.get("required_facts") or []:
        if token and str(token).lower() not in low:
            problems.append("required fact %s is missing: %r" % (label, token))
    for phrase in brief.get("forbidden_facts") or []:
        if phrase.lower() in low:
            problems.append("forbidden phrase present: %r" % phrase)

    problems.extend(_check_reference(text, brief))
    login = brief.get("login")
    if login:
        if login["time"] not in text:
            problems.append("security notice is missing the login time %s"
                            % login["time"])
        place = login.get("place")
        if place and place.lower() not in low:
            problems.append("security notice is missing the login place %r" % place)

    problems.extend(_check_dates(text, brief))
    problems.extend(_check_hygiene(text))
    problems.extend(_check_claims(text, brief))
    problems.extend(_check_domains(text, world))

    if not subject.strip():
        problems.append("empty subject")
    if len(body.strip()) < 40:
        problems.append("body is implausibly short (%d chars)" % len(body.strip()))

    if gold is not None:
        leaked = contracts.find_gold_leakage(text, gold)
        if leaked:
            problems.append("gold leaked into rendered text: %s" % leaked)

    msg = build_message(brief, subject, body)
    facts = brief.get("facts_ref") or {}
    facts = dict(facts)
    facts.setdefault("scenario_id", brief.get("brief_id"))
    # The message id is stable per brief for lint labelling.
    for err in plausibility.check_scenario(facts, [msg], world):
        problems.append("plausibility: %s" % err)

    return problems


def message_records(brief, subject, body):
    """A ``(message, facts)`` pair suitable for ``plausibility.check_case``."""
    return build_message(brief, subject, body), dict(brief.get("facts_ref") or {})
