# Pipeline stage queue: decouple mail fetch from downstream work

Status: phase 2 implemented 2026-10-05 (see "Phases"). Design agreed after the
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
3. Index stage from the local cache (body cache in the fetch stage): the stage
   worker stops connecting, and classification can use bodies rather than
   snippets.
4. `act` stage drains all mutations; assistant writes and drafts join it.

## Tests

Phase 1 adds: jobs table unit checks (idempotent enqueue, priority, per-kind
limits, lease requeue, budget bypass for manual), a fetch-enqueues/classify-
drains decoupling check, a "classification opens no IMAP" check (MailClient
patched to raise), and keeps the existing classify/failure/UI contracts. The
mock suite replaces `engine.process_mailbox()` with a `pump()` helper
(fetch + drain) so tests remain synchronous while production stays decoupled.
