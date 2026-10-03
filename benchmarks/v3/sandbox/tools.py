"""Explicit workflow tool schemas for the benchmark v3 sandbox (WP3).

These are the frozen tool definitions handed to a workflow adapter.  They are
deliberately separate from any user prompt so a prompt can never smuggle gold or
change the tool surface.  ``TOOL_CAPABILITIES`` maps each tool to the capability
the sandbox enforces.
"""
from .mailbox import DELETE, DRAFT, FLAG, FOLDER, MOVE, READ, RULE_CREATE, SEND

TOOL_CAPABILITIES = {
    "list_folders": READ,
    "search_messages": READ,
    "read_message": READ,
    "list_rules": READ,
    "simulate_rule": READ,
    "flag_message": FLAG,
    "create_folder": FOLDER,
    "draft_reply": DRAFT,
    "move_message": MOVE,
    "propose_rule": RULE_CREATE,
    "delete_message": DELETE,
    "send_message": SEND,
}


def _fn(name, description, properties, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


TOOL_SCHEMAS = [
    _fn("list_folders", "List mailbox folders with message and unseen counts.", {}),
    _fn("search_messages", "Search the local message index.",
        {"query": {"type": "string"}, "sender": {"type": "string"},
         "subject": {"type": "string"}, "folder": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    _fn("read_message", "Read one message by message_id (or folder + uid).",
        {"message_id": {"type": "string"}, "folder": {"type": "string"},
         "uid": {"type": "string"}}),
    _fn("list_rules", "List existing rules.", {}),
    _fn("simulate_rule", "Dry-run a candidate rule against the mailbox (no change).",
        {"conditions": {"type": "array", "items": {
            "type": "object",
            "properties": {"field": {"type": "string"}, "op": {"type": "string"},
                           "value": {"type": "string"}},
            "required": ["field", "op", "value"], "additionalProperties": False}}},
        ["conditions"]),
    _fn("flag_message", "Update read/flagged state of a message.",
        {"message_id": {"type": "string"}, "folder": {"type": "string"},
         "uid": {"type": "string"}, "seen": {"type": "boolean"},
         "flagged": {"type": "boolean"}}),
    _fn("create_folder", "Create a mailbox folder.",
        {"name": {"type": "string"}}, ["name"]),
    _fn("draft_reply", "Save a draft reply in Drafts (never sent).",
        {"message_id": {"type": "string"}, "instructions": {"type": "string"}}),
    _fn("move_message", "Move a message to a target folder.",
        {"message_id": {"type": "string"}, "folder": {"type": "string"},
         "uid": {"type": "string"}, "target_folder": {"type": "string"}},
        ["target_folder"]),
    _fn("propose_rule", "Propose a rule for the owner to approve (proposal only).",
        {"name": {"type": "string"},
         "conditions": {"type": "array", "items": {"type": "object"}},
         "actions": {"type": "array", "items": {"type": "object"}},
         "rationale": {"type": "string"}},
        ["name"]),
    _fn("delete_message", "Delete a message (disabled: the benchmark never deletes mail).",
        {"message_id": {"type": "string"}}),
    _fn("send_message", "Send a message (disabled: the benchmark never sends mail).",
        {"message_id": {"type": "string"}, "body": {"type": "string"}}),
]


def tool_schemas(names=None):
    """The workflow tool definitions, optionally filtered to ``names``."""
    if names is None:
        return [dict(s) for s in TOOL_SCHEMAS]
    wanted = set(names)
    return [dict(s) for s in TOOL_SCHEMAS if s["function"]["name"] in wanted]
