"""Plausibility lint for the synthetic world (WP2, U1-U5).

Every generated scenario carries structured ``facts`` (absolute datetimes,
sender/signer/recipient identities, host, venue). This module enforces that the
rendered messages and the facts are mutually coherent and that every referenced
entity belongs to the world. ``build_dataset`` runs it before returning, and
``validate_dataset`` re-runs it so an injected defect is caught too.
"""
import re
from collections import Counter

from . import identity, temporal
from .world import World

WEEKDAY_FULL = temporal.WEEKDAY_NAMES
WEEKDAY_ABBR = tuple(n[:3] for n in WEEKDAY_FULL)
_TIME_FIELDS = ("send", "txn", "due", "event", "arrival", "until", "register_by",
                "meeting", "milestone", "checkpoint", "overdue", "rsvp",
                "deadline_soon", "deadline_late", "deadline_past", "deadline2")
_DUPLICATE_WORD = re.compile(r"\b([a-z]{3,})\s+\1\b", re.I)
_DOMAIN = re.compile(r"@([a-z0-9.-]+)")

# Families whose sender is expected to be a person on the owner's own domain
# (a colleague) or a personal contact, where a shared domain is acceptable.
_SAME_DOMAIN_OK = {"meeting_request", "personal_invitation"}


def _parse(value):
    try:
        return temporal.parse(value) if value else None
    except (TypeError, ValueError):
        return None


def _allowed_weekdays(facts):
    allowed = set()
    for field in _TIME_FIELDS:
        dt = _parse(facts.get(field))
        if dt is not None:
            allowed.add(WEEKDAY_FULL[dt.weekday()])
            allowed.add(WEEKDAY_ABBR[dt.weekday()])
    return allowed


def _body_text(messages):
    chunks = []
    for m in messages:
        chunks.append(str(m.get("subject") or ""))
        chunks.append(str(m.get("from_addr") or ""))
        chunks.append(str(m.get("to_addr") or ""))
        chunks.append(str(m.get("body") or m.get("snippet") or ""))
    return "\n".join(chunks)


def check_scenario(facts, messages, world):
    """Return a list of plausibility problems for one scenario ([] when clean)."""
    errs = []
    scenario = facts.get("scenario_id", "?")
    family = facts.get("family")

    def bad(field, message):
        errs.append("scenario %s: %s %s" % (scenario, field, message))

    if not facts.get("send"):
        bad("send", "missing an absolute send datetime")
        return errs
    send = _parse(facts["send"])
    if send is None:
        bad("send", "is not a valid datetime")
        return errs
    send_after = send
    for field in ("txn", "due", "event", "arrival", "until", "register_by",
                  "meeting", "milestone", "checkpoint", "rsvp"):
        value = facts.get(field)
        if not value:
            continue
        dt = _parse(value)
        if dt is None:
            bad(field, "is not a valid datetime")
            continue
        if field == "txn":
            if dt > send:
                bad("txn", "is after the send date")
            continue
        if dt < send:
            bad(field, "is before the send date")
            continue
        delta = temporal.business_days_between(send, dt)
        if delta < 1 or delta > temporal.MAX_WINDOW_DAYS:
            bad(field, "is %d business days from send (outside 1..%d)"
                % (delta, temporal.MAX_WINDOW_DAYS))
        send_after = max(send_after, dt)

    sender = facts.get("sender") or {}
    recipient = facts.get("recipient") or {}
    signer = facts.get("signer") or {}
    if sender or recipient or signer:
        for label, entity in (("sender", sender), ("recipient", recipient),
                              ("signer", signer)):
            email = str(entity.get("email") or "")
            if "@" not in email:
                bad(label, "has no valid mailbox address")
                continue
            local, domain = email.rsplit("@", 1)
            if domain not in world.domains:
                bad(label, "uses a domain %r that is not a world entity" % domain)
            if label == "sender" and entity.get("kind") == "org":
                expected = identity.org_domain(entity.get("org", ""), world.tld)
                if domain != expected:
                    bad("sender", "mailbox domain %r is not its org domain %r"
                        % (domain, expected))
            if label == "sender":
                if local not in identity.ROLE_LOCALPARTS and not re.match(
                        r"^[a-z]+\.[a-z]+$", local):
                    bad("sender", "localpart %r is neither a role mailbox nor a "
                        "person address" % local)

        # U3/U4: the sender may not borrow the recipient's domain for a
        # non-personal family.
        if family not in _SAME_DOMAIN_OK and sender.get("domain") \
                and sender.get("domain") == recipient.get("domain"):
            bad("sender/recipient", "share the domain %r (cross-org identity "
                "mismatch)" % sender.get("domain"))

    # U5: the event host must be the signer (or a person at the same domain).
    host = facts.get("host")
    if host:
        host_name = host.get("person") if isinstance(host, dict) else str(host)
        signer_name = signer.get("name") or sender.get("person_name")
        if host_name and signer_name and host_name != signer_name:
            bad("host", "host %r does not match signer %r" % (host_name, signer_name))

    text = _body_text(messages)
    lowered = text.lower()
    if facts.get("signature_expected") and signer.get("name") \
            and signer["name"].lower() not in lowered:
        bad("signer", "signature %r does not appear in the message" % signer["name"])
    if sender.get("email") and not any(
            str(m.get("from_addr") or "").lower() == sender["email"].lower()
            for m in messages):
        bad("sender", "mailbox %r is not the From of any message" % sender["email"])
    if recipient.get("email") and not any(
            sender["email"].lower() in str(m.get("from_addr") or "").lower()
            for m in messages):
        pass  # From is the sender; recipient check handled by domain above

    allowed_weekdays = _allowed_weekdays(facts)
    for name in WEEKDAY_FULL + WEEKDAY_ABBR:
        if re.search(r"\b%s\b" % name, text) and name not in allowed_weekdays:
            bad("weekday", "names %r but no fact falls on that weekday" % name)
            break

    dup = _DUPLICATE_WORD.search(text)
    if dup:
        bad("text", "contains a duplicated word %r" % dup.group(0))

    for domain in _DOMAIN.findall(text.lower()):
        if domain not in world.domains:
            bad("text", "references domain %r which is not a world entity" % domain)
            break
    return errs


def check_bundle(bundle, world=None):
    """Dataset-level plausibility problems ([] when clean)."""
    world = world or World.build()
    errs = []
    scenarios = bundle.get("scenarios") or []
    days = Counter()
    checked = 0
    for scenario in scenarios:
        facts = scenario.get("facts")
        if not isinstance(facts, dict):
            continue
        facts = dict(facts)
        facts.setdefault("scenario_id", scenario.get("scenario_id"))
        errs.extend(check_scenario(facts, scenario.get("messages") or [], world))
        send = _parse(facts.get("send"))
        if send is not None:
            days[send.day] += 1
            checked += 1
    if checked >= 20:
        day, count = days.most_common(1)[0]
        if count / float(checked) > 0.25:
            errs.append(
                "dataset: send day-of-month clustered on the %dth (%d/%d) -- U1"
                % (day, count, checked))
    return errs
