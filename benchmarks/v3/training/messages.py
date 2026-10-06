"""Canonical SFT messages with per-turn masks (acceptance AC5).

A canonical example is transport-neutral: a list of messages with roles
``system``/``user``/``assistant``/``tool``, an optional per-message ``supervised``
flag, and, for assistant turns, ``think`` (reasoning) plus parsed ``tool_calls``.
The same canonical example is later rendered by *each model's own native
template* -- never by a chat-only patch that drops the ``tool`` role.

The canonical form keeps the raw tool-call arguments string alongside the parsed
arguments so a malformed call is representable without being silently coerced.
"""
from __future__ import annotations

TRAIN_SCHEMA_VERSION = "mail-sft-0.1"

ROLES = ("system", "user", "assistant", "tool")


class MessageError(ValueError):
    """Raised when a canonical message stream is malformed."""


def message(role, content="", *, tool_calls=None, tool_call_id=None, name=None,
            supervised=False, think=None, rejected=False, meta=None):
    """Build one canonical message."""
    if role not in ROLES:
        raise MessageError("unknown role %r" % role)
    msg = {
        "role": role,
        "content": "" if content is None else str(content),
        "supervised": bool(supervised),
    }
    if tool_calls:
        msg["tool_calls"] = [dict(tc) for tc in tool_calls]
    if tool_call_id is not None:
        msg["tool_call_id"] = tool_call_id
    if name is not None:
        msg["name"] = name
    if think is not None:
        msg["think"] = str(think)
    if rejected:
        msg["rejected"] = True
    if meta:
        msg["meta"] = dict(meta)
    return msg


def assistant_message(content="", *, think=None, tool_calls=None, supervised=True,
                      rejected=False):
    return message("assistant", content, think=think, tool_calls=tool_calls,
                   supervised=supervised and not rejected, rejected=rejected)


def tool_message(name, wrapped_result, tool_call_id):
    """A tool result message.  Content is the untrusted wrapper's JSON payload."""
    return message("tool", wrapped_result, tool_call_id=tool_call_id,
                   name=name, supervised=False)


def apply_mask(messages, bad_indices=()):
    """Return ``messages`` with supervised=False on any bad (rejected) turn.

    ``bad_indices`` are indices of assistant turns the student should not learn
    from (e.g. a malformed or unverified call).  Inputs stay masked regardless.
    """
    bad = set(int(i) for i in bad_indices)
    out = []
    for i, msg in enumerate(messages):
        copy = dict(msg)
        if msg.get("role") != "assistant" or i in bad:
            copy["supervised"] = False
        out.append(copy)
    return out


def mask_list(messages):
    """Per-message supervision flags in order (for template token masking)."""
    return [bool(m.get("supervised")) for m in messages]


def to_native_messages(messages):
    """Convert canonical messages to the dict shape ``apply_chat_template`` wants.

    Nested/list/dict tool-call argument values are JSON-encoded strings so the
    model's XML template renders them losslessly (see ``minicpm``); ``think`` is
    folded into the content as ``<think>...</think>`` exactly as the released
    MiniCPM template splits it back out.
    """
    from .minicpm import encode_argument_value

    out = []
    for msg in messages:
        native = {"role": msg["role"]}
        content = msg.get("content", "")
        if msg["role"] == "assistant" and msg.get("think"):
            content = "<think>\n%s\n</think>\n\n%s" % (msg["think"], content)
        native["content"] = content
        # Preserve supervision so the training mask is derived from the canonical
        # flag, not by assuming every assistant turn is learnable (B1).
        native["supervised"] = bool(msg.get("supervised"))
        if msg.get("rejected"):
            native["rejected"] = True
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            calls = []
            for tc in msg["tool_calls"]:
                raw = tc.get("arguments", {})
                if isinstance(raw, str):
                    import json as _json
                    try:
                        parsed = _json.loads(raw)
                    except ValueError:
                        parsed = {}
                else:
                    parsed = raw
                args = {k: encode_argument_value(v)
                        for k, v in (parsed or {}).items()}
                calls.append({"id": tc.get("id"),
                              "type": "function",
                              "function": {"name": tc.get("name"),
                                           "arguments": args}})
            native["tool_calls"] = calls
        if msg["role"] == "tool":
            native["tool_call_id"] = msg.get("tool_call_id")
            native["name"] = msg.get("name")
        out.append(native)
    return out


def canonical_valid(messages):
    """Structural problems with a canonical message stream ([] when valid)."""
    problems = []
    if not messages:
        return ["empty message stream"]
    if messages[0]["role"] != "system":
        problems.append("first message must be a system message")
    seen_supervised = False
    for i, msg in enumerate(messages):
        if msg.get("role") not in ROLES:
            problems.append("message %d has role %r" % (i, msg.get("role")))
        if msg.get("role") == "assistant" and msg.get("supervised"):
            seen_supervised = True
        if msg.get("role") == "tool" and not msg.get("tool_call_id"):
            problems.append("tool message %d has no tool_call_id" % i)
    if not seen_supervised:
        problems.append("no supervised assistant turn")
    return problems
