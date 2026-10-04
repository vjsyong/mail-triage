"""LLM rendering path: brief + agent card -> email (WP6, item 3).

The renderer asks an OpenAI-compatible chat endpoint for a complete email that
expresses one brief, generates ``n_candidates`` completions, and accepts the
first candidate that passes the deterministic verifier.  When no candidate
passes, the verifier's reasons are fed back and the render is retried, bounded by
``max_retries``.  A message is never accepted by weakening a check: if every
candidate is rejected the render raises.

Prompts describe the sender's voice and the facts to convey.  They never
prescribe reusable sentence templates.
"""
import json
import re

from .rng import stream
from .verify import verify_message, build_message


class RenderRejected(RuntimeError):
    """Raised when no candidate passes the deterministic verifier."""

    def __init__(self, brief_id, problems, candidates=None):
        self.brief_id = brief_id
        self.problems = list(problems or [])
        self.candidates = list(candidates or [])
        super(RenderRejected, self).__init__(
            "no acceptable render for %s after retries: %s"
            % (brief_id, "; ".join(self.problems[:6])))


# One-line, non-templated purpose per family.  These say *what the email is for*,
# never how to word it.
FAMILY_PURPOSE = {
    "receipt_confirmation": "confirm a payment the sender has received and issue "
                            "a receipt; no reply is expected",
    "invoice_receipt": "issue an invoice that is still outstanding and tell the "
                       "recipient how and by when to pay",
    "payment_reminder": "remind the recipient that a payment is still outstanding "
                        "and give the due date",
    "refund_status": "tell the recipient the status of their refund",
    "order_request": "confirm an order and its reference",
    "shipping_travel_update": "give a shipment or travel update with a tracking "
                              "reference and an arrival date",
    "support_exchange": "respond to a support request with a status and next step",
    "operational_alert": "alert the recipient to an operational change or case",
    "security_notification": "warn the recipient about a sign-in event, naming the "
                             "time and place",
    "project_request": "ask the recipient to act on a project matter with a "
                       "reference and a deadline",
    "project_status": "give a project status update with a checkpoint date",
    "document_request": "request or provide a policy/tenancy document",
    "request_approval": "ask the recipient to approve something with a cost and a "
                        "deadline",
    "meeting_request": "propose a meeting at a stated date and ask for a reply",
    "personal_invitation": "invite the recipient to a social event with an RSVP "
                           "date",
    "school_community": "send a school/community notice about an event and its "
                        "RSVP date",
    "event_registration": "invite the recipient to register for an event by a "
                          "registration date",
    "newsletter_digest": "send a newsletter digest",
    "legitimate_promo": "send a legitimate promotional offer with its expiry",
    "ambiguous_marketing": "send a promotional mailing",
    "suspicious_phishing": "send a suspicious message that pressures the recipient",
}


def _system_prompt(brief, card, style_text):
    return (
        "You write one complete email as a specific fictional sender. Match the "
        "sender's voice: %s. Write in natural prose; do not use bullet templates "
        "or placeholder syntax. Every factual token given to you (dates, amounts, "
        "reference numbers, names, times, places) must appear verbatim. Never "
        "invent a fact, a date, a person, a domain, an amount or a reference. "
        "Return ONLY a JSON object of the form {\"subject\": \"...\", "
        "\"body\": \"...\"} with no surrounding commentary."
        % (style_text or "clear and natural")
    )


def _facts_block(brief):
    lines = []
    for label, token in brief.get("required_facts") or []:
        lines.append("- %s: %s" % (label, token))
    if brief.get("login"):
        lines.append("- place: %s" % brief["login"]["place"])
    return "\n".join(lines) or "- (no mandatory tokens)"


def _dates_block(brief):
    lines = []
    for field, entry in sorted((brief.get("dates") or {}).items()):
        if field == "send":
            continue
        lines.append("- %s: %s (%s)" % (field, entry["deadline"], entry["time"]))
    return "\n".join(lines) or "- (none)"


def _thread_block(brief, prior_messages):
    thread = brief.get("thread")
    if not thread:
        return ""
    lines = ["This message continues an existing thread."]
    for i, prev in enumerate(prior_messages or []):
        lines.append("Previous message %d subject: %s" % (i + 1, prev.get("subject", "")))
        lines.append("Previous message %d body:\n%s" % (i + 1, prev.get("body", "")))
    state = thread.get("state")
    if state:
        lines.append("Current thread state: %s." % state)
    lines.append("Do not contradict the previous messages.")
    return "\n".join(lines)


def build_messages(brief, card, style_text, prior_messages=None, feedback=None,
                   extra_instruction=None):
    sender = brief.get("sender") or {}
    recipient = brief.get("recipient") or {}
    purpose = FAMILY_PURPOSE.get(brief.get("family"), "write a clear business email")
    user_parts = [
        "SENDER: %s%s <%s>%s" % (
            sender.get("name") or "",
            (" (%s, %s)" % (sender.get("role"), sender.get("org")))
            if sender.get("org") else "",
            sender.get("email") or "",
            (" [%s]" % brief.get("relationship")) if brief.get("relationship") else ""),
        "RECIPIENT: %s <%s>" % (recipient.get("name") or "",
                                 recipient.get("email") or ""),
        "LOCALE: %s (%s spelling)" % (brief.get("locale") or "-",
                                      brief.get("spelling") or "british"),
        "PURPOSE: %s" % purpose,
        "SEND TIME (for reference; the header is added separately): %s"
        % brief["send"]["header"],
        "FACTS THAT MUST APPEAR EXACTLY:\n%s" % _facts_block(brief),
        "OTHER DATES AVAILABLE:\n%s" % _dates_block(brief),
        "MUST NOT STATE:\n%s"
        % ("\n".join("- " + p for p in brief.get("forbidden_facts") or [])
           or "- (nothing forbidden)"),
        "SIGN-OFF NAME: %s" % (brief.get("signer") or {}).get("name", ""),
    ]
    if brief.get("object", {}).get("term"):
        user_parts.append("SUBJECT MATTER: %s" % brief["object"]["term"])
    thread = _thread_block(brief, prior_messages)
    if thread:
        user_parts.append(thread)
    if feedback:
        user_parts.append("A previous attempt was rejected for these reasons; "
                          "fix them:\n%s"
                          % "\n".join("- " + p for p in feedback))
    if extra_instruction:
        user_parts.append(extra_instruction)
    user_parts.append("Write the email now as JSON {\"subject\": ..., \"body\": ...}.")
    return [{"role": "system", "content": _system_prompt(brief, card, style_text)},
            {"role": "user", "content": "\n\n".join(user_parts)}]


_JSON_OBJ = re.compile(r"\{.*\}", re.S)


def parse_render_output(content):
    """Parse ``{subject, body}`` from a completion; raise ValueError on failure."""
    if not content or not content.strip():
        raise ValueError("empty completion")
    match = _JSON_OBJ.search(content)
    if not match:
        raise ValueError("no JSON object in completion")
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        raise ValueError("completion JSON did not parse")
    if not isinstance(obj, dict):
        raise ValueError("completion JSON was not an object")
    subject = obj.get("subject")
    body = obj.get("body")
    if not isinstance(subject, str) or not isinstance(body, str):
        raise ValueError("completion JSON must have string subject and body")
    return subject.strip(), body.strip()


def _diversity_score(text, prior_texts):
    """Max char-5-gram Jaccard of ``text`` against the already-accepted corpus."""
    if not prior_texts:
        return 0.0
    from . import audit
    grams = audit._char_ngrams(text, 5)
    return max(audit._jaccard(grams, audit._char_ngrams(p, 5)) for p in prior_texts)


def render_message(brief, card, *, client, world, style_text=None,
                   n_candidates=2, max_retries=2, seed=0, temperature=0.9,
                   max_tokens=900, prior_messages=None, gold=None,
                   extra_instruction=None, diversity_texts=None):
    """Render and accept one email; raise :class:`RenderRejected` on failure.

    All ``n_candidates`` completions of a round are generated and verified; when
    more than one is acceptable the least similar to the already-accepted corpus
    is chosen, so two messages from the same family do not collapse onto one
    phrasing.  The choice is deterministic given the seed.
    """
    rng = stream(seed, "render:%s" % brief.get("brief_id"))
    attempts = []
    feedback = None
    for attempt in range(int(max_retries) + 1):
        round_candidates = []
        valid = []
        for cand in range(int(n_candidates)):
            call_seed = rng.randint(1 << 30)
            messages = build_messages(brief, card, style_text, prior_messages,
                                      feedback, extra_instruction)
            response = client.chat(messages, temperature=temperature,
                                   seed=call_seed, max_tokens=max_tokens,
                                   candidate_index=cand, retry=attempt)
            record = {"attempt": attempt, "candidate_index": cand,
                      "seed": call_seed, "raw": response.get("content"),
                      "finish_reason": response.get("finish_reason"),
                      "cache": getattr(client, "last_provenance", None)}
            try:
                subject, body = parse_render_output(response.get("content"))
            except ValueError as exc:
                record["problems"] = ["parse error: %s" % exc]
                attempts.append(record)
                round_candidates.append(record)
                continue
            problems = verify_message(brief, subject, body, world=world, gold=gold,
                                      prior_messages=prior_messages)
            record["subject"] = subject
            record["body"] = body
            record["problems"] = problems
            attempts.append(record)
            round_candidates.append(record)
            if not problems:
                valid.append(record)
        if valid:
            chosen = min(valid, key=lambda r: _diversity_score(
                "%s\n%s" % (r["subject"], r["body"]), diversity_texts or []))
            chosen["diversity_score"] = round(_diversity_score(
                "%s\n%s" % (chosen["subject"], chosen["body"]),
                diversity_texts or []), 4)
            return {
                "subject": chosen["subject"], "body": chosen["body"],
                "attempt": chosen["attempt"],
                "candidate_index": chosen["candidate_index"],
                "seed": chosen["seed"], "attempts": attempts,
                "diversity_score": chosen["diversity_score"],
                "brief_id": brief.get("brief_id"),
            }
        best = min(round_candidates, key=lambda r: len(r.get("problems") or []),
                   default=None)
        feedback = (best or {}).get("problems") or ["the previous response did not "
                                                    "contain the required JSON"]
    best = min(attempts, key=lambda r: len(r.get("problems") or []), default={})
    raise RenderRejected(brief.get("brief_id"), best.get("problems") or
                         ["every candidate failed"], candidates=attempts)
