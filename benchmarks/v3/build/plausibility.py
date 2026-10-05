"""Plausibility lint for the synthetic world (WP2, W2-1..W2-4).

Two layers:

* ``check_scenario`` validates the structured facts and the source message --
  temporal ordering/bounds, event season against the actual held date, sender /
  recipient / signer identity, membership, catalog compatibility.
* ``check_case`` validates the **rendered** model input against the declared
  facts per message -- the header date, From/To envelope, every printed calendar
  date and weekday, the signer, and the bound catalog object. Quoted/clip
  variants declare how much of the source is projected, so legitimate quoted
  boilerplate is not falsely rejected and the lint is never disabled.

Failures name the offending scenario/case and field.
"""
import re
from collections import Counter

from . import identity, recipes, temporal
from .world import World

WEEKDAY_FULL = temporal.WEEKDAY_NAMES
WEEKDAY_ABBR = tuple(n[:3] for n in WEEKDAY_FULL)
MONTH = ("January|February|March|April|May|June|July|August|September|October|"
         "November|December")
_TIME_FIELDS = ("send", "txn", "due", "event", "arrival", "until", "register_by",
                "meeting", "milestone", "checkpoint", "overdue", "rsvp",
                "deadline_soon", "deadline_late", "deadline_past", "deadline2")
_ORDERED_FIELDS = ("txn", "due", "event", "arrival", "until", "register_by",
                   "meeting", "milestone", "checkpoint", "rsvp")
# A repeated word is a hygiene defect, but the second occurrence must not be the
# local part of an email address ("Quanta Telecom Updates\nupdates@x.com" is a
# footer, not a doubled word), hence the ``(?!@)`` guard.
_DUPLICATE_WORD = re.compile(r"\b([a-z]{3,})\s+\1(?!@)\b", re.I)
_DOMAIN = re.compile(r"@([a-z0-9-]+(?:\.[a-z0-9-]+)*)")
# Rendered calendar dates: full-month forms only (abbreviated header dates are
# checked separately), with an optional weekday.
_DATE_DAY_MONTH = re.compile(r"\b(?:(%s)\s+)?\d{1,2}\s+(%s)\s+\d{4}\b"
                             % ("|".join(WEEKDAY_FULL), MONTH))
_DATE_MONTH_DAY = re.compile(r"\b(?:(%s),\s+)?(%s)\s+\d{1,2},?\s+\d{4}\b"
                             % ("|".join(WEEKDAY_FULL), MONTH))
_HEADER_DATE = re.compile(r"^Date:\s*(.+?)\s*$", re.M)
_HEADER_FROM = re.compile(r"^From:\s*(.+?)\s*$", re.M)
_HEADER_TO = re.compile(r"^To:\s*(.+?)\s*$", re.M)

_SAME_DOMAIN_OK = {"meeting_request", "personal_invitation"}

# Phrases that assert an out-of-band fact (a CC/team copy, a parallel
# workstream, or a prior exchange). They are only allowed when the scenario
# declares the corresponding fact; no current scenario declares one, so these
# phrases are rejected by default rather than blanket-banned by a dead flag.
_CLAIM_PHRASES = (
    ("copied the wider team", "cc_recipients"),
    ("wider team", "cc_recipients"),
    ("other workstream", "workstream"),
    ("earlier exchange", "history"),
    ("followed this thread", "history"),
    ("as we discussed earlier", "history"),
)
_CLAUSE_OPENERS = ("We", "This", "There", "Please", "Let", "Hope", "Thank",
                   "Kindly", "Our", "Your", "If", "See")
_DOUBLED_PUNCT = re.compile(r"[.!?]{2,}")
_LOWER_AFTER_TERM = re.compile(r"[.!?][ \t]+([a-z])")
# Common abbreviations that legitimately precede a lowercase word.
_ABBREV = {"co", "ltd", "inc", "corp", "plc", "gmbh", "etc", "vs", "no", "st",
           "dr", "mr", "mrs", "ms", "e.g", "i.e", "p.o"}
_MID_SENTENCE_OPENER = re.compile(r"[a-z,][ \t]+(%s)\b" % "|".join(_CLAUSE_OPENERS))


def _lowercase_after_terminator(text):
    for match in _LOWER_AFTER_TERM.finditer(text):
        head = text[:match.start() + 1]
        tokens = head.split()
        prev = tokens[-1].strip(".,!?;:").lower() if tokens else ""
        if prev not in _ABBREV:
            return True
    return False


def _parse(value):
    try:
        return temporal.parse(value) if value else None
    except (TypeError, ValueError):
        return None


def _region_dict(facts):
    key = facts.get("region")
    for region in recipes.regions():
        if region["region"] == key:
            return region
    return recipes.regions()[0]


def _fact_dates(facts):
    out = []
    for field in _TIME_FIELDS:
        dt = _parse(facts.get(field))
        if dt is not None:
            out.append((field, dt))
    return out


def _normalize_date(text):
    text = re.sub(r"\s+", " ", text.replace(",", ", ")).strip()
    return re.sub(r"\b0(\d)\b", r"\1", text)


def _allowed_date_map(facts):
    region = _region_dict(facts)
    styles = [region, {"date_style": "day_month"}, {"date_style": "month_day"}]
    allowed = {}
    for _field, dt in _fact_dates(facts):
        for style in styles:
            allowed[_normalize_date(temporal.format_date(dt, style))] = dt
            allowed[_normalize_date(temporal.format_deadline(dt, style))] = dt
    return allowed


def _extract_dates(text):
    found = []
    for pattern in (_DATE_DAY_MONTH, _DATE_MONTH_DAY):
        for match in pattern.finditer(text):
            found.append((_normalize_date(match.group(0)), match.group(1)))
    return found


def _allowed_weekdays(facts):
    allowed = set()
    for _field, dt in _fact_dates(facts):
        allowed.add(WEEKDAY_FULL[dt.weekday()])
        allowed.add(WEEKDAY_ABBR[dt.weekday()])
    return allowed


def check_scenario(facts, messages, world):
    """Structured-facts + source-message plausibility ([] when clean)."""
    errs = []
    sid = facts.get("scenario_id", "?")
    family = facts.get("family")

    def bad(field, message):
        errs.append("scenario %s: %s %s" % (sid, field, message))

    if not facts.get("send"):
        bad("send", "missing an absolute send datetime")
        return errs
    send = _parse(facts["send"])
    if send is None:
        bad("send", "is not a valid datetime")
        return errs
    for field in _ORDERED_FIELDS:
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
        # rsvp / register_by must precede the event they answer for.
        if field in ("rsvp", "register_by") and facts.get("event"):
            if dt >= _parse(facts["event"]):
                bad(field, "is not before the event date")

    # W2-1: the named event's season must match the actual held date.
    if facts.get("event") and facts.get("event_season"):
        held = _parse(facts["event"])
        season = temporal.season_of(held, {"region": facts.get("region")})
        if season != facts["event_season"]:
            bad("event", "named as %r but the held date is %s"
                % (facts["event_season"], season))

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
        # W2-3: the sender must be a real member of the org it claims, holding
        # the role it claims.
        if sender.get("kind") == "org":
            org_id = sender.get("org_id")
            org = world.org_by_id.get(org_id)
            if org is None:
                bad("sender", "org membership %r is not a world org" % org_id)
            else:
                if sender["domain"] != org["domain"]:
                    bad("sender", "domain does not belong to its declared org")
                role = sender.get("role")
                if role and role not in (org.get("role_mailboxes") or {}):
                    bad("sender", "role %r is not held by %s" % (role, org["name"]))
                if facts.get("catalog_item"):
                    terms = {t for bucket in (org.get("catalog") or {}).values()
                             for t in bucket}
                    if facts["catalog_item"] not in terms:
                        bad("catalog", "object %r is not in %s's catalog"
                            % (facts["catalog_item"], org["name"]))
        if sender.get("kind") == "person" and sender.get("role") == "Colleague":
            owner_org = next((o for o in world.owners.values()
                              if o["org"] == sender.get("org")), None)
            if owner_org is None or sender.get("domain") != owner_org["domain"]:
                bad("sender", "colleague is not a member of the owner's org")
        if family not in _SAME_DOMAIN_OK and sender.get("domain") \
                and sender.get("domain") == recipient.get("domain"):
            bad("sender/recipient", "share the domain %r (cross-org identity "
                "mismatch)" % sender.get("domain"))

    host = facts.get("host")
    if host:
        host_name = host.get("person") if isinstance(host, dict) else str(host)
        signer_name = signer.get("name") or sender.get("person_name")
        if host_name and signer_name and host_name != signer_name:
            bad("host", "host %r does not match signer %r" % (host_name, signer_name))

    for message in messages:
        text = "%s\n%s\n%s\n%s" % (message.get("subject") or "",
                                   message.get("from_addr") or "",
                                   message.get("to_addr") or "",
                                   message.get("body") or message.get("snippet") or "")
        errs.extend(_check_text(text, facts, world, "scenario " + sid, quoted=False))
        if sender.get("email") and str(message.get("from_addr") or "").lower() \
                != sender["email"].lower():
            bad("sender", "source From is not the declared sender")
        if recipient.get("email") and str(message.get("to_addr") or "").lower() \
                != recipient["email"].lower():
            bad("recipient", "source To is not the declared recipient")
        header = _HEADER_DATE.search(text)
        if header and header.group(1).strip() != temporal.format_header(send):
            bad("header date", "does not match the send datetime")
    return errs


def _check_text(text, facts, world, label, quoted, expect=None):
    """Shared rendered-text checks: dates, weekdays, signer, catalog.

    ``expect`` carries the per-message projection (which template tokens this
    message actually rendered); it defaults to the scenario-level flags.
    """
    expect = expect or {
        "item": facts.get("item_expected"),
        "service": facts.get("service_expected"),
        "signer": facts.get("signature_expected"),
    }
    errs = []
    allowed_dates = _allowed_date_map(facts)
    for rendered, weekday in _extract_dates(text):
        dt = allowed_dates.get(rendered)
        if dt is None:
            errs.append("%s: rendered date %r does not match any declared fact"
                        % (label, rendered))
            continue
        # A weekday printed next to a date must be that date's weekday, so a
        # weekday swapped in from another fact is rejected.
        if weekday:
            expected = WEEKDAY_FULL[dt.weekday()]
            if weekday not in (expected, expected[:3]):
                errs.append("%s: weekday %r does not match date %r (%s)"
                            % (label, weekday, rendered, expected))
    allowed_weekdays = _allowed_weekdays(facts)
    if quoted:
        allowed_weekdays |= set(facts.get("clip_quoted_weekdays") or [])
    for name in WEEKDAY_FULL + WEEKDAY_ABBR:
        if re.search(r"\b%s\b" % name, text) and name not in allowed_weekdays:
            errs.append("%s: weekday %r does not match any declared fact"
                        % (label, name))
            break
    lowered = text.lower()
    for phrase, key in _CLAIM_PHRASES:
        if phrase in lowered and not facts.get(key):
            errs.append("%s: unbacked claim %r (no declared %r fact)"
                        % (label, phrase, key))
            break
    if not quoted:
        if _DOUBLED_PUNCT.search(text):
            errs.append("%s: doubled terminal punctuation" % label)
        if _lowercase_after_terminator(text):
            errs.append("%s: lowercase after a sentence terminator" % label)
        opener = _MID_SENTENCE_OPENER.search(text)
        if opener:
            errs.append("%s: clause opener %r injected mid-sentence"
                        % (label, opener.group(1)))
    if not quoted and expect.get("signer") and (facts.get("signer") or {}).get("name") \
            and facts["signer"]["name"].lower() not in lowered:
        errs.append("%s: signature %r does not appear" % (label, facts["signer"]["name"]))
    if not quoted and expect.get("item") and facts.get("catalog_item") \
            and facts["catalog_item"].lower() not in lowered:
        errs.append("%s: catalog object %r does not appear"
                    % (label, facts["catalog_item"]))
    if not quoted and expect.get("service") and facts.get("catalog_bucket") == "services" \
            and facts.get("catalog_item") and facts["catalog_item"].lower() not in lowered:
        errs.append("%s: catalog service %r does not appear"
                    % (label, facts["catalog_item"]))
    dup = _DUPLICATE_WORD.search(text)
    if dup:
        errs.append("%s: duplicated word %r" % (label, dup.group(0)))
    if not quoted:
        for domain in _DOMAIN.findall(lowered):
            if domain not in world.domains:
                errs.append("%s: references domain %r which is not a world entity"
                            % (label, domain))
                break
    return errs


def check_case(case, facts, world):
    """Rendered model-input plausibility for one case ([] when clean)."""
    rendered = case.get("rendered_input") or {}
    text = rendered.get("user") or ""
    label = "case %s" % case.get("case_id")
    role = (case.get("relation") or {}).get("relation_type")
    quoted = role == "clip_variant"
    errs = _check_text(text, facts, world, label, quoted,
                       expect=case.get("audit") or None)
    if not text:
        return errs
    send = _parse(facts.get("send"))
    header = _HEADER_DATE.search(text)
    if header and send is not None and header.group(1).strip() != temporal.format_header(send):
        errs.append("%s: rendered header date does not match the send datetime" % label)
    frm = _HEADER_FROM.search(text)
    sender_email = str((facts.get("sender") or {}).get("email") or "").lower()
    if frm and sender_email and frm.group(1).strip().lower() != sender_email:
        errs.append("%s: rendered From does not match the declared sender" % label)
    to = _HEADER_TO.search(text)
    recipient = facts.get("recipient") or {}
    if to and recipient.get("email"):
        got = to.group(1).strip().lower()
        expected = recipient["email"].lower()
        team = "team@%s" % recipient.get("domain", "").lower()
        if got not in (expected, team):
            errs.append("%s: rendered To %r is neither the recipient %r nor its "
                        "team alias" % (label, got, expected))
    return errs


def check_bundle(bundle, world=None):
    """Dataset-level plausibility problems ([] when clean)."""
    world = world or World.build()
    errs = []
    days = Counter()
    checked = 0
    facts_by_scenario = {}
    for scenario in bundle.get("scenarios") or []:
        facts = scenario.get("facts")
        if not isinstance(facts, dict):
            continue
        facts = dict(facts)
        facts.setdefault("scenario_id", scenario.get("scenario_id"))
        facts_by_scenario[scenario.get("scenario_id")] = facts
        errs.extend(check_scenario(facts, scenario.get("messages") or [], world))
        send = _parse(facts.get("send"))
        if send is not None:
            days[send.day] += 1
            checked += 1
    for case in bundle.get("cases") or []:
        facts = facts_by_scenario.get(case.get("scenario_id"))
        if facts is None:
            continue
        errs.extend(check_case(case, facts, world))
    if checked >= 20:
        day, count = days.most_common(1)[0]
        if count / float(checked) > 0.25:
            errs.append("dataset: send day-of-month clustered on the %dth (%d/%d)"
                        % (day, count, checked))
    return errs
