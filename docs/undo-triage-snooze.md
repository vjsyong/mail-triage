# Undo, triage queue, snooze, log tools (2026-10-01)

Follow-up to the critic round: the three strategic gaps vs the commercial bar,
plus log search/window/pause and a favicon.

## Undo for machine-applied filings
- `undo_log` table + `store.record_move(msg, to_folder, source)` hooked on ALL six
  move sites: rules (engine `_process_folder`), flow steps, LLM auto-file, assistant
  tool, Trash, and the manual viewer File button (source = rule/flow/auto-file/
  assistant/trash/manual).
- Dashboard card "Recent filings" (`.frow`, id=filings): subject, source chip,
  destination + origin, relative time, one-tap Undo. Entries whose message moved
  again show "moved on" instead of Undo. Card hides when empty.
- `POST /undo/<lid>` -> `engine.undo_filing`: verifies the message is still where
  the entry says, MOVE back over IMAP, restores prev status/action_taken, marks the
  entry, and calls `store.keep_message(msgid)`.
- Keep guard: `keep_ids` table keyed by Message-ID. `_process_folder` skips rule
  actions + flows for kept messages (status becomes "kept", badge "kept (undo)");
  the classify phase skips flow/auto-filing for them too. Manual re-File clears the
  keep (user changed their mind).
- Why the guard: an undone message returns to INBOX with a fresh uid; without it,
  the next scan would re-file it (and the Gmail/Fastmail bar is "undo means undo").

## Triage queue in the viewer
- `store.neighbors(mid, filt)` -> (newer_id, older_id) in the same sort order as the
  list (sort_ts DESC, id DESC); the viewer shows `.qbar` with `← Newer` / `Older →`
  (dimmed spans when at the end) that carry `?f=` so the queue stays scoped to the
  filter you came from. Message-list row links now carry `f`; the backlink reads
  e.g. "← Messages (needs reply)".
- "File & next": the File button with `next=1` files via the LLM suggestion and
  redirects to the next message in the queue (falls back to the filtered list at
  the end).
- Still open (from the critique): keyboard j/k bindings, per-message read state.

## Snooze
- `messages.snoozed_until` (epoch). Excluded from every default filter and from the
  dashboard needs-reply count; a new "Snoozed" chip on Messages lists what's parked
  (badge shows "snoozed" with the wake time in the title); the viewer shows
  "snoozed until <date>" + Wake now, or 1 day / 3 days / 1 week presets.
- `POST /messages/<id>/snooze {hours}` (0 = wake). Resurfacing is purely dynamic:
  the filters compare against now, so no cron job is needed.

## Log tools
- `/log` gained `q` (substring), `mins` (15/60/1440 window) and composes with the
  existing level chips + debug toggle (all links carry the full state).
- Live mode: the page refreshes every 10s unless paused via the Pause/Resume button
  (persisted in localStorage) or while the user is typing in the search box; the
  timer re-arms itself and no-ops if the page was swapped (Turbo).

## Favicon
- Generated from the app icon: `icons/favicon.ico` (16/32/48) + `favicon-32.png`
  (Pillow), head links (`rel=icon` ico + png + 192), and a `/favicon.ico` route for
  direct requests.

Suite: 436 checks green. Commits: favicon 87e0a8e, undo 1b84fe5, triage d1f08e9,
snooze ced8fe3, log e9d50ae (+ reorder follow-up).


## Retroactive audit sweep (2026-10-01, evening)
`engine.sweep_msg_events(msg_id=None)` reconstructs audit trails from stored state for
messages that predate the msg_events feature - never invents anything beyond what the
message row and flow_runs already know. Sources: llm_* fields + classified_by ->
`classify` (meta flagged `_backfilled`, shown as "· reconstructed"); rule_id ->
`rule`; `action_taken` flow:/move: -> `flow`/`move` (source label from rule_id /
status: assistant-moved -> assistant, llm-moved -> auto-file); user_tag -> `tag`;
snoozed_until -> `snooze`. Real flow_runs timestamps are used when present.
- Idempotent: only inserts kinds that are absent for that message; includes a cleanup
  pass that relabels assistant moves from an earlier sweep pass.
- Triggers: once automatically at Worker startup (logs "audit backfill: N events"),
  plus a per-message `POST /messages/<id>/sweep` ("Backfill from stored state" button
  in the empty audit card). A `backfill` marker event records what the sweep did.
- Live result: 6,037 events reconstructed across ~3,590 messages (3,554 classify +
  2,480 move + rule/flow few); 34 messages have no stored signals and stay empty.

## Clearing "needs reply" (2026-10-02)
When the LLM flags a message but the user disagrees, the flag must be clearable:
- **Bulk** on Messages: select rows -> "No reply needed" (stays inside the current
  filter). **Per message** in the viewer: the queue bar's "No reply, next" and the
  Actions card's "No reply needed".
- Clearing sets `llm_needs_reply=0` (queue + counts drop immediately), records a
  **weight-4 `explicit_user_correction` label** (`labels`, task `needs_reply`, value
  "0") that the learning loop consumes like any other label, and writes a
  `needs_reply · cleared (by ui)` audit event + an event-log line.
- **A re-classification cannot re-flag it**: `engine.classify_and_store` writes
  `_needs_reply_effective(msg_id, llm_value)`, and `store.user_needs_reply()` (the
  latest explicit correction) outranks every model verdict.
- Only rows that were actually flagged change, so bulk clears never mint labels for
  messages the user did not correct.
