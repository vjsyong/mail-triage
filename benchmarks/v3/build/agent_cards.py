"""Agent cards derived from the world entities (WP6 pilot, item 1).

A card is the stable writing persona for one synthetic sender: identity, role,
organisation, the recipient relationship, locale, and a personality/writing-style
seed.  Cards are derived deterministically from the world entities (never
invented per message), so two messages from the same sender share a voice.

The style can be *authored* (the role/relationship descriptor below) or
*LLM-generated once and cached per agent* (``ensure_style`` with a client).
Prompts built from a card describe the voice; they never prescribe reusable
sentence templates.
"""
import hashlib

from .rng import stream

# Role-grounded voice descriptors for organisational senders.  These describe a
# register/tone, not sentences to copy.
ROLE_STYLE = {
    "billing": "precise, factual accounts register; states amounts and reference "
               "numbers plainly; no small talk",
    "accounts": "formal accounts register; short, unambiguous, reference-driven",
    "support": "helpful support register; acknowledges the issue, gives a status "
               "and the next step",
    "no-reply": "automated system voice; declarative, no greeting-first warmth, "
                "explicitly states whether a reply is expected",
    "offers": "retail marketing register; upbeat but specific about the offer and "
              "its window",
    "bookings": "travel/logistics register; concrete about times, places and "
                "references",
    "subscribe": "newsletter register; editorial, scannable, links to the issue",
    "security": "security-notice register; sober and specific about the event, "
                "the time and the recommended action",
    "orders": "order-desk register; confirms what was ordered and the order "
              "reference",
}

# Relationship-grounded descriptors for individual (person) senders.
PERSON_STYLE = {
    "colleague": "collegial and direct; assumes shared context but restates the ask",
    "friend": "warm and informal; personal, no business boilerplate",
    "parent": "practical parent-school register; friendly and specific",
    "customer": "polite customer register; states what is needed and by when",
    "client": "professional client register; clear about scope and timing",
    "student": "student register; earnest and specific",
    "tenant": "tenant register; practical and courteous",
    "subscriber": "reader register; brief and appreciative",
}

DEFAULT_STYLE = ("clear, natural business correspondence; specific about the "
                 "purpose and any action or date")


def _hash(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def agent_key(sender, relationship):
    """Stable identity for a sender, independent of the specific message."""
    if sender.get("kind") == "org":
        return "org:%s:%s" % (sender.get("org_id") or sender.get("org"),
                              sender.get("role") or "")
    return "person:%s:%s" % (sender.get("email") or sender.get("person_name"),
                             relationship or "")


def _style_descriptor(sender, relationship, rng):
    if sender.get("kind") == "org":
        pool = [ROLE_STYLE.get(sender.get("role") or "")]
        base = next((p for p in pool if p), None)
        if base is None:
            base = rng.pick(list(ROLE_STYLE.values()) or [DEFAULT_STYLE])
    else:
        base = PERSON_STYLE.get(relationship or "") or DEFAULT_STYLE
    # A bounded secondary flourish keeps two same-role agents distinguishable
    # without ever becoming a reusable sentence template.
    flourishes = [
        "writes in short paragraphs",
        "occasionally uses a one-line opener",
        "keeps signatures minimal",
        "prefers explicit time references",
        "avoids exclamation marks",
        "uses British spelling" ,
    ]
    return "%s; %s" % (base, rng.pick(flourishes))


def build_card(sender, recipient, relationship, region, *, seed=0):
    """Deterministically derive an agent card from world entities."""
    rng = stream(seed, "agentcard:%s" % agent_key(sender, relationship))
    locale = (region or {}).get("locale") or (region or {}).get("region") or "en"
    return {
        "agent_key": agent_key(sender, relationship),
        "kind": sender.get("kind"),
        "name": sender.get("person_name") or sender.get("org"),
        "email": sender.get("email"),
        "role": sender.get("role"),
        "org": sender.get("org"),
        "org_id": sender.get("org_id"),
        "domain": sender.get("domain"),
        "relationship": relationship,
        "region": (region or {}).get("region"),
        "locale": locale,
        "timezone": (region or {}).get("timezone"),
        "style_seed": _style_descriptor(sender, relationship, rng),
        "style_id": _hash(agent_key(sender, relationship), seed),
    }


def authored_style_text(card):
    """The deterministic authored fallback style block (no LLM call)."""
    return card["style_seed"]


def ensure_style(card, client=None, cache=None, *, temperature=0.7, seed=None):
    """Return the agent's style text, generating it once and caching per agent.

    With no client this is the authored seed (always available offline).  With a
    client the style is generated once per ``agent_key`` and cached, so repeated
    messages from the same sender reuse one voice without extra calls.
    """
    if client is None:
        return authored_style_text(card)
    key_extra = "agent_style:%s" % card["agent_key"]
    messages = [
        {"role": "system",
         "content": ("You define the writing voice of a fictional email sender. "
                     "Describe the tone, register and formatting habits in 2-3 "
                     "sentences. Do not write example sentences and do not "
                     "prescribe reusable wording.")},
        {"role": "user",
         "content": ("Sender: %s\nRole: %s\nOrganisation: %s\nRelationship to "
                     "recipient: %s\nRegion: %s\nAuthored style seed: %s"
                     % (card.get("name"), card.get("role") or "-",
                        card.get("org") or "-", card.get("relationship") or "-",
                        card.get("region") or "-", card.get("style_seed")))},
    ]
    resp = client.chat(messages, temperature=temperature, seed=seed,
                       max_tokens=160, candidate_index=0, extra_key=key_extra)
    text = (resp.get("content") or "").strip()
    return text or authored_style_text(card)
