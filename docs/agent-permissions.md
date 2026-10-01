# Agent permissions — design (2026-10)

Fine-grained, enforced boundaries for what the **assistant agent** may do to the
mailbox. Replaces the single `assistant_actions_apply` flag (live/dry-run).

## Principles

1. **Least privilege with explicit opt-in.** Every mutating capability has its own
   permission; nothing mutating happens unless the user has allowed exactly that.
2. **The agent can never exceed its grant.** Enforcement is server-side, at one
   choke point (`AssistantAgent.call_tool`), not in the prompt. The prompt merely
   *describes* the grants so the model can plan and explain refusals.
3. **Dangerous means visibly dangerous.** Irreversible or externally-visible actions
   (send, delete) default to **off**, carry a red warning, need an explicit confirm
   to enable, and are audited.
4. **Standing approvals are explicit objects.** `ask` level creates a *pending action*
   card the user clicks to execute — the agent itself never executes in ask mode.
5. **Everything is audited.** Every allow / deny / pending / apply is a log event.

## Levels

| Level  | Meaning |
|--------|---------|
| `off`  | Tool is refused. The agent is told the capability is disabled and to say so to the user. |
| `ask`  | The call becomes a **pending action** (stored, shown as a card with Apply / Dismiss). The agent cannot execute it. The user's click executes it server-side. |
| `auto` | The agent executes it directly (as today's live mode). |

Reads (`mailbox_overview`, searches, `read_message`, `list_*`, `propose_rule`) are
always allowed — they are required for the product to function and mutate nothing.
`propose_rule` stays allowed at all times: it is itself a proposal mechanism.

## Capability matrix

| Capability | Tools | Risk | Default | Notes |
|---|---|---|---|---|
| classify | `classify_message` *(new)* | safe | auto | Runs the normal classification pipeline on a message (same as the UI Classify button). Filing follows `llm_apply` like every other classify path. |
| flag | `flag_message` | caution | auto | Read/unread, star. Reversible. |
| tag | `tag_message` *(new)* | caution | auto | Applies user-style tags, recorded with `user_tag_by='assistant'`; heuristics training treats assistant tags as non-primary labels. |
| move | `move_message` | caution | auto | Moves mail between folders (never deletes). |
| create_folder | `create_folder` | safe | auto | Empty folder creation. |
| classify-management | `train_classifier`, `manage_classifier` | caution | auto | Heuristics pipeline (currently ungated → bring under the matrix). |
| rules | `delete_rule`, `set_rule_enabled` | caution | auto | Deletes a filter rule, or pauses/resumes one without deleting. Rule *proposals* (one-click cards) stay outside the matrix. |
| draft | `draft_reply` *(new)* | safe | auto | Generates a reply and saves it into Drafts; nothing leaves the mailbox. |
| delete | `delete_message` *(new)* | **dangerous** | **off** | Soft delete: moves mail to the Trash folder (recoverable until the server purges). Warning shown; default off. |
| send | `send_message` *(new)* | **dangerous** | **off** | Sends new mail / replies via the embedded OAuth proxy's SMTP listener; appends a copy to Sent. Hourly cap `sends_per_hour` (default 5, 0 = unlimited). Warning shown; default off. |

## Enforcement

Single choke point `AssistantAgent.call_tool(name, args)`:

```
cap = CAPABILITY_OF_TOOL.get(tool_name)      # reads → None → allow
lvl = load_permissions().get(cap, default)
off  → {"ok": False, "permission_denied": cap, ...}   + audit event
ask  → create agent_actions row (pending) + SSE "action_proposals" event
       → {"ok": True, "pending_approval": True, ...}   + audit event
auto → execute handler (existing code paths)
```

- Pending actions live in `agent_actions`; `POST /agent/actions/<id>/apply` executes
  server-side via the same executor functions the tool handlers use
  (`engine.agent_exec_*`), `POST /agent/actions/<id>/dismiss` drops them.
- Run-time context: the system prompt gains a generated `permissions` block
  ("may: classify, flag, move …; requires approval: draft; disabled: delete, send"),
  `mailbox_overview` reports it, and refused calls instruct the model to explain the
  setting's location (Settings → AI Settings → Agent permissions).
- Sent-mail safety: `send` also consults `sends_per_hour` (counted from `agent_actions`
  applied + direct sends in the last hour).

## Storage

- Settings: `perm_<capability>` ∈ {off, ask, auto} (+ `sends_per_hour` int).
- Table `agent_actions`: id, created_ts, session_id, capability, tool, status
  (pending/applied/dismissed/failed), preview (human line), payload (JSON
  {tool, args, resolved}), result (JSON), applied_ts.
- `messages.user_tag_by` ('user' | 'assistant' | '') to separate label provenance.
- Audit via `store.log_event` lines: `agent: move denied (off) [msg 123]`,
  `agent: delete pending (2 pending)`, `agent: send applied to x@y [action 7]` …

## Migration

- `assistant_actions_apply = False` (old dry-run): set `perm_move`, `perm_flag`,
  `perm_create_folder`, `perm_classify-management` to `ask`, others to defaults.
- `True`/absent: defaults above. The old key is retired from the UI; kept in the DB
  for reference and ignored after migration.

## UI

- **Settings → AI Settings → "Agent permissions"** card replacing the current
  "Assistant may act on mail" checkbox: one row per capability — name, description,
  tools, risk badge (safe/caution/dangerous), level select (Off / Ask me / Auto).
  Dangerous rows render a warning banner; changing a dangerous capability away from
  `off` asks for a JS confirm. Savebar posts `section=behavior`.
- **Assistant page**: "Pending approvals" panel (server-rendered + SSE appended) with
  Apply / Dismiss; tool chips show a `pending` state; the header hint shows a compact
  permission summary. Pending count badge on the Assistant nav item.
- **Log page**: agent audit events appear like other events.

## Tests (mock_e2e)

- off: tool refused, no IMAP mutation, audit line, model told why.
- ask: agent_actions row pending, no IMAP mutation; apply route executes; dismiss works.
- auto: executes as before (existing rename of the old dry-run test).
- dangerous defaults: delete/send off out of the box; enabling stays possible.
- send: mock SMTP capture — correct headers, Sent append, hourly cap enforced.
- tag provenance: `user_tag_by='assistant'`; training excludes agent tags by default.

## Out of scope (v1)

Per-account permissions, per-sender allowlists, undo queue for applied actions,
approval expiry. Candidates for later passes.
