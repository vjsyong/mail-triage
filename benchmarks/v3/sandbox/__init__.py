"""Benchmark v3 workflow sandbox (WP3).

A deterministic, per-case **deep-fresh** mailbox state machine plus an explicit
tool surface and event log.  It is the only place a workflow case can actually
change state; it never touches a real mailbox, never deletes or sends, and never
sees gold.

Public interface (frozen for the WP4 runner and WP5 scorer)::

    Mailbox(fixture, permissions=None, case_id="")   # fresh deep copy
    mailbox.execute(tool_name, args, status="executed", approve=False) -> model result
    mailbox.tool_events                              # structured event log
    mailbox.snapshot() / mailbox.final_state()       # scoreable final state
    mailbox.folder_counts()                          # counts derived from messages
    tool_schemas()                                   # explicit OpenAI-style defs

Permissions normalise to ``off`` / ``ask`` / ``auto`` per capability and are
enforced *separately* from writes: a denied or approval-pending call records an
attempt and performs no mutation.

**Handoff to WP2 (build) and WP5 (scoring).**  A workflow case carries three
extra keys beyond the foundation case schema::

    mailbox      -> the deep-copied fixture (shape in ``mailbox.py``)
    tools        -> optional list of tool names to expose (default: all)
    permissions  -> optional explicit levels or a policy permission block

``final_state()`` is the scoreable end state (derived folder counts, per-message
folder/seen/flagged, moves, drafts, proposed_rules, simulations, and the event
log).  A workflow gold's ``answer`` may carry both a partial ``expected_state``
(a subset of that snapshot, matched structurally) and a list of structured
``assertions`` such as ``{"kind": "message_in_folder", "message_id", "folder"}``,
``{"kind": "moved", "message_id", "from", "to"}``, ``{"kind": "noop"}`` or
``{"kind": "denied", "capability"}``.  Scoring owns interpreting these; the
sandbox only ever reports what actually happened (attempted / denied / pending /
mutated / no-op), never what was claimed.
"""

from .mailbox import (
    Mailbox,
    SandboxError,
    ToolArgumentError,
    UnknownToolError,
    normalize_permissions,
    CAPABILITIES,
    READ,
    MOVE,
    DRAFT,
    RULE_CREATE,
    FLAG,
    FOLDER,
    DELETE,
    SEND,
    DANGEROUS,
    CAPABILITY_LEVELS,
)
from .tools import tool_schemas, TOOL_SCHEMAS

__all__ = [
    "Mailbox", "SandboxError", "ToolArgumentError", "UnknownToolError",
    "normalize_permissions", "tool_schemas", "TOOL_SCHEMAS",
    "CAPABILITIES", "READ", "MOVE", "DRAFT", "RULE_CREATE", "FLAG", "FOLDER",
    "DELETE", "SEND", "DANGEROUS", "CAPABILITY_LEVELS",
]
