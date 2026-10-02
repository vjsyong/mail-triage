# Plugin architecture & refactoring blueprint

Mail Triage (local-first AI email co-pilot) - microkernel decomposition, plugin SDK
contract, sandbox strategy, and a 4-phase rollout plan.

- Status: **draft for review** (analysis + interface contracts only; no implementation)
- Date: 2026-10-02
- Scope: `~/mail-triage` @ `ce0b5e9` (suite: 557 checks green)
- Companion reading: `docs/agent-permissions.md` (the assistant permission matrix this
  design deliberately extends), `docs/app-inventory.md`, `AGENTS.md` (workflow rules)

---

## 0. Executive summary

Today the application is 9 Python modules (~18.2k LOC) in one container: Flask UI +
worker threads + an embedded OAuth mail proxy, with SQLite and a local RAG index on
this host. Extension points already exist *inside* the process (`register_kind`
registries in `heuristics.py`/`learning.py`, swappable RAG backends, the assistant's
`_tool_*` dispatch, the assistant permission matrix). What is missing is an extension
point *across* a process boundary: versioned, sandboxed plugins that the assistant
auto-discovers as tools.

**Recommended architecture (decisions at a glance):**

| Decision | Choice | Rationale |
|---|---|---|
| Plugin authoring language | TypeScript compiled to a single JS bundle (esbuild) | Best agent/DX ecosystem; small host API surface means no npm needed |
| Manifest | `manifest.json`, JSON Schema 2020-12, semver `engines.sdk` | Validated as data, never evaluated |
| Runtime | QuickJS compiled to WebAssembly (Extism-style), embedded in the Python host | Only tier where memory + CPU limits and zero ambient authority are all enforceable; no second runtime service in the container |
| Trust model | Manifests + bundles are untrusted code; capabilities are explicit host functions; user grants are per-plugin | Mirrors `docs/agent-permissions.md` principles |
| v1 plugin reach | **Read-only mail + propose-actions** | Mutations stay in kernel code paths; plugins request, the kernel executes on user click |
| Tool integration | Namespace `plugin__<id>__<tool>`, merged into `ASSISTANT_TOOLS`, executed through `AssistantAgent.call_tool` | One choke point, one audit trail, existing Action Cards reused |
| Discovery roots | `plugins/` (built-in, shipped in repo/image, `mt-` prefix) + `$DATA_DIR/plugins/` (user) | Built-ins version with the app; user plugins never shadow them |
| Phasing | 1) kernel seams + SDK definition, 2) sandbox runtime, 3) two dogfood plugins, 4) assistant tool discovery + Plugins page | Each phase independently shippable with a green suite |

Non-goals for v1 (explicit): plugin-provided UI panes, plugin cron/trigger registration,
direct mail mutation from inside a plugin, npm dependencies inside the sandbox, a plugin
marketplace, and plugin code signing (integrity hashing only; revisit later).

---

## 1. Codebase audit & decomposition

### 1.1 Current shape (measured 2026-10-02)

| Module | LOC | Role | Coupling notes |
|---|---|---|---|
| `app.py` | 7,468 | 95 Flask routes + **all** page templates as string constants | Templates in-file (repo convention); routes call engine functions directly |
| `engine.py` | 4,803 | IMAP client, MIME parsing, rules/flows, classification, drafts, assistant agent | The broadest coupling: transport + domain + orchestration in one module |
| `store.py` | 1,919 | SQLite layer, 28 tables incl. audit trails (`events`, `msg_events`, `observations`, `undo_log`) | Clean seam; everything else depends on it - keep it that way |
| `learning.py` | 1,373 | Learning loop, specialists, logreg trainers, eval | Own `register_kind` registry (train/predict/describe) |
| `proxy.py` | 991 | email-oauth2-proxy manager (OAuth2 bridge: accounts, ports, redirects) | Credential boundary; never pluggable |
| `rag.py` / `rag_lite.py` | 683 / 465 | Search backends (lite = CPU ONNX, default) | Already two interchangeable implementations - precedent for capability-based backends |
| `heuristics.py` | 464 | Fast-path classifiers (naive bayes, decision list) + `auto_refine` | Own `register_kind` registry; closest thing to a plugin today |
| `config.py` | 45 | Env-driven config (`DATA_DIR`, LLM/embed endpoints) | `DATA_DIR` is where the new plugins dir hangs off |
| `tests/mock_e2e.py` | - | Mock E2E suite (mock IMAP/LLM/TEI), 557 checks | Every phase below adds checks; never weaken the suite |

**Coupling hotspots a kernel split must respect:**

1. `engine.py` mixes transport, domain and agent concerns (e.g. `_process_folder` at
   engine.py:1378 does IMAP fetch, rule matching, moves and audit in one function).
2. A single feature is often referenced from three places: a route (`app.py`), an
   engine function, and an assistant tool (`_tool_*` methods on `AssistantAgent`).
   Example: drafts = route + `generate_draft`/`save_draft` (engine.py:2351/2371) +
   `draft_reply` tool.
3. Templates live inside `app.py`; that stays (it is the repo convention and the SPA
   shell depends on it - see `docs/spa-turbo.md`).

**Existing in-process extension seams worth naming (the design builds on these):**

- `heuristics.register_kind(name, train_fn, predict_fn, describe_fn, blurb)` (and the
  same pattern in `learning.py`) - a real registry: classifier kinds are already
  data-driven. A plugin classifier impersonates a kind; it does not need new plumbing.
- `rag_lite.search(..., mode=)` + the `rag.py` twin - two backends already coexist, so
  "retriever provider" is a proven concept locally.
- `AssistantAgent` (engine.py:3587): tool schemas (`ASSISTANT_TOOLS`, engine.py:2631),
  dispatch by name (`getattr(self, "_tool_" + name)`, engine.py:3631), permission
  matrix and pending actions (`agent_actions`), SSE streaming with collapsed tool
  chips. This is the integration surface for plugin tools.
- `store` settings table + `events` audit log - reuse for grants and plugin auditing.

### 1.2 Core microkernel (stays native, never pluggable)

| # | Component | Current home | Why it must stay native |
|---|---|---|---|
| 1 | Storage & durability: schema, migrations, audit trails, never-delete guarantee | `store.py` | Data integrity; the app's core promise ("never deletes mail") cannot depend on plugin code |
| 2 | Mail transport: IMAP/SMTP, OAuth2 bridge, credentials | `engine.MailClient`, `proxy.py`, `config.py` | Credential boundary; reliability of sync |
| 3 | Workers & scheduling: scan loop, classify queue, index passes | `engine.Worker`, `ClassifyJob`, `process_mailbox` | Ordering/idempotency guarantees |
| 4 | HTTP shell & router: Flask app, `BASE_TMPL`, SPA nav, mobile shell | `app.py` | Single rendering pipeline; plugin pages are out of scope v1 |
| 5 | Assistant orchestrator: schema assembly, step loop, permission gate, audit | `engine.AssistantAgent`, `docs/agent-permissions.md` | The security choke point must not be bypassable |
| 6 | **Plugin subsystem (new)**: registry, manifest validation, runtime host, capability mediation, action bridge | new: `plugins.py`, `plugin_rt.py` | The kernel of the microkernel |
| 7 | RAG/index substrate: chunk store, embed client, FTS | `rag_lite.py`, `store` (`chunks2`, `index2_state`) | Heavy, shared, and safety-relevant (it feeds search tools) |
| 8 | Config & secrets: env + settings | `config.py`, settings table | Plugins receive mediated handles, never raw secrets |

### 1.3 Extraction candidates (built-in plugins)

Priority P1 = first dogfood (Phase 3), P2 = next wave, P3 = later. "Kind" maps to the
manifest `kind` enum in section 2.

| Feature (current location) | Plugin kind | Interface the plugin consumes | Priority |
|---|---|---|---|
| Promo / newsletter fast-path classifiers (`heuristics.py` train/predict, `auto_refine`) | `classifier` | `predict(features) -> label + confidence` via host hook | **P1** |
| Category sorter / specialists (`learning.py`, `specialists` table) | `classifier` | same; needs the eval harness shipped with it | P2 |
| Rule condition evaluators (`engine._cond_field`, `rule_matches`, engine.py:732-777) | `matcher` | `match(condition, fields) -> bool` | P2 |
| Flow step handlers (`engine._apply_flow`, engine.py:915; move/draft/tag steps) | `action-handler` | propose-only in v1 (kernel executes) | P2 |
| Draft generation providers (`LLMClient.draft_reply`, `_fallback_draft` engine.py:1612) | `draft-provider` | `draft(msg, instructions) -> text` | P2 |
| Semantic search ranking / query expansion (`rag_lite.search` modes) | `retriever` | `rank(query, candidates) -> ordered ids` | P3 |
| Digest / summary composer (assistant "Summarize my inbox" path) | `tool` | new capability | P3 |
| Webhook / notification on rule or flow fire (nothing today) | `integration` | kernel emits event -> plugin notified (egress-mediated) | P3 |
| Invoice / PDF attachment parsing (nothing today) | `tool` + `parser` | `mailbox.read` + `llm.complete` + card output | P3 |

Explicitly **not** extraction candidates: IMAP/SMTP, OAuth proxy, schema migrations,
the permission gate, guard rules (`is_guard_rule`), and the never-delete path.

### 1.4 Target architecture

```
+---------------------------------------------------------------------------+
| KERNEL (native, trusted)                                                  |
|  store.py (SQLite, audit)   engine.MailClient / proxy.py (IMAP, OAuth)    |
|  workers + scheduler        app.py (Flask shell, routes, templates)       |
|---------------------------------------------------------------------------|
| Assistant orchestrator (engine.AssistantAgent)                            |
|   ASSISTANT_TOOLS + PluginToolRegistry.schemas()                          |
|   call_tool()  --> permission gate (off/ask/auto) --> audit (events)      |
|---------------------------------------------------------------------------|
| Plugin subsystem (new)                                                    |
|   plugins.py    registry, manifest validation, grants, PluginToolRegistry |
|   plugin_rt.py  runtime host: kv | llm | http | mail.read | action.propose|
|        ^ host functions only (no ambient authority)                       |
|        +---------------------------------------+                          |
|        |  QuickJS-in-WASM interpreter instance |  one per plugin          |
|        +---------------------------------------+                          |
|---------------------------------------------------------------------------|
| Plugin tiers:  plugins/ (built-in, mt-*, shipped)   $DATA_DIR/plugins/    |
+---------------------------------------------------------------------------+
```

---

## 2. Plugin manifest & interface specification (plugin-sdk v0.1)

### 2.1 On-disk layout & discovery roots

```
plugins/                          # built-ins: versioned in repo, copied into image
  mt-promo-fastpath/
    manifest.json
    dist/plugin.js                # single esbuild bundle
    README.md
$DATA_DIR/plugins/                # user plugins (env PLUGINS_DIR overrides;
  acme-invoice-finder/            #  default ~/.mail-triage/plugins when running
    manifest.json                 #  bare-metal, <repo>/data/plugins in Docker)
    dist/plugin.js
    icon.svg                      # optional; shown in the Plugins page (Phase 4)
```

Rules: plugin `id` is unique across roots; ids starting `mt-` are reserved for
built-ins; a user plugin may not shadow a built-in id. Path resolution for
`entrypoint` must stay inside the plugin directory (traversal guard). Manifests are
capped at 1 MB and parsed as data, never evaluated.

New kernel tables (Phase 1 migration):

```sql
CREATE TABLE IF NOT EXISTS plugins (
  id TEXT PRIMARY KEY,             -- manifest id
  version TEXT NOT NULL,
  root TEXT NOT NULL,              -- 'builtin' | 'user'
  dir TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  grants_json TEXT NOT NULL DEFAULT '[]',
  last_error TEXT,
  installed_ts INTEGER, updated_ts INTEGER);

CREATE TABLE IF NOT EXISTS plugin_kv (
  plugin_id TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL, updated_ts INTEGER,
  PRIMARY KEY (plugin_id, k));
```

`enabled` starts 0 for everything: a plugin exists but is inert until the user
enables it (and re-consents when a version bump adds permissions - see 3.4).

### 2.2 `manifest.json` JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://mail-triage.local/schemas/plugin-manifest.schema.json",
  "title": "Mail Triage plugin manifest",
  "type": "object",
  "additionalProperties": false,
  "required": ["id", "name", "version", "description", "engines", "runtime",
               "entrypoint", "kind"],
  "properties": {
    "id":          { "type": "string", "pattern": "^[a-z0-9][a-z0-9-]{1,40}$",
                     "description": "unique; 'mt-' prefix reserved for built-ins" },
    "name":        { "type": "string", "maxLength": 64 },
    "version":     { "type": "string", "pattern": "^\\d+\\.\\d+\\.\\d+$",
                     "description": "semver; bumping permissions forces re-grant" },
    "description": { "type": "string", "maxLength": 280 },
    "author":      { "type": "string", "maxLength": 120 },
    "homepage":    { "type": "string", "format": "uri" },
    "license":     { "type": "string", "maxLength": 40 },
    "engines":     { "type": "object", "required": ["sdk"], "additionalProperties": false,
                     "properties": { "sdk": { "type": "string",
                       "description": "semver range against the host SDK, e.g. '>=0.1 <1.0'" } } },
    "runtime":     { "enum": ["quickjs-wasm"] },
    "entrypoint":  { "type": "string", "description": "relative path, e.g. 'dist/plugin.js'" },
    "kind":        { "type": "array", "minItems": 1, "uniqueItems": true,
                     "items": { "enum": ["tool", "classifier", "matcher",
                                         "draft-provider", "retriever", "integration"] } },
    "permissions": { "type": "array", "uniqueItems": true,
                     "items": { "enum": ["mailbox.read", "llm.complete", "llm.embed",
                                         "net.http"] },
                     "description": "v1 is read + propose only; no mail mutation or send" },
    "net":         { "type": "object", "additionalProperties": false, "properties": {
                       "hosts": { "type": "array", "items": { "type": "string",
                         "pattern": "^[a-z0-9.*-]+(:\\d+)?$" },
                         "description": "hostname or *.suffix wildcard, optional :port" },
                       "max_requests_per_day": { "type": "integer", "minimum": 0,
                                                 "maximum": 10000, "default": 50 } } },
    "required_llm_capability": { "enum": ["none", "llm.complete", "llm.embed"],
                                 "default": "none",
                                 "description": "install-time warning when a cloud LLM fallback is configured (snippets would leave the box)" },
    "limits":      { "type": "object", "additionalProperties": false, "properties": {
                       "memory_mb":  { "type": "integer", "minimum": 8, "maximum": 256, "default": 64 },
                       "timeout_ms": { "type": "integer", "minimum": 100, "maximum": 30000, "default": 5000 },
                       "kv_bytes":   { "type": "integer", "minimum": 0, "maximum": 1048576, "default": 65536 } } },
    "config":      { "type": "object",
                     "description": "JSON Schema for per-install settings; rendered as a form on the Plugins page (Phase 4)" },
    "tools":       { "type": "array", "items": { "type": "object",
                     "required": ["name", "description", "parameters"],
                     "additionalProperties": false, "properties": {
                       "name":        { "type": "string", "pattern": "^[a-z][a-z0-9_]{1,40}$" },
                       "description": { "type": "string", "maxLength": 400,
                                        "description": "written for the model: when to call it, not how it works" },
                       "parameters":  { "type": "object",
                                        "description": "JSON Schema subset: object/string/number/integer/boolean/array/enum/required; no $ref/$defs/oneOf in v1" },
                       "side_effects":{ "enum": ["none", "local_write", "mailbox_write", "external"],
                                        "default": "none" },
                       "surface":     { "enum": ["assistant", "hidden"], "default": "assistant" },
                       "output_schema": { "type": "object" } } } },
    "assistant":   { "type": "object", "additionalProperties": false, "properties": {
                       "when_to_use": { "type": "string", "maxLength": 280 } } }
  }
}
```

### 2.3 `plugin-sdk.d.ts` (author-facing contract)

```ts
// plugin-sdk.d.ts - Mail Triage plugin SDK v0.1 (draft)
// The SDK is intentionally tiny: everything a plugin can touch is a host function
// listed here. There is no filesystem, no process, no network beyond ctx.http,
// and no way to reach the mailbox except ctx.mail (read) and ctx.action (propose).

export interface MessageRef {
  id: number; folder: string; uid: number; subject: string;
  from: string; date: string; snippet: string;
}

export interface Message extends MessageRef {
  to: string; body_text: string; category: string | null;
  tags: string[]; needs_reply: boolean;
}

export interface ToolCall {
  tool: string;                          // tool name as declared in the manifest
  args: Record<string, unknown>;         // validated against the declared parameters schema
  deadline_ms: number;                   // remaining wall clock for this call
}

export interface ToolResult {
  ok: boolean;
  summary: string;                       // one line for the chat transcript
  data?: unknown;                        // machine-readable payload for the model
  card?: ActionCard;                     // optional rich card in the UI
  error?: { code: "invalid_args" | "denied" | "timeout" | "quota" | "internal";
            message: string };
}

export interface CardAction {
  id: string;
  label: string;                         // e.g. "Apply", "Archive", "Open"
  kind: "apply" | "dismiss" | "link";    // "apply" returns control to the kernel
  url?: string;                          // only for kind: "link"
}

export interface ActionCard {
  title: string;
  markdown?: string;                     // rendered with the app's markdown renderer
  fields?: Array<{ label: string; value: string }>;
  actions: CardAction[];                 // rendered like existing proposal cards
}

export interface PluginKV {
  get<T = unknown>(key: string): Promise<T | undefined>;
  set(key: string, value: unknown): Promise<void>;   // JSON; quota from limits.kv_bytes
  delete(key: string): Promise<void>;
  list(prefix?: string): Promise<string[]>;
}

export interface PluginMail {
  // requires permission: mailbox.read
  search(q: { query?: string; sender?: string; subject?: string;
              folder?: string; limit?: number }): Promise<MessageRef[]>;
  read(id: number): Promise<Message>;
}

export interface PluginLLM {
  // requires permission: llm.complete / llm.embed
  complete(req: { prompt: string; system?: string; max_tokens?: number;
                  json?: boolean }): Promise<{ text: string }>;
  embed(texts: string[]): Promise<number[][]>;
}

export interface HttpResponse {
  status: number; headers: Record<string, string>; body: string;
}

export interface PluginContext {
  readonly plugin: { id: string; version: string };

  log(level: "debug" | "info" | "warn" | "error", message: string): void;

  config: { get(): Record<string, unknown> };   // validated against manifest.config

  kv: PluginKV;
  mail: PluginMail;
  llm: PluginLLM;

  http: {
    // requires permission: net.http, host must match manifest.net.hosts
    fetch(url: string, init?: { method?: "GET" | "POST";
                                headers?: Record<string, string>;
                                body?: string; timeout_ms?: number }): Promise<HttpResponse>;
  };

  action: {
    // Propose a user-visible action. The kernel stores it as a pending action
    // (agent_actions) and renders an Action Card; executing it is a kernel code
    // path the user triggers with a click. Plugins never mutate mail directly.
    propose(card: ActionCard): Promise<{ action_id: string }>;
  };
}

// ---- lifecycle (the entrypoint bundle must export these) ----

/** Called once when the plugin is enabled or after a host restart. */
export function onLoad(ctx: PluginContext): void | Promise<void>;

/** Called for every tool invocation routed to this plugin. */
export function execute(ctx: PluginContext, call: ToolCall):
  ToolResult | Promise<ToolResult>;

/** Called before unload/disable/reload. Best-effort; must not throw. */
export function onUnload(ctx: PluginContext): void | Promise<void>;
```

The `.d.ts` is the *authoring* contract. At runtime the QuickJS bundle talks to the
same surface through the host-function import table (section 4); a build-time
`esbuild` preset + `tsc --noEmit` against this file gives authors (and coding agents)
type safety without shipping a JS framework.

### 2.4 Capability model (how grants map to enforcement)

| Manifest `permissions` | Host API unlocked | Enforcement point | Default when plugin enabled |
|---|---|---|---|
| `mailbox.read` | `ctx.mail.search/read` | Host function refuses without grant; redacts to index fields only | `ask` (first call prompts) ; `auto` after user flips it in the Plugins page |
| `llm.complete` / `llm.embed` | `ctx.llm.*` | Routed through the kernel LLM client (priority/fallback respected); logged to `llm_log` with plugin id | `ask` |
| `net.http` | `ctx.http.fetch` | URL matched against `net.hosts` allowlist; egress counted per day; every call audited | `ask` |
| (implicit, always) | `ctx.kv`, `ctx.config`, `ctx.log` | Quota-enforced store writes; config pre-validated | `auto` (no expansion of reach) |
| (none) | mail mutation, send, filesystem | **does not exist in v1**; reachable only as `ctx.action.propose(...)` -> user click -> kernel executes | - |

Assistant-side, each plugin gets one capability entry `plugin:<id>` in the existing
matrix from `docs/agent-permissions.md` (levels off/ask/auto, audited at the
`AssistantAgent.call_tool` choke point). The manifest's per-tool `side_effects` seeds
the default level: `none` -> auto, `local_write`/`external` -> ask, and anything
`mailbox_write` (reserved for a future tier) -> off/ask with a red warning.

### 2.5 Minimal example (for DX review)

`manifest.json`:

```json
{
  "id": "acme-invoice-finder",
  "name": "Invoice finder",
  "version": "0.1.0",
  "description": "Finds recent invoices and extracts amount + due date.",
  "author": "ACME",
  "license": "MIT",
  "engines": { "sdk": ">=0.1 <1.0" },
  "runtime": "quickjs-wasm",
  "entrypoint": "dist/plugin.js",
  "kind": ["tool"],
  "permissions": ["mailbox.read", "llm.complete"],
  "required_llm_capability": "llm.complete",
  "limits": { "memory_mb": 64, "timeout_ms": 15000 },
  "tools": [{
    "name": "find_invoices",
    "description": "Look for invoices in the mailbox and list amount and due date per invoice. Use when the user asks about bills, invoices or payments.",
    "parameters": {
      "type": "object",
      "properties": {
        "since_days": { "type": "integer", "description": "look back N days", "default": 90 },
        "min_amount": { "type": "number", "description": "only invoices at or above this amount" }
      },
      "required": ["since_days"]
    },
    "side_effects": "none"
  }],
  "assistant": { "when_to_use": "questions about invoices, bills, payment deadlines" }
}
```

`src/plugin.ts` (skeleton, ~30 lines):

```ts
import type { PluginContext, ToolCall, ToolResult } from "mail-triage-plugin-sdk";

export function onLoad(ctx: PluginContext) {
  ctx.log("info", `invoice-finder ${ctx.plugin.version} loaded`);
}

export async function execute(ctx: PluginContext, call: ToolCall): Promise<ToolResult> {
  const { since_days, min_amount } = call.args as { since_days: number; min_amount?: number };
  const hits = await ctx.mail.search({ query: "invoice", limit: 50 });
  const rows: Array<{ subject: string; amount: string; due: string }> = [];
  for (const m of hits.slice(0, 10)) {
    const full = await ctx.mail.read(m.id);
    const out = await ctx.llm.complete({
      system: "Extract amount and due date as JSON: {amount, due}. Use null when absent.",
      prompt: full.subject + "\n\n" + full.body_text.slice(0, 2000),
      json: true, max_tokens: 120,
    });
    try {
      const j = JSON.parse(out.text);
      if (j.amount && (!min_amount || parseFloat(j.amount) >= min_amount)) {
        rows.push({ subject: m.subject, amount: j.amount, due: j.due ?? "-" });
      }
    } catch { /* skip unparseable */ }
  }
  return {
    ok: true,
    summary: `Found ${rows.length} invoice(s) in the last ${since_days} days.`,
    data: { invoices: rows },
    card: { title: "Invoices found", fields: rows.map(r => ({ label: r.subject, value: `${r.amount} due ${r.due}` })), actions: [{ id: "ok", label: "Dismiss", kind: "dismiss" }] },
  };
}
```

---

## 3. Dynamic AI tool discovery pipeline

### 3.1 Discovery, validation, registration

Trigger points: app boot (after `store` init, before the first assistant request),
`POST /plugins/rescan` (Plugins page button / CLI), and an optional debounced
filesystem watch (2 s) on the user plugins dir.

For each candidate directory, in both roots:

1. Read `manifest.json` (<= 1 MB) as data.
2. Validate against the JSON Schema (section 2.2). On failure: skip, record in
   `plugins.last_error`, log an `events` row, never crash the boot.
3. Check `engines.sdk` against the host SDK version range. `entrypoint` resolved and
   confirmed inside the plugin dir (traversal guard); hash the file (sha256).
4. Upsert into `plugins`. New plugin -> `enabled = 0` (inert). Version bump ->
   diff old and new `permissions`; if the set grew, force `enabled = 0` and flag
   "re-grant needed" so the user re-consents. Manifest hash mismatch with a stored
   record for the same version marks the plugin "modified on disk" (shown in UI).
5. If enabled: load into the runtime (lazy by default; `onLoad` runs on first use or
   eagerly when `autoload` is set in future manifest revisions).

### 3.2 Manifest -> function-calling schema synthesis

`PluginToolRegistry.schemas()` yields OpenAI/Ollama-style function schemas from each
enabled plugin's `tools[]`:

```json
{ "type": "function",
  "function": {
    "name": "plugin__acme-invoice-finder__find_invoices",
    "description": "Invoice finder: Look for invoices in the mailbox and list amount and due date ...",
    "parameters": { "...": "the manifest JSON Schema subset, passed through" } } }
```

- Names are namespaced `plugin__<id>__<tool>` (ids contain `-`, mapped to `-` kept as
  is inside the segment; `__` never appears in native tool names, so routing by
  prefix is unambiguous).
- The parameter schema is normalized: only the supported subset survives validation
  (objects, scalars, enums, arrays; `$ref`/`oneOf` rejected at load time, not at call
  time). Defaults from the schema are injected by the kernel before invoke.
- Token budget: the assistant includes at most `plugin_tools_budget` plugin schemas
  per request (settings key, default 8). When more are enabled, selection ranks by
  keyword overlap between the user message and each tool's `description` +
  `assistant.when_to_use`; `surface: "hidden"` tools are callable only by exact name
  (for plugins invoked by other kernel code, never advertised to the model).

### 3.3 Registration into the assistant sidebar

`engine.py:4440` currently reads `use_tools = ASSISTANT_TOOLS if (...) else None`.
Phase 4 changes the assembly to:

```python
use_tools = (ASSISTANT_TOOLS + plugin_registry.schemas(query_text)) if (...) else None
```

and the available-tools self-description (engine.py:3635) gains a plugin section, so
the model knows namespaces exist and can explain them. Everything else - the step
loop, SSE streaming, collapsed tool chips, action cards - is untouched: plugin calls
appear in the transcript exactly like native calls.

### 3.4 Execution routing (model call -> plugin -> card)

```
model emits tool_call: plugin__acme-invoice-finder__find_invoices
  |
  v
AssistantAgent.call_tool(name, args)          # single choke point (existing)
  |-- namespace parse -> capability "plugin:acme-invoice-finder"
  |-- permission matrix (docs/agent-permissions.md levels)
  |     off  -> {"ok": false, "permission_denied": ...} + events row
  |     ask  -> agent_actions row (pending) + SSE action card
  |             user clicks Apply -> POST /agent/actions/<id>/apply
  |             -> kernel re-invokes with the same args (marked confirmed)
  |     auto -> invoke now
  |
  v
PluginRuntime.invoke(plugin_id, tool, args, deadline=limits.timeout_ms)
  |-- args validated against the tool schema
  |-- wasm interpreter started (or reused) ; host functions bound to grants
  |-- ctx.mail/llm/http calls re-checked per call (defence in depth)
  |-- watchdog: soft interrupt at deadline, hard kill at 2x
  |-- events row: plugin, tool, ms, outcome, bytes, egress count
  |
  v
ToolResult -> validated (shape + optional output_schema)
  |-- summary -> chat transcript (collapsed chip like native tools)
  |-- data    -> returned to the model in the next step
  |-- card    -> rendered with the shared .proposal card template (kind tag "plugin")
```

Failure modes and their surfacing:

| Failure | Kernel behavior | User sees |
|---|---|---|
| Timeout | Interpreter killed; result `{ok:false, error:{code:"timeout"}}` | "The invoice finder timed out." + tool chip stays red |
| Crash / invalid output | Same, plus a strike; 3 consecutive strikes -> plugin auto-disabled + `events` row | Plugin card in Settings says "disabled after repeated failures" |
| Permission denied (host fn) | Structured `denied` error naming the capability | Model is told to say which permission is missing and how to grant it |
| Quota exceeded (http/kv) | `quota` error; counters in the row | "Daily request limit reached for this plugin." |
| Schema-invalid args | Kernel rejects before invoke; error back to model | Model corrects and retries (existing behavior for native tools) |

### 3.5 The card path reuses existing UI

`ActionCard` -> the same `.proposal` component introduced for rule/flow proposals
(`✦` kind tag, expandable), with the plugin id as source badge. `kind:"apply"` cards
become `agent_actions` rows exactly like assistant pending actions, so
`/agent/actions/<id>/apply` and the audit trail are shared. No new UI primitives are
required for v1.

---

## 4. Sandboxing & runtime strategy

### 4.1 Threat model

Plugins are code from external agents or power users running against a personal
mailbox. Threats ranked by impact: (1) mail content exfiltration (via network or by
smuggling into LLM prompts), (2) credential theft (IMAP/OAuth/API keys), (3)
resource exhaustion (fork bomb equivalent: infinite loops, allocation bombs), (4)
persistence / tampering (writing outside its own store), (5) supply-chain drift (a
"new version" of an already-trusted plugin changes behavior or widens permissions),
(6) side-channel confusion (a plugin impersonating core UI or another plugin).

### 4.2 Candidate evaluation

| Criterion | QuickJS-in-WASM (Extism-style) | Node worker_threads (+ `--experimental-permission`) | isolated-vm |
|---|---|---|---|
| Isolation boundary | Interpreter inside a Wasm sandbox: no syscalls, no ambient JS intrinsics; only declared host functions exist | Same-process threads; **not** a security boundary by default; Node's permission model + workers gets close but is coarser and newer | One V8 isolate per plugin: strong heap separation, but same process |
| Memory limits | Wasm linear-memory cap per instance (hard) | `resourceLimits.maxOldGenerationSizeMb` (soft-ish; OOM kills the worker) | `memoryLimit` per isolate (hard for the isolate heap) |
| CPU / timeout | Interrupt handler + hard kill; deterministic, no dependence on cooperation | Watchdog + `worker.terminate()` (works, but teardown is heavier) | No built-in timeout primitive; watchdog + `isolate.dispose()` |
| Host API mediation | Excellent: host functions are the *only* capability surface (import table) | Requires discipline; `require` reachability must be locked down | Good: contexts see only what you inject |
| Embedding in a Python host | Excellent: single wheel (e.g. extism-py) or wasmtime-py; no Node in the container | Requires shipping + supervising a Node sidecar in the container | Requires a Node sidecar AND a native addon build |
| DX for authors | No npm runtime; esbuild bundle with shims for the small ctx surface; source-mapped debugging via the dev tier | Full Node DX | Full Node DX-ish, but no dynamic import quirks |
| Ops cost | One Python dependency; per-plugin memory is a few MB | Second runtime in the image; supervision + crash loops | Same as Node plus native build matrix |
| Maturity | QuickJS is mature; Wasm embedding pattern is proven (Extism is exactly this packaged) | Stable, but the permission model is still experimental in spirit | Mature (Screeps and others run it in production) |

### 4.3 Recommendation

**Primary: QuickJS compiled to WebAssembly, embedded in the Python host** (the
Extism pattern: QuickJS + a thin host-function bridge, shipped as a Python wheel; a
direct wasmtime + quickjs.wasm wrapper is the fallback if the wheel's ergonomics
disappoint). Reasons: it is the only option where all three limits (memory, CPU,
capability) are hard and deterministic; it keeps the deployment story "one container,
one process tree" that everything else in this repo preserves; and it makes the
capability story literal - if a host function was not granted, it does not exist
inside the sandbox.

**Dev tier: Node subprocess** (`--plugins dev <id>`), unsandboxed, running the same
bundle with a `ctx` shim that JSON-RPCs to the host. Source-mapped breakpoints in
familiar tooling; refuses to run plugin sets that declare `net.http` beyond
localhost unless `--plugins dev --i-know` is passed. Never used in production paths.

**Rejected for v1: worker_threads as the production sandbox** (not a boundary),
**isolated-vm** (native build + watchdog-only timeouts + a Node sidecar for a Python
host - all cost, and the benefit over Wasm-QuickJS is mostly DX we already get from
the dev tier).

A one-day spike in Phase 2 validates the exact embedding (wheel vs wasmtime-py,
startup latency, interrupt reliability) against this table before code depends on it.

### 4.4 Enforcement mechanisms (as designed into the runtime)

| Dimension | Mechanism |
|---|---|
| Memory | Per-instance Wasm memory cap (`limits.memory_mb`, ceiling 256); allocation failure surfaces as `internal` error, interpreter restarted next call |
| CPU / wall clock | Per-call deadline from `limits.timeout_ms` (ceiling 30 s): soft interrupt at T, hard kill at 2T; per-plugin rolling budget (e.g. 5 min/hour) to stop slow-drip abuse |
| Capabilities | Host-function import table built from grants; ungranted functions are absent (not merely blocked); every host call re-checks the grant server-side |
| Network | Only `ctx.http.fetch`, only to `manifest.net.hosts`, counted against `max_requests_per_day`, each call audited (url, status, bytes) |
| Mailbox | `ctx.mail` returns index fields + body text only; attachments, raw headers and credentials are never reachable; mutation exists only as `ctx.action.propose` |
| LLM | Calls proxied through the kernel client so model/fallback/priority policy and `llm_log` apply; `required_llm_capability` triggers an install-time warning about cloud fallback reaching snippets |
| Storage | `ctx.kv` -> `plugin_kv` rows with `limits.kv_bytes` quota; JSON values only; no filesystem |
| Secrets | The sandbox never sees IMAP/OAuth/API keys; host functions authenticate on the plugin's behalf and log usage |
| Audit | Every load/unload/invoke/host-call -> `events` rows (plugin id, tool, duration, outcome, bytes); visible per plugin in the Plugins page |
| Integrity | Manifest + entrypoint hashes stored; changes to an enabled plugin require explicit re-enable (and re-grant if permissions grew) |

---

## 5. Step-by-step refactoring roadmap

Ground rules for every phase: one feature = one worktree per `AGENTS.md`; suite must
stay green (never weaken a check); Dockerfile `COPY` lines must be added for any new
top-level file (the suite cannot catch a missing COPY); deploy only after merge from
the main checkout.

### Phase 1 - Kernel seams + SDK definition (no behavior change)

Deliverables: the registry skeleton and the frozen contract, nothing executes yet.

- `plugins.py` (new): scan roots, manifest load/validate, `plugins`/`plugin_kv`
  migrations, registry queries, `PluginToolRegistry` stub returning schemas from
  manifests (used by tests only).
- `schemas/plugin-manifest.schema.json` + vendored validator (small pure-python
  validator or `jsonschema` dep decision).
- `sdk/plugin-sdk.d.ts`, `sdk/README.md`, `sdk/build.mjs` (esbuild preset).
- `config.py`: `PLUGINS_DIR` (default `<DATA_DIR>/plugins`), `PLUGIN_SDK_VERSION`.
- CLI: `app.py --plugins list|validate <dir>` (read-only).
- Tests: fixture plugins under `tests/plugins_fixture/` (valid, invalid-manifest,
  bad-sdk-range, traversal attempt); ~15 new checks; suite stays green.
- Exit criteria: boot with 0/2 fixture plugins; validation rejects each malformed
  case with a specific `last_error`; no runtime dependency added yet.
- Rollback: delete the module + migration (tables are additive and harmless).

### Phase 2 - Sandbox runtime

Deliverables: `plugin_rt.py` (runtime ABC + WasmQuickJS adapter + NodeDev adapter +
Mock adapter for the suite), host functions, limits, audit.

- Spike first (one day): wheel vs wasmtime-py embedding decision, per the 4.2 table.
- `PluginRuntime` ABC: `load()`, `invoke(plugin_id, tool, args, deadline)`,
  `unload()`, `health()`. The suite binds the Mock adapter; one optional check runs
  the real adapter when the wheel is importable (skip otherwise, marked explicitly).
- Host functions: `kv.*`, `llm.complete/embed`, `http.fetch`, `mail.search/read`,
  `action.propose` - each enforcing grants and logging.
- Watchdog: interrupt + hard kill; strike counter -> auto-disable.
- CLI: `app.py --plugins check <dir>` (loads, onLoad, dry-run each tool with schema
  stubs) and `--plugins dev <id>`.
- Tests: timeout kill, memory cap, denied host call, kv quota, http allowlist
  miss, audit rows; ~20 new checks.
- Exit criteria: `docs/examples/hello-plugin` round-trips through the real runtime in
  the container; all enforcement rows in 4.4 have at least one test.
- Rollback: runtime flag `plugins_runtime=off` (discovery still works).

### Phase 3 - Dogfood: convert two native features

1. `mt-promo-fastpath` (classifier kind): ports the promo path from
   `heuristics.py` behind a settings flag; validated for prediction parity against
   `heuristics.evaluate_heuristic` on the same dataset before the flag flips.
2. `mt-invoice-finder` (tool kind): the section 2.5 example, real version; exercises
   `mailbox.read` + `llm.complete` + card output end-to-end.

- Both live in the repo `plugins/` root (built-in, `mt-` prefix), shipped in the
  image; the heuristics conversion is reversible by flag.
- Tests: parity checks for the classifier; end-to-end tool invocation for the
  finder (mock LLM); ~15 new checks.
- Exit criteria: with flags on, daily behavior is indistinguishable (same moves,
  same labels) and the finder produces a card in the assistant sidebar.
- Rollback: flags off -> native paths; plugins stay installed but unused.

### Phase 4 - Dynamic tool discovery for the assistant

- Merge registry schemas at engine.py:4440 with the token budget (3.2); extend the
  available-tools description (3.3).
- Capability entries `plugin:<id>` seeded from manifest permissions (3.4); ask/auto
  flows through `agent_actions`.
- Plugins page under Settings: list, enable/disable, grants, per-plugin audit tail,
  rescan button, config form rendered from `manifest.config` (Phase 4 stretch).
- `POST /plugins/rescan` + docs: `docs/plugins-authoring.md` (DX guide + the
  checker + dev tier), update `docs/README.md`, README quick start.
- Tests: assistant calls a fixture plugin tool end-to-end (mock LLM emits the tool
  call); ask-level pending card apply path; budget cap honored (N+1 tools enables
  keyword pruning); denied-capability message; ~20 new checks.
- Exit criteria: install a third-party example plugin by dropping a folder, rescan,
  ask the assistant, get a card - with the audit trail showing the full chain.
- Rollback: `plugin_tools_budget=0` removes plugin tools from the model while
  plugins remain manageable.

---

## 6. Risks & open questions

| # | Risk / question | Recommendation |
|---|---|---|
| 1 | Plugin language: TS/JS vs Python | TS/JS as specified. The sandbox story is decisively better for JS (Wasm-QuickJS) and the DX target (external coding agents) is JS-native. Revisit a "trusted native Python plugin" tier only if a real need appears. |
| 2 | v1 reach: read-only + propose | Yes. Mutation-by-plugin multiplies the blast radius for little dogfood value; `action.propose` keeps actions user-visible and kernel-executed. |
| 3 | What happens when the assistant's LLM fallback is a cloud model? | Install-time warning driven by `required_llm_capability`; the grants UI states plainly that snippet data may leave the box when cloud fallback is active. |
| 4 | Tool-schema token cost with many plugins | Budget + keyword pruning (3.2); expose the count in the Plugins page so the user can see the cost paid. |
| 5 | Supply-chain drift on version bumps | Permission-diff forces re-grant; entrypoint hash change on the same version flags "modified"; both surfaced in the Plugins page. |
| 6 | Sandbox-vs-native performance for classifiers | The promo classifier is fast and high-volume: measure first (Phase 3 parity run), keep the native path available behind the flag if the sandbox adds unacceptable latency. |
| 7 | Naming collision with flows/rules terminology | Keep "plugin" strictly for the SDK tier; flows/rules remain product concepts. |
| 8 | Signing third-party plugins | Integrity hashes + re-grant for v1; signing (minisign/sigstore) deferred until there is a distribution channel worth attacking. |

---

## Appendix A - Module inventory (2026-10-02)

| File | LOC | Key symbols |
|---|---|---|
| `app.py` | 7,468 | 95 routes; `BASE_TMPL`, page templates (`DASH_TMPL`, `MESSAGES_TMPL`, `SIMULATE_TMPL`, ...) |
| `engine.py` | 4,803 | `MailClient` (436), `LLMClient` (1051), `Worker` (1308), `_apply_flow` (915), `simulate_email` (1710), `generate_draft` (2351), `ASSISTANT_TOOLS` (2631), `AssistantAgent` (3587), `_tool_*` handlers (3790-4222) |
| `store.py` | 1,919 | 28 tables (settings, rules, flows, messages, events, observations, agent_actions, specialists, ...) |
| `learning.py` | 1,373 | `register_kind`, logreg trainers, `specialists`, eval |
| `proxy.py` | 991 | email-oauth2-proxy accounts/config/ports (OAuth2 bridge) |
| `rag.py` / `rag_lite.py` | 683 / 465 | `search()` backends; indexing passes |
| `heuristics.py` | 464 | `train_naive_bayes`, `train_decision_list`, `auto_refine`, `register_kind` |
| `config.py` | 45 | env config incl. `DATA_DIR` |

## Appendix B - Glossary

- **Kernel** - native code that owns data, credentials, scheduling and permissions.
- **Plugin** - versioned, sandboxed bundle + manifest; runs only via the runtime host.
- **Host function** - a capability exported into the sandbox (the only way out).
- **Grant** - user consent for a declared permission, stored in `plugins.grants_json`.
- **Kind** - what a plugin contributes: tool, classifier, matcher, draft-provider,
  retriever, integration.
- **Capability entry** - `plugin:<id>` in the assistant permission matrix, level
  off/ask/auto.

## Appendix C - References

- `docs/agent-permissions.md` (this repo) - the permission matrix extended here.
- Extism (QuickJS-in-Wasm packaging + host SDKs) - pattern reference for 4.3.
- QuickJS - interpreter with memory/time limits and interrupt support.
- Node.js permission model (`--experimental-permission`) - the dev-tier guard.
- isolated-vm - V8-isolate plugin isolation (evaluated, not chosen).
- OpenAI/Ollama function-calling schema - the synthesis target for tool schemas.

*Verify library versions and APIs at Phase 2 spike time; this document fixes the
contract, not the vendors.*

---

# Implementation notes (as built, 2026-10-02)

All four phases are implemented; where reality diverged from the plan above, this
section is the record of what shipped.

## Runtime: supervised worker process, not in-process Wasm

The python-quickjs binding (1.19.4, the pragmatic embed) refuses host callbacks
while its time-limit watchdog is active ("Can not call into Python with a time
limit set", verified 2026-10-02). Instead of giving up either host functions or
timeouts, the runtime moved to **one persistent worker process per plugin**
(`plugin_worker.py`), driven over newline-delimited JSON:

- the interpreter enforces MEMORY (`set_memory_limit`, hard),
- the parent enforces WALL CLOCK by killing the worker at the deadline,
- the parent runs EVERY host call (grants, quotas, audit) - the worker has no
  I/O of its own,
- crashes are contained by the process boundary; success resets the strike
  counter, three consecutive failures auto-disable the plugin.

This is a strictly harder enforcement story than the in-process variant, at the
cost of ~1 MB/worker and a lazy spawn (~0.3 s) per plugin. The `PluginRuntime`
class keeps the swap to a Wasm-packaged QuickJS adapter behind the same
interface if we ever want it.

## SDK v0.1 is synchronous

`execute`/`classify`/`onLoad` must be synchronous (the bridge is blocking
JSON-RPC); async throws a clear error. TypeScript authors bundle with esbuild to
a single IIFE assigning `globalThis.__mt_plugin`.

## Classifier kind (Phase 3 as built)

`heuristics.classify()` falls back to opted-in classifier plugins after native
heuristics abstain. The host feeds `{kind, model, feats}` - a native heuristic's
model, usually parked disabled - and the plugin returns label/confidence. The
shipped `mt-promo-fastpath` mirrors `decision_list` and `naive_bayes` predictions
bit-exactly (suite checks float equality to 1e-9). Opt in via the
`plugin_classifiers` setting: `[{"plugin": "mt-promo-fastpath", "heuristic_id": N}]`.

## Registry / CLI / settings as built

- Tables: `plugins`, `plugin_kv`. New settings: `plugins_enabled`,
  `plugin_tools_budget` (default 8), `plugin_classifiers` (opt-in list),
  `perm_plugin:<id>` (assistant gate per plugin: off/ask/auto).
- Assistant reach: plugin tools appear as `plugin__<id>__<tool>` in the tool
  inventory (token-budgeted, keyword-ranked), execute through
  `AssistantAgent.call_tool` under a `plugin:<id>` capability entry, and can
  return Action Cards (rendered via the existing pending-action surface).
- CLI: `python app.py --plugins list | validate <dir> | rescan |
  enable|disable <id> | grant <id> <perm...> | invoke <id> <tool> [json]`.
- UI: `/plugins` page (enable, grants, assistant level, rescan) linked from the
  sidebar (System group), the More page and Settings; the agent permission prompt
  text includes plugin capabilities.

## Files

`plugins.py` (kernel: scan/validate/registry/schemas/CLI) ·
`plugin_rt.py` (supervisor + host calls) · `plugin_worker.py` (sandbox worker) ·
`schemas/plugin-manifest.schema.json` · `sdk/` (d.ts, runtime.js, README) ·
`plugins/mt-promo-fastpath`, `plugins/mt-invoice-finder` (built-ins) ·
tests: suite sections T43-T46 + `tests/plugins_fixture/`.

## Deferred (explicit)

Node dev-tier adapter (`--plugins dev`), plugin code signing, per-plugin config
forms on the Plugins page (values are honored from `plugin_config:<id>` already),
Wasm adapter swap, retire-the-native-heuristic automation for mirrored fast-paths.
