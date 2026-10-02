# Mail Triage plugin SDK v0.1

Write a plugin as **one JS file** (bundle) plus a `manifest.json`. The host
interprets the bundle inside a sandboxed QuickJS context and calls into it.

## Shape of a plugin

```
my-plugin/
  manifest.json      # see schemas/plugin-manifest.schema.json
  dist/plugin.js     # the bundle (entrypoint)
```

The bundle must assign `globalThis.__mt_plugin`:

```js
globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "hello from " + ctx.plugin.id); },
  execute: function (ctx, call) {
    return { ok: true, summary: "pong", data: { tool: call.tool, args: call.args } };
  },
  onUnload: function (ctx) {}
};
```

TypeScript authors: compile with esbuild
`esbuild src/plugin.ts --bundle --format=iife --global-name=__mt_plugin --outfile=dist/plugin.js`
and type-check against `plugin-sdk.d.ts` (`tsc --noEmit`).

## Rules the sandbox enforces

- **Synchronous only** (SDK v0.1): no async/await in `onLoad`/`execute`.
- No filesystem, no sockets, no secrets. The only way out is the `ctx` API.
- `ctx.mail` is read-only. To change anything, return an `ActionCard` or call
  `ctx.action.propose` - the user clicks, the kernel executes.
- Limits come from the manifest (`limits.memory_mb`, `limits.timeout_ms`).
  Blowing them throws a JS exception you can catch; the call is then failed.
- `ctx.http.fetch` only reaches hosts listed in `manifest.net.hosts`.

## Install & check

Drop the folder into `~/.mail-triage/plugins/` (or `data/plugins/` in Docker),
then run `python app.py --plugins rescan` and enable it:

```
python app.py --plugins list
python app.py --plugins validate ~/.mail-triage/plugins/my-plugin
python app.py --plugins enable my-plugin
```

Full design: `docs/plugin-architecture.md`.

## Classifier kind

A plugin with `"kind": ["classifier"]` can stand in for a native heuristic
(fast-path). Opt in via the `plugin_classifiers` setting:

```json
[{"plugin": "mt-promo-fastpath", "heuristic_id": 12}]
```

When the native heuristics abstain, the host loads heuristic #12's `kind` +
`model`, featurizes the message, and calls your `classify()` in the sandbox.
The mirrored heuristic is normally parked disabled (enabled=0) - the plugin
replaces its predictions. Native heuristics always keep the first word.

## The other kinds (one built-in plugin each)

- **matcher** (`mt-cjk-matcher`): export `match(ctx, {fields, text})`. Rules and
  flows reference it as `{"field":"subject","op":"plugin","plugin":"mt-cjk-matcher"}`
  (the rule builder has a `plugin` operator). Opt in under "Pipeline use".
- **draft-provider** (`mt-mirror-language`): export `draft(ctx, {subject, from, snippet, instructions})`
  -> `{text}`. Flow draft steps use mode "Draft via plugin" in the builder, or
  `{"type":"draft","mode":"plugin","plugin":"<id>"}` in a proposal.
- **retriever** (`mt-priority-first`): export `rank(ctx, {query, candidates})` ->
  `{ids}`. Opt in under "Pipeline use"; it re-orders the assistant's semantic search.
- **integration** (`mt-webhook-notify`): export `onEvent(ctx, {type, payload, ts})`.
  Fires on `mail.filed` (rule/flow moved a message) and `mail.classified`.
  Webhooks need `"net": {"allow_config_hosts": true}` in the manifest and the
  host listed in the config's `allowed_hosts` (the endpoint you typed is the consent).
- **tool** (`mt-daily-digest`, `mt-invoice-finder`): plain assistant tools.

## Config

A manifest `config` schema renders a settings form on the Plugins page (flat
strings / numbers / booleans / string-arrays). Plugins read the values via
`ctx.config.get().values` (or the CLI: `python app.py --plugins config <id> '[json]'`).
