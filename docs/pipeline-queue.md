# Pipeline stage queue: decouple mail fetch from downstream work

Status: phase 4 implemented 2026-10-05 (see "Phases"). Design agreed after the
Office365 "User is authenticated but not connected." incident: the 16-wide
classify pool held one IMAP connection per LLM thread, which (plus worker +
indexer + assistant) exceeded Exchange Online's per-mailbox concurrent-IMAP
allowance and made every new login fail until the batch finished. The fix is
not a bigger connection budget - it is removing IMAP from the work stages.

## Goal

Mail fetch only pulls mail and stores it locally. Everything downstream
(classification, indexing, actions) runs off the local store, and each stage
subscribes to free work slots in a durable queue according to its own
requirements (LLM concurrency, CPU slots, one writer session). IMAP session
count then stays constant regardless of queue depth.

## Queue (`jobs` table, SQLite)

    jobs(
      id, kind, message_id, payload, state, priority, attempts,
      not_before, lease_until, worker, error,
      created_at, started_at, finished_at
    )

- `kind` is the stage (`classify` in phase 1; later `index`, `triage`, `act`,
  `replies`). `message_id` links a job to a message; `payload` carries
  stage-specific JSON (e.g. `{"manual": true}`).
- A partial unique index on `(kind, message_id) WHERE state IN
  ('queued','running')` makes enqueue idempotent: the worker re-enqueues the
  backlog every pass without duplicating in-flight work.
- Claim is one `BEGIN IMMEDIATE` transaction: pick the oldest queued jobs whose
  kind has a free slot (`running(kind) < limit(kind)`), respecting
  `not_before`, then mark them `running` with a lease. Workers self-subscribe:
  each asks "is there a job of a kind I can run with a free slot?".
- Crashes: `requeue_expired_jobs()` returns lease-expired `running` jobs to
  `queued` at startup and periodically. Completion is `finish_job` /
  `fail_job` (with optional retry backoff via `not_before`).
- Rate limits live in the claim: the classify stage passes the hourly LLM
  budget (`max_llm_per_hour - llm_count_last_hour()`); manual jobs
  (`payload.manual`) bypass it, matching "Retry parked" semantics.

## Stages

- `fetch` (Worker pass): scan folders, store metadata/snippet, enqueue
  downstream jobs. No LLM, no moves, no embeddings.
- `triage` (deterministic rules + heuristics, phase 3): decides skip / classify
  / act from cached fields.
- `classify` (phase 1): LLM HTTP only; verdict persists, then any
  flow/auto-file action runs through the single action connection below.
- `index` (phase 2): chunk + embed from locally cached bodies; never opens
  IMAP.
- `act` (phase 4): every mailbox mutation (move/flag/draft/append/rules/flows)
  on one serialized writer connection; the assistant's write tools join later.

## One writer connection

All action execution shares `engine.with_action_mail(fn)`: a module-level
`MailClient` guarded by a lock, reconnected on death and closable via
`reset_action_mail()` (proxy restarts, tests). Classification itself never
opens IMAP; if a stage action needs mail access it borrows this one session.
The worker's scan connection is separate and short-lived.

## Stage-worker process (phase 2)

`stage_worker.py` is the supervised child process that owns the CPU-heavy
stages so the web process keeps its GIL and CPU time:

- Claims singleton `index` jobs (`message_id=0`) from the queue, runs the same
  `rag.Indexer` pass, and schedules the incremental refresh cadence itself.
- Runs the heuristic auto-refine loop (the web Worker skips it via
  `engine.external_stages()`).
- Publishes live state to the `stage_state` table; `app.index_status()` merges
  it so the dashboard shows progress, errors and "last run" seamlessly.
- Manual Index now / Rebuild enqueue jobs (`stage_worker.enqueue_index`), which
  promote an already queued scheduled pass rather than duplicating.
- CPU placement: `STAGE_CPUS` (e.g. `4-7`) pins the child with
  `sched_setaffinity` and `STAGE_NICE` (default 5) lowers its priority;
  `STAGE_WORKER=off` falls back to the old in-process indexer (dev/tests).
- `stage_worker.Supervisor` keeps the child alive from `app.py` main
  (same container, so the embedded proxy stays reachable on localhost).

## Body cache (phase 3)

The index stage no longer reads mail. The fetch stage owns every mailbox read
and stores decoded bodies in `message_bodies`:

- The scan (`_process_folder`) already downloaded the full message for its MIME
  walk; `MailClient.fetch_meta` now keeps the decoded text (20k chars) and the
  worker caches it, so new mail is indexed without any second fetch.
- `engine.BodyFetcher` (app process) runs the index-scope walk: it advances each
  folder's `index2_state` cursor, stores metadata + bodies for new UIDs, and
  drains `fetch.body` jobs. It releases its connection when idle and owns one
  read session, separate from the worker's short scan connection.
- `rag_lite.index_pass` reads only SQLite: messages missing chunks are chunked
  and embedded from the cached body; a message without a body gets a
  `fetch.body` job instead, the pass reports "N awaiting body fetch", and
  `IndexRunner` re-enqueues the pass every ~15 s while fetch jobs are pending.
  The stage worker therefore has zero IMAP traffic.
- Rebuilds are cache-first: if bodies are cached the whole index is rebuilt
  without touching the network; only messages that predate the cache (or were
  never fetched) trigger `fetch.body` jobs.
- Classification prefers the cached body over the stored snippet when one
  exists (still capped at 1500 chars in the prompt), so the verdict never
  depends on which fetch path produced the row.

`index2_state.last_uid` is the fetch-stage scan cursor; folder completeness is
recomputed from the DB (`missing_chunks_by_folder`) on every index pass. A
message that parses to no text is still marked handled (`messages.indexed_at`)
so it cannot stall folder completion; `clear_rag2` resets the marker so rebuilds
reindex everything.

## Act stage (phase 4)

Every mailbox mutation for indexed mail is a durable `act` job executed by
`engine.ActRunner` on the shared single-writer connection:

- Ops: `move` (rules, flows, auto-file, assistant, UI file, trash, undo),
  `flags` (read/star, assistant), `create_folder`, `flow` (the whole flow runs
  atomically on the writer), `draft` (save_draft).
- Producers that need the outcome call `engine.run_act(op, message_id, params)`:
  it enqueues the job and waits for its `result` (jobs carry a JSON result
  column). Rules during scan, classify flow/auto-file, assistant tools, the
  message-file route and undo all go through it, so mutations are serialized in
  one place regardless of producer.
- When no runner is alive in the process (tests, CLI) `run_act` executes inline
  on the same writer connection, keeping behaviour identical without threads.
  `stage_worker` sets `engine.set_act_remote(True)`: it only enqueues and waits,
  the app process owns the runner.
- Crash recovery: on startup `store.requeue_running_jobs()` returns every
  orphaned `running` job (act/classify/index) to the queue; leases still protect
  against a hung worker at runtime.
- Not through the act queue (deliberately): SMTP send (its own channel; only the
  best-effort Sent append touches IMAP), `heal_locations` repairs, and the
  assistant's folder+uid fallback when a message has no local row yet.

## Phases

1. (done) `jobs` core + `classify` stage. The worker enqueues queued mail and
   records `N queued` in its summary; `ClassifyJob` becomes the stage consumer
   + UI facade over the same queue (`trigger`, `_run_job`, `state`,
   `request_stop` keep their contracts). Actions from classification route
   through `with_action_mail`, so batch classification uses 0-1 IMAP sessions
   instead of one per thread.
2. (done) Stage-worker process: index + learning run in a supervised child
   process with their own GIL/CPU budget; manual triggers and live state cross
   the process boundary through the queue and `stage_state`.
3. (done) Body cache: the fetch stage scans index folders and caches decoded
   bodies; `rag_lite.index_pass` chunks and embeds from the cache and never
   opens IMAP; classification reads cached bodies.
4. (done) `act` stage: all mutations of indexed mail drain through one writer
   connection via durable jobs; rules, flows, auto-file, assistant writes,
   drafts, file/undo all call `run_act`.

## Tests

Phase 1 adds: jobs table unit checks (idempotent enqueue, priority, per-kind
limits, lease requeue, budget bypass for manual), a fetch-enqueues/classify-
drains decoupling check, a "classification opens no IMAP" check (MailClient
patched to raise), and keeps the existing classify/failure/UI contracts. The
mock suite replaces `engine.process_mailbox()` with a `pump()` helper
(fetch + drain) so tests remain synchronous while production stays decoupled.

## Adversarial review fixes (2026-10-06)

Four independent adversarial reviews (one per phase) produced findings that are
now fixed:

- **Budget is hard**: `claim_jobs` counts already-running non-manual jobs
  against the hourly LLM budget, so a stage cannot overshoot by its width; the
  classify drain passes absolute concurrency (no double subtraction) and takes
  `budget=0` when `llm_suggest` is off, so disabling classification also stops
  draining the queue.
- **Claim fairness**: selection runs per kind, so one kind's backlog cannot
  starve another's free slot.
- **Runtime crash recovery**: the act runner runs a periodic lease reaper and
  job-retention prune; the index and fetch stages reaper their own kinds; a
  restarted stage worker requeues its orphaned running index jobs immediately.
  `requeue_running_jobs`/`requeue_expired_jobs` are kind-scopable, and the act
  runner no longer blanket-requeues other stages' genuinely running jobs.
- **Act contract**: `run_act` cancels a job that never started and reports
  `{"pending": true}` for one still running; classify never auto-files when a
  flow is pending; assistant tools report "still running" instead of a false
  failure; drafts raise `ActPending` and the UI warns instead of inviting a
  duplicate APPEND. Identical active act ops dedupe via `jobs.dedupe_key`.
  `record_move` happens only after a successful move; the shared writer keeps
  its connection on application errors (only IMAP failures drop it).
- **Stage worker**: supervisor restarts use exponential backoff with a reset
  after a stable run, a SIGTERM handler stops the child with the app, the learn
  runner no longer records `last_ok` on failure, and the dashboard treats a
  silent child (`stage_state` older than 30 s) as not running instead of
  showing a stale "indexing" and reloading forever.
- **Body cache**: UIDVALIDITY resets also trigger when the cursor is unknown
  (0) but stored rows disagree, the skip path only trusts an exact
  (folder, uid, uidvalidity) row, `_process_folder` resets orphan chunks too,
  `index_pass` never queues bodies for folders resolved off the live mailbox,
  three terminal `fetch.body` failures stop retrying, and a late real body
  resets `messages.indexed_at` so it gets chunked.
- **Classify**: a parked (3-strike) message's job is recorded `failed`, not
  `done`; idle ticks check for queued work before opening a write transaction.
