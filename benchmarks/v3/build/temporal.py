"""Temporal engine for the synthetic world (WP2 world model).

Every message is anchored to an absolute datetime. Named weekdays and deadlines
are always *derived* from that datetime (never hardcoded), deadlines fall inside
a bounded business-day window measured from the send date, and dates are
formatted per the world region's locale. The old fixed "12 September" vocabulary
cannot be expressed here: there is no date string anywhere in the fixtures.
"""
from datetime import datetime, timedelta

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December")

# Relative business-day windows per family, measured from the send date.
# ``due`` is the primary reply/payment deadline, ``event``/``arrival``/``until``
# is the secondary named date, and ``txn`` places a receipt/invoice transaction
# date relative to the send. All offsets are bounded and explainable.
WINDOWS = {
    "request_approval": {"due": (3, 7)},
    "receipt_confirmation": {"txn": (-3, 0)},
    "payment_reminder": {"due": (10, 20), "overdue": (3, 10)},
    "invoice_receipt": {"txn": (-2, 0)},
    "refund_status": {"txn": (-5, -1)},
    "newsletter_digest": {},
    "ambiguous_marketing": {"until": (7, 21)},
    "legitimate_promo": {"until": (7, 21)},
    "security_notification": {},
    "shipping_travel_update": {"arrival": (2, 7)},
    "support_exchange": {"due": (2, 5)},
    "project_request": {"due": (3, 7), "milestone": (10, 25)},
    "project_status": {"checkpoint": (5, 10)},
    "personal_invitation": {"event": (5, 21), "rsvp": (2, 6)},
    "school_community": {"event": (7, 28), "rsvp": (2, 5)},
    "operational_alert": {},
    "suspicious_phishing": {"until": (1, 2)},
    "meeting_request": {"meeting": (2, 10)},
    "order_request": {"due": (2, 5)},
    "document_request": {"due": (2, 5)},
    "event_registration": {"event": (12, 40), "register_by": (3, 10)},
}

DEFAULT_DUE = (3, 7)
MAX_WINDOW_DAYS = 60


def parse(value):
    return datetime.fromisoformat(value)


def _weekday_ok(day, weekdays):
    return day.weekday() in weekdays


def add_business_days(dt, days, weekdays=(0, 1, 2, 3, 4)):
    """Move ``days`` business days from ``dt`` (0 = next business day)."""
    step = 1 if days >= 0 else -1
    remaining = abs(int(days))
    out = dt
    while remaining > 0:
        out = out + timedelta(days=step)
        if _weekday_ok(out, weekdays):
            remaining -= 1
    if days == 0:
        while not _weekday_ok(out, weekdays):
            out = out + timedelta(days=step)
    return out


def business_days_between(a, b, weekdays=(0, 1, 2, 3, 4)):
    """Signed count of business days from ``a`` to ``b``."""
    if a == b:
        return 0
    step = 1 if b > a else -1
    out = 0
    cur = a
    while cur.date() != b.date():
        cur = cur + timedelta(days=step)
        if _weekday_ok(cur, weekdays):
            out += step
    return out


def format_header(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def format_date(dt, region):
    name = MONTH_NAMES[dt.month - 1]
    if region.get("date_style") == "month_day":
        return "%s %d, %d" % (name, dt.day, dt.year)
    return "%d %s %d" % (dt.day, name, dt.year)


def format_deadline(dt, region):
    name = MONTH_NAMES[dt.month - 1]
    weekday = WEEKDAY_NAMES[dt.weekday()]
    if region.get("date_style") == "month_day":
        return "%s, %s %d, %d" % (weekday, name, dt.day, dt.year)
    return "%s %d %s %d" % (weekday, dt.day, name, dt.year)


def season_of(dt, region):
    """Hemisphere-aware season for a date in a region."""
    month = dt.month
    southern = region.get("region", "").startswith("au")
    if southern:
        boundaries = {12: "summer", 1: "summer", 2: "summer",
                      3: "autumn", 4: "autumn", 5: "autumn",
                      6: "winter", 7: "winter", 8: "winter",
                      9: "spring", 10: "spring", 11: "spring"}
    else:
        boundaries = {12: "winter", 1: "winter", 2: "winter",
                      3: "spring", 4: "spring", 5: "spring",
                      6: "summer", 7: "summer", 8: "summer",
                      9: "autumn", 10: "autumn", 11: "autumn"}
    return boundaries[month]


def offset_between(lo, hi, pick):
    """A deterministic integer in ``[lo, hi]`` using a callable picker."""
    if hi <= lo:
        return lo
    return lo + pick(hi - lo + 1)
