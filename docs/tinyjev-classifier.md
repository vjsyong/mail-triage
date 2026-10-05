# TinyJev classifier: full-lifecycle plugin + sidecar

*Built 2026-10-05. The production classifier counterpart to the Fusion Lab
try-out rig: TinyJev-0.6B decides the category on the real classify path when
native heuristics abstain — fully local, one forward pass, no generation.*

## What it is

- **`tinyjev/` sidecar** — a small HTTP service that owns the model lifecycle:
  first boot downloads `TinyJev-0.6B` (~1.2 GB) from HuggingFace into a
  persistent volume, loads it, warms it up, and serves `/classify`. Status
  (phase + download progress) is on `GET /status`; `/healthz` flips 503→200
  when ready. After the first download the service is fully offline.
- **`plugins/mt-tinyjev-classifier`** — built-in plugin, `kind: [classifier,
  tool]`. The sandbox cannot run a 1.2 GB model (no filesystem, no sockets),
  so the plugin is the bridge: `classify()` builds the message state and asks
  the sidecar; tools report lifecycle status, test-drive a snippet, and kick a
  (re)download. No LLM permission — only `net.http` to the sidecar host.

## Why a sidecar

The plugin runtime is a QuickJS sandbox: every way out is a host-mediated
function, and `ctx.http.fetch` is the only network. The model needs torch +
1.2 GB of weights + a warm process — that is a service, not a sandbox bundle.
This is the same split the Fusion Lab uses (`fusion/` sidecar + plugin), and
the plugin deliberately speaks the fusion sidecar's `/classify` response shape
(`category`, `category_confidence`, `category_probabilities`, `components`,
`latency_ms`), so `tinyjev_url` may point at either service.

## Lifecycle

```
boot ──► download ──► load ──► ready
              │          │
              └── error ◄┘        POST /load restarts from any phase
```

- **download**: `huggingface_hub.snapshot_download` into `$HF_HOME/hub`
  (mounted volume `tinyjev-cache:/data/hf`). A watcher thread polls the cache
  size against `HfApi().model_info(..., files_metadata=True)` every 2 s, so
  `/status` reports real progress. Cache-complete boots skip this phase
  (`local_files_only` probe first).
- **load**: `tinyjev.load(TINYJEV_MODEL, device=cpu[, quantize=N])` + one
  warm-up `predict` off the request path. Quantize 8/4 halves/quarters the
  footprint (measured by upstream: int8 ≈ free, int4 ≈ -2 points).
- **ready**: `POST /classify` answers. One forward pass answers the category
  `Choice` **and** the needs_reply `Noul` question together (`TINYJEV_NR=on`,
  threshold 0.40) — the Noul half rides along in the same pass and lands in
  the plugin's `detail` for audit; the pipeline only consumes the category.

## The classify path

`heuristics.classify()` → native heuristics first (they always win) → when all
abstain → `_classify_via_plugins()` → the plugin → the sidecar. The plugin is
opted in from its Plugins page: tick *classification fast-path* **and pick the
bound heuristic** (the binding select). The binding stores
`plugin_classifiers = [{"plugin": id, "heuristic_id": N}]`; the heuristic row
supplies the `min_confidence` gate (park it disabled — the plugin stands in
for it) and may carry `{"categories": [...]}` in its model JSON to override
the plugin's configured category list. Anything else (sidecar busy,
unreachable, off-enum label, below the gate) abstains quietly and the LLM
decides as before — the plugin path can only *replace* the LLM call, never
misfile silently.

> Before 2026-10-05 the Plugins-page checkbox stored a bare plugin-id string,
> which `_classify_via_plugins` ignores (it needs the heuristic binding). The
> save handler now stores the functional dict form and the detail page renders
> the binding select; `mt-promo-fastpath` benefits from the same fix.

## State building

The host featurizes messages for heuristics (`{tokens, from, domain, subject,
body}`); the plugin rebuilds a text state from those fields (the model wants
prose, not token counts):

```
From: <from>
Subject: <subject>

<body, clipped to state_chars (default 1500)>
```

Categories come from the bound heuristic's model JSON, else the plugin
config (`categories`, default the app's six), and the sidecar's answer is
checked against that enum before it is trusted.

## Running it

```
docker compose -f tinyjev/docker-compose.yml up -d --build   # joins mail-triage_default
curl -s http://127.0.0.1:8099/status                         # watch download/load
```

Then in the UI: Plugins → TinyJev classifier → enable, tick *classification
fast-path*, pick a bound heuristic, save; set the service URL in Settings if
the sidecar is not on the default `http://tinyjev:8099`.

Port 8099 (8098 is fusion). Env: `TINYJEV_MODEL` (default TinyJev-0.6B),
`TINYJEV_DEVICE` (cpu), `TINYJEV_QUANTIZE` (""/8/4), `TINYJEV_NR` (on/off),
`TINYJEV_NR_THRESHOLD`, `TINYJEV_THREADS`, `HF_HOME`. Re-download from
scratch: `docker compose -f tinyjev/docker-compose.yml down -v`.

## Testing

`tests/mock_e2e.py` section T66 (`plugins`, `core`): sidecar helpers (repo
resolution, Noul decision, lifecycle shape), plugin registration + host
allowlist + grants, a stateful mock sidecar driving the status/classify/load
tools, the classify path (label/confidence mapping, config vs bound-heuristic
categories, off-enum + busy + empty abstains, sidecar untouched on empty), the
full pipeline through `heuristics.classify` with the binding, the confidence
gate, and the Plugins-page opt-in storing/rending the binding.

## Limits / honest notes

- First boot on a cold cache downloads ~1.2 GB — `/status` shows progress;
  until `ready` the plugin abstains and the LLM keeps classifying.
- The 0.6B answers category choice well (fusion measurements: category
  p50 ~1.0 s on 4 cores); its Noul needs_reply recall is lower (that is why
  fusion pairs the category head with the 2B for needs_reply). Treat the Noul
  half as informational here.
- TinyJev 4B v2 runs through Ollama, not in-process; the sidecar keeps to the
  0.6B/4B-v1 pointer models (`TINYJEV_MODEL`).
