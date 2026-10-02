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
