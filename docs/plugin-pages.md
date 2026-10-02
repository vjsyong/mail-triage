# Plugin pages (browser UI) - two tiers

Status: implemented 2026-10-02 (v1: browser-bundle pages; this revision adds a
sandboxed composed default and explicit approval for trusted views). A plugin
may declare an optional `ui` block. There are two modes, and they are not equal:

| Mode | Default? | Plugin browser JS | Reach | Gate |
|---|---|---|---|---|
| `composed` | yes | **none** | validated component tree rendered by the host | none beyond enable + grants |
| `trusted` | no | yes, in an opaque-origin iframe | read-only backend ops through the host bridge | **explicit per-content user approval** |

The composed tier is the safe default: a malicious backend cannot inject markup
or a network sink, because the host parses a whitelisted tree and renders it
itself. The trusted tier exists for richer views but must be approved by the
user, and its residual egress is disclosed rather than denied.

## Manifest

```json
"ui": {
  "mode": "composed",
  "pages": [{ "id": "desk", "title": "Mail Desk" }],
  "navigation": [{ "page": "desk", "label": "Mail Desk", "group": "mail", "icon": "mail", "order": 5 }]
}
```

Trusted mode adds `entrypoint` (a browser bundle) and `operations` (the read-only
tool allowlist):

```json
"ui": {
  "mode": "trusted",
  "entrypoint": "ui/page.js",
  "pages": [{ "id": "main", "title": "My view" }],
  "operations": ["my_read_op"]
}
```

Validation (`plugins.py`, `schemas/plugin-manifest.schema.json`) rejects:
composed with an `entrypoint`/`operations`, trusted without an `entrypoint`,
duplicate page ids, navigation to an undeclared page, unknown group/icon,
`operations` naming an undeclared or non-read-only tool, and a traversal or
`</script`/`<!--`-bearing trusted bundle. Existing plugins without a `ui` block
are unaffected; every plugin still installs disabled.

## Composed tier

The sandbox bundle exports synchronous `uiOpen` / `uiDispatch` / `uiClose`
(QuickJS, same worker/limits as everything else). Build the tree with the
data-only `MTUIB` helpers from `sdk/compose.js`:

```js
uiOpen: function (ctx, input) {
  var state = { q: input.state.q || "", selected: 0 };
  return { tree: build(ctx, state), state: state };
},
uiDispatch: function (ctx, input) {
  var state = input.state, ev = input.event;
  if (ev.kind === "search") state.q = String(ev.value || "").slice(0, 200);
  if (ev.kind === "select") state.selected = parseInt(ev.value, 10) || 0;
  return { tree: build(ctx, state), state: state, url: { q: state.q, message: String(state.selected) } };
},
uiClose: function (ctx, input) {}
```

Host guarantees:

- **No plugin browser JS.** The route renders host HTML only; the plugin bundle
  is never embedded in a page.
- **Strict server-side validation** (`plugin_ui.validate_tree`): whitelisted
  component types (Stack/Grid/SplitPane/Tabs/Tab/Text/Badge/Separator/Button/
  Input/Menu/MenuItem/Dialog/List/ListItem/StateView/MessageList/MessageReader/
  SearchField), whitelisted props/enums, no `rawHTML`/`style`/`href`/`src`/
  `srcdoc`/handler functions/unknown node keys/prototype keys; bounds on node
  count, depth, children, text and state bytes.
- **Independent host renderer** (`plugin_ui.render_tree`) escapes all text and
  emits no URLs, scripts, iframes or `on*` attributes.
- **Bounded state + revision.** The host owns the controller state, the event
  ids present in the current tree and a monotonic revision. Dispatch must name
  an event that exists in the rendered tree; list events accept only the item ids
  that were rendered; a mismatched revision is rejected (409) so stale responses
  cannot overwrite newer state. Per-view serialization + small per-plugin/global
  concurrency + rate limits bound the work.
- **URL state** is host-composed from a bounded `{q, message}` hint and written
  with `history.pushState(Object.assign({}, history.state, {mtExt:1}), ...)`
  (Turbo metadata preserved); deep links seed `uiOpen`'s initial state.
- **Read-only host capability subset.** Controller execution may use
  `ctx.plugin`, `ctx.log`, `ctx.config.get`, `ctx.kv.get/list`, `ctx.mail.read/
  search`; `ctx.http.fetch`, `ctx.llm.*`, `ctx.action.propose` and `ctx.kv.set/
  delete` are denied (`plugin_rt.UI_HOST_ALLOW`).
- A bounded status poll clears the tree and shows a recovery message when the
  plugin is disabled, changed or unapproved (server-side expiry/revoke).

## Trusted tier

The browser bundle runs in `<iframe sandbox="allow-scripts">` (no
`allow-same-origin`), via srcdoc with a nonce CSP (`connect-src 'none'`,
`frame-src/worker-src/form-action 'none'`), and talks to the host over
`postMessage`; the host page holds a server-minted view session and mediates
every call through `POST /extensions/<id>/<page>/rpc` (same-origin + session +
`X-MT-Op` + the `operations` allowlist) into the normal sandbox `invoke()` path.

**Disclosure, not denial.** An isolated frame can still navigate itself
(`location.href`, meta refresh), so a bundle that receives mail text can send it
anywhere. The CSP blocks `fetch`/images/forms/workers, but self-navigation is an
inherent browser capability and is **not** constrained by the plugin's
`net.http` grant. Therefore:

- The view does not run until the user approves it on the plugin detail page
  (**Approve browser view**), through an unambiguous warning.
- Approval is stored in settings and bound to the exact manifest + backend +
  frontend content digests and version; editing or updating the plugin
  invalidates it and requires fresh consent. **Revoke approval** removes it.
- The host page polls a bounded status endpoint; on revoke/disable/tamper it
  clears the tree and tears the frame down, and the host disposes the bridge
  (and ignores late results) on any unexpected frame navigation.
- Host responses for `/extensions/*` carry `X-Frame-Options: DENY` and
  `Content-Security-Policy: frame-ancestors 'none'`.

The trusted fixture/sample is inert (public SDK, no network) so approval can be
tested safely. Enable, grants, built-in origin, CLI enable and manifest
declaration never auto-approve a trusted view.

## The demo: `plugins/mt-mail-desk` (composed)

Read-only Mail Desk, exercising only public contracts:

- Two declared hidden/read-only tools plus a composed controller; `ctx.mail`
  read-only under `mailbox.read`.
- Search (capped), select, plain-text reader with headers/category/tags/
  needs-reply, Refresh, loading/empty/denied/error states.
- Desktop split list/reader; <=767px a list that pushes to the reader with a
  host-controlled layout (`data-mt-layout`, sent over the bridge).
- `?q=` / `?message=` deep links survive reload/back/forward.
- **Index-only and honest:** the reader shows the locally indexed body text only;
  attachments and rich mail HTML are never read, no LLM is called, nothing is
  moved or sent.

## Install locally

```
cp -r plugins/mt-mail-desk ~/.mail-triage/plugins/
python app.py --plugins validate ~/.mail-triage/plugins/mt-mail-desk
python app.py --plugins rescan
python app.py --plugins enable mt-mail-desk
```

See also `docs/plugins-authoring.md` (sandbox contract) and `sdk/README.md`.
