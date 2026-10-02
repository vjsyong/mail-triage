# Getting started

Self-hosted triage for a single mailbox. No GPU is required: rules and semantic
search run on CPU, and the only thing an LLM adds is classification and drafting.
This guide covers the install, how to pick an LLM for your machine, and the
first-run checklist.

## 1. Requirements

- Docker Engine + Compose v2.
- ~2GB RAM and a few GB of disk for the app; the search index adds roughly the
  size of your mail archive; a local LLM server is extra.
- A mailbox reachable over IMAP. The embedded email-oauth2-proxy handles OAuth
  providers (Gmail, Outlook/Microsoft 365 and most university providers) and
  owns the token refresh; look in `data/emailproxy/` after first run.

## 2. Install

```bash
git clone <this repo> && cd mail-triage
cp .env.example .env
# edit .env: set LLM_BASE_URL + LLM_MODEL (see section 3), everything else can stay blank
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

Everything except classification and drafting works out of the box. Two ways to
get an LLM:

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

   Quality is modest (shorter summaries, more classification misses) but fully
   private and free. Note `host.docker.internal` requires Docker 20.10+.

### 8-16GB GPU

A quantized 7-14B instruct model fits with room to spare and is a solid triage
model. Serve it with vLLM or ollama and point `LLM_BASE_URL` at it. Example with
ollama (which does its own quantization):

```bash
ollama pull qwen2.5:14b-instruct
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=qwen2.5:14b-instruct
```

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

Any other model that fits works the same way; the folder is an example, not a
requirement.

> Tip: keep `LLM_FALLBACK_*` pointed at a hosted API if you run a local model -
> the app uses it only when the primary endpoint fails, so a crashed GPU server
> degrades instead of stopping.

## 4. First run

Open http://localhost:8097. The app lands on the **Get started** checklist
(also under More → Get started) which tracks four steps:

1. **Connect your mailbox** (Accounts page) - the page shows the exact redirect
   URI to register with your provider and drives the OAuth login.
2. **Point at an LLM** - set it in `.env` (above) or on the Settings page, then
   press "Test LLM". Blank settings fall back to `.env`.
3. **Build the search index** - CPU-only and resumable; "Index now" on the
   dashboard or the checklist starts it.
4. **Sort some mail** - tag a few messages or write a rule. That is the raw
   material the learning loop trains on.

The dashboard banner tracks the same state and hides once you dismiss the
checklist.

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
