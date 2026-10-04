"""Scenario -> brief compilation for the LLM rendering path (WP6, item 2).

A *brief* is the authoritative, seeded-Python-RNG specification of one email:
who writes it, to whom, the exact absolute datetimes (random hour **and**
minute), the amount/currency, the document reference, the rendering-relevant
facts, and the facts the rendered text must contain or must never contain.

Nothing in a brief is produced by the LLM.  The renderer may only express the
brief; the verifier (``verify.py``) rejects any message that contradicts it.
Reference ids are random alphanumerics with a per-document-type prefix and are
unique corpus-wide.
"""
import re
from datetime import timedelta

from . import recipes, temporal
from .rng import stream

# family -> (document_type, prefix style).  A prefix is never reused for a
# different purpose: invoices use INV, bills BILL, receipts RCPT, orders ORD,
# tickets TKT, cases CASE, policies POL, and tracking numbers are carrier-like
# (1Z... / SF...), never invoice-like.
DOC_TYPE = {
    "receipt_confirmation": ("receipt", "RCPT"),
    "refund_status": ("receipt", "RCPT"),
    "invoice_receipt": ("invoice", "INV"),
    "payment_reminder": ("bill", "BILL"),
    "order_request": ("order", "ORD"),
    "shipping_travel_update": ("tracking", "TRACK"),
    "support_exchange": ("ticket", "TKT"),
    "operational_alert": ("case", "CASE"),
    "security_notification": ("case", "CASE"),
    "project_request": ("case", "CASE"),
    "project_status": ("case", "CASE"),
    "document_request": ("policy", "POL"),
}

# Money / deadline / identity obligations.
MONEY_FAMILIES = {
    "receipt_confirmation", "invoice_receipt", "payment_reminder",
    "refund_status", "order_request", "request_approval", "legitimate_promo",
    "ambiguous_marketing", "project_request",
}
RECEIVED_FAMILIES = {"receipt_confirmation", "refund_status"}
OUTSTANDING_FAMILIES = {"payment_reminder", "invoice_receipt"}
SOCIAL_FAMILIES = {"personal_invitation", "school_community",
                   "event_registration", "meeting_request"}
PERSONAL_FAMILIES = SOCIAL_FAMILIES | {"newsletter_digest", "legitimate_promo",
                                       "ambiguous_marketing", "suspicious_phishing"}

_TIME_FIELDS = ("send", "txn", "due", "event", "arrival", "until", "register_by",
                "meeting", "milestone", "checkpoint", "overdue", "rsvp",
                "deadline_soon", "deadline_late", "deadline_past", "deadline2")

_RECEIVED_FORBIDDEN = (
    "if already paid", "if you have already paid", "if payment has already",
    "disregard this notice if payment", "ignore this notice if payment",
    "already been arranged", "if your payment has already",
)
_OUTSTANDING_FORBIDDEN = (
    "we have received", "we've received", "payment received",
    "your payment has been received", "thank you for your payment",
    "thanks for your payment", "we received your payment",
    "your payment was received",
)

_ALNUM = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class RefAllocator(object):
    """Deterministic, corpus-wide-unique reference id allocator."""

    def __init__(self, seed=0, salt="pilot"):
        self._rng = stream(seed, "refs:%s" % salt)
        self.used = set()

    def _token(self, n):
        return "".join(_ALNUM[self._rng.randint(len(_ALNUM))] for _ in range(n))

    def allocate(self, kind):
        """Return a fresh id for ``kind`` (never repeats corpus-wide)."""
        for _ in range(200):
            if kind == "TRACK":
                # carrier-like, never invoice-like
                ident = ("1Z" + self._token(16)) if self._rng.randint(2) == 0 \
                    else ("SF" + "".join(str(self._rng.randint(10))
                                         for _ in range(12)))
            elif kind == "invoice":
                ident = "INV-" + self._token(7)
            elif kind is None:
                return None
            else:
                ident = "%s-%s" % (kind, self._token(7))
            if ident not in self.used:
                self.used.add(ident)
                return ident
        raise RuntimeError("reference allocator exhausted")

    def alloc_for_family(self, family_id):
        spec = DOC_TYPE.get(family_id)
        if not spec:
            return None
        doc_type, prefix = spec
        return {"type": doc_type, "id": self.allocate(prefix)}


def _fmt_date(dt, region):
    return temporal.format_date(dt, region)


def _fmt_deadline(dt, region):
    return temporal.format_deadline(dt, region)


def retime_facts(facts, family_id, rng, config):
    """Translate every datetime fact by one delta.

    The delta sets a random hour (business vs personal pool) **and a fully
    random minute** (never only :00/:15/:30), and puts social mail on a weekend.
    A pure translation preserves every date ordering, so the world's temporal
    relationships still hold.
    """
    send = temporal.parse(facts["send"])
    if family_id in PERSONAL_FAMILIES:
        hours = [17, 18, 19, 20, 21]
    else:
        hours = list((config.get("business") or {}).get("send_hours")
                     or [8, 9, 10, 14, 16])
    target = send.replace(hour=int(rng.pick(hours)), minute=int(rng.randint(60)),
                          second=0, microsecond=0)
    if family_id in SOCIAL_FAMILIES:
        while target.weekday() not in (5, 6):
            target = target + timedelta(days=1)
    else:
        while target.weekday() not in (0, 1, 2, 3, 4):
            target = target + timedelta(days=1)
    delta = target - send
    out = dict(facts)
    for field in _TIME_FIELDS:
        value = facts.get(field)
        if not value:
            continue
        try:
            out[field] = (temporal.parse(value) + delta).isoformat()
        except (TypeError, ValueError):
            pass
    out["send"] = target.isoformat()
    return out, delta


def _deadline_keys(family_id):
    spec = dict(temporal.WINDOWS.get(family_id, {}))
    override = {"invoice_receipt": "due"}
    if family_id in override:
        spec.setdefault(override[family_id], (1, 60))
    return spec


def _primary_deadline_field(facts, family_id):
    spec = _deadline_keys(family_id)
    for key in ("due", "arrival", "until", "meeting", "checkpoint", "milestone"):
        if key in spec and facts.get(key):
            return key
    return None


def _secondary_deadline_field(facts, family_id):
    primary = _primary_deadline_field(facts, family_id)
    for key in ("milestone", "deadline2", "overdue", "register_by", "rsvp"):
        if facts.get(key) and key != primary:
            return key
    return None


def build_brief(*, family_id, slots, facts, region, persona, allocator,
                rng, thread=None, config=None):
    """Compile a deterministic brief from world scenario facts.

    ``facts`` must already have been retimed (see :func:`retime_facts`).
    """
    send = temporal.parse(facts["send"])
    region = region or {}
    keys = set(_deadline_keys(family_id)) | {"send"}
    if facts.get("txn"):
        keys.add("txn")
    dates = {}
    for field in sorted(keys):
        value = facts.get(field)
        if not value:
            continue
        try:
            dt = temporal.parse(value)
        except (TypeError, ValueError):
            continue
        dates[field] = {
            "iso": dt.isoformat(),
            "date": _fmt_date(dt, region),
            "deadline": _fmt_deadline(dt, region),
            "weekday": temporal.WEEKDAY_NAMES[dt.weekday()],
            "time": dt.strftime("%H:%M"),
        }

    family = recipes.families().get(family_id) or {}
    money = bool(slots.get("amount")) and family_id in MONEY_FAMILIES
    reference = allocator.alloc_for_family(family_id)
    primary = _primary_deadline_field(facts, family_id)
    signature_expected = bool(facts.get("signature_expected"))
    item = facts.get("catalog_item") if (facts.get("item_expected")
                                         or facts.get("service_expected")) else None
    item = item or slots.get("item")

    required = []          # (label, exact token that must appear)
    if money:
        required.append(("amount", slots.get("amount")))
    if reference:
        required.append(("reference", reference["id"]))
    if primary and dates.get(primary):
        required.append(("deadline", dates[primary]["deadline"]))
    if signature_expected and (facts.get("signer") or {}).get("name"):
        required.append(("signer", facts["signer"]["name"]))
    if item and family_id not in ("newsletter_digest", "security_notification"):
        required.append(("object", item))
    if dates.get("event"):
        if facts.get("event_name"):
            required.append(("event_name", facts["event_name"]))
        if facts.get("venue"):
            required.append(("venue", facts["venue"]))
    login = None
    if family_id == "security_notification":
        # The sign-in notice must carry the login time (and place).
        login_dt = send - timedelta(minutes=7)
        login = {"time": login_dt.strftime("%H:%M"),
                 "date": _fmt_date(login_dt, region),
                 "place": facts.get("venue") or "an unrecognised device"}
        required.append(("login_time", login["time"]))

    forbidden = []
    if family_id in RECEIVED_FAMILIES:
        forbidden.extend(_RECEIVED_FORBIDDEN)
    if family_id in OUTSTANDING_FAMILIES:
        forbidden.extend(_OUTSTANDING_FORBIDDEN)
    if thread:
        state = (thread.get("state") or "").lower()
        if state == "resolved":
            forbidden.extend(("outstanding", "still due", "overdue",
                              "action is required", "please pay"))
        elif state == "awaiting_reply":
            forbidden.extend(("you already approved", "as you confirmed"))

    return {
        "brief_id": "brf_%s_%s" % (persona.get("persona", "p"), family_id),
        "family": family_id,
        "doc_type": (reference or {}).get("type"),
        "persona": persona.get("persona"),
        "relationship": facts.get("relationship"),
        "sender": {
            "name": (facts.get("signer") or {}).get("name"),
            "email": (facts.get("sender") or {}).get("email"),
            "role": (facts.get("sender") or {}).get("role"),
            "org": (facts.get("sender") or {}).get("org"),
            "domain": (facts.get("sender") or {}).get("domain"),
        },
        "recipient": {
            "name": (facts.get("recipient") or {}).get("name"),
            "email": (facts.get("recipient") or {}).get("email"),
            "domain": (facts.get("recipient") or {}).get("domain"),
        },
        "signer": dict(facts.get("signer") or {}),
        "send": {
            "iso": send.isoformat(),
            "header": temporal.format_header(send),
            "date": _fmt_date(send, region),
            "time": send.strftime("%H:%M"),
            "weekday": temporal.WEEKDAY_NAMES[send.weekday()],
        },
        "dates": dates,
        "primary_deadline": primary,
        "secondary_deadline": _secondary_deadline_field(facts, family_id),
        "amount": slots.get("amount") if money else None,
        "currency": region.get("currency"),
        "reference": reference,
        "object": {
            "bucket": facts.get("catalog_bucket"),
            "term": item,
        },
        "event_name": facts.get("event_name"),
        "venue": facts.get("venue"),
        "locale": region.get("locale"),
        "region": region.get("region"),
        "spelling": region.get("spelling"),
        "login": login,
        "required_facts": [f for f in required if f[1]],
        "forbidden_facts": list(dict.fromkeys(forbidden)),
        "thread": dict(thread) if thread else None,
        "facts_ref": facts,
    }


def split_by_document_type(text):
    """Return the set of reference prefixes found in rendered text."""
    prefixes = set()
    for prefix in ("INV", "BILL", "RCPT", "ORD", "TKT", "CASE", "POL"):
        if re.search(r"\b%s-[A-Z0-9]" % prefix, text):
            prefixes.add(prefix)
    if re.search(r"\b1Z[A-Z0-9]{6,}\b", text) or re.search(r"\bSF\d{8,}\b", text):
        prefixes.add("TRACK")
    return prefixes
