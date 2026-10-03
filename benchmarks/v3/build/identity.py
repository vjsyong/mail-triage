"""Identity construction for the synthetic world (WP2 world model).

Every mailbox address is derived from an authored entity: an organization's
name becomes its own domain (legal suffixes stripped) and role mailboxes live on
that domain, and a person's address lives on their own organization's domain.
No address is ever built from another entity's domain, which is what makes the
cross-org/identity defects impossible to emit.
"""
import re
import unicodedata

# Trailing tokens that are legal/company words rather than part of the identity.
_LEGAL_SUFFIXES = {
    "ltd", "limited", "inc", "incorporated", "llc", "llp", "plc", "gmbh",
    "sarl", "sas", "bv", "nv", "ag", "sa", "spa", "co", "corp", "corporation",
    "company", "group", "holdings", "partners", "associates",
}

ROLE_LOCALPARTS = {
    "billing", "support", "orders", "no-reply", "accounts", "security",
    "tracking", "bookings", "appointments", "claims", "offers", "subscribe",
    "registry", "office", "events", "volunteers", "boxoffice", "services",
}


def ascii_fold(text):
    normalized = unicodedata.normalize("NFKD", text or "")
    return normalized.encode("ascii", "ignore").decode("ascii")


def slugify(text):
    """A lower-case hyphenated slug safe for a DNS label."""
    folded = ascii_fold(text).lower()
    folded = folded.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-") or "x"


def org_slug(name):
    """Organization slug with legal suffixes and connectors removed.

    ``"Cedar & Co."`` -> ``"cedar"``; ``"The Maker Society"`` ->
    ``"maker-society"``; ``"Northwind Supplies"`` -> ``"northwind-supplies"``.
    """
    tokens = [t for t in slugify(name).split("-") if t and t not in ("and",)]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    return "-".join(tokens) or "org"


def org_domain(name, tld):
    return "%s.%s" % (org_slug(name), tld)


def person_localpart(given, family):
    return slugify("%s %s" % (given, family)).replace("-", ".")


def person_email(given, family, domain):
    return "%s@%s" % (person_localpart(given, family), domain)


def role_email(role, domain):
    return "%s@%s" % (role, domain)


def display_name(given, family):
    return ("%s %s" % (given, family)).strip()


def signature_block(person):
    lines = list(person.get("signature_lines") or [])
    if not lines:
        lines = [display_name(person["given"], person["family"])]
    return "\n".join(lines)
