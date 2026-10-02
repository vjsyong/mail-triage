# Mail Triage

**Your mailbox. Your machine. Your rules.**

Mail Triage is a self-hosted email assistant that turns a busy inbox into a
manageable workflow. Connect your institutional OAuth2 account, let a local AI
agent help you find and organize mail, and build automations that handle the
repetitive work. Keep using your usual email client.

It combines **local-first AI, a visual flow builder, natural-language actions,
and an extensible plugin system** in one app.

![Mail Triage dashboard: system status, triage metrics, recent filings, and recent mail](docs/img/dashboard.png)

[Quick start](#quick-start) · [Features](#why-mail-triage) · [Documentation](docs/README.md) · [Write a plugin](docs/plugins-authoring.md)

## Why Mail Triage?

### Connect institutional email with built-in OAuth2

Mail Triage bundles [simonrob/email-oauth2-proxy](https://github.com/simonrob/email-oauth2-proxy)
to connect OAuth2-enabled mailboxes, including institutional accounts. Add an
account, authorize it, and check its token status from the **Accounts** page—the
proxy runs inside the app's container, so there is no separate proxy service to
manage.

Your provider's OAuth app registration and access policies still apply. The setup
guide walks through the connection process.

### Privacy first, powered by a local agent

Run classification, mailbox chat, and drafting against a **local LLM**. The
database and search index live on your host, and the default semantic search
backend runs locally on CPU. You do not need a cloud AI service to use the app.

You choose the model endpoint. If you configure a hosted model or cloud fallback,
the email content included in those requests goes to that provider. Use local
endpoints to keep AI processing on your own machine.

### Build powerful flows visually

Turn inbox habits into repeatable, multi-step automations with a visual
**trigger → filters → steps** builder. Match exact message fields, an AI-assigned
category, or a topic by meaning, then chain actions in order:

- Move messages into folders.
- Tag, star, or mark them read.
- Create drafts from fixed text, saved templates, or AI instructions.

For example: **when a message is about a project deadline, tag it, star it, and
draft an acknowledgment.** Drafts land in your mailbox's Drafts folder for review.
You can build a flow on the canvas or describe it to the assistant and approve
its proposal with one click.

### Go from natural language to action

The assistant is an action interface across the app, with broad coverage of
everyday mailbox and automation tasks. Search and read mail, organize messages,
classify and tag them, draft replies, create folders, propose or update rules and
flows, and train or evaluate classifiers—all through conversation.

Try requests like:

> “Find the email about my tax refund.”
>
> “Move this message to Receipts and mark it read.”
>
> “When mail is about a project deadline, tag it and draft an acknowledgment.”
>
> “Train a classifier from the messages I've tagged.”

Responses stream live, tool calls and results are visible, and actions are logged.
Per-capability permissions let you choose **Off**, **Ask me**, or **Auto** for
supported actions; rule and flow proposals have one-click approval.

### Extend it with plugins

Add capabilities without changing the core app. Plugins can provide **assistant
tools, classifiers, rule conditions, draft providers, search re-rankers, and event
integrations**.

Built-in examples include an invoice finder, a daily digest, language-aware drafts,
and webhook notifications. Manage each plugin's settings and permissions in the
UI. Plugins run in separate sandboxed worker processes with memory, time, and
host-call limits; access to mail, models, and the network is permission-gated and
audited.

Start with the [plugin authoring guide](docs/plugins-authoring.md) and
[SDK](sdk/README.md) to build your own.

## More than an AI chat window

- **Rules first.** Deterministic filters handle predictable mail before AI is
  called. Guard rules keep important messages in place.
- **Classification with context.** Get a category, confidence, summary, and reason
  for messages that rules do not catch. AI auto-filing starts off.
- **Learning that reduces AI calls.** Train small, auditable classifiers from your
  tags or previous verdicts. Confident matches skip the LLM; learning-loop
  specialists run in shadow mode until you promote them.
- **Search by meaning or exact detail.** Find a topic even when you cannot remember
  the wording, or narrow results by sender, date, and exact terms. The default
  search backend needs no GPU.
- **A practical triage workspace.** Sanitized message viewing, file-and-next,
  snooze, tags, bulk classification, an undo trail, and a responsive mobile UI.

See the [full feature tour](docs/features.md) for details.

## Quick start

You need **Docker and Compose**, a mailbox, and—if you want AI features—an
OpenAI-compatible model endpoint. Rules and local search run on CPU; LLM hardware
requirements depend on the model you choose.

Clone this repository, open its directory, then run:

```bash
cp .env.example .env
# Set LLM_BASE_URL and LLM_MODEL for your local model.
# Leave them blank to start in rules-only mode.
docker compose up -d --build
```

Open **http://localhost:8097**. The **Get started** checklist walks you through:

1. Connecting and authorizing your mailbox.
2. Configuring and testing your model endpoint.
3. Building your search index.

Then create a rule, build a flow, or ask the assistant to help organize your mail.
Without an LLM, rules and search still work; AI classification and drafting wait
until an endpoint is available.

See [Getting started](docs/getting-started.md) for hardware tiers, local model
options, and account setup. See [Deployment](docs/deployment.md) for remote access,
TLS, backups, and upgrades.

## How it works

One container runs the web UI, background worker, and embedded OAuth mail proxy.
The database and search index persist on your host; the model endpoint is configured
separately.

```text
Your email provider
        ↕ OAuth2 / TLS
┌──────────────────────────────────────────────┐
│ Mail Triage container                        │
│                                              │
│ email-oauth2-proxy ↔ worker ↔ web UI :8097     │
└──────────────────────┬───────────────────────┘
                       ├── Local database + search index
                       └── Your LLM endpoint (local recommended)
```

The triage pipeline applies **rules → fast-path classifiers → LLM for the rest**,
then files or suggests according to your settings. Flows add ordered actions when
their conditions match. Each decision keeps an audit trail so you can see what
happened and why.

## Stay in control

- **AI auto-filing is opt-in.** Rules act live by default; use dry-run mode to
  preview automation.
- **Assistant permissions are explicit.** Choose which capabilities may run
  automatically and which need approval. Sending and moving mail to Trash are
  disabled by default.
- **Draft before sending.** Flow-generated replies go to Drafts for review in
  your normal email client.
- **Protect and undo.** Guard rules pin matching mail in place; Undo restores
  filed messages and keeps automation off those messages.
- **Review before promoting.** Learning-loop specialists start in shadow mode
  and only take over when you promote them.
- **Inspect what happened.** Classification reasons, tool results, automation
  events, and plugin activity are visible and logged. LLM calls are capped per hour.

## Operations

Run these commands from the repository root:

```bash
docker compose ps                                      # status
docker logs -f mail-triage                              # live logs
docker compose up -d --build                            # rebuild after code changes
docker restart mail-triage                              # restart
docker exec mail-triage python app.py --check            # read-only health report
docker exec mail-triage python app.py --doctor           # hardware + model setup advice
docker exec mail-triage python app.py --index            # resumable search indexing
docker exec mail-triage python app.py --plugins list     # installed plugins
docker exec mail-triage python learning.py report        # learning loop state
```

Persistent state lives in `data/`: `triage.db` holds app data, and
`data/emailproxy/` holds the generated proxy configuration, encrypted token cache,
and proxy log. Back up this directory; see the
[deployment guide](docs/deployment.md).

## Documentation and development

| Guide | What you'll find |
| --- | --- |
| [Getting started](docs/getting-started.md) | Installation, hardware, models, and first-run setup |
| [Feature tour](docs/features.md) | Detailed coverage of the app's capabilities |
| [Deployment](docs/deployment.md) | Ports, TLS, backups, and upgrades |
| [Plugin authoring](docs/plugins-authoring.md) · [SDK](sdk/README.md) | Write, install, and test extensions |
| [Plugin architecture](docs/plugin-architecture.md) | Extension types, sandboxing, and assistant integration |
| [Learning loop](docs/mail-intelligence/design.md) | Model lifecycle, evaluation, and decision provenance |
| [Docs index](docs/README.md) | All guides, design records, and research |

The core lives in `app.py` (UI), `engine.py` (mail and automation), `store.py`
(SQLite), and `proxy.py` (OAuth proxy management). Search lives in `rag.py` /
`rag_lite.py`; learning in `learning.py` / `heuristics.py`; the plugin system in
`plugins.py`, `plugin_rt.py`, and `plugin_worker.py`.

Run the mock end-to-end suites with the project's Python environment:

```bash
.venv/bin/python tests/mock_e2e.py     # app suite: mock IMAP, LLM, and embeddings
.venv/bin/python tests/proxy_e2e.py    # proxy suite: mock OAuth and IMAP
```

## License and acknowledgments

Mail Triage is [MIT licensed](LICENSE).

OAuth2 mailbox connectivity is powered by
[email-oauth2-proxy](https://github.com/simonrob/email-oauth2-proxy) by Simon Robinson.
