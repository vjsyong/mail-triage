# Mail Triage

Self-hosted triage for a single mailbox. Deterministic rules sort most mail, a local
LLM classifies the rest, and a learning loop compiles the LLM's repeated reasoning
into small auditable models that answer before the LLM is ever called.

![The dashboard: system status, triage metrics, recent filings, recent mail](docs/img/dashboard.png)

One container on one host: Flask UI + worker + embedded OAuth mail proxy. Mail stays
on the machine; the LLM defaults to a local model, and a cloud fallback is optional.

## Features

- **Rules first.** Match from / to / subject / body (contains, equals, regex; ALL or
  ANY), then move, mark read, flag. First match wins; a rule with no actions is a
  guard that pins matching mail in place so nothing else can move it. Folders are
  created if missing; tokens of 3 characters or fewer match whole words only.
- **LLM classification.** Whatever no rule matched is classified into your
  categories with a confidence, a one-line summary, and a short reason. Auto-filing
  starts off; per-hour call cap.
- **Fast-path classifiers.** Deterministic models (decision lists, naive Bayes)
  trained from your tags or the LLM's own verdicts. A confident hit skips the LLM,
  runs identically every time, and can't be steered by instructions hidden inside
  email content. Every classifier has a dataset page for reviewing and correcting
  the exact samples it learns from.
- **Learning loop.** Small specialists train on mailbox history in seconds (reply
  detector, category sorter), watch in shadow mode first (recording what they would
  do, changing nothing), get scored against a hand-labeled test set, and only take
  over when you promote them. Models are plain JSON; every decision keeps its
  evidence. See `docs/mail-intelligence/design.md`.
- **Flows.** Multi-step automations: WHEN a message matches (exact fields, AI
  category, or topic by meaning) then run steps in order - move, tag, flag, mark
  read, draft from a template or with the LLM into Drafts.
- **Assistant.** A streaming tool-calling chat over your whole mailbox (live IMAP,
  any folder): search, read, move, flag, create folders, propose rules. Thinking and
  every tool step are visible; actions can run dry-run; every action is logged.
- **Plugins.** Sandboxed extensions with per-plugin enable, permissions and
  settings: assistant tools (invoice finder, daily digest), classification
  fast-paths (promo fast-path), rule conditions (CJK matcher), draft providers,
  search re-rankers and event integrations (webhook notifications on filed /
  classified events). Each plugin runs in its own worker process with hard
  memory, time and host-call limits; the Plugins page gives every plugin a detail
  view with a plain-language permission list. Write your own from
  `docs/plugins-authoring.md`.
- **Semantic search.** Hybrid local index: fielded FTS5 + sqlite-vec dense
  retrieval, RRF fusion, small CPU cross-encoder reranker. No GPU needed.
- **Message viewer and triage queue.** Sanitized HTML rendering, one-click
  file-and-next, undo trail, snooze that resurfaces, tagging, clearing the
  needs-reply flag (bulk or per message), bulk "classify selected" / "classify all
  unclassified".
- **Templates and drafting.** Reply templates with placeholders; draft with the LLM
  and save straight into Drafts to send from your normal client.
- **Accounts.** Mailbox sign-in via OAuth handled in the UI through the embedded
  email-oauth2-proxy; token status live, restart/remove managed there.

Full detail on every feature: [docs/features.md](docs/features.md).

## Quick start

Docker + Compose. A GPU is optional - any OpenAI-compatible LLM endpoint works, and
search embeddings run on CPU by default.

```bash
git clone <this repo> && cd mail-triage
cat > .env <<'EOF'
LLM_BASE_URL=http://127.0.0.1:8040/v1     # any OpenAI-compatible endpoint
LLM_MODEL=gemma-4-26b-a4b
UI_PORT=8097
DATA_DIR=/data
EOF
docker compose up -d --build
# then open http://localhost:8097
```

First run:

1. **Accounts** - add your mailbox and complete the OAuth flow (the page shows the
   exact redirect URI to register, drives the login, reports token status).
2. **Settings** - set the LLM endpoint (blank fields fall back to `.env`) and press
   "Test LLM endpoint".
3. Optional: run the bundled local model server - `cd gemma && docker compose up -d`
   (one GPU, vLLM, port 8040).
4. Tags and corrections you make now feed the learning loop; train your first
   specialist from **Learning**.

## How it works

```
+-------------------------------------------- mail-triage container --------------+
|   email-oauth2-proxy (child process)          Flask UI :8097 + worker loop      |
|   OAuth 2.0 / TLS  ==========> provider      accounts: Accounts page (SQLite)   |
|   127.0.0.1:1993 plain IMAP <---- reads <---- MailClient                        |
+-------------------------------|--------------------------|----------------------+
                                  |                          |
                    +-------------v------------+   +---------v------------------+
                    |  local LLM (vLLM)        |   |  semantic search index     |
                    |  GPU 0 :8040, optional   |   |  FTS5 + sqlite-vec (CPU)   |
                    |  fallback: any endpoint  |   |  or legacy TEI :8041/:8042 |
                    +--------------------------+   +----------------------------+
```

Cycle: poll inbox -> rules -> fast-path classifiers -> LLM for the rest -> file or
suggest. Each stage records what it decided and why (per-message audit trail).

## Safety model

- **Never deletes mail.** Worst case it files something into a folder; Undo puts it
  back and keeps automation off that message.
- Rules act live by default (dry-run toggle in Settings); LLM auto-filing starts off.
- The assistant can move/flag/create folders - never delete or send - and can be
  switched to dry-run.
- **Plugins are sandboxed and read-only over mail.** Each runs in its own worker
  process with hard memory/time limits; host calls (mailbox reads, the LLM,
  network) are permission-gated, rate-capped and audited; the assistant needs the
  plugin's off/ask/auto gate before it can call its tools; disabling a plugin
  stops it everywhere.
- The learning loop changes nothing until you promote it: shadow first, human
  promotion, plain-JSON models, hand-labeled test sets, full decision provenance.
- LLM calls are capped per hour; every automation and agent action is logged on the
  Log page.

## Operations

All commands from the repo root:

```bash
docker compose ps                     # status
docker logs -f mail-triage            # live logs
docker compose up -d --build          # rebuild after code changes (image bakes the app)
docker restart mail-triage            # simple restart
docker exec mail-triage python app.py --check          # read-only health JSON
docker exec mail-triage python app.py --index          # run the search indexer (resumable)
docker exec mail-triage python app.py --reindex        # wipe + rebuild the index
docker exec mail-triage python app.py --heal-snippets  # repair legacy raw-MIME snippets
docker exec mail-triage python app.py --plugins list   # plugin registry (list|enable|disable|grant|config|invoke)
docker exec mail-triage python learning.py report      # learning loop state
.venv/bin/python tests/mock_e2e.py                     # E2E suite (mock IMAP + LLM; 600+ checks)
.venv/bin/python tests/proxy_e2e.py                    # embedded-proxy E2E (mock OAuth + IMAP)
```

Data lives in `data/`: `triage.db` (messages, rules, templates, chat, learning
tables) and `data/emailproxy/` (generated config, encrypted token cache, proxy log).

## Docs

- [docs/](docs/README.md) - index of everything, sorted by purpose (start here /
  design records / research / history)
- [docs/features.md](docs/features.md) - the full feature tour
- [docs/plugin-architecture.md](docs/plugin-architecture.md) - the plugin system
  as built: kernel, sandbox, kinds, assistant integration
- [docs/plugins-authoring.md](docs/plugins-authoring.md) and
  [sdk/README.md](sdk/README.md) - writing, installing and testing plugins
- [docs/plugins-ui.md](docs/plugins-ui.md) - the Plugins page design (research +
  applied patterns)
- [docs/mail-intelligence/design.md](docs/mail-intelligence/design.md) - learning
  loop design and safety invariants
- [docs/mail-intelligence/improvement-roadmap.md](docs/mail-intelligence/improvement-roadmap.md) -
  test sets, retraining triggers, where it goes next

## Layout

```
app.py            Flask UI + routes + worker start      Dockerfile
engine.py         IMAP client, rules, LLM, assistant    docker-compose.yml
store.py          SQLite schema + queries               .env (secrets fallback, 600)
config.py         env fallbacks for deployments         tests/
proxy.py          embedded email-oauth2-proxy manager   docs/
rag.py, rag_lite.py   semantic search (legacy GPU, CPU) gemma/ (local model server, GPU 0)
learning.py       learning loop (specialists, decisions, test sets)
heuristics.py     fast-path classifiers                 static/, fonts/, icons/
plugins.py        plugin kernel (manifest, registry, grants)    sdk/, schemas/
plugin_rt.py      sandbox supervisor, host calls, events        plugins/ (built-ins)
plugin_worker.py  sandboxed QuickJS interpreter per plugin
```

Private personal project; no license granted. Times in the UI follow the display
timezone setting (default UTC+8).
