# Fusion Lab: TinyJev + MiniCPM, and the assistant fallback switch

Status: implemented 2026-10-03 on `fusion-tryout`. This is a try-out rig, not
the default pipeline: nothing routes real mail through the fusion unless the
user runs it from the Fusion Lab page.

## What it is

A local decision system that splits classification into two cheap jobs:

| job | model | why |
|---|---|---|
| `category` | TinyJev-0.6B (Qwen3-0.6B + pointer head, `Choice` with one-line category descriptions) | 0.6B CPU-friendly, no generation, structurally cannot emit off-enum labels |
| `needs_reply` + summary/reason | MiniCPM5-2B, **direct** needs_reply prompt, thinking **off** | thinking-off full-JSON collapses to "no"; the dedicated recall prompt recovers it at 4x the speed |

Historical experiment snapshot on the v2 classification suite (acceptance split,
106 cases, official scorer). Raw spike artifacts are not distributed with this
release, so these figures cannot be independently recomputed from a clean clone:

| system | quality | category acc | nr P / R / F1 | missed replies | false alarms | criticals |
|---|---:|---:|---|---:|---:|---:|
| baseline gemma-4-26b (reference) | 84.9 | 79.2 | .569 / 1.000 / .725 | 0 | 22 | 1 |
| MiniCPM5-2B thinking on | 75.8 | 57.5 | .633 / .655 / .644 | 10 | 11 | 2 |
| **fusion (recall prompt, thinking off)** | **81.8** | 67.9 | .562 / .931 / .701 | **2** | 21 | **0** |

Paired vs baseline: −3.15 pp (CI −9.39…+4.81), inconclusive but **not proven
noninferior**; vs the 2B thinking-off: +8.18 pp (better). Known weak spots: long
boilerplate mail (TinyJev category drops; a >4k-char fallback fixes it) and the
2 remaining misses are both that long-thread review-request scenario.

## Pieces

- `fusion/` - the sidecar: `server.py` (TinyJev + the recall prompt against any
  OpenAI-compatible 2B), `Dockerfile`, `docker-compose.yml`.
- `plugins/mt-fusion-lab` - the A/B surface: a trusted browser page (System →
  Fusion Lab) that lists indexed mail and shows the stored verdict next to
  primary, fallback and fusion verdicts with timings. Two hidden read-only
  tools (`list_messages`, `compare_message`) back it; nothing is written.
- The plugin host `ctx.llm.complete` gained `endpoint: "primary"|"fallback"`
  (and `thinking`) so the A/B can hit each configured endpoint explicitly.
- **Assistant model switch** - Settings → AI Settings → Assistant model: a
  switch that makes the assistant chat answer from the configured fallback
  endpoint (primary stays the safety net). The assistant header shows a
  `fallback: <model>` badge while it is on. Only the chat stream follows the
  switch; classification and drafting keep the primary endpoint.

## Setup

1. Serve a 2B (any OpenAI-compatible server), e.g.
   `benchmarks/serving/serve_llamacpp.sh /path/MiniCPM5-2B-Q4_K_M.gguf minicpm5-2b 8042 32768 4 --no-reasoning-preserve`
2. Start the sidecar:
   `docker compose -f fusion/docker-compose.yml up -d --build`
   (`FUSION_LLM_BASE_URL` points at the 2B; default `host.docker.internal:8042/v1`.)
   The TinyJev model is baked into the image and loaded offline; the port opens
   immediately and `/healthz` answers 503 until the model is ready (~8s: torch +
   transformers imports ~3.4s, weights ~3.7s, first forward ~1.2s).
3. In the app: enable **Fusion Lab** on the Plugins page (grant `mailbox.read`,
   `llm.complete`, `net.http`), set the Fusion service URL if not
   `http://fusion:8098`, then approve the browser view once.
4. For the primary/fallback comparison configure both endpoints under
   Settings → AI Settings → LLM endpoint.

## Transparent mode (no host changes)

The sidecar also exposes an OpenAI-compatible facade, so the fusion can be used
without the plugin, the endpoint selector, or any app modification: point a
client's base URL at `http://fusion:8098/v1` (model list comes from `/v1/models`).

- requests whose system prompt is the production classify prompt (detected and
  parsed for its category list) are answered by the fusion: TinyJev picks the
  category, the 2B's direct recall prompt fills `needs_reply`/summary/reason,
  and the reply is the production JSON shape;
- every other chat request is proxied verbatim to `FUSION_LLM_BASE_URL`
  (streaming included), so the same endpoint still behaves as the plain 2B;
- if the needs_reply half fails, the facade returns a 502 instead of a fake
  verdict, letting the caller's normal fallback handle it.

This is what "the model is a fusion" looks like from the app's perspective: one
ordinary OpenAI endpoint whose classification calls happen to be decided by the
decision head. The Fusion Lab plugin and `/classify` remain available for A/B.

## Caveats

- This is a classification experiment, not a complete six-suite model acceptance
  run or an end-to-end CPU agent performance benchmark. TinyJev runs on CPU in the
  sidecar; the separately served 2B model can use different hardware. The default
  serving example uses GPU-backed llama.cpp.
- 106 acceptance cases; numbers have wide confidence intervals (family-clustered
  bootstrap used throughout).
- `summary`/`reason` come from the 2B; TinyJev only decides the category.
- The sidecar keeps TinyJev loaded (~1.2 GB fp16, CPU); the 2B needs its own
  server. Thinking-off output is 36–50 tokens per email and produced zero
  malformed JSON across the runs.
- The trusted page requires explicit approval, runs in an opaque-origin frame,
  and reaches only its two declared read-only operations; the fusion URL is
  allowlisted by the manifest plus `allowed_hosts` config.
