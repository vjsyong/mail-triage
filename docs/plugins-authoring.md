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
