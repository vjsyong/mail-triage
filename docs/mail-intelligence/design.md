# Mail-intelligence: continuous improvement subsystem (design + audit)

Status: **first vertical slice implemented** on branch `mail-intelligence`
(worktree `~/mail-triage-intel`). Companion audits (read alongside this file):

| file | covers |
|---|---|
| `audit-store.md` | every table/column; provenance, confidence, versions, joinability |
| `audit-classification.md` | classification tasks, heuristics registry, LLM path, scan order |
| `audit-feedback.md` | every user-feedback + implicit signal in the codebase; gaps |
| `audit-data.md` | live production numbers: what a learner can actually train on |
| `audit-rag.md` | embeddings/retrieval artifacts reusable by specialists |

The goal: **use the LLM for novelty, learned specialists for repetition, deterministic
rules for stable high-confidence patterns** - while every decision keeps evidence and
provenance, and the system gets cheaper, faster and more auditable over time. The LLM
teacher/labeller/proposer never writes executable code and never flips production
behavior directly: `DISCOVER -> PROPOSE -> VALIDATE -> SHADOW -> PROMOTE -> MONITOR
-> RETIRE`.

---

## 1. Audit: what exists today (Phase 1 summary)

**1. Classification tasks.** One LLM call answers: `category` (6 classes), `needs_reply`
(bool), `confidence`, `summary`, `reason` (+ raw CoT in `llm_thinking`). Deterministic
tasks: rule match (12 rules, 11 enabled), flow match (1 flow; conditions include
`category`/`topic` kinds with embedding similarity), guard/keep. Heuristics registry
(`heuristics.py`): `decision_list` + `naive_bayes` kinds, trained from tags or
"classified" bootstrap, weak-labels flagged in stats.

**2/3. LLM vs deterministic split.** LLM: category + needs_reply + summary/reason per
message (3,554 verdicts), topic-similarity for flow conditions (embed endpoint). Both
heuristics and LLM are funneled through `engine.classify_verdict`/`classify_and_store`,
which is the single choke point this subsystem hooks.

**4/5. Feedback signals + explicit corrections.**
Existing and per-message: user tags (`messages.user_tag`, `user_tag_by`), dataset
relabel (rewrites `llm_category` + `classified_by='user'`), dataset remove/re-include
(`heuristics.excluded`), undo (`undo_log` + `keep_ids`), proposal/action
accept-reject (`rule_proposals.applied`, `agent_actions.status`).
Source: `audit-feedback.md` has the full 24-row table.

**6. Implicit behavior.** Captured: filing outcomes (`undo_log`, `msg_events` kinds
move/file/rule/flow/guard/undo/tag/snooze/wake/draft/classify), snooze/wake, draft
generation (not body), assistant tool actions (`agent_actions`). **Not captured (top
gaps):** reply detection (Sent never scanned; no In-Reply-To/References stored),
read/open observations, external-client actions (stale rows repaired silently - gap
D5), explicit keep (only via undo), per-message "wrong label", draft edits, priority
of any kind.

**7. Features extracted today.** Only token bags for heuristics (`d:`domain `s:`sender
`t:`subject `b:`body`). RAG stores 2,560-dim chunk embeddings for 2,120 messages.
No message-level feature schema exists - this branch adds one (below).

**8/9. Provenance + confidence.** `classified_by` exists ("llm" / "heuristic:<id>
<name>" / "user") but **was added recently and is not backfilled**: live DB has 8
real values out of 3,554 verdicts. `llm_confidence` exists but is inflated (86% >=
0.95; avg 0.966) - weak as an uncertainty signal. No decision records, no model
versions, no feature versions before this branch.

**10. Predictions vs outcomes.** Nothing reconciles verdicts against later outcomes.
`msg_events` + `undo_log` went live 2026-10-01 with 2 classify events, 0 outcome
events. Column-level outcomes (`status`, `action_taken`, `llm_suggested_folder`) are
untimestamped and were driven by the same verdicts (corroboration, not independent
labels).

**11/12. Registry + versioning/rollback.** `heuristics` is a mini model registry
(JSON model + stats, created_by) but rows are updated **in place** (no versions, no
rollback) and `auto_refine` silently rewrites models. No rule versioning.

**Live-data verdict (audit-data.md).** 3,563 messages: 3,554 LLM verdicts
(935 needs_reply / 2,619 no), **zero human labels** (0 tags, 0 undos, 0 snoozes, 0
user classifications), 290 senders, 376 subject-pseudo-threads (no real threading),
no priority signal anywhere. Conclusion: bootstrap on LLM weak labels with explicit
provenance; genuine evaluation needs the feedback channels this branch starts
recording, plus deliberate human labelling of a sample.

---

## 2. Canonical decision/event model (Phase 2) - as implemented

Tables added in `store.py` (`_migrate`), accessors in the `learning loop` section:

```
specialists(id, name, task, kind, version, status, feature_schema_version, model,
            stats, metrics, min_confidence, enabled, created_by, created, updated,
            superseded_by)                      -- the registry (versioned; rollback
                                                    = re-activate a prior version)
decisions(id, ts, msg_id, task, predicted_value, confidence, source_type, source_id,
          model_version, feature_version, shadow, routed)
decision_evidence(id, decision_id, feature_name, feature_value, contribution)
observations(id, ts, msg_id, event_type, event_value, source)
labels(id, ts, msg_id, task, label, confidence, source, source_detail,
       UNIQUE(msg_id, task, label, source))
```

Mapping: the brief's `message_id` = `msg_id` (matches `msg_events`/`undo_log`).
`source_type` in {`llm`, `heuristic`, `specialist`, `router`, `rule`(future)}.
`shadow=1` = the decision did not influence behavior. `routed` carries router intent
for non-router rows.

**OBSERVATION vs INFERRED vs CONFIRMED** is enforced, not aspirational:
- OBSERVATION -> `observations` rows (`undo`, `keep`, `tag`, `untag`, `relabel`,
  `move`, `snooze`, `wake`, `reclassify`, future `reply`/`read`).
- INFERRED LABEL -> `labels` with `source='inferred_behavior'` (weight 0.7; nothing
  is written to this tier yet on purpose - the inference rules need the modeling
  assumptions pinned, e.g. "undo implies the filing was wrong" is NOT chosen).
- CONFIRMED LABEL -> `labels` with `explicit_user_correction` (relabel/undo-keep
  semantics) or `explicit_user_label` (tags).
- Weak teacher signal -> `llm_annotation` (weight 1.0), **written at training-read
  time from the stored LLM verdict, never minted as a labels row** (test-enforced).

Training weights live in `learning.LABEL_SOURCES` (confirmed 4.0/3.0, deterministic
events 2.0, established model 1.5, LLM 1.0, inferred 0.7). Weights tune the
classifier; they do not silently upgrade truth.

---

## 3. Feature layer (Phase 3) - `learning.py`, `FEATURE_SCHEMA_VERSION = 1`

Controlled, versioned, pure-stdlib; computed from local data only (no IMAP):
22 features across message / contact / temporal groups -
`f_body_len f_subject_len f_question_marks f_exclaim_marks f_caps_words
f_n_recipients f_direct_recipient f_contains_money f_has_dates f_contains_request
f_deadline_lang f_automated_hint f_newsletter_hint f_reply_marker f_has_url
c_sender_known c_sender_count_log c_sender_reply_rate c_internal t_hour t_weekday
t_age_days`.

Rules: every model stores its own `features` list + scaler (mean/std) so old models
keep working when the order grows; `specialists.feature_schema_version` declares the
schema a model was trained against; the router/serving path checks nothing silently.
Contact aggregates come from one cached `GROUP BY from_addr` per process.
**Deliberately excluded for v1:** attachment counts (not stored), true thread features
(no In-Reply-To/References - audit-data §7; subject proxies only), embedding features
(chunk-level vectors exist; message-level vectors are a planned addition via
`audit-rag.md`), TF-IDF (add with the next model kind).

Legacy `heuristics.featurize` stays as-is for the existing registry.

## 4. Specialist abstraction (Phase 4)

One task = one config in `learning.TASKS`; models plug in via `register_kind` exactly
like `heuristics.register_kind` (`train(examples, params) -> (model_json, stats)`,
`predict(model, feats) -> {prediction, proba, confidence, contributions}`,
`describe(model)`).

Shipped kind: **`logreg`** - L2 logistic regression, standardized features, full-batch
gradient descent with early stop; artifact = JSON (`weights`, `bias`, `mean`, `std`,
`features`, `trained_at`, `samples`, `iters`, `log_loss`). No pickle, no executable
artifact, deterministic training, CPU-cheap (~seconds for 3.5k x 22). Interpretable:
per-decision contributions.

Planned kinds (same interface): `decision_list` (reuse heuristics' learned conditions
as a rule candidate), `rule` (below), `tfidf_linear`, `centroid` (embeddings),
`ridge`. Kinds go through code review like any code; the LLM can propose which
features/kind to use (Phase 9) but cannot author the algorithm.

## 5. Confidence routing (Phase 5)

`route_decision(task_policy, coverage)` over the tasks ONE classify call answers
(`call_task_policy()`: `category` + `needs_reply`): every task must be covered by a
decision >= `min_accept` to skip the LLM; >= `min_verify` but < accept = `verify`;
anything missing/uncertain = `llm`. The router is recorded per message as a `route`
decision (`source_type='router'`, `routed` = the intent payload).

Modes (`settings.learning_route_mode`):
- **`shadow` (default)** - the router only records what it WOULD do. Nothing changes.
- **`enforce`** - the one behavior change shipped is safe and narrow: when the
  heuristic path already avoided the LLM, an ACTIVE needs_reply specialist fills the
  needs_reply slot (the heuristic path hard-codes `False` today). Full-call skipping
  unlocks when a category specialist exists (next milestone).

`learning_sample_pct` (default 1.0) is reserved for exploration sampling in enforce
mode (Phase 15): in shadow mode every decision is LLM-audited by construction, which
is the strongest audit there is; sampling matters once calls are actually skipped.

Thresholds are policy, not constants baked into behavior: they live in `TASKS` /
`call_task_policy()`, and the shadow period exists precisely to evaluate them before
enforcement. Calibration is reported per specialist (Brier + reliability buckets) so
thresholds can be set on calibrated probabilities; the LLM's own confidence is NOT
used for routing (it is inflated - audit-data §2).

## 6. LLM as weak supervisor (Phase 6)

Today the teacher signal is the stored verdict (`llm_needs_reply` + `llm_category`),
read as `llm_annotation` weight 1.0. The richer annotation schema from the brief
(`reason_codes` etc.) is a prompt-schema change to `LLMClient.classify` - recorded as
a next step; the decision store already has a place for it (`decision_evidence`).
Source provenance (`LABEL_SOURCES`) is the firewall: the training pipeline can filter
or weight by source, and tests enforce that predictions never mint labels (Phase 22).

## 7. Pattern discovery + rule DSL (Phases 7/8) - designed, not yet shipped

Rule candidates are the next candidate type after the needs_reply specialist
lifeline is proven. Design (frozen here so the LLM proposal path has a target):

```json
{"rule_type": "classification", "task": "needs_reply",
 "conditions": [{"feature": "f_automated_hint", "operator": "eq", "value": 1.0},
                {"feature": "c_internal", "operator": "eq", "value": 0.0}],
 "prediction": false}
```

- Operators allowlist: `eq neq gt gte lt lte contains in prefix` (+ `regex` only
  compiled with `re.fullmatch` on a length-capped pattern, evaluated by the trusted
  executor).
- The executor lives in trusted code (`learning.py`), evaluates against the versioned
  feature dict, and emits the same decision/evidence records as model kinds, with
  `source_type='rule'`.
- Retrospective evaluation is precision + coverage over the decision store /
  dataset (`precision = correct fires / fires`, `coverage = fires / eligible`);
  high precision + low coverage is a GOOD rule, never rejected for coverage alone.
- Candidate generation is a periodic offline job fed with: recent disagreements
  (`recent_disagreements()`), current metrics, feature names, and the failing
  examples - and "no useful pattern found" is an explicit valid output. The LLM
  returns structured proposals only; they land in the candidate registry as
  `status='proposed'` and follow the identical lifecycle.

## 8. Lifecycle (Phase 10) - as implemented

```
proposed -> validated -> shadow -> active -> degraded -> retired
     \-> rejected                          (any) -> retired
```

`learning.transition(sid, to, reason)` enforces the graph (test: validated cannot
jump to active), flips `enabled`, logs every transition to `events`, and a promoting
`active` version retires the previous active one (`superseded_by` set - rollback =
re-activate the previous version, which exists as a row with its metrics intact).
Training lands candidates at `validated` with `enabled=0`; deployment to shadow is a
separate explicit action (CLI `learning.py deploy <id>` or the Learning page).

## 9. Validation + shadow (Phases 11/12)

- **Retrospective (VALIDATE):** `train_specialist` builds the dataset from raw SQL
  (list filters like snooze must not drop samples), sorts by time, splits
  train/validation **temporally** (holdout = newest 20%), fits on the train slice,
  and reports per-slice: n, confusion, precision, recall, F1, accuracy, Brier,
  average precision, calibration buckets. Stored in `specialists.metrics`.
- **Shadow (SHADOW):** enabled specialists run on every new classification (hook in
  `classify_and_store`, the single funnel used by worker/ClassifyJob/manual/assistant).
  Each run records a `specialist` decision (`shadow=1` unless enforcing AND active)
  with model version + feature version + top feature contributions, plus the system's
  own decision and the router intent. Agreement and disagreement are then computed
  from stored evidence, not memory: `specialist_live_stats()` (last 50/100/500 via
  window param), `recent_disagreements()`.

## 10. Promotion, monitoring, GC (Phases 13/14/16)

- Promotion is a deterministic human-triggered transition; the LLM has no transition
  capability anywhere. The evaluator criteria (support, precision floor, recall,
  calibration, stability, per-task risk) live in task config / next milestone; the
  shadow stats it will consume are already stored.
- Monitoring: rolling agreement + calibration recomputed on demand
  (`evaluate_specialist` re-scores against current labels; live stats against system
  decisions). `degraded` state exists in the graph; the periodic recompute job that
  demotes automatically is a next step (simple rolling stats, no heavy drift infra).
- GC: specialists keep full provenance (`created_by`, `created`, `superseded_by`,
  metrics per version); retirement never deletes. The periodic "stale/redundant"
  report is a next step; nothing auto-deletes historical versions.

## 11. Explainability (Phase 17)

A `logreg` decision explains itself without the LLM: the classifier row's nested
detail on /learning and any decision record can be traced to
`decision_evidence` rows (`feature_name`, `feature_value`, `contribution`).
Example: `f_contains_request +0.82`, `c_sender_reply_rate +0.55`,
`f_newsletter_hint -0.31`, ... Structured, honest, no prose invented post-hoc.

## 12. Scheduler + feedback-loop guards (Phases 18/21/22)

- On message: features -> specialists -> router -> decisions (in the classify funnel).
- On user action: observations + labels at the route sites (tag / relabel / undo /
  keep / snooze / wake / move / reclassify).
- Periodically: `learning.py reconcile` (tag/relabel -> labels), retrain via CLI or
  the Learning page button; a cron/scheduled job wiring is a next step (repo already
  has worker cycles + cron conventions to reuse).
- Feedback-loop guards: weak labels are read-time only; `llm_annotation` labels are
  counted separately from confirmed labels in every training summary; predictions
  never become labels (test); `store.messages()` filters bypassed in training reads;
  reconcile trusts only `user_tag_by='user'` (flow-applied tags currently lack
  provenance - audit-feedback gap 12 - so they are excluded on purpose).

## 13. Metrics that matter (Phase 24)

`status_report()` / the Learning page expose: LLM escalation rate (last N classified),
would-skip rate (coverage), verify queue, specialists' validation metrics + live
shadow agreement, decision/observation/label counts by source. Baseline today:
escalation ~100% (every message's call goes to the LLM unless a heuristic matched),
would-skip 0% until a category specialist exists. `llm_log` already counts calls
(3,599 in 2 days of operation); per-100-email and token accounting comes with the
`llm_log` schema extension (model/latency/tokens columns - noted as next step).

## 14. Safety boundaries + rollback + first task (Phases 9/25/26 items)

- No arbitrary code: models are JSON; kinds are code-reviewed plug-ins; the LLM's
  only future write path is structured proposals into the registry (rules per §7,
  model proposals = algorithm + feature list + training window, executed by the
  trusted trainer).
- Rollback: every candidate is a versioned row; `active` promotion auto-retires the
  prior active with `superseded_by` intact; nothing is deleted; disabling
  `learning_enabled` stops all recording/running at once.
- **First task chosen: `needs_reply`** - it has 3,554 weak labels today, a real
  user-facing cost (the needs-reply queue), and its specialist pairs with the
  existing heuristic path. **Priority is explicitly deferred**: audit-data shows zero
  priority signal in any table; training on nothing would be theatre. It becomes
  tractable once explicit priority feedback (or reply-latency behavior) exists.
- What this branch ships vs. the brief's later phases: rule DSL executor, category
  specialist + full-call skip, exploration sampling in enforce mode, automatic
  drift/GC jobs, annotation schema v2 (`reason_codes`), and the instrumentation gaps
  from audit-feedback §4 (reply detection first). The lifecycle, store, features,
  shadow loop and router are the load-bearing half; the rest bolts onto them.

---

## Appendix: the slice as shipped (branch `mail-intelligence`)

- `store.py`: 5 tables + 22 accessors; `learning_enabled/route_mode/sample_pct`
  settings defaults.
- `learning.py` (new): feature layer, logreg kind, metrics (P/R/F1/AP/Brier/
  calibration), dataset builder (temporal split, source weighting), trainer,
  evaluator, lifecycle transitions, router, shadow hook, live stats, reconcile,
  status report, CLI (`report|train|deploy|promote|reconcile`).
- `engine.py`: shadow hook in `classify_and_store`; undo observation.
- `app.py`: `/learning` page (routing card, specialists table, disagreements,
  library) + train/transition routes; nav entry; observation hooks at tag / relabel /
  reclassify / snooze / wake / manual-file.
- `tests/mock_e2e.py`: T44 (23 checks) - features, logreg determinism, label
  provenance, idempotency, illegal-transition guard, shadow decisions + evidence,
  router intent, no-labels-from-predictions, live stats, page render, retire.
- Suite: **489 passed, 0 failed.**

## UI (2026-10, same day): the Learning page, redesigned for newcomers

Research round (TheFinch *UX Best Practices for AI/ML Dashboards*: jobs-to-be-done ordering,
10-second scan vs 2-minute investigation, hierarchy top=state / middle=drivers / bottom=evidence,
plain-language uncertainty, versioning/lineage visible, "what it means" + "what to do" near
indicators; Lollypop *Progressive Disclosure*: essential first, labelled reveals, never hide
task-critical info): the page was rebuilt as a story - **Status** (one plain sentence + a 4-step
tracker: learned -> scored -> watching now -> take over) -> **Reply detector** card (one-sentence
job, plain-language scores, "ceiling not truth" honesty note, actions) -> **folds** (disagreements
with subjects, raw numbers, all versions, how-it-works + guard-rails). Hero always shows the
RUNNING version; a retrain produces a `validated` candidate deployable from the Versions fold;
deploying any version supersedes other running versions of the same task (one runner per task).
