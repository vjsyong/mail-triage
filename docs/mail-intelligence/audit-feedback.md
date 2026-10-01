# mail-triage — feedback & implicit-behavior signal audit (code-level)

**Snapshot:** commit `c6db3c5` (`/home/xrim/mail-triage-intel`).
**Method:** static read of `app.py`, `engine.py`, `store.py`, `heuristics.py` (including the `_SCHEMA` DDL and `_migrate`). Read-only: no application code modified, no writes to the DB, no writes outside this file.
> **Reference frame:** line refs are to the working tree at audit time (2026-10-01 ~15:10 UTC). During the audit, `store.py` gained uncommitted learning-loop schema/helpers (+289 lines, e.g. `specialists`, `decisions`, `decision_evidence`, `observations`, `labels` for `docs/mail-intelligence/design.md`) — those tables are OUT of scope here. `store.py` refs below are remapped to that tree and every one was content-verified against `c6db3c5` (refs are annotated with the function/section name, so anchor on that name: the raw c6db3c5 line numbers are lower by a region-dependent offset). `app.py`/`engine.py`/`heuristics.py` are unmodified from `c6db3c5`.

**Scope:** every place a *user action* or *system behavior observation* is persisted, so the learning design can separate **OBSERVATIONS** ("user moved this"), **INFERRED LABELS** (conclusions drawn from behavior) and **CONFIRMED LABELS** (explicit corrections).

**How the `strength` values map onto the design tiers**

| strength | design tier | meaning |
|---|---|---|
| explicit correction | CONFIRMED LABEL | user overwrote / rejected a machine output (undo, relabel, exclusion, dismiss) |
| explicit label | CONFIRMED LABEL | user asserted a class/decision directly (tag, apply proposal, approve action) |
| behavioral observation | OBSERVATION (→ INFERRED LABEL with modeling assumptions) | a thing happened and was logged; intent must be inferred |
| ambiguous | not usable as-is | the act is conflated with another or not recorded at all; needs instrumentation first |

**Caveat that applies to every behavioral row:** all of these observations are *one-sided*. A recorded action proves something happened; the absence of an action proves nothing (the user may never have seen the message). Do not train on "no action = negative" without modelling exposure.

---

## 1. Storage surfaces that hold signals

| surface | what it holds | schema / writer |
|---|---|---|
| `messages.user_tag`, `user_tag_by` | the single per-message human/machine tag | migration `store.py:286-289`; writer `store.tag_messages` `store.py:1492-1499` |
| `messages.snoozed_until` | wake time (0 = awake) | migration `store.py:290-291`; writer `store.snooze_message` `store.py:928-932` |
| `messages.status` / `action_taken` / `folder` / `uid` | current outcome snapshot (moved / kept / flow …) | written all over `engine.py` (`1402-1403`, `1448-1452`, `2008-2012`, `3626-3629`, `4248-4251`, …) |
| `msg_events` | per-message audit timeline, `kind` + `detail` | schema `store.py:309-316`; helpers `store.record_move` `store.py:935-951` and `store.log_msg_event` `store.py:954-960` |
| `undo_log` | one row per filing, with `source`, `from/to_folder`, `prev_status`, `prev_action_taken`, `undone_ts` | schema `store.py:293-305`; writer `store.record_move` `store.py:943-947`; undo mark `store.mark_undone` `store.py:991-995` |
| `keep_ids` | "do not re-file" registry, keyed by **Message-ID** | schema `store.py:306-308`; writers `store.keep_message` `store.py:998-1006` / `clear_keep` `store.py:1009-1015`; readers `kept_ids`/`is_kept` `store.py:1018-1028` |
| `heuristics.excluded` | JSON array of message ids removed from a classifier's dataset | schema `store.py:169` + migration `store.py:284-285`; writer `app.py:2582`; reader `heuristics.heuristic_excluded` `heuristics.py:240-245` |
| `heuristics.model/stats` | trained model + `source`, `weak_labels`, `trained_label_count`, `excluded` count | `heuristics.train_heuristic` `heuristics.py:322-336` |
| `rule_proposals` | tag-learn proposals (`source`, `rule`, `note`, `applied`) | schema `store.py:209-214`; writers `store.add_rule_proposal` `store.py:1590-1595`, `mark_rule_proposal_applied` `store.py:1621-1623` |
| `flow_runs` | one row per executed flow (dedupe + run log) | schema `store.py:115-122`; writer `store.record_flow_run` `store.py:596-599` |
| `agent_actions` | assistant tool approvals + audit (`capability`, `tool`, `status`, `preview`, `payload`, `result`) | schema `store.py:215-222`; writers `store.add_agent_action` `store.py:1519-1527`, `store.set_agent_action` `store.py:1536-1544` |
| `assistant_messages.proposals` / `.meta` | assistant rule/flow proposals (+`applied` marker) and the chat transcript incl. `tools_log` | schema `store.py:182-187` + migration `store.py:250-253`; writer `store.add_assistant_message` `store.py:1211-1223`, `set_assistant_proposals` `store.py:1226-1229` |
| `llm_log` | one row per LLM call (`msg_id`, `ok`, `error`) — no prompt/response | schema `store.py:173-176`; writer `store.add_llm_log` `store.py:1176-1179` |
| `events` | global free-text app log (`level`, `message`) | schema `store.py:155-158`; writer `store.log_event` `store.py:1162-1165` |

## 2. `msg_events` kind registry (all 12 kinds)

Every kind, its writer, and the call sites that produce it:

| kind | written by | call sites |
|---|---|---|
| `move` | `store.record_move` `store.py:948-950` (same transaction as `undo_log`) | all 6 move sites (5 in `engine.py` + the manual route in `app.py`): `rule` `engine.py:1382`, `flow` `engine.py:889`, `auto-file` `engine.py:2006`, `assistant` `engine.py:3621`, `trash` `engine.py:4243`, `manual` `app.py:4504` |
| `classify` | `store.log_msg_event` `engine.py:1945-1949` (JSON payload: category/confidence/by/reason/summary/needs_reply/thinking) | `engine.classify_and_store` → worker `engine.py:2032`, ClassifyJob `engine.py:2191`, single-button `app.py:4456`, assistant tool `engine.py:4159` |
| `rule` | `engine.py:1408-1410` (applied) and `engine.py:1368` (skipped-because-kept) | `_process_folder` `engine.py:1362-1410` |
| `flow` | `engine.py:976-978` (step summary incl. dry-run marker) | `_process_flow` `engine.py:958-978` |
| `file` | `engine.py:2014-2015` ("auto-filed … (LLM suggested)") | LLM category-map filing `engine.py:1999-2015` |
| `guard` | `engine.py:1966-1967` | guard-rule check inside `classify_and_store` `engine.py:1957-1968` |
| `undo` | `engine.py:1457-1458` | `engine.undo_filing` `engine.py:1421-1459` |
| `tag` | `app.py:4476` | single-message tag route only (`/messages/<id>/tag`) |
| `snooze` / `wake` | `app.py:2224` / `app.py:2230` | `message_snooze` `app.py:2209-2232` |
| `draft` | `app.py:4529` | draft **generation** route only; body not stored |

Rendered in the viewer audit card `app.py:4040-4055`; the badge map at `app.py:4045` knows exactly these kinds (no `keep`, no `trash`, no `flag`).

---

## 3. The signals

### A. Explicit per-message labels & corrections (user-driven)

| Signal | User action / trigger | Storage (table.col, file:line) | Explicit / inferred | Strength |
|---|---|---|---|---|
| A1. Tag one message | viewer tag field + Save (`app.py:4009-4015`) → `POST /messages/<id>/tag` `app.py:4472-4478` | `messages.user_tag` + `user_tag_by='user'` via `store.tag_messages` `store.py:1492-1499` (call `app.py:4475`); `msg_events(kind='tag')` "tagged “X”" `app.py:4476` | Explicit — typed by user | explicit label |
| A2. Bulk tag | select rows + tag field `app.py:3641-3644` → `POST /messages/tag` `app.py:3775-3785` | `messages.user_tag`/`user_tag_by='user'` `store.py:1492-1499` (call `app.py:3782`); **only an aggregate** `events` line "tagged N message(s) as 'X'" `app.py:3783`; **no per-message `msg_events` row** | Explicit — typed by user | explicit label (audit trail weaker than A1) |
| A3. Clear a tag (single) | same route with empty tag → `app.py:4475-4476` | `messages.user_tag=''` + `user_tag_by=''` (`store.py:1498` sets `by` only when tag non-empty); `msg_events(kind='tag')` "tag cleared" `app.py:4476` | Explicit — user act | explicit correction (the old tag value is NOT kept in the event) |
| A4. Bulk untag | select rows → `POST /messages/untag` `app.py:3788-3796` | `messages.user_tag=''`, `user_tag_by=''` via `store.untag_messages` `store.py:1502-1503`; **no `events`, no `msg_events`** | Explicit act, silently captured | ambiguous |
| A5. Dataset relabel — tag-sourced classifier | dataset review dropdown → `POST /classifiers/<id>/dataset/relabel` `app.py:2540-2566` | rewrites **`messages.user_tag = <chosen category>`** `app.py:2556`; `events` line `app.py:2560`; toast via `?toast=in|out` `app.py:2562-2565` | Explicit — user picks the category | explicit correction |
| A6. Dataset relabel — classified-source classifier | same route, `stats.source != 'tags'` `app.py:2557-2559` | rewrites **`messages.llm_category = <cat>`, `classified_by='user'`, `llm_confidence=1.0`** `app.py:2558-2559`; `events` `app.py:2560` | Explicit — user overrides the model | explicit correction (the only writer that ever sets `classified_by='user'`) |
| A7. Dataset remove / re-include | dataset review buttons `app.py:2484-2488` → `/dataset/remove|reinclude` `app.py:2591-2598` | `heuristics.excluded` = JSON id list via `store.update_heuristic` `app.py:2582` (`heuristics.py:240-245`, applied in `heuristics.sample_rows` `heuristics.py:259-261` and at every training call site: retrain `app.py:2420`, `train_heuristic` `heuristics.py:322-336`, `auto_refine` `heuristics.py:415-442`); `events` `app.py:2583` | Explicit — user excludes a sample | explicit correction |
| A8. Manual classify (single) | "Classify with LLM" `app.py:3999-4007` → `POST /messages/<id>/classify` `app.py:4449-4469` | machine verdict overwrites `messages.llm_category/llm_confidence/llm_summary/llm_reason/llm_thinking/llm_needs_reply/llm_suggested_folder/classified_by/status` `engine.py:1934-1944`; `msg_events(kind='classify')` JSON `engine.py:1945-1949`; `llm_log` row `app.py:4457` | Explicit act, but the resulting label is machine output | ambiguous (act-of-interest only; label is a weak label) |
| A9. Manual classify (batch) | "Classify selected" `app.py:3646`/`3799-3807`, "Classify all" `app.py:3634`/`3810-3814`, Stop `app.py:3817-3821` | identical per-message traces as A8 (ClassifyJob `engine.py:2191-2194`); the batch itself is **only** in-memory (`classifier.state` `engine.py:2154`) — no record of who started it or when | Explicit act | behavioral observation |
| A10. File button (confirm the suggested folder) | "File to <folder>" `app.py:3997-3998` → `POST /messages/<id>/file` `app.py:4481-4516` | `undo_log` + `msg_events(kind='move')` via `record_move(..., 'manual')` `app.py:4504`; `messages.status='llm-moved'`, `action_taken='move:<folder>'`, `folder`, `uid` `app.py:4501-4505`; **`store.clear_keep`** `app.py:4506`; `events` `app.py:4507` | Explicit — user performs the machine's suggestion | explicit correction (confirms category→folder; note it also clears any keep guard) |
| A11. Undo a filing | Undo card on the dashboard `app.py:2076-2086` (`filings=store.recent_moves(limit=8)` `app.py:2189`) → `POST /undo/<lid>` `app.py:2235-2239` | `engine.undo_filing` `engine.py:1421-1459`: moves back, restores `messages.folder` + `status`/`action_taken` from `undo_log.prev_*` `engine.py:1448-1452`, **`keep_ids` insert** `engine.py:1453` (`store.py:998-1006`), `undo_log.undone_ts/undo_uid` `engine.py:1454` (`store.py:991-995`), `msg_events(kind='undo')` `engine.py:1457`, `events` `engine.py:1455` | Explicit — user rejects the machine outcome | explicit correction (strongest negative signal; also creates a standing instruction) |
| A12. Keep guard | no direct UI — created only by undo (A11), cleared only by File (A10) | `keep_ids.msgid` (+`ts`) `store.py:998-1006`; effects: rule skip + `messages.status='kept'`, `action_taken='kept (undo)'` `engine.py:1360-1369`; classify/flows/filing skip `engine.py:1933`, `1957`, `1969`, `1999` | Explicit (via undo) | explicit correction (durable, but keyed on Message-ID and invisible in the UI) |
| A13. Snooze | viewer "1 day/3 days/1 week" `app.py:4023-4025` → `POST /messages/<id>/snooze` `app.py:2209-2225` | `messages.snoozed_until` `store.snooze_message` `store.py:928-932` (call `app.py:2221`); `msg_events(kind='snooze')` `app.py:2224`; `events` `app.py:2222` | Explicit act; intent inferred | behavioral observation (defer intent) |
| A14. Wake | "Wake now" `app.py:4021` → same route, hours=0 `app.py:2226-2231` | `snoozed_until=0` `app.py:2227`; `msg_events(kind='wake')` `app.py:2230`; `events` `app.py:2228` | Explicit act | behavioral observation |
| A15. Draft generation | "Draft with LLM" (+template) `app.py:3968-3974` → `POST /messages/<id>/draft` `app.py:4519-4534` | `msg_events(kind='draft')` "reply draft generated [with template N]" `app.py:4529`; draft body is **in-memory only** (`engine.generate_draft` `engine.py:2101-2118`); failures record nothing | Explicit act; intent inferred | behavioral observation (likely needed a reply) |
| A16. Draft save | edit + "Save to Drafts" `app.py:3978-3985` → `POST /messages/<id>/save` `app.py:4537-4544` | IMAP APPEND to `\Drafts` + **only** an `events` line "draft saved for '<subject>'" `engine.save_draft` `engine.py:2121-2133`; **no `msg_events`, no message link, no body, no record of edits** | Explicit act | behavioral observation (strong reply intent; currently unlinked and unfindable per message) |
| A17. Learn rules from tags | "Learn rules from tags (N)" `app.py:3635` → `POST /learn-rules` `app.py:3829-3843` | `rule_proposals` rows (`source='tags'`, `rule` JSON, `note`) `store.add_rule_proposal` `store.py:1590-1595` (call `app.py:3834`); input = `store.tagged_examples(80)` `engine.propose_rules_from_tags` `engine.py:2296-2334`; **no event that learning ran** | Explicit act; output is machine abstraction | explicit label (the tags are the label) |
| A18. Apply a tag-learned rule proposal | "Add/Update" on the proposal card `app.py:3846-3887` | `rules` row added/updated `app.py:3871-3881`; `rule_proposals.applied=1` `app.py:3875`/`3882` (`store.py:1621-1623`); `events` `app.py:3876`/`3883` | Explicit — user accepts the hypothesis | explicit label (strongest confirmation that tag→pattern was right) |
| A19. Dismiss a tag-learned proposal | "Dismiss" `app.py:3890-3893` | `rule_proposals.applied=1` only (`store.py:1621-1623`); **no event**; apply and dismiss become indistinguishable | Explicit act, indistinguishable in storage | ambiguous (negative signal lost) |
| A20. Rules CRUD from the UI | Rules page `app.py:2744-2798` | add → `rules` row + `events` `app.py:2751-2752`; edit → **nothing** `app.py:2758-2774`; toggle → **nothing** `app.py:2777-2782`; delete → **nothing** `app.py:2785-2788`; move → **nothing** `app.py:2791-2798` | Explicit config | explicit label for add; ambiguous for edit/toggle/delete/move |
| A21. Flows CRUD from the UI | Flows page `app.py:3398-3468` | add → `flows` row + `events` `app.py:3406-3407`; edit → **nothing** `app.py:3414-3441`; toggle → `events` `app.py:3449-3451`; delete → `events` `app.py:3459-3460`; move → **nothing** `app.py:3464-3468` | Explicit config | explicit label (partial audit) |
| A22. Classifier create/retrain/toggle/delete | Classifiers page `app.py:2399-2437`; assistant tools `engine.py:3881-3980` | `heuristics` row via `store.add/update/delete_heuristic`; retrain re-derives samples **with** `heuristic_excluded` `app.py:2416-2420`; `events` `app.py:2405`, `2422`, `2435` | Explicit config | explicit label |
| A23. Settings saves (categories, category→folder map, permissions, caps, watch folders …) | Settings page `POST /settings` `app.py:5772-5800` | `settings` rows (`store.set_setting` `store.py:455-458`); **no event of any kind** | Explicit config | ambiguous (preference drift un-auditable) |
| A24. Reply templates CRUD | Templates page `app.py:3544-3573` | `templates` rows (`store.py:890-906`); **no events** | Explicit config | ambiguous |

### B. Machine outcomes the user can react to (the "what happened" record)

| Signal | User action / trigger | Storage (table.col, file:line) | Explicit / inferred | Strength |
|---|---|---|---|---|
| B1. Rule filing | worker scan `engine.py:1354-1410` | `record_move(..., 'rule')` `engine.py:1382` → `undo_log(source='rule')` + `msg_events(kind='move')` `store.py:943-950`; `messages.status='matched'`/`'matched-dry'`, `action_taken` ("move:X", "read", "flag"), `rule_id` `engine.py:1402-1403`; `msg_events(kind='rule')` `engine.py:1408`; `events` `engine.py:1404` | Automatic — system-written | behavioral observation |
| B2. Guard rule blocked filing | guard match `engine.py:1957-1968` | `msg_events(kind='guard')` `engine.py:1966-1967`; `events` `engine.py:1964`; no status change (row keeps `status='classified'` `engine.py:1943`) | Automatic | behavioral observation |
| B3. Kept message skipped a rule | kept msgid + matching rule `engine.py:1360-1369` | `messages.status='kept'`, `action_taken='kept (undo)'`, `rule_id` `engine.py:1363-1364`; `msg_events(kind='rule')` skipped `engine.py:1368`; `events` `engine.py:1365` | Automatic | behavioral observation (snapshot only — a later scan can overwrite it; `keep_ids` is the durable record) |
| B4. Flow run | scan `engine.py:1357-1359`/`1411-1413` or post-verdict `engine.py:1969-1998` | `flow_runs(flow_id, msgid, message_id, ran_at)` **only when live** `engine.py:970-971` (`store.py:596-599`, dedupe `store.py:589-593`); `messages.status='flow'`/`'flow-dry'`, `action_taken='flow:<name>'` `engine.py:967-968`; `msg_events(kind='flow')` with steps `engine.py:976-978`; `events` `engine.py:972`; move steps add `record_move('flow')` with `from_folder` override `engine.py:889` | Automatic | behavioral observation |
| B5. LLM auto-file (category→folder map) | worker queue, `llm_apply` on `engine.py:1999-2015` | `record_move(..., 'auto-file')` `engine.py:2006` → `undo_log` + `msg_events(kind='move')`; `messages.status='llm-moved'`, `action_taken='move:<folder>'` `engine.py:2008-2012`; `msg_events(kind='file')` `engine.py:2014` | Automatic | behavioral observation |
| B6. Classification verdict (LLM or heuristic) | worker queue `engine.py:2030-2036`, ClassifyJob `engine.py:2191-2194`, manual `app.py:4456`, assistant `engine.py:4159` | `messages.llm_*` + `classified_by` (`'llm'` or `'heuristic:<id> <name>'`) + `status='classified'` `engine.py:1934-1944`; `msg_events(kind='classify')` JSON incl. `by`/`reason`/`thinking` `engine.py:1945-1949`; `llm_log(msg_id, ok)` rows `store.py:1176-1179` written at `engine.py:2034`/`2036`, `engine.py:2192-2193`, `app.py:4457` — **not** by the assistant tool `engine.py:4148-4173` | Automatic | behavioral observation |
| B7. LLM-call accounting | every pipeline call | `llm_log` `store.py:1176-1179` (no model/prompt/response columns; hourly cap read `store.llm_count_last_hour` `store.py:1182-1186`) | Automatic | behavioral observation |
| B8. Heuristic auto-refine | worker after each cycle | `heuristics.model/stats` rewrite `heuristics.auto_refine` `heuristics.py:415-442`; `events` "heuristic 'X' retrained (+N new label(s))" `engine.py:1316-1318` | Automatic (triggered by tag growth) | behavioral observation |
| B9. Global app log | most routes and engine actions | `events(ts, level, message)` `store.log_event` `store.py:1162-1165` — free text, usually not keyed to a message id | Automatic | behavioral observation (forensics only) |

### C. Assistant-mediated actions (which tool writes where)

| Signal | User action / trigger | Storage (table.col, file:line) | Explicit / inferred | Strength |
|---|---|---|---|---|
| C1. Tool-call audit wrapper | every mutating assistant tool call | `agent_actions` row for any tool with a capability (`AGENT_CAPS` `engine.py:2579-2601`, map `engine.py:2602`): auto path `engine.py:3254` + status `engine.py:3266-3269`, ask path `engine.py:3240-3253`/`3276-3283`; `events` for pending/applied/denied `engine.py:3234`, `3246`, `3272` | Automatic | behavioral observation |
| C2. `move_message` | user asks assistant to move | `record_move(..., 'assistant')` `engine.py:3621` → `undo_log` + `msg_events(kind='move')`; `messages.status='assistant-moved'`, `action_taken`, `folder`, `uid` `engine.py:3625-3629`; `events` `engine.py:3630`; `agent_actions` (cap `move`) | Mixed — machine act on user instruction | behavioral observation (weak user-intent evidence) |
| C3. `flag_message` | mark read/unread, star/unstar | **`events` only** `engine.py:3657` + `agent_actions` (cap `flag`); no column, no `msg_events` | Machine act | ambiguous (state not persisted; after the fact unqueryable) |
| C4. `create_folder` | assistant creates a folder | `events` only `engine.py:3669` + `agent_actions` (cap `create_folder`) | Machine act | behavioral observation (weak) |
| C5. `tag_message` | assistant tags/untags | `messages.user_tag` + **`user_tag_by='assistant'`** `engine.py:4188` (`store.py:1492-1499`); `events` `engine.py:4189`; `agent_actions` (cap `tag`); quarantined from tag-learning: `store.tagged_examples` excludes `user_tag_by='assistant'` by default `store.py:1506-1509` | Machine act, provenance-marked | machine label — correctly NOT treated as user feedback |
| C6. `delete_message` (Trash) | assistant moves to Trash (cap default **off** `store.py:46`) | `record_move(..., 'trash')` `engine.py:4243` → `undo_log` + `msg_events(kind='move')`; `messages.status='assistant-deleted'`, `action_taken='trash'` `engine.py:4248-4251`; `events` `engine.py:4252`; `agent_actions` (cap `delete`) | Machine act | behavioral observation (undoable through the same undo_log) |
| C7. `draft_reply` | assistant writes + saves a reply draft | `engine.save_draft` `engine.py:4222` → `events` only `engine.py:2132`; `agent_actions` (cap `draft`); body not persisted | Machine act | behavioral observation |
| C8. `send_message` | assistant actually sends mail (cap default **off** `store.py:47`) | SMTP send `engine.py:4324-4331`, optional `\Sent` append `engine.py:4336-4340`, `events` `engine.py:4343-4344`; `agent_actions` row (cap `send`) doubles as the sends-per-hour counter (`store.agent_actions_since` `store.py:1558-1564`, check `engine.py:4271-4276`) | Machine act | behavioral observation (**the only "a reply was sent" trace, and it is not linked to the original message row**) |
| C9. `classify_message` | assistant classifies | `msg_events(kind='classify')` via `classify_and_store` `engine.py:4159`; `events` `engine.py:4164`; `agent_actions` (cap `classify`); no `llm_log` row | Machine act | behavioral observation |
| C10. `train_classifier` / `manage_classifier` | assistant trains/enables/deletes a classifier | `heuristics` row (`store.add_heuristic` `store.py:619-626`, update/delete `engine.py:3912-3960`); `agent_actions` (cap `classifiers`) | Machine act on user instruction | explicit label (user asked for it) |
| C11. `delete_rule` / `set_rule_enabled` / `delete_flow` / `set_flow_enabled` | assistant edits automation | `rules`/`flows` rows + `events` `engine.py:3704`, `3725`; `agent_actions` (cap `rules`) | Machine act on user instruction | explicit label |
| C12. `propose_rule` / `propose_flow` (read-only tools: `mailbox_overview`, `search_messages`, `search_mail`, `semantic_search`, `read_message`, `list_*`, `list_tagged`, `evaluate_classifier`) | assistant proposal / reads | proposals → `assistant_messages.proposals` at stream end `engine.py:4133-4136` (`store.py:1211-1223`); applied marker `app.py:5063-5065` (`store.set_assistant_proposals` `store.py:1226-1229`); **read-only tools leave no trace except the truncated transcript** (`tools_log`: name + args≤300 chars + ok + summary `engine.py:4088-4094`) | Machine act; only the transcript records reads | behavioral observation (proposal apply = explicit label, C13) |
| C13. Proposal one-click apply | "Add/Update" on an assistant card `app.py:5068-5151` | `rules`/`flows` row added/updated `app.py:5109-5118`, `5135-5145`; proposal `applied` marker `app.py:5113`/`5119`/`5139`/`5146`; `events` `app.py:5114`, `5120`, `5140`, `5147` | Explicit — user accepts | explicit label |
| C14. Pending action approve / dismiss | approval card `POST /agent/actions/<id>/apply|dismiss` `app.py:5154-5203` | `agent_actions.status` = `applied`/`failed`/`dismissed` + `result` JSON `app.py:5180-5181`, `5200` (`store.set_agent_action` `store.py:1536-1544`); `events` `app.py:5182`, `5201` | Explicit — accept or reject | approve = explicit label; dismiss = explicit correction (the ONE place a rejection is recorded distinctly) |

### D. Derived / view-level signals (not stored, recomputed)

| Signal | Trigger | Where | Explicit / inferred | Strength |
|---|---|---|---|---|
| D1. "Sorted" list filter | view | `action_taken LIKE 'move%'` `store.py:1087-1088`, chip `app.py:3764` | Derived from B/C moves | behavioral observation |
| D2. "Tagged" list filter | view | `user_tag != ''` `store.py:1091-1092` — includes assistant- and flow-applied tags | Derived | ambiguous (provenance via `user_tag_by`) |
| D3. "Snoozed" filter / needs-reply count | view | `snoozed_until > now` `store.py:1077-1080`; needs-reply exclusion `app.py:244` | Derived | behavioral observation |
| D4. Stale-filing indicator | dashboard Undo card render | `recent_moves` compares `messages.folder` vs `undo_log.to_folder` `store.py:969-982` | Derived | behavioral observation |
| D5. External move discovered on view | opening a message whose stored folder/uid is wrong | `_find_message_location` `app.py:4065+` called at `app.py:4160`; row silently repaired (`folder`/`uid` rewritten `app.py:4185-4188`) — **no event, no signal** | Automatic detection, discarded | ambiguous (an external move is observable here but currently thrown away) |

---

## 4. Signals that do NOT exist today

Ordered by how much the learning design will miss them.

1. **Reply detection.** Nothing observes that the user replied to a message. The assistant's `send_message` logs an `events` line (`engine.py:4343`) but never links the sent mail to the original message row; `In-Reply-To`/`References` are never stored (confirmed in `docs/mail-intelligence/audit-data.md` §7); the Sent folder is never scanned (worker scans only `watch_folders`, `engine.py:1328-1340`). "This needed a reply" can therefore never be *confirmed* from behavior — only guessed.
2. **Read / open observation.** The message viewer records nothing (`app.py:4295-4319`); there is no last-opened / view-count / read-at signal. IMAP `\Seen` is never fetched into the DB (`fetch_meta` reads headers+body only, `engine.py:543-581`).
3. **External-client actions.** Moves/reads/flags/deletes done in the user's own mail client are invisible. A stale row is only noticed lazily when the message is opened, repaired silently (`app.py:4160-4188`), and the discovery is discarded. There is no reconciliation pass against IMAP flags/folders.
4. **Mark-important / star / read-state by the user.** No UI route and no column. Only rules/flows/assistant set `\Flagged`/`\Seen` (`engine.py:917`, `1390-1393`, `3646-3653`), and those outcomes are not persisted as message state (rules/flows put them in `action_taken` text; the assistant logs a free-text `events` line only, `engine.py:3657`).
5. **User delete / archive.** No user-facing route; the only delete is the assistant's `delete_message` (Trash), default **off** (`store.py:46`, `engine.py:4231-4255`). "Archive" as an intent does not exist.
6. **Explicit "keep" action.** `keep_ids` is written *only* by undo (`engine.py:1453`) and cleared only by the File button (`app.py:4506`). A user cannot protect a message directly, cannot see the list, and cannot edit/clear it except by filing.
7. **Negative-tag / "not this category".** The only ways to say "wrong label" are undo (filing outcomes) and dataset remove/re-include (classifier-scoped, and only reachable if the classifier already surfaced the message). There is no per-message "this isn't Notification" action.
8. **Bulk untag is unrecorded.** `POST /messages/untag` writes no `events` row and no `msg_events` row (`app.py:3788-3796`), and the column reset destroys the previous tag — unlike bulk tag, which at least logs an aggregate line (`app.py:3783`). Single-message tag/untag keeps a `msg_events(kind='tag')` line but not the old value either.
9. **Rule/flow lifecycle edits are mostly unlogged.** No record for rule edit/toggle/delete/move (`app.py:2758-2798`) or flow edit/move (`app.py:3414-3441`, `3464-3468`); only rule add, flow add/toggle/delete log events. Nothing records **template** CRUD (`app.py:3544-3573`) or **settings** changes (`app.py:5772-5800`).
10. **Proposal dismissal vs acceptance.** `rule_proposals.applied` is the same flag for both (`store.py:1621-1623`, `app.py:3890-3893`), and dismiss logs nothing — the negative signal from "the learned rule was wrong" is lost. (Assistant-side dismissals are fine: `agent_actions.status='dismissed'`, `app.py:5200`.)
11. **Draft content and edits.** Neither the generated draft (`engine.py:2101-2118`) nor the saved one (`engine.py:2121-2133`) is persisted; edits made in the textarea are lost. The strongest style/tone preference signal available to this product ("I changed what the model wrote") is not captured.
12. **Flow-applied tags are indistinguishable from user tags.** The flow `tag` step writes `user_tag` without setting `user_tag_by` (`engine.py:921-925`), so machine tags feed `store.tagged_examples` (`store.py:1506-1509`), tag-learning (`engine.py:2298`) and tag-sourced classifiers as if human. (The assistant's tag tool does mark provenance, `engine.py:4188`.)
13. **`status='kept'` is a snapshot, not a registry.** Set only in the scan rule path (`engine.py:1363`) and overwritable by a later scan (`engine.py:1414-1415`); the durable record is `keep_ids` (msgid only — no reason, no actor, ever).
14. **Classifier-pipeline asymmetries.** The assistant's `classify_message` bypasses `llm_log` (`engine.py:4148-4173`), so "manual classify" counts differ by entry point; and the dataset relabel route rewrites `llm_category`/`user_tag` without touching `status`, while the classified-source dataset only samples rows whose `status` is `classified`/`llm-moved` (`heuristics.py:228-231`) — a relabel can therefore move a message in the DB without moving it in the dataset.
15. **No search/query log.** The assistant's searches exist only in the truncated transcript (`engine.py:4088-4094`); the app's own list/search views record nothing. No "user looked for X and found Y" signal.
16. **No priority/importance labels of any kind** — no field, no tag convention, no ranking event (consistent with `audit-data.md`).
17. **`undo_log.undo_uid` and the `recent_moves` staleness check are the only external-move awareness**, and neither is written by an external move — only by an app-side undo.
18. **Nothing distinguishes "user started classify-all" from a worker run** except the in-memory `classifier.state` (`engine.py:2154`); both produce identical `msg_events(kind='classify')` and `llm_log` rows.

---

## 5. Bottom line for the learning design

- **CONFIRMED-label channels that exist today (usable now):** tags (`messages.user_tag`, A1–A4), dataset relabel / exclusion (A5–A7), undo + keep (A11–A12), proposal/action accept-reject (A18–A19, C13–C14). All are per-message and timestamped except bulk untag (A4) and proposal dismissal (A19).
- **Behavioral OBSERVATION channels (need an inference step before becoming labels):** filing outcomes (`undo_log` + `msg_events`, section B), snooze/wake, draft generation/save, and every assistant mutating tool via `agent_actions`.
- **Biggest instrumentation gaps to close first:** reply detection (1), read/open + external-client reconciliation (2–4), explicit keep (6), per-message negative labels (7), draft-edit capture (11), and provenance for flow-applied tags (12).
