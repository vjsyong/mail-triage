# Audit: how mail-triage classifies mail today

**App:** `~/mail-triage-intel` @ commit `c6db3c5` (2026-10-01).
**Files audited:** `heuristics.py` (464 lines), `engine.py` (4,383), `store.py` (1,334), `app.py` (6,844), `rag.py` (526).
**Method:** full read of `heuristics.py`; targeted reads of every classification/decision site in `engine.py`, `store.py`, `app.py`, `rag.py`. Read-only audit — no application code was modified.

**Pipeline in one line:** scan (`_process_folder`: rules → deterministic/topic flows → `queued`) → classify (`classify_verdict`: enabled heuristics first, LLM only on abstention) → post-verdict category flows → guard check → category→folder filing when `llm_apply` is on.

---

## A. `heuristics.py` — the deterministic classifier registry

### A.1 Registry

| Item | Detail | Ref |
|---|---|---|
| Registry dict | `KINDS = {}` — name → `{"train", "predict", "describe", "blurb"}` | `heuristics.py:165` |
| Plugin API | `register_kind(name, train_fn, predict_fn, describe_fn, blurb="")` | `heuristics.py:168-170` |
| Introspection | `kind_names()` → sorted names | `heuristics.py:173-174` |
| Dispatch | `train(kind, examples, params=None)` raises `RuntimeError` on unknown kind | `heuristics.py:177-180` |
| | `predict(kind, model, feats)` → `fn(model, feats)` or `None` | `heuristics.py:183-185` |
| | `describe(kind, model, limit=6)` → str or `""` | `heuristics.py:188-190` |
| Shipped registrations | `register_kind("decision_list", ...)` at `:193-195`; `register_kind("naive_bayes", ...)` at `:196-198` | `heuristics.py:193-198` |

There are exactly **two shipped kinds**; nothing else in the app calls `register_kind` (verified by grep). Adding a kind requires only a `register_kind` call — the pipeline, DB, UI and assistant tools are kind-agnostic (`heuristics.py:10-11`).

### A.2 Kinds implemented

**`decision_list`** — learned ordered token conditions with precision/support gating.
- `train_decision_list(examples, params)` → `(model, stats)` — `heuristics.py:115-134`.
  - `params`: `min_precision` (default 0.9), `min_support` (default 3) — `:116-117`.
  - Builds per-label token counts via `_label_token_counts` (`:62-71`); candidates need `support >= min_support` and **shrunk precision** `(c+0.5)/(tot+1) >= min_precision` (`:123-128`).
  - Candidates sorted by `(-prob, -support)`, capped at **40 conditions** — `:131-132`.
  - Model: `{"conditions": [{token, label, prob, precision, support, seen}, ...]}`; stats: `{samples, labels, conditions}` — `:132-134`.
- `predict_decision_list(model, feats)` → `(label, prob, detail)` or `None` — `heuristics.py:137-147`.
  - Returns the **first matching condition in model order** (not the highest-probability one) — `:138-146`.
  - `detail` is human-readable: `"domain contains 'x' (92% over 12 mails)"` — `:141-145`.
- `describe_decision_list` → `"; "`-joined condition list or `"(no conditions met the precision bar)"` — `:155-162`.

**`naive_bayes`** — multinomial NB over sender/domain/subject/body tokens.
- `train_naive_bayes(examples, params)` → `(model, stats)` — `heuristics.py:76-93`.
  - `params`: `max_vocab` (default 4000), `min_df` (default 1) — `:77-78`.
  - Per-class `log_prior` and Laplace-smoothed `log_lik` per vocab token — `:83-90`.
  - Model: `{"classes": {label: {log_prior, log_lik}}, "vocab": n}`; stats: `{samples, labels, vocab}` — `:91-92`.
- `predict_naive_bayes(model, feats)` → `(best_label, prob, None)` or `None` — `heuristics.py:96-112`.
  - `prob` = softmax-normalized posterior over the classes present in the model — `:109-112`.
  - **Third element is `None`** (no detail string).
- `describe_naive_bayes` → `"naive Bayes, vocab N, classes: ..."` — `:150-152`.

### A.3 Classifier interface (function names + signatures)

The registry interface is **train/predict/describe** (there is no per-kind `evaluate`, `classify` or `serialize` in the registry; those are module-level):

| Function | Signature | Returns | Ref |
|---|---|---|---|
| `featurize` | `featurize(msg)` | `{"tokens": {...}, "from", "domain", "subject", "body"}` | `heuristics.py:42-59` |
| per-kind train | `train_fn(examples, params)` | `(model, stats)` — both JSON-serializable | `:76`, `:115` |
| per-kind predict | `predict_fn(model, feats)` | `(label, prob, detail)` or `None` | `:96`, `:137` |
| per-kind describe | `describe_fn(model, limit=6)` | str | `:150`, `:155` |
| dispatch | `train(kind, examples, params=None)` | `(model, stats)` | `:177-180` |
| dispatch | `predict(kind, model, feats)` | `(label, prob, detail)` / `None` | `:183-185` |
| dispatch | `describe(kind, model, limit=6)` | str | `:188-190` |
| register | `register_kind(name, train_fn, predict_fn, describe_fn, blurb="")` | None | `:168-170` |
| pipeline inference | `classify(msg)` | verdict dict or `None` | `:385-409` |
| training end-to-end | `train_heuristic(kind, category, source="tags", params=None, limit=500, min_confidence=0.8, name="", created_by="assistant", exclude=None)` | `(model, stats)` | `:322-336` |
| evaluation | `evaluate_heuristic(heuristic, limit=500)` | metrics dict | `:339-377` |
| dataset review | `dataset_for(h, limit=2000)` | display dict | `:278-319` |
| UI view | `view(h)` | dict | `:447-464` |
| auto-retrain | `auto_refine()` | `[(id, name, new_labels)]` | `:415-442` |

`examples` is always `[(label, feats), ...]` where feats comes from `featurize` (`heuristics.py:266-275`).

**Serialization:** there are no `serialize`/`deserialize` functions. Models and stats are plain JSON and are serialized at the DB boundary with `json.dumps` (write) / `json.loads` (read):
- writes: `store.add_heuristic(..., model=json.dumps(model), stats=json.dumps(stats))` (`engine.py:3913-3915`), `store.update_heuristic(hid, model=json.dumps(model), stats=json.dumps(new_stats))` (`app.py:2421`, `heuristics.py:438`).
- reads: `json.loads(h["model"])` in `heuristics.classify` (`:391`), `evaluate_heuristic` (`:350`), `view` (`:453-456`).

### A.4 How models are stored in the DB

`heuristics` table (`store.py:155-168`):

```
id, name, kind, category, enabled, min_confidence (default 0.8), priority (default 0),
model TEXT '{}', stats TEXT '{}', excluded TEXT '[]', created_by, created, updated
```

- CRUD: `store.list_heuristics(enabled_only=False)` — `ORDER BY priority, id` (`store.py:532-538`); `get_heuristic` `:541-544`; `add_heuristic` `:547-556`; `update_heuristic` (arbitrary columns) `:559-565`; `delete_heuristic` `:568-570`.
- `excluded` was added by migration — `store.py:279-281`.
- `model` = trained weights (conditions / per-class likelihoods); `stats` = provenance + training metadata: `source`, `trained_label_count`, `negatives`, `excluded`, `trained_at`, `params`, `weak_labels`, plus kind stats (`samples`, `labels`, `vocab`/`conditions`) and `refined_from` after auto-refine (`heuristics.py:330-335`, `:437`).
- **One special semantics:** `min_confidence` is per-classifier and gates `classify()` (`heuristics.py:400`); `priority` is never written by the UI/assistant (`add_heuristic` default 0) — it only affects the list order used for tie-breaking (`store.py:536`, `heuristics.py:402-405`).

### A.5 How `excluded` samples are applied

- `heuristic_excluded(h)` parses the JSON column to a set of message ids; tolerates bad JSON (`heuristics.py:240-245`).
- Applied in **training** by `sample_rows(...)`: drops excluded ids from both positives and negatives before the negative cap (`heuristics.py:259-262`); `build_examples` calls it (`:272`).
- Applied in **dataset display** by `dataset_for`: samples stay visible but flagged `excluded: true` and counted in `pos_excluded` / `neg_excluded` (`heuristics.py:294`, `:311`, `:318-319`).
- Applied by **every training path** (verified): UI retrain `exclude=heuristics.heuristic_excluded(row)` (`app.py:2420`), assistant train/retrain (`engine.py:3903-3908`), auto-refine (`heuristics.py:436`).
- Written by the dataset page: remove → add id, re-include → discard id, stored via `store.update_heuristic(hid, excluded=json.dumps(sorted(excl)))` (`app.py:2569-2588`).
- Exclusions affect the next trained model only; they never re-label the message (relabel is separate, see A.10).

### A.6 How `__other__` abstention works

- `OTHER_LABEL = "__other__"` (`heuristics.py:204`) is the label given to every **negative** training example in `build_examples` (`:274`).
- Kinds learn negative-class conditions too (decision-list candidates per label include `__other__`; naive Bayes gets an `__other__` class). A positive-class match therefore competes with explicit "not this category" evidence.
- At inference, any predicted label starting with `__` is **discarded and the heuristic abstains** for that message: `if str(label).startswith("__"): continue` (`heuristics.py:398-399`). This is what keeps `__other__` from ever being emitted as a category.
- Abstention = the classifier contributes no verdict; if every enabled heuristic abstains (or none clears its `min_confidence`), `classify()` returns `None` and the message falls through to the LLM (`engine.py:1910-1912`).

### A.7 How training sets are derived

`build_examples(category, source="tags", limit=500, exclude=None)` (`heuristics.py:266-275`) → positives labelled with the category, negatives labelled `__other__`:

- **source `"tags"`** (the good labels): positives = rows whose `user_tag` (lowercased) equals the category; negatives = all other tagged rows — `sample_rows` `:251-255`, fed by `store.tagged_examples(limit)` (`store.py:1217-1224`, excludes assistant-sourced tags unless `include_agent`).
- **source `"classified"`** (bootstrap/weak labels): positives = `store.messages(limit)` rows with `llm_category == category` and `status in ("classified","llm-moved")` (`labels_from_classified`, `:228-231`); negatives = classified rows of any **other** category (`negatives_from_classified`, `:234-237`).
- **Negative sampling cap:** `neg = neg[:max(20, 3 * len(pos))]` — at least 20, at most 3× positives, so precision numbers stay meaningful (`:262`, rationale at `:268-271`).
- **Minimum:** `MIN_EXAMPLES = 5` labelled positives, else `train_heuristic` raises with a "tag mail" hint (`:203`, `:325-328`).
- `train_heuristic` then trains the kind, then augments stats with `source`, `trained_label_count`, `negatives`, `excluded`, `trained_at`, `params`, `weak_labels = (source != "tags")` (`:329-335`).
- `dataset_for` shows only the first 300 positives / 250 negatives but includes true totals (`:278-319`).

### A.8 Featurization (`featurize`)

`featurize(msg)` (`heuristics.py:42-59`) works on a `messages` **row/dict** and produces a token-count map + raw fields:

- `from_addr` lowercased; domain extracted as text after `@` — `:44-45`.
- Tokens are prefixed by source field: `d:` domain ×1, `s:` full sender ×2, `t:` subject tokens ×2 (first 40 regex matches), `b:` body tokens ×1 (first 400) — `:48-58`.
- Token regex `[a-z0-9@._\-]{3,}`; tokens in `STOPWORDS` (English + MIME scaffolding words) are dropped — `:29`, `:31-39`, `:53-58`.
- Body text comes from `msg["snippet"]`, lowercased, passed through `_clean_body_for_features` (`:47`, `:211-219`), which:
  1. lazily imports `engine` and, if `engine.looks_like_mime_junk(body)`, replaces it with `engine.readable_body(body, limit=2000)` (`:214-216`) — repairs legacy raw-MIME snippets;
  2. scrubs leftover MIME words with `_MIME_SCRUB` (`:205-208`, `:219`).
- Returns `{"tokens": {...}, "from": ..., "domain": ..., "subject": ..., "body": ...}`; only `tokens` is used by both shipped kinds' predict paths (`:101`, `:138`).

### A.9 Runtime inference path (`classify`)

`heuristics.classify(msg)` (`heuristics.py:385-409`), called once per message by `engine.classify_verdict` (`engine.py:1904`) when `heuristics_enabled` is true (setting default `True`, `store.py:22`):

1. `featurize(msg)` once (`:387`).
2. For every **enabled** heuristic, in `priority, id` order (`store.list_heuristics(enabled_only=True)`, `store.py:532-538`):
   - parse `model` JSON (bad JSON → skip) — `:389-393`;
   - `predict(kind, model, feats)`; no output → skip — `:394-396`;
   - label starting `__` → abstain — `:398-399`;
   - `prob` missing or `< heuristic.min_confidence` (default `DEFAULT_MIN_CONFIDENCE = 0.8`) → skip — `:382`, `:400-401`;
   - keep the **maximum-probability** verdict; ties go to the earlier heuristic (strict `>`) — `:402-405`.
3. Winner → `{category, confidence, heuristic_id, heuristic_name, kind, detail, reason}` where `reason` is `"heuristic '<name>' (<kind>) - <detail>"` (detail only exists for decision_list) — `:403-408`. No winner → `None`.

Note: `classify()` re-reads the whole heuristics table and re-parses model JSON **for every message**; there is no in-process model cache (`:389-394`).

### A.10 Lifecycle & entry points

- **Train**: assistant tool `train_classifier` (`engine.py:3881-3936`, tool declared `:2510`); retrains by `retrain_id`, validates `kind in kind_names()`, writes with `add_heuristic`/`update_heuristic`.
- **Retrain from UI**: `/classifiers/<id>/retrain` re-runs `train_heuristic` with the stored `source`/`params` and applies `excluded` (`app.py:2410-2427`).
- **Auto-refine**: worker cycle end, if `heuristic_autorefine` is on; tag-sourced classifiers only; retrains when `current_labels - trained_label_count >= AUTO_REFINE_MIN_NEW (5)`, preserving `params` and `excluded`, and records `refined_from` (`heuristics.py:412-442`; invoked `engine.py:1315-1320`).
- **Evaluate**: `evaluate_heuristic` is **in-sample over the current label source** — positives correct iff predicted as the category, negatives correct iff predicted as anything else; reports `accuracy`, `false_positives`, `positives/negatives`, up to 3 `mistakes` — `heuristics.py:339-377`. Exposed only via the assistant tool `evaluate_classifier` (`engine.py:3967-3989`); **not persisted**.
- **Enable/disable/delete**: `/classifiers/<id>/toggle`, `/delete` (`app.py:2399-2437`); assistant `manage_classifier` (`engine.py:3944-3966`); `register_kind` list surfaced by `list_classifiers` (`engine.py:3938-3942`).
- **Dataset review page**: `/classifiers/<id>/dataset`, sample remove/re-include (`app.py:2526-2598`), relabel (`app.py:2540-2566`): tag-sourced → rewrites `user_tag`; classified-sourced → rewrites `llm_category` + `classified_by="user"` + `llm_confidence=1.0` (`:2555-2559`).

---

## B. `engine.py` — the classification path

### B.1 `classify_verdict(msg, settings)` — heuristic-first, then LLM

`engine.py:1901-1913`:

1. `hres = heuristics.classify(msg)` if `settings["heuristics_enabled"]` (default true), else `None` — `:1904`.
2. **Heuristic win:** build `res = {category, confidence, summary: "", reason: hres["reason"], needs_reply: False, _heuristic_id, _heuristic_name}` — `:1905-1909`. Note the hard-coded **`needs_reply=False` and empty `summary`** for heuristic verdicts (the honest default; the LLM is never asked, so those fields carry no information).
3. **Abstention:** `res = LLMClient().classify(msg, settings["categories"], settings["my_name"])` — `:1910-1912`.
4. Returns `(res, hres)`.

Also used by the simulator dry-run (`engine.py:1609`) and `classify_and_store` (`:1925`).

### B.2 `classify_and_store(msg, settings, mc=None)` — one message end-to-end

`engine.py:1916-2025` (docstring: reused by worker queue and manual batch job; raises on LLM failure):

1. `res, hres = classify_verdict(msg, settings)` — `:1925`.
2. Extract `category`, `conf` (float, default 0) — `:1926-1930`.
3. `folder = settings["category_folders"].get(category, "")` — `:1931`.
4. Preconditions: `already_filed = action_taken.startswith("move")`; `kept = store.is_kept(msgid)` — `:1932-1933`.
5. **Persist verdict fields** (`:1934-1944`): `llm_category`, `llm_confidence`, `llm_summary[:200]`, `llm_reason[:200]`, `llm_thinking[:6000]`, `llm_needs_reply` (0/1), `llm_suggested_folder`, `classified_by`, `status="classified"`.
6. **Per-message audit event** with category/confidence/by/reason/summary/needs_reply/thinking as JSON (`store.log_msg_event(..., "classify", ...)`) — `:1945-1949`.
7. Verdict object for fuzzy flows: `{"category", "confidence", "source": "heuristic"|"llm"}` — `:1952-1953`.
8. **Post-verdict category flows**: `classify_flows = enabled flows where _needs_verdict(flow)` (`:1954`); guard check runs first if any filing/flow could act and the message was not already moved (`:1956-1968`); then flows are evaluated in list order and the **first match runs** via `_process_flow`, tagged `why="AI category <cat> <N>%"`; on success `res["_flow"]` is set and the row status becomes `flow`/`flow-dry` (`:1969-1998`).
9. **Category-map filing** (`:1999-2023`), only if `filing_wanted = settings["llm_apply"] and folder` (`:1955`) **and** not already filed **and** no guard **and** not kept **and** no flow matched:
   - open/select MailClient if needed, `ensure_selected(msg.folder)`, `ensure_folder(folder)` — `:2000-2005`;
   - `store.record_move(msg, folder, "auto-file")` (undo trail) — `:2006`;
   - IMAP move, then `status="llm-moved"`, `action_taken="move:<folder>"`, new `folder`/`uid`, `res["_moved_to"]` — `:2007-2013`;
   - message event `"auto-filed to '<folder>' (LLM suggested)"` — `:2014-2015`.
10. Final `store.update_message(msg["id"], **fields)` — `:2024`; return `res`.

### B.3 The LLM call and its JSON contract

`LLMClient.classify(msg, categories, my_name="Sean")` — `engine.py:1205-1242`:

- **System prompt** (`:1207-1210`) demands one JSON object, shape:
  `{"category": one of [<settings.categories>], "needs_reply": true|false, "confidence": 0.0-1.0, "summary": "one short sentence", "reason": "why, max 15 words"}`.
  If `categories` is empty the fallback list is `Action, Notification, Newsletter, Receipt, Personal, Promo` — `:1206`.
- **User prompt** (`:1211-1216`): `From/To/Subject/Date` + body from `msg["snippet"]` (MIME-junk snippets are repaired with `readable_body(limit=1500)` first — `:1212-1213`), body truncated to **1500 chars**.
- **Call** (`:1222-1231`): `_chat(..., json_mode=True, max_tokens=4096, full=True, thinking=(llm_thinking != "off"))`; on empty content with `finish_reason == "length"` retries once at `max_tokens=8192`.
- **Parsing** (`:1224-1239`): regex `\{.*\}` over content (copes with code fences); `json.loads`; requires a dict with non-empty `category`, else `RuntimeError("LLM JSON missing category")`.
- **Thinking** (`:1232-1241`): `message.reasoning` / `reasoning_content` captured and attached as `res["_thinking"][:6000]`.
- **Fields returned to the caller:** `category`, `needs_reply`, `confidence`, `summary`, `reason` (model-authored) + `_thinking` (added by the client). No other field is validated; `needs_reply`/`confidence`/`summary`/`reason` may be missing and default downstream (`conf` → 0.0 at `:1927-1930`; `needs_reply` falsy → 0; `summary`/`reason` → `""`).
- Transport details: `_chat_once` posts to `<base>/chat/completions`, `temperature: 0`, optionally `chat_template_kwargs={"enable_thinking": true}` and `response_format={"type":"json_object"}`, stripping extensions one-by-one on 400/404/422 (`:1046-1085`); primary→fallback failover with an event log (`_chat_convo`, `:1028-1044`).

**Where the raw fields land:** `llm_category` :1935, `llm_confidence` :1936, `llm_summary` :1937, `llm_reason` :1938, `llm_thinking` :1939, `llm_needs_reply` :1940, `llm_suggested_folder` :1941.

### B.4 Hourly cap and `llm_log`

- Table: `llm_log(id, ts, msg_id, ok, error)` — `store.py:169-172`.
- Writers: `store.add_llm_log(msg_id, ok, error="")` — `store.py:887-890`.
- Readers: `store.llm_count_last_hour()` counts **all** rows in the last 3600 s, success or failure — `store.py:893-897`; `store.llm_fail_count(msg_id)` counts `ok=0` rows for the 3-strike park rule — `store.py:900-904`.
- **Cap enforcement (worker queue only):** `process_mailbox` runs the queue iff `settings["llm_suggest"]`; `budget = max_llm_per_hour (default 40) - llm_count_last_hour()`; `batch = max(0, min(llm_batch_per_cycle (default 5), budget))` — `engine.py:2071-2075`; defaults `store.py:15-18`.
- Writer sites:
  - worker queue: success only when the verdict was **not** heuristic → `add_llm_log(msg_id, True)` (`engine.py:2032-2034`); failure → `add_llm_log(msg_id, False, repr(exc))` (`:2036`) and after 3 failures the row is parked `status="error"` (`:2037-2045`).
  - ClassifyJob: same conditional-success pattern (`engine.py:2190-2194`) and failure path (`:2247-2251`).
  - Single-message button: `store.add_llm_log(mid, True)` **unconditionally**, even when a heuristic produced the verdict (`app.py:4456-4457`).
  - Assistant `classify_message` tool: **no** `llm_log` row at all (`engine.py:4148-4174`).
- Retry: `/retry-errors` → `store.retry_parked_errors()` requeues `status='error'` rows that have `llm_log` failures and clears their failure rows; then kicks the worker (`store.py:907-917`, `app.py:2243-2247`).

### B.5 Guard-rule check

- Definition: `is_guard_rule(rule)` = rule whose `actions` JSON is empty **or** has truthy `keep` — `engine.py:726-732`.
- Scan-time: guard rules still "match" like normal rules, keep the mail in place and stop processing (no LLM queue) — `engine.py:1354`, `:1370-1410` (branch with `taken == []` logs `kept (guard)`).
- Classify-time: inside `classify_and_store`, before any filing, when `classify_flows or filing_wanted` and not already filed: iterate **enabled guard rules** and `rule_matches` against `{from,to,subject,body=snippet}`; first match blocks **both** category flows and category-map filing, logs an info event + msg event (`engine.py:1956-1968`). One check covers every filing path (worker queue, single button, classify-all job) — per skill note 18.
- Keep/undo registry (different mechanism): message-id allowlist `keep_ids` (`store.py:709-741`, table at `:302-304`) consulted at `:1360-1361` (`status="kept"` at scan) and `:1933` (`kept` in classify).

### B.6 Category→folder filing (`llm_apply`)

- Target folder comes from the `category_folders` setting map (default map: Notification→Notifications, Newsletter→Newsletters, Receipt→Receipts, Promo→Promotions — `store.py:25-30`), resolved at `engine.py:1931` and stored in `llm_suggested_folder` even when filing is off (suggest-only mode).
- Actual move only under `llm_apply` (default **False**, `store.py:16`) and the gates listed in B.2 step 9. Move uses `MailClient.move` (COPYUID new uid captured) — `engine.py:2007`; destination recorded on the row (`folder`, `uid`, `action_taken="move:<folder>"`, `status="llm-moved"`).
- `record_move` writes the `undo_log` row that powers the dashboard Undo (`store.py:646-663`, `engine.py:2006`).

### B.7 Provenance: `classified_by`

- Written once per classification: `"heuristic:<id> <name>"` when a heuristic decided, else `"llm"` — `engine.py:1942`.
- Overwritten by the dataset relabel UI to `"user"` (with `llm_confidence=1.0`) for classified-sourced datasets — `app.py:2558-2559`.
- Displayed as a message badge and carried in the classify msg-event JSON (`by` field, `engine.py:1946`).
- Set/created by migration (`store.py:267-268`); column in schema at `store.py:147`.

### B.8 Other entry points into the same path

- Worker queue: `process_mailbox → _process_llm_queue` (B.4).
- Manual batch: `ClassifyJob` thread pool — `engine.py:2138-2271`; routes `POST /messages/classify` (selected) and `/messages/classify-all` (`app.py:3799-3814`); progress via `GET /classify/status` (`app.py:3824-3826`); stop via `/classify/stop` (`:3817-3821`). Explicit-action design: **no hourly cap**, newest-first, 3-strike park (`engine.py:2139-2143`, `:2196-2256`); concurrency `classify_concurrency` clamped to 16 (`:2202`), per-thread IMAP connections via `threading.local()` (`:2180-2188`), ids claimed into `_skip` at submit time (`:2228`).
- Single message buttons: `POST /messages/<mid>/classify` → `engine.classify_and_store` and re-render (`app.py:4449-4466`); buttons at `app.py:3999-4006`.
- Assistant tool `classify_message` → `classify_and_store` (`engine.py:4148-4174`, tool declared `:2538`; capability `classify` in `AGENT_CAPS`, `engine.py:2579-2581`).
- Simulator dry-run: `engine.simulate_email` calls `classify_verdict` only when "Ask the classifier" is ticked — no writes; nothing is persisted (`engine.py:1548-1648`, verdict at `:1609-1628`; route `/simulate`, `app.py:6715-6728`).

---

## C. Scan pipeline order

### C.1 Worker cycle

`Worker.run` polls every `poll_interval` (default 90 s, floor 15) — `engine.py:1287-1295`; `run_cycle` calls `process_mailbox()`, then (at the end, outside the try) runs `heuristics.auto_refine()` when `heuristic_autorefine` is on — `engine.py:1297-1320`.

`process_mailbox()` — `engine.py:2059-2085`:
1. load settings, enabled rules, enabled flows;
2. one `MailClient` for the whole pass;
3. for each folder in `watch_folders` (default `["INBOX"]`): `_process_folder(...)`;
4. **after** scanning: drain the LLM queue under the hourly cap, only if `llm_suggest`.

### C.2 `_process_folder` decision order

`engine.py:1328-1418`, per folder, at most **120 UIDs per cycle** (`:1343`):

1. `mc.select(folder)` → UIDVALIDITY; if it changed, reset the folder index and fall back to the lookback window (`:1329-1334`). New folder: `SINCE` lookback (default 48 h, `store.py:12`); else `UID <last+1>:*` filtered to `> last_uid` (`:1335-1340`).
2. `fetch_meta(uid)` (full `BODY.PEEK[]` + MIME walk, decoded snippet) → `store.insert_message` (no-op if known) → load row (`:1345-1350`).
3. **Rule match:** `rule = match_first(rules, _fields_for(meta))` — first enabled rule in position order whose conditions match (`engine.py:713-739`, `:1354`; `_fields_for` builds `{from,to,subject,body=snippet}` at `:1323-1325`).
4. **Flow match (scan phase):** only flows **without** a category condition are candidates (`scan_flows = [f for f in flows if not _needs_verdict(f)]`, `:1357`); ctx text = `subject\nsnippet` (`:1358`); `match_first_flows(scan_flows, fields, f_ctx)` — first match wins (`:1359`, `:866-870`). Deterministic conditions short-circuit before any embedding (`flow_matches`, `:843-863`); topic conditions use `_topic_matches` (cosine via the embed endpoint, default threshold 0.45 — `:795-819`, `:754`).
5. **Branches (mutually exclusive, in this order):**
   - `rule and kept(msgid)` → `status="kept"`, `action_taken="kept (undo)"`, rule skipped (`:1362-1369`);
   - `rule` → execute actions when `rules_apply` (default True; else `matched-dry`): move/read/flag; writes `status`, `rule_id`, `action_taken`, and new `folder`/`uid` on move (`:1370-1410`);
   - `flow and not kept` → `_process_flow` (dedupe per `(flow, msgid)` via `flow_runs`; live unless `flows_apply` false → `flow-dry`; logs the topic score in `why` when present) (`:1411-1413`, `:958-978`);
   - else → `store.update_message(row["id"], status="queued")` (`:1414-1415`).

So the scan phase decides: which rule matches, whether the message is kept/undo-protected, the rule's actions, which deterministic/topic flow matches, and queueing. **Category-conditional flows and all classification are deferred.**

### C.3 The `_needs_verdict` split

`_needs_verdict(flow)` is true when any condition has `kind == "category"` (`engine.py:763-769`). Those flows are filtered out of scan (`:1357`) and evaluated only in `classify_and_store` after the verdict exists (`:1954`, `:1969-1998`), with `ctx = {"verdict": {category, confidence, source}, "text": subject+"\n"+snippet}` (`:1973-1974`). Matching rules: category equality (case-insensitive) + `min_confidence` floor (`_cond_matches`, `:822-837`). A matched category flow **suppresses** category-map filing for that message (`not res.get("_flow")` guard at `:1999`) — flows win over `category_folders`, guards beat both.

### C.4 Where `classify_and_store` runs relative to the scan

| Path | Trigger | Cap | Ref |
|---|---|---|---|
| Worker LLM queue | after each scan, `status='queued'` rows, only if `llm_suggest` | hourly budget (`max_llm_per_hour`) | `engine.py:2059-2085`, `2028-2056` |
| `ClassifyJob` (bulk) | `Classify selected` / `Classify all` buttons → thread pool | **none** (explicit action) | `engine.py:2138-2271`, `app.py:3799-3814` |
| Single message | `POST /messages/<mid>/classify` button | none | `app.py:4449-4466` |
| Assistant | `classify_message` tool | none | `engine.py:4148-4174` |
| Simulator | only `classify_verdict`, dry-run | n/a (no persistence) | `engine.py:1548-1648` |

Only the worker queue is budgeted by the hourly cap; the manual/bulk/assistant paths deliberately bypass it (`engine.py:2141`).

### C.5 Statuses written along the way

`new` (insert default, `store.py:631`) → `matched`/`matched-dry` (rule, `engine.py:1378/1395`) or `kept` (`:1363`) or `flow`/`flow-dry` (`:967`, `:1993`) or `queued` (`:1415`) → `classified` (`:1943`) → `llm-moved` (`:2008`) or `flow`/`flow-dry` again if a category flow fired; `error` after 3 LLM failures (`:2039`, `:2249`). `flow_runs` guards re-scans (`store.py:517-527`); `undo_log` records machine moves (`store.py:646`).

---

## D. Tasks inventory table

Every discrete prediction/decision the system makes today. "Confidence" = a numeric score exists and is stored/used (not merely a boolean).

### D.1 Mail-facing decisions

| # | Task / decision | Decider today | Decided at (file:line) | Stored at (table.column / ref) | Confidence? |
|---|---|---|---|---|---|
| 1 | Which rule matches (first enabled rule, position order, all/any conditions) | Deterministic | `engine.py:713-739`, `:1354` | `messages.rule_id`, `action_taken` (`:1402-1403`) | No |
| 2 | Is a rule a guard ("keep in place, stop") | Deterministic | `engine.py:726-732` | implicit in `actions` JSON; `status='kept'` (`:1363-1364`) | No |
| 3 | Rule action set (move/read/flag) + live vs dry | Deterministic | `engine.py:1372-1401` (`rules_apply` default true) | `messages.action_taken`, `folder`/`uid`; `undo_log` | No |
| 4 | Keep/undo protection (skip automation) | Deterministic (Message-ID allowlist) | `engine.py:1360-1361`, `:1933`; `store.py:709-741` | `keep_ids` table; `messages.status='kept'` | No |
| 5 | Flow match — deterministic field conditions (`field/op/value`, 3-char whole-word rule) | Deterministic | `engine.py:690-710`, `:843-863` | `flow_runs` (`store.py:524-527`); `messages.status/action_taken` (`:966-971`) | No |
| 6 | Flow match — **topic similarity** ("is about X") | Embedding model (Qwen3-Embedding via TEI; cosine + threshold, default 0.45) | `engine.py:795-819` (`_topic_vector` `:781-792`), called from `flow_matches` `:838-839` | **Not persisted as a column** — score only in `ctx["last_topic_score"]` → event log `why` (`:1412`, `:972-978`) | Cosine score exists (transient, logged) |
| 7 | Flow match — **category condition** (category ± `min_confidence`) | Consumes verdict (#8/#9); deterministic comparison | `engine.py:822-837`, `:1954`, `:1969-1998` | `messages.status/action_taken`, `flow_runs` | Uses verdict confidence as floor |
| 8 | **Heuristic category verdict** (decision_list / naive_bayes) | Statistical heuristic model (trained, deterministic at inference) | `heuristics.py:385-409` | `heuristics.model`/`stats`/`excluded`; verdict → `messages.llm_category`, `classified_by="heuristic:<id> <name>"` (`engine.py:1942`) | **Yes** — decision_list: shrunk precision `(c+0.5)/(seen+1)`; naive_bayes: softmax-normalized posterior |
| 9 | **LLM category** | LLM (`LLMClient.classify`) | `engine.py:1205-1242` | `messages.llm_category`, `classified_by="llm"` | **Yes** — self-reported `confidence` 0–1, stored `llm_confidence` |
| 10 | LLM `needs_reply` flag | LLM | prompt `engine.py:1207-1210`; stored `:1940` | `messages.llm_needs_reply` | No (boolean; heuristic path forces False, `:1907`) |
| 11 | LLM `summary` | LLM | prompt `:1207-1210`; stored `:1937` | `messages.llm_summary` | No |
| 12 | LLM `reason` (short rationale) | LLM | prompt `:1207-1210`; stored `:1938` | `messages.llm_reason` | No |
| 13 | LLM `thinking` (chain-of-thought) | LLM | captured `:1232-1241`; stored `:1939` | `messages.llm_thinking` | No |
| 14 | Heuristic rationale/detail string | Derived from model stats at predict time | `heuristics.py:137-147`, `:407-408` | inside verdict `reason` → `messages.llm_reason` (engine `:1938`) | Carries the firing condition's precision (%, n) — not a calibrated confidence |
| 15 | Combined verdict (category + confidence + source) | Fusion: heuristic if any, else LLM | `engine.py:1901-1913`, `:1952-1953` | `messages.llm_category/llm_confidence/classified_by` | Yes (from #8/#9) |
| 16 | Suggested destination folder | Deterministic map `category_folders` | `engine.py:1931` | `messages.llm_suggested_folder` | No |
| 17 | File-or-not (`llm_apply` × not-already-filed × no guard/keep/flow) | Deterministic policy | `engine.py:1955`, `:1957`, `:1969`, `:1999` | `status='llm-moved'`, `action_taken`, `folder`/`uid`; `undo_log` | No |
| 18 | Guard check at classify time (blocks flows + filing) | Deterministic | `engine.py:1956-1968` | event + msg_event (`:1964-1967`) | No |
| 19 | Flow step execution (move/read/flag/tag/draft) | Deterministic runner | `engine.py:873-955` | `messages.*`, `flow_runs`, `undo_log` (`:889`) | No |
| 20 | Reply draft body | LLM (`draft_reply` / `generate_draft`), or deterministic template/fixed render in flow steps | `engine.py:1244-1261`, `:2101-2118`; flow mode dispatch `:926-949`; template render `:364-375` | Saved to the IMAP `\Drafts` folder, **not** in SQLite (`engine.py:2121-2133`); LLM instructions threaded `:938`, `:1255-1257` | No |
| 21 | Rule proposals learned from tags | LLM (`LEARN_SYSTEM`) → proposed rules | `engine.py:2276-2346` (`LEARN_SYSTEM` `:2276-2293`, `propose_rules_from_tags` `:2296-2346`) | `rule_proposals` table (`store.py:205-210`, `:1301-1306`) | No |
| 22 | Flow proposals (assistant `propose_flow`) | LLM drafts; deterministic validation `_validate_flow` | `engine.py:3802-3844`, `:2929-3043` | `assistant_messages.proposals`; flows table on apply | No |
| 23 | Classifier training (the model itself) | Deterministic training algorithm over labels (#24) | `heuristics.py:322-336` | `heuristics.model/stats` | In-sample stats only |
| 24 | Classifier evaluation accuracy | Deterministic, in-sample | `heuristics.py:339-377` | Not persisted (returned to assistant tool `engine.py:3980`) | Accuracy / false_positives / mistakes |
| 25 | Auto-refine trigger (retrain when ≥5 new labels) | Deterministic count rule, tag-sourced only | `heuristics.py:412-442`; invoked `engine.py:1315-1320` | `heuristics.stats.trained_at/refined_from` | No |
| 26 | Dataset labels / inclusions / exclusions / relabels | Human (user) + UI; assistant tags | `app.py:2540-2598`; `store.py:1217-1224` (`tagged_examples`); `heuristics.py:240-246` | `messages.user_tag`/`user_tag_by`; `messages.llm_category` + `classified_by='user'`; `heuristics.excluded` | No (relabel sets `llm_confidence=1.0`, `app.py:2559`) |
| 27 | Category taxonomy (label set for LLM prompt + UI) | Human setting (default 6 categories) | `store.py:24`; consumed `engine.py:1206`, `:1911` | `settings.categories`, `settings.category_folders` | No |

### D.2 Pipeline/meta decisions (not mail content, but part of "what the system decides")

| Task | Decider | Ref | Stored | Confidence |
|---|---|---|---|---|
| Which UIDs to scan (UIDVALIDITY reset, lookback window, ≤120/cycle) | Deterministic | `engine.py:1329-1343` | Scan cursor = `MAX(uid)`/`MAX(uidvalidity)` per folder over the `messages` table (`store.last_uid`, `store.py:859-864`); UIDVALIDITY change deletes the folder's rows (`reset_folder_index`, `store.py:866-870`) | No |
| Which folders to watch / whether LLM queue runs / filing modes | Settings (human) | `store.py:12-19`; `engine.py:2067, 2071` | `settings` table | No |
| Whether to retrain tag-sourced classifiers | Deterministic | `heuristics.py:415-442` | `heuristics.stats` | No |
| Park/retry on repeated LLM failure | Deterministic (3 strikes) | `engine.py:2037-2045`, `:2248-2251`; `store.py:900-917` | `llm_log`, `messages.status='error'` | No |
| Snooze / needs-reply surfacing | Deterministic | `store.py:639-643`; dashboard/UI filters | `messages.snoozed_until` | No |

### D.3 Confidence semantics (as-is) — design-relevant notes

- The only stored confidences are `messages.llm_confidence` (LLM self-report or heuristic probability) and, implicitly, `heuristics.min_confidence` per classifier. There is **no calibration** anywhere, and the two numbers come from incomparable scales (self-reported LLM certainty vs a trained-model probability).
- Category-condition flows consume the verdict confidence as a floor (`min_confidence`); topic flows use a raw cosine with default 0.45, calibrated on a real mailbox per `engine.py:755-759`.
- Heuristic verdicts structurally **cannot** produce `needs_reply`, `summary`, `thinking` (hard-coded at `engine.py:1907`), so replacing the LLM for a category silently drops those outputs.
- Heuristic wins correctly skip `llm_log` in the worker and bulk paths (`engine.py:2033`, `:2192`) but **not** on the single-message button (`app.py:4457` logs unconditionally) and the assistant path logs nothing — the hourly budget accounting is inconsistent across entry points.
- `__other__` abstention means a classifier trained on one category can only ever vote "category X" or nothing; multi-category routing today is a set of independent binary-ish specialists selected by max confidence (`heuristics.py:402-405`), not a single multiclass model.
- Training labels are gold (`tags`) vs weak (`classified`, `stats.weak_labels`), negatives are sampled/capped at `max(20, 3×pos)` (`heuristics.py:262`), and evaluation is in-sample only (`heuristics.py:339-377`) — there is no held-out split, so no current signal distinguishes "learned the category" from "memorised the window".
- Everything deterministic is already interpretable and replayable (rules, flow conditions, decision-list conditions); the LLM decisions (category/needs_reply/summary/reason/drafts/proposals) are the removable/learnable surface, with provenance already recorded per message in `classified_by` and per classifier in `heuristics.stats`.

### D.4 Files quick map

| Concern | Location |
|---|---|
| Registry, kinds, featurizer, training sets, evaluate, auto-refine, inference | `heuristics.py` (whole file) |
| Rule/flow matching, LLM client, classify_verdict/classify_and_store, worker + ClassifyJob, simulator | `engine.py` |
| Schema + all persistence (messages, heuristics, llm_log, flow_runs, undo_log, keep_ids, msg_events, rule_proposals) | `store.py` |
| UI routes for classifiers/dataset, manual classify buttons, retrain/relabel | `app.py` |
| Embedding endpoint used by topic conditions | `rag.py:88-122` (`embed`, `embed_one`) |
