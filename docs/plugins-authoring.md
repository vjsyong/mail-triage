# Writing a plugin

Mail Triage plugins are small, sandboxed extensions: a `manifest.json` plus one
JS bundle. They run in a worker process with no filesystem, no sockets and no
secrets - every way out is a host function the kernel mediates and audits. Start
from `sdk/plugin-sdk.d.ts` (the full surface) and `sdk/runtime.js` (the bridge).

## Layout

```
my-plugin/
  manifest.json          # contract: id, version, kinds, permissions, tools
  dist/plugin.js         # the bundle; must assign globalThis.__mt_plugin
  icon.svg               # optional
```

Install: drop the folder into `<DATA_DIR>/plugins/` (user plugins; the built-ins
live in the app's `plugins/` dir), then rescan:

```
python app.py --plugins validate path/to/my-plugin     # manifest + entry check
python app.py --plugins rescan
python app.py --plugins enable my-plugin
python app.py --plugins list
```

Or use the Plugins page (sidebar → Plugins, or Settings → Plugins): enable, tick
the capability boxes you consent to, set the assistant permission, rescan.

## The bundle

```js
globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "hello from " + ctx.plugin.id); },
  execute: function (ctx, call) {          // one per tool invocation
    return { ok: true, summary: "done", data: { echo: call.args } };
  },
  // schedule kind: kernel runs this on the manifest's cadence while enabled
  onSchedule: function (ctx, input) {       // {every_minutes, last_run, now, run_tool}
    ctx.action.propose({ title: "Scheduled report", markdown: "…",
                         actions: [{ id: "ok", label: "Dismiss", kind: "dismiss" }] });
  },
  // classifier kind only:
  classify: function (ctx, input) {         // {kind, model, feats} -> {label, confidence}
    return { label: "", confidence: 0 };    // empty label = abstain
  },
  onUnload: function (ctx) {}
};
```

Rules that bite:

- **Synchronous only** (SDK v0.1). An `async` function throws "must be
  synchronous" at call time; the bridge is blocking JSON-RPC.
- TypeScript: compile with
  `esbuild src/plugin.ts --bundle --format=iife --global-name=__mt_plugin --outfile=dist/plugin.js`
  and type-check against `sdk/plugin-sdk.d.ts`.
- Tool arguments are validated against your declared `parameters` schema before
  the call; return `{ok:false, error:{code:"invalid_args", ...}}` for bad values
  the schema cannot express.
- Answers are seen by the model and by the chat: keep `summary` one line,
  `data` small (64 KB cap), and put rich output in a `card`.
- A tool that times out or crashes counts as a strike; three in a row disable
  the plugin until you re-enable it.

## The context (ctx)

`ctx.plugin` (id/version) · `ctx.log` · `ctx.config.get()` · `ctx.kv`
(namespaced JSON store, `limits.kv_bytes`) · `ctx.mail.search/read`
(permission `mailbox.read`; index-only, read-only - v1 plugins cannot mutate
mail) · `ctx.llm.complete/embed` (permissions `llm.complete`/`llm.embed`; routed
through the kernel's configured endpoints, logged) · `ctx.http.fetch`
(permission `net.http`; host must match `manifest.net.hosts`, daily cap) ·
`ctx.action.propose(card)` (a pending Action Card the user clicks; executing it
is kernel code).

Mail mutation does not exist in v1 by design. If your plugin wants something to
happen, propose a card; never claim an action happened.

## Permissions & consent

`manifest.permissions` declares reach; the registry starts every plugin
**disabled**. Enabling consents to the declared set; the user can revoke
individual grants on the Plugins page and the host function then simply refuses
(`denied`). A version bump that adds a permission forces a re-grant. Every
invoke and host call lands in the event log.

Privacy note: if the assistant LLM fallback is a cloud endpoint, plugin LLM
calls follow the same routing. If your plugin sends snippets, say so in the
manifest (`required_llm_capability` drives the install-time warning).

## Classifier kind

A `classifier` plugin can stand in for a native fast-path heuristic. Opt in via
Settings (`plugin_classifiers`):

```json
[{"plugin": "my-classifier", "heuristic_id": 12}]
```

When native heuristics abstain, the host loads heuristic #12's `kind` + `model`,
featurizes the message (`{tokens, from, domain, subject, body}`) and calls your
`classify()`. Keep it fast - it runs on the classification path - and return
`{label: "", confidence: 0}` to abstain. `plugins/mt-promo-fastpath` is the
reference implementation (bit-exact ports of `decision_list`/`naive_bayes`
predictions from `heuristics.py`).

## Scheduled runs

A tool plugin can ask the kernel to call `onSchedule(ctx, input)` on a cadence:

```json
"schedule": { "every_minutes": 1440, "run_tool": "my_tool", "enabled": true }
```

`every_minutes` is 15–10080. A plugin with a `schedule` must be `kind: ["tool"]`
with at least one tool; `run_tool` (optional) must name one of them and is passed
to `onSchedule` as a hint. Runs happen on a background thread — never the mail
pipeline — and `last_run` is recorded before the call, so a crash cannot
hot-loop; three consecutive failures still auto-disable the plugin. Set the
`plugin_schedules_enabled` setting to 0 to pause every scheduled run.

Deliver results by calling `ctx.action.propose(card)`: the user gets a pending
card on the assistant page. Returning a `ToolResult` is fine too (it is logged
and shown in the plugin's activity), but it does not notify anyone. Keep within
`limits.timeout_ms`; LLM calls count against it. See `mt-commitments` and
`mt-subscription-watch` for working examples.

## Testing your plugin

- `python app.py --plugins validate <dir>` - schema + semantic checks (reserved
  ids, traversal, permission consistency, tool-schema subset).
- `python app.py --plugins invoke <id> <tool> '{"json":"args"}'` - run one tool
  through the real sandbox (grants and limits apply).
- The mock suite (`tests/mock_e2e.py` sections T43-T46) shows how fixtures drive
  the kernel, runtime, and assistant integration; copy `tests/plugins_fixture/good-runtime`
  as a starting harness.

## Versioning

`engines.sdk` must admit the host SDK (`python app.py --plugins list` prints it).
Bump `version` on every change you ship; same-version edits are flagged
"modified on disk" and permission-growing bumps force re-consent.

## Browser pages (optional)

A plugin can ship a page in the sidebar. There are two modes; see
`docs/plugin-pages.md` for the full contract:

- **`composed`** (default, sandboxed): the bundle exports synchronous
  `uiOpen`/`uiDispatch`/`uiClose` and returns a data-only component tree built
  with the `MTUIB` helpers from `sdk/compose.js`. The host validates and renders
  it; no plugin browser JS runs, and controller execution gets a read-only host
  capability subset (mail read/search, config, kv read, log).
- **`trusted`**: a browser bundle in an isolated iframe. It only runs after the
  user explicitly approves it on the plugin page, and the warning states that
  such a view can transmit any data it receives by navigating itself (not
  constrained by `net.http`).

```json
"ui": {
  "mode": "composed",
  "pages": [{ "id": "desk", "title": "My page" }],
  "navigation": [{ "page": "desk", "label": "My page", "group": "system", "icon": "puzzle" }]
}
```

`plugins/mt-mail-desk` is the composed reference; `sdk/compose.js` is the
component builder.
