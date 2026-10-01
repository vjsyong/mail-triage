# Mail-triage production DB — learning-data audit

**Snapshot:** 2026-10-01 15:02–15:05 UTC (live DB; worker writes every ~90 s).
**Method:** read-only SQLite (`file:/data/triage.db?mode=ro`, `uri=True`) via `docker exec -i mail-triage python3 -`. No writes performed.
**Container:** `mail-triage` (healthy), DB `/data/triage.db`, ~14.6 MB of user data (3,563 messages + 5,239 chunks + 3,599 LLM calls).

---

## 1. Volume and date range

- **Total messages: 3,563** (table `messages`)
- `date_ts` range: **0 → 1790864853** (2026-10-01 14:27:33 UTC). 3 rows have `date_ts = 0` (missing Date header: ids 1410, 3549, 3550).
- `sort_ts` (reliable sort column) range: **1753074023 (2025-07-21 05:00:23 UTC) → 1790864853 (2026-10-01 14:27:33 UTC)**, no NULLs/zeros.
- **All 3,563 rows were ingested/processed in one backfill window: 2026-09-30 08:11 → 2026-10-01 14:28 UTC** (`processed_at` range; `llm_log` and `events` start at the same moment). This DB has ~30 hours of live operation over a year of mail history.

### Messages per month (by `date_ts`)

| month | msgs |
|---|---|
| 1970-01 (date_ts=0 artifacts) | 3 |
| 2025-07 | 42 |
| 2025-08 | 106 |
| 2025-09 | 212 |
| 2025-10 | 199 |
| 2025-11 | 152 |
| 2025-12 | 259 |
| 2026-01 | 303 |
| 2026-02 | 246 |
| 2026-03 | 236 |
| 2026-04 | 271 |
| 2026-05 | 227 |
| 2026-06 | 425 |
| 2026-07 | 237 |
| 2026-08 | 237 |
| 2026-09 | 403 |
| 2026-10 (2 days) | 5 |

**Last 12 months (2025-11 → 2026-10): 3,001 messages.** Bodies are indexed as 5,239 chunks across 2,120 messages.

---

## 2. Label signal counts

### Human signal (user_tag)
- `user_tag` non-empty: **0 / 3,563**. `user_tag_by`: blank for all 3,563. **There is zero human label feedback in the production DB.**
- `snoozed_until`: 0 for all rows (snooze feature added 2026-10-01; never used yet).

### LLM verdicts (`llm_category`)
- non-empty: **3,554 / 3,563 (99.7%)**; 9 rows unclassified (ids 3549, 3550, 3552, 3556–3561).

| category | count |
|---|---|
| Notification | 1,381 |
| Action | 958 |
| Newsletter | 813 |
| Promo | 264 |
| Personal | 122 |
| Receipt | 16 |

### `classified_by` distribution
| value | count |
|---|---|
| `''` (empty) | 3,555 |
| `'llm'` | 7 |
| `'heuristic:4 Newsletter fast-path'` | 1 |
| NULL | 0 |

⚠️ **`classified_by` is not backfilled**: only 8 rows carry a real value (all classified after ~2026-10-01 03:00). The other 3,554 verdicts are known to be LLM output only via `llm_log` (distinct msg_id = 3,554). Do not use `classified_by` alone to gate training data.

### `llm_needs_reply`
- **0 → 2,619 | 1 → 935 | NULL → 9.** All 9 NULLs are the unclassified rows; no row has a category without a needs_reply verdict.

### `llm_confidence`
- non-null: **3,554** (the 9 unclassified have NULL). min = 0.8, avg = 0.9658, max = 1.0.
- buckets: **<0.5 → 0 · [0.5,0.8) → 0 · [0.8,0.95) → 482 · [0.95,1.0] → 3,072** (exactly 1.0: 1,604).
- ⚠️ confidence is highly concentrated (86% ≥ 0.95) — weak as a difficulty/uncertainty signal.

### needs_reply by category (class priors)
Action 863/958 (90.1%), Personal 64/122 (52.5%), Notification 7/1381 (0.5%), Promo 1/264 (0.4%), Newsletter 0/813, Receipt 0/16.

---

## 3. Outcome / event evidence

### `msg_events` (per-message audit timeline) — 2 rows total, both `kind='classify'`
| id | msg_id | ts | kind | detail (head) |
|---|---|---|---|---|
| 1 | 3563 | 2026-10-01 14:36:42 | classify | `{"category": "Notification", "confidence": 0.95, ...}` |
| 2 | 3562 | 2026-10-01 14:41:06 | classify | `{"category": "Action", "confidence": 1.0, ...}` |

No `move` / `undo` / `tag` / `snooze` / `wake` / `draft` / `guard` events exist yet. `sqlite_sequence` high-water mark for `msg_events` = 2 → the table has **never** held more rows.

### `undo_log` — 2 rows total (auto-increment seq = 2)
| id | ts | msg_id | from → to | source | undone_ts |
|---|---|---|---|---|---|
| 1 | 2026-10-01 14:21:42 | 3562 | INBOX → Personal | flow | 0 (pending) |
| 2 | 2026-10-01 14:28:19 | 3563 | INBOX → Notifications | auto-file | 0 (pending) |

**No undo has ever been performed (0 rows with `undone_ts > 0`).** Zero negative feedback.

### Why so little? — the trail is forward-only
Git log in `/home/xrim/mail-triage-intel` shows the audit trail was added **today**:
- `1b84fe5` 2026-10-01 14:18 UTC — "undo: trail for every machine/manual filing - undo_log + keep guard";
- `6dd2452` 2026-10-01 14:36 UTC — "per-message audit timeline (classify/rule/flow/move/undo/guard/tag/snooze/draft events)".

The ~2,478 filings performed during the 2026-09-30 backfill (before these commits) left no event rows.

### Messages with an LLM verdict AND a reconcilable outcome event
- llm verdict + any `move`/`undo`/`tag` event: **0**
- llm verdict + any event at all: **2** (both just 'classify')
- llm verdict + `undo_log` row: **2**
- llm verdict + **column-based** outcome (`action_taken` non-empty): **2,479** — usable as a static label but without an event timestamp.

### Static outcome proxies that DO exist (no timestamps)
`status`: `llm-moved` 2,447 · `classified` 1,085 · `assistant-moved` 22 · `new` 6 · `matched` 2 · `flow` 1.
`action_taken` top: `move:Notifications` 1,368 · blank 1,081 · `move:Newsletters` 813 · `move:Promotions` 264 · `move:Canvas` 18 · `move:Receipts` 16 · `move:PO/DPO` 1 · `flow:*` 2.
Current folder: Notifications 1,357 · Newsletters 807 · INBOX 649 · Sent Items 309 · Promotions 264 · Archive 136 · Canvas 18 · Receipts 16 · Personal 2 · Tasks 2 · Drafts 1 · PO/DPO 1 · Trash 1.
`llm_suggested_folder` non-empty: 2,474 (Notifications 1,381 / Newsletters 813 / Promotions 264 / Receipts 16) — agreement between suggested and actual move is directly checkable.
`events` app log: 5,272 rows (info 3,855 / debug 1,383 / warn 23 / error 11), 2026-09-30 08:11 → 2026-10-01 15:02; mentions 'move' 2,498, 'classif' 3,581, 'undo' 0, 'draft' 8. Text log only — not keyed for labels.

---

## 4. `llm_log`

- Columns: `id, ts, msg_id, ok, error` — **no model name, latency, tokens, prompt or raw response columns.**
- Total rows: **3,599**; `ok=1` for all 3,599; 0 rows with an error; distinct `msg_id` = **3,554** (≈1 entry per classified message; 45 extra rows = re-classifications).
- `ts` range: 2026-09-30 08:11:24 → 2026-10-01 14:41:06 (only 2 calendar days).
- Per-day (only 2 days exist): 2026-09-30 → 3,590; 2026-10-01 → 9.
- Per-hour (backfill shape): 09-30 13:00 → 3,172 (bulk classify run), 09-30 14:00 → 378, 09-30 08:00 → 35; 10-01: 03:00 → 4, 06:00 → 1, 08:00 → 1, 14:00 → 3.

The log proves LLM classification happened but stores nothing trainable beyond the message link.

---

## 5. `heuristics` (2 rows)

| id | name | kind | category | enabled | min_conf | created_by | updated | stats.samples | labels | negatives | trained_label_count | excluded |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | Newsletter fast-path | decision_list | Newsletter | 1 | 0.8 | demo | 2026-09-30 16:29 | **376** | Newsletter / \_\_other\_\_ | 282 | 94 | [3128] |
| 6 | Promo Fast-path | decision_list | Promo | 1 | 0.8 | assistant | 2026-09-30 16:44 | **172** | Promo / \_\_other\_\_ | 129 | 43 | [] |

- Both models embed a full decision list in the `model` column (40 conditions each, tokens like `s:calendar@ust.hk`, `b:seminar`, with precision/support stats).
- `stats.source = 'classified'` and `stats.weak_labels = true` for both: the training sets were **bootstrapped from LLM labels, not human labels** (376 and 172 samples respectively).
- `heuristics` seq high-water mark = 6 → 4 earlier heuristic versions were deleted. Only 1 message was ever classified **by** a heuristic (`classified_by='heuristic:4 ...'`, msg 105).

---

## 6. Crosstab: category × classified_by (top 8 — only 6 categories exist)

| category | llm | user | heuristic | blank | NULL | other | total |
|---|---|---|---|---|---|---|---|
| Notification | 4 | 0 | 0 | 1,377 | 0 | 0 | 1,381 |
| Action | 1 | 0 | 0 | 957 | 0 | 0 | 958 |
| Newsletter | 1 | 0 | 1 | 811 | 0 | 0 | 813 |
| Promo | 0 | 0 | 0 | 264 | 0 | 0 | 264 |
| Personal | 1 | 0 | 0 | 121 | 0 | 0 | 122 |
| Receipt | 0 | 0 | 0 | 16 | 0 | 0 | 16 |

The 7 `classified_by='llm'` rows are ids 30, 1410, 3553, 3554, 3555, 3562, 3563; the one heuristic row is id 105. **No human (`user`) classification exists anywhere.**

---

## 7. Thread / linkage

- **Message-ID:** 3,560 / 3,563 messages carry a non-empty `msgid`; all distinct (no duplicates); no angle brackets in stored values. 3 missing: ids 1410 (Drafts), 3549, 3550 (Tasks, empty messages).
- **In-Reply-To / References: NOT STORED.** A column-name scan over every table for `refer|reply|thread|parent|link` matched only `messages.llm_needs_reply`. **True threads cannot be reconstructed from headers.**
- **Senders:** **290 distinct** normalized (`lower(trim(from_addr))`); 2 messages have an empty `from_addr`.
- **Top 10 senders** (n / needs_reply=1 / user_tag / action_taken):

| sender | n | needs_reply=1 | user_tag | action_taken |
|---|---|---|---|---|
| calendar@ust.hk | 415 | 0 | 0 | 415 |
| seanyong@ust.hk | 400 | 253 | 0 | 73 |
| catering@ust.hk | 159 | 0 | 0 | 159 |
| media@ust.hk | 147 | 0 | 0 | 147 |
| cei@ust.hk | 114 | 1 | 0 | 113 |
| aloysius@ust.hk | 94 | 67 | 0 | 25 |
| safety@ust.hk | 74 | 4 | 0 | 68 |
| notifications@instructure.com | 64 | 4 | 0 | 63 |
| oktevent@ust.hk | 63 | 2 | 0 | 59 |
| no-reply@zoom.us | 60 | 0 | 0 | 60 |

- **Subject-thread proxy** (only linkage available): 2,040 distinct normalized subjects; **376 subjects contain ≥2 messages** (reconstructable pseudo-threads) covering **1,890 messages**; largest: "hkust daily event alert" 354, "hkust weekly event summary" 61, "you have late tasks" 31.
- Supporting context: 5,239 body chunks across 2,120 messages (FTS indexed); embeddings 2560-dim (Qwen/Qwen3-Embedding-4B); `body_html` stored for 3,452; `index_state` covers Archive 133, Canvas 18, Drafts 1, INBOX 646, Newsletters 813, Notifications 513(working). `keep_ids` 0, `rule_proposals` 0, 12 rules (11 enabled), 1 flow enabled. Assistant chat: 19 sessions / 42 messages, but effective proposal content is nearly empty (38 rows = `[]`, 4 non-empty). `llm_reason` persisted for 8 rows, `llm_thinking` for 7 (both recently added), `llm_summary` for 3,553.

---

## Verdict: how much data exists TODAY for training `needs_reply` / priority

**`needs_reply`:** usable **model-label** supervision exists for **3,554 messages** (935 positive / 2,619 negative) — a decent class balance as-is, all with confidence ≥ 0.8 (86% ≥ 0.95). But **every label is LLM-generated**; the DB contains **zero human corrections** (user_tag 0/3,563, zero undos, zero snoozes, no human-classified rows). Outcome reconciliation is possible only **implicitly and untimestamped** via `status`/`action_taken`/`llm_suggested_folder` (2,479 verdicts with an `action_taken`, 2,474 with a suggested folder) — and the move direction was itself driven by these same verdicts, so it corroborates rather than independently labels. The timestamped audit machinery (`msg_events`, `undo_log`) went live only at 14:18–14:36 UTC today: it holds **2 classify events and 0 outcome events**, and will accumulate real outcome/undo signal only from now on. **Bottom line: ~3.55k rows of free LLM-generated labels (noisier than it looks given 0.95+ confidence inflation), ~0 rows of human/outcome ground truth today; the pipeline to collect genuine feedback (tag/undo/snooze/move events) started logging today and needs weeks of runtime plus deliberate human tagging.** A real training set should be seeded by human re-labeling a sample of the 3,554 verdicts.

**Priority:** **there is no priority signal at all in this DB.** No priority field, no priority tags, no ranking events, no urgency labels exist in any table. The only learnable targets are category (6 classes), needs_reply (binary), and folder destination (via action_taken/suggested-folder agreement). Thread context for features is limited to subject-proxy pseudo-threads (376 threads / 1,890 msgs) since In-Reply-To/References are never stored; sender (290 distinct), subject, snippet, and 2,120 chunk-indexed bodies are available as features.
