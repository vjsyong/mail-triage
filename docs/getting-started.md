# Getting started

Self-hosted triage for a single mailbox. No GPU is required: rules and semantic
search run on CPU; LLM classification, assistant chat, and AI drafting need a
separately configured model endpoint.
This guide covers the install, how to pick an LLM for your machine, and the
first-run checklist.

## 1. Requirements

- Docker Engine + Compose v2.
- Budget several GB of RAM and disk for CPU retrieval models in addition to the
  app and mail index. In the measured deployment, the embedder alone used about
  1.9 GB RAM after load; bulk indexing can use substantially more. See the
  [retrieval resource notes](rag-lite-report.md#resource-tradeoffs). A local LLM
  server is extra.
- A mailbox reachable over IMAP. The embedded email-oauth2-proxy handles OAuth
  providers (Gmail, Outlook/Microsoft 365 and most university providers) and
  owns the token refresh; look in `data/emailproxy/` after first run.

## 2. Install

```bash
git clone https://github.com/vjsyong/mail-triage.git
cd mail-triage
cp .env.example .env
# Optional: set LLM_BASE_URL + LLM_MODEL (see section 3).
# Leave them blank to start without LLM features.
docker compose up -d --build
# open http://localhost:8097
```

The container keeps everything in `./data` (database, index, OAuth tokens) and
`./ragmodels` (the CPU embedding/reranker models, downloaded on first use).

## 3. Choosing an LLM

The app talks to any OpenAI-compatible `/chat/completions` endpoint. What to run
depends on your hardware, and the app can tell you what it sees:

```bash
# inside the container (sees only GPUs the container was given)
docker exec mail-triage python app.py --doctor

# host-side equivalent, before deciding anything
nvidia-smi --query-gpu=name,memory.total --format=csv
```

`--doctor` maps what it finds to a tier and prints the advice, and the same
detection shows on the **Get started** page (`/welcome`).

### No GPU (or a small one)

Rules, the UI, and CPU search work without an LLM once the mailbox is connected
and indexed. Assistant chat, classification, AI drafting, and other LLM-backed
features need a compatible endpoint. Two ways to get one:

1. **A hosted OpenAI-compatible API** (usually the best quality). Example `.env`:

   ```bash
   LLM_BASE_URL=https://api.your-provider.example/v1
   LLM_API_KEY=sk-...
   LLM_MODEL=your-model-name
   ```

   The cloud endpoint receives the same prompts the local model would: message
   snippets go to it. Keep a local model instead if that matters to you.

2. **A small local model on CPU** via [ollama](https://ollama.com) or
   llama.cpp, which expose an OpenAI-compatible server:

   ```bash
   ollama pull qwen2.5:7b-instruct      # or a 3B for weaker machines
   ollama serve                          # serves http://127.0.0.1:11434/v1
   ```

   ```bash
   LLM_BASE_URL=http://host.docker.internal:11434/v1
   LLM_MODEL=qwen2.5:7b-instruct
   ```

   This is a connection example, not an evaluated model recommendation. CPU
   latency and tool-calling quality depend on the model and hardware. Compose
   supplies the `host.docker.internal` host-gateway mapping on Linux; the model
   server must also listen on an interface reachable from Docker. A server bound
   only to host `127.0.0.1` is not reachable through the Linux bridge gateway.

### 8-16GB GPU

A quantized 7-14B instruct model fits with room to spare and is a solid triage
model. Serve it with vLLM or ollama and point `LLM_BASE_URL` at it. Example with
ollama (which does its own quantization):

```bash
ollama pull qwen2.5:14b-instruct
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=qwen2.5:14b-instruct
```

> Benchmarked (2026-10): small local models were measured against this app's own
> workload. The current benchmark does **not** establish a drop-in smaller-model
> replacement, and prompt-injection failures occurred in both a small candidate
> and the larger baseline. Read [model-evaluation.md](model-evaluation.md) before
> choosing an endpoint; parameter count alone is not a reliability guarantee.

### 24GB+ GPU

`gemma/` in this repo is a complete, validated vLLM setup for a 26B MoE on a
single 24GB card (the exact stack this project was developed against, including
a boot-time patch for the KV-cache quantization it uses):

```bash
cd gemma
HF_HOME=$HOME/models docker compose up -d     # first boot downloads weights
cd ..
# .env:
LLM_BASE_URL=http://<this-host>:8040/v1
LLM_MODEL=gemma-4-26b-a4b
```

Other models use the same endpoint configuration, but chat templates, tool
calling, structured output, and thinking settings can require adaptations. The
folder is an example, not a requirement.

> Optional fallback: `LLM_FALLBACK_*` can point at a second endpoint if the
> primary fails. A hosted fallback receives the message content included in those
> requests. Leave it blank for local-only processing.

## 4. First run

Open http://localhost:8097. A fresh install lands directly on the **setup wizard**
(`/welcome`, also under More → Get started). It is a full-screen three-step flow,
resumable and skippable - state is auto-detected, so you can exit and come back
any time:

1. **Connect your mailbox** - links to the Accounts page, which shows the exact
   redirect URI to register with your provider and drives the OAuth login. Come
   back and the step shows as done.
2. **Point at an LLM** - edit the endpoint right in the wizard (same settings as
   the Settings page), then "Test connection". The wizard shows what hardware it
   detected to help you choose; skip it and rules plus search still work.
3. **Build the search index** - CPU-only and resumable; the wizard starts it and
   shows live progress.

Afterwards, keep an eye on the dashboard banner if you skipped a step - it links
back to the wizard until setup is complete. Tagging a few messages as you triage
feeds the learning loop that turns repeated LLM decisions into small local
models.

The dashboard banner tracks the same state; "Hide setup help" on the final screen
dismisses it.

## 5. Troubleshooting

- **Nothing gets classified.** Check the LLM line in `--doctor`: an unreachable
  or unconfigured endpoint leaves classification idle by design (rules and
  search keep working). The dashboard's system card flags it too.
- **"Test LLM" fails.** Confirm the URL ends in `/v1` (OpenAI-compatible base),
  the model name matches what the server serves (`curl <base>/models`), and from
  inside the container the host is reachable (use `host.docker.internal` for
  services on the Docker host).
- **OAuth flow doesn't complete.** Loopback-mode accounts use the paste-back
  box; tailnet/reverse-proxy setups can use the callback ports
  (41810-41819, see docs/deployment.md). The Log page and the proxy log card
  show the proxy's own output.
- **Search is slow on first use.** The first index run downloads the CPU
  embedding models and embeds your whole archive; later runs are incremental.
