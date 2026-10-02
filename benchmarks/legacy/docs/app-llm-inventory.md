# Mail-Triage — LLM Task Inventory (Phase 1 audit)

Audit date: 2026-10-02. Source: `~/mail-triage` @ master `5374d26` (the deployed
line; live container inspected where noted). Worktree for this work:
`~/mail-triage-bench` (branch `model-bench`).

The application talks to ONE OpenAI-compatible endpoint (production: local vLLM
`gemma-4-26b-a4b` @ `:8040`, 16K ctx, AWQ-4bit + int8 KV). There are **7 distinct
LLM call sites**; everything funnels through `engine.LLMClient`.

## Call sites

### 1. `LLMClient.classify()` — mail classification (engine.py:1206)
- **Purpose**: classify each unclassified message into one of the configured
  categories + decide `needs_reply` + write a one-line summary/reason.
- **Callers**: worker LLM queue (`_process_llm_queue`, cap `max_llm_per_hour=40`,
  batch `llm_batch_per_cycle=5`), manual single-message button (`app.py:4554`),
  bulk `ClassifyJob` (thread pool, concurrency 16, explicit user action — no cap).
- **System prompt** (fixed template, categories injected):
  `You triage incoming email for Sean. Reply with a single JSON object and nothing
  else. Shape: {"category": one of [Action, Notification, Newsletter, Receipt,
  Personal, Promo], "needs_reply": true|false, "confidence": 0.0-1.0,
  "summary": "...", "reason": "..."}` (~90 tokens).
- **User content**: `From/To/Subject/Date` headers + body text **truncated to
  1500 chars** (snippet or MIME-salvaged readable body).
- **Output**: JSON object; regex-tolerant parse; **thinking ON** (CoT arrives in
  `message.reasoning`, JSON stays clean); `response_format=json_object`;
  `temperature=0`; `max_tokens=4096`, one 8192 retry on `finish_reason=length`.
- **Retrieval**: none. **Tools**: none. **History**: none.
- **Side effects**: YES — auto-files into `category_folders` when `llm_apply`,
  triggers classify-conditioned flows, feeds `msg_events` audit + learning hook.
- **Failure handling**: 3-strike park (`status=error`), optional fallback endpoint,
  heuristics (2 decision-list fast-paths: Newsletter, Promo) run BEFORE the LLM.
- **Measured context**: classified messages avg body 1,744 chars → ~400-600 token
  prompt. Live single call (~cold): 11.8 s wall, 188 prompt tok, 1,326 completion
  tok (CoT-heavy, ~4.7K chars thinking). CoT avg stored: 2,500 chars.

### 2. `LLMClient.draft_reply()` — reply drafting (engine.py:1245)
- **Purpose**: write a reply body (plain text) for a message; used by the viewer
  "Draft reply" card and by flow `draft` steps (mode llm).
- **System**: persona ("You write email replies as Sean… Output ONLY the
  plain-text reply body"), template guidance + free-text instructions (≤1000 chars).
- **User**: original headers + body up to **6000 chars**. **JSON**: no. temp 0.
- **Side effects**: none (user reviews; save appends to IMAP Drafts).

### 3. `example_draft_for()` — simulator example email (engine.py:1600)
- **Purpose**: generate a sample email that satisfies a rule/flow, for the
  simulator. JSON `{from,subject,body}`; deterministic condition-built fallback.
- **Context**: rule/flow text (small). Low stakes, low frequency.

### 4. `propose_rules_from_tags()` — "Learn rules from tags" (engine.py:2386)
- **Purpose**: from the user's manual tags (≤80 rows: tag/from/subject lines) +
  existing rules/flows + categories, propose up to 5 filter rules as JSON
  `{reply, proposed_rules:[{name,match_mode,conditions[],actions{},placement}]}`.
- **Structured**: YES (strict JSON; guard-rule semantics: empty actions).
- **Side effects**: proposals applied only by explicit user click. Input can be
  2-6K chars. Failure → error surfaced; no auto-apply.

### 5. `AssistantAgent.stream()` — streaming tool-calling agent (engine.py:3269)
- **Purpose**: the chat assistant: answers mailbox questions AND acts (move/flag/
  create folder; proposes rules/flows; trains classifiers; drafts/sends mail).
- **System**: `ASSISTANT_SYSTEM` (6.2 KB: workflow, output discipline, permissions)
  + `_assistant_context()` (live rules/flows/categories/folders/counts ~1.5 KB)
  + optional page-context block (e.g. "the user is reading message #367").
- **Tools**: 27 OpenAI function schemas (17.5 KB) — search_messages, search_mail,
  semantic_search, read_message, move/flag/create_folder, list_tagged,
  propose_rule/propose_flow, delete_rule/set_rule_enabled, list_flows,
  train_classifier/list_classifiers/manage_classifier/evaluate_classifier,
  classify_message, tag_message, draft_reply, delete_message, send_message, etc.
- **History**: last 24 messages of the session. **Stream**: yes (SSE);
  `temperature=0`, `max_tokens=2500`, `repetition_penalty=1.05`, thinking ON.
- **Loop**: ≤8 tool rounds + 1 forced wrap-up; ≤4 tool calls per model turn;
  each tool result ≤4,500 chars fed back; 30K transcript budget; repetition-loop
  guard kills degenerate CoT.
- **Side effects**: YES (folder/flag moves live per agent permissions; rule
  proposals one-click; send_message gated by capability level).
- **Failure handling**: tools-unsupported fallback (drops tools), empty-reply
  detection, fallback endpoint.

### 6. `_summarize_thoughts()` — one-line reasoning summary (engine.py:4438)
- Cosmetic chip text ("Checked the rule list…"); max_tokens 60; trivial.

### 7. `settings_test_llm` — connectivity ping (app.py:5934)
- "Reply with the single word ok". Trivial.

## Capability matrix (drives benchmark composition)

| Task | Frequency | Difficulty | Structured output | Tool use | Typical context | Failure cost |
|---|---|---|---|---|---|---|
| classify | VERY HIGH (every msg; 3,600 calls logged; caps 40/h + bulk jobs) | low–med | strict JSON | none | ~0.4–0.9K tok | medium: misfile/wrong folder, wrong needs_reply; worst: mislabel Action mail |
| draft_reply | med (on demand + flows) | med | plain text | none | 1–2.5K tok | high: user may send it |
| learn from tags | low | high | strict JSON (rules) | none | 2–6K tok | high: bad rules auto-file future mail |
| simulator example | low | low | JSON | none | <0.5K tok | low |
| assistant agent | med (23 sessions so far) | HIGH | tool-calls + text | 27 tools | 3–15K tok | CRITICAL: acts on real mail; injection-prone |
| thought summary | per chat turn | trivial | short text | none | ≤6K chars | cosmetic |
| endpoint test | rare | trivial | none | none | tiny | none |

## Why this shapes the benchmark
1. **Classification is the volume workload** → most cases; category + needs_reply
   + JSON validity are programmatically scorable.
2. **The assistant is the critical-risk workload** → tool-selection, argument
   correctness, grounding, and prompt-injection resistance get their own suites;
   severity weighting (CRITICAL) lives here.
3. **Drafts and rule learning are low-frequency but user-visible** → rubric /
   schema scoring on a smaller sample.
4. **Thinking-mode coexistence + JSON mode + tool calling + streaming** are all
   production behaviors that candidates must reproduce (the same payloads are
   replayed by the harness).
5. Current mitigations already in place (heuristic fast-paths, guard rules,
   confidence routing opportunities) determine the "quality required AFTER
   routing" — Phase 17 of the brief.
