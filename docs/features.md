# Features - the full tour

The [README](../README.md) has the short version; this page keeps the detail. Each
section is grounded in what the code actually does.

## Rules (Rules page)

Match on from / to / subject / body snippet (contains, equals, regex; ALL or ANY),
then move to a folder, mark read, and/or flag. First matching rule wins, top to
bottom; folders are created if missing. A rule with no actions is a **guard**:
matching mail stays put and nothing else (later rules, LLM filing) can move it -
that's how "never move X out of the inbox" is expressed. Guards belong at the top
(there is a move-to-top button; assistant proposals land there automatically when
the rule asks for it). Values of 3 characters or fewer match whole words only
("PO" won't fire on "support"), so short tokens are safe.

## Flows (Flows page)

Multi-step automations: WHEN a message matches -> THEN run steps in order. WHEN
conditions can be exact fields or AI-based: an **AI category** condition fires when
the classifier tags the message that way (checked right after classification), and
an **about (topic)** condition matches by meaning via embeddings - no exact words
needed. Both take an optional minimum score. Steps: move, tag, flag, mark read,
draft from a template, or draft with the LLM following your instructions (saved to
Drafts). Rules stay for single-action cases and run first. Dry-run toggle in
Settings; the builder is a canvas (trigger -> filters -> step chain), and the
assistant can propose complete flows for one-click approval. Design records:
[docs/flows-builder-v2.md](flows-builder-v2.md), [docs/flows-fuzzy-classifier.md](flows-fuzzy-classifier.md).

## Classification (Messages + Settings)

Anything no rule matched is classified into your categories (Action, Notification,
Newsletter, Receipt, Personal, Promo by default) with a confidence, a one-sentence
summary, and a short reason ("why: ..."), shown on the message page. Auto-filing by
category starts off - the LLM suggests until you enable it.

On demand: tick rows and "Classify selected", or "Classify all unclassified" as a
background job (newest first, progress + Stop). The batch job classifies several
messages in parallel against the local model (Settings -> "Classify concurrency",
default 8, cap 16; measured ~41 msg/min at 16 on an RTX 3090). Each message keeps
the model's reasoning, collapsible on its page; there is also a single "Classify
with LLM" button and "File to <suggested folder>".

## Fast-path classifiers (Classifiers page)

Deterministic heuristic models that run BEFORE the LLM. Two kinds ship:
`decision_list` (learned ordered conditions with precision/support, fully
interpretable) and `naive_bayes` (token statistics). They train from your labels
(manual tags) or from the LLM's own auto-tags on classified mail; a confident
verdict is applied with no LLM call - consistent run to run, and immune to
instructions hidden inside email content (prompt injection).

Every classifier has a **dataset page**: review the exact samples it learns from
(with the label and confidence) and either remove anything that doesn't belong or
reclassify it with a dropdown - the sample moves between the in-set and out-of-set
immediately, and for LLM-labeled data the correction also updates the message's own
record. Removals stick across retraining and auto-refine, and can be re-included.
The assistant trains, retrains and evaluates them on request. Toggle in Settings.

## The learning loop (Learning page)

The long-term direction: replace repeated LLM reasoning with small, auditable
models. The loop is DISCOVER -> PROPOSE -> VALIDATE -> SHADOW -> PROMOTE ->
MONITOR -> RETIRE, and nothing in it changes production behavior until a human
promotes it.

- **Specialists** train on mailbox history in seconds (`needs_reply` as a logistic
  regression over 22 deterministic features; `category` as one-vs-rest). Models are
  plain JSON - weights, scaler, feature list - so every decision is interpretable,
  and re-activating a previous version is the rollback.
- **Every decision is recorded** with its evidence: which model, which version,
  which features contributed, with what confidence (the "Machine decisions" card on
  each message shows this; shadow rows are labeled).
- **Shadow by default**: a specialist records what it WOULD have decided against
  the LLM's verdict and influences nothing. A confidence router decides when
  confident calls could skip the LLM.
- **Test sets**: ~50 messages sampled once and labeled by hand (blind - no model
  output shown), frozen, and kept out of every training window. All other metrics
  grade against the AI's own labels (imitation); this set is the only truth-based
  score, including "when the model and the AI disagree, who matched you".
- **Fast-paths and learners are one surface**: the same cards, badges and controls
  for tag-taught classifiers (already deciding) and AI-taught specialists (watching
  until promoted).

Design, audits and the research-backed roadmap:
[docs/mail-intelligence/design.md](mail-intelligence/design.md),
[docs/mail-intelligence/improvement-roadmap.md](mail-intelligence/improvement-roadmap.md).

## Assistant (Assistant page)

A streaming, tool-calling chat over the whole mailbox. The reply streams token by
token; the model's thinking renders in a live block; every tool call shows as a
card with its result, and the transcript survives reloads. Chats are listed and
resumable; a docked sidebar is available on every other page with page-aware
starter suggestions.

Tools: `mailbox_overview` - `search_messages` (local index) - `search_mail` (live
IMAP search: full history, any folder) - `read_message` - `move_message` -
`flag_message` - `create_folder` - `list_folders` - `propose_rule`.

Safety: moves/flags/folders act live by default (it can never delete or send);
untick "Assistant may act on mail" in Settings for dry-run. Rules are only proposed
in chat - they go live when you click "Add rule". Each turn is capped (8 tool
rounds, 4 calls per round) and everything is logged.

## Semantic search (RAG)

Two backends behind the same `rag.search` interface; pick with the "RAG backend"
setting (default **lite**). Both store into the same `triage.db` and each keeps its
own tables + folder state, so switching either way is a settings flip with no
reindex.

**lite (default)** - CPU-first, no extra services, no GPU: fielded FTS5
(subject/sender/body BM25) + sqlite-vec dense KNN over quote-stripped chunks + RRF
fusion + a small CPU cross-encoder, all inside the app:

```
embed    Qwen/Qwen3-Embedding-0.6B          1024-dim, FastEmbed/ONNX on CPU
rerank   jinaai/jina-reranker-v1-turbo-en   FastEmbed/ONNX on CPU
models   ./ragmodels   (mounts to /ragmodels, FASTEMBED_CACHE_PATH)
```

Queries extract sender/date hints and exact tokens (INV-39281) and push them down
as SQL pre-filters + quoted FTS terms, so metadata and lexical signals stay
first-class. The reranker sees the first 1,200 chars of each candidate (jina-turbo's
window is ~512 tokens; measured 97.9 R@1 at 1,200 chars vs 95.8 at 2,000, and ~350 ms
faster). Switched live 2026-10-01 (full backfill: 3,556 messages / 6,642 chunks,
18/18 folders). Live numbers (`tests/retrieval_eval.py`, 48 labelled queries):

```
mode                R@1     R@5     R@10     MRR    ms/q
fts               85.4%   91.7%    95.8%   0.884     16
vector            81.2%   97.9%   100.0%   0.885    233
hybrid            91.7%   97.9%   100.0%   0.942    175
hybrid+rerank     97.9%  100.0%   100.0%   0.990   1188
```

The legacy stack scored hybrid+rerank R@1 89.6 / MRR 0.941 on the same set (its
index never exceeded ~60% coverage). Prototype evaluation (0.6B parity with the 4B,
small-reranker comparison incl. the 34 exact-query set) - `docs/rag-lite-report.md`
on branch `rag-lite-eval` in `~/mail-triage-rag`.

CPU query cost (live): ~150 ms query embed, ~0.2 s end-to-end without rerank,
~1.2 s with; ~1.9 GB RSS, zero VRAM. For a first full backfill, point the embed
protocol at a scratch GPU TEI running the same 0.6B model (fast, identical
vectors), then flip back to local; during backfills drop `index_refresh_minutes`
to 1 so a restarted run resumes within a minute. Swap the reranker to
bge-reranker-v2-m3 from Settings for maximum exact-query quality while the GPU
exists.

**legacy (rollback)** - the original GPU stack: two HuggingFace TEI servers on the
second 3090 (`embed/`, CDI `nvidia.com/gpu=1`): `:8041` Qwen/Qwen3-Embedding-4B
(embeddings), `:8042` BAAI/bge-reranker-v2-m3 (rerank).

Build/refresh from the dashboard "Index now" (resumable, folder by folder) or
`docker exec mail-triage python app.py --index`. Quality harness:
`tests/retrieval_eval.py` + `tests/eval_queries.json` (recall@1/5/10 and MRR for
fts / vector / hybrid / hybrid+rerank).

## Message viewer and triage queue

The viewer renders real HTML email (sanitized) - formatted text, tables, inline
images; remote images are blocked by default (per-message "Load images", or
always-on in Settings). The sidebar carries Details, the per-message audit trail
(every event: classification, rules, moves, with reasoning), Actions, Replies.
Triage queue: file and jump straight to the next message; undo puts a message back
and keeps automation off it; snooze hides and resurfaces it; clearing “needs reply” (bulk or per
message) turns the flag off and outranks the model on any re-classify. The simulator page
dry-runs a rule, flow or classifier decision against any message and shows the
stage-by-stage verdict.

## Reply templates and drafting

Templates carry placeholders and live on the Templates page. Pick a message,
choose a template (or none), "Draft with LLM", review, copy, or "Save to Drafts" -
the draft lands in your Drafts folder to send from your normal client.

## Accounts and endpoints

**Accounts**: add mail accounts and sign them in via OAuth - the page shows the
exact redirect URI to register at the provider, drives the Authorise flow (open the
login link, paste the final URL back for loopback flows), reports token status
live, and can reset tokens or remove accounts. The email-oauth2-proxy runs as a
child process inside the container and exposes a plain local IMAP listener.

**Settings**: the LLM endpoint (any OpenAI-compatible server: base URL, model, API
key, timeout, thinking mode, optional fallback endpoint) and the RAG endpoints
(backend, protocols, base URL, model, optional key) are set in the UI and stored in
SQLite. Blank fields fall back to the container env (`.env`), so
environment-based deployments keep working.

## Mobile

The UI is responsive with a phone-first tab bar (Dashboard / Messages / Assistant /
More), native-feel page transitions, and dedicated mobile layouts per page. Design
records: docs/mobile-ui.md and friends.
