# Plugin pages (browser UI) - design + authoring

Status: implemented 2026-10-02. A plugin may now ship one browser page bundle
alongside its sandbox bundle. The host renders it at `/extensions/<id>/<page>`
and mediates every backend call. This document is the trust-boundary record and
the authoring guide; `docs/plugins-authoring.md` covers the sandbox side.

## What a plugin declares

`manifest.json` gains an optional `ui` object (validated by
`schemas/plugin-manifest.schema.json` plus semantic checks in `plugins.py`):

```json
"ui": {
  "entrypoint": "ui/page.js",          // browser bundle, inside the plugin dir
  "pages": [
    { "id": "desk", "title": "Mail Desk", "description": "Search and read" }
  ],
  "navigation": [
    { "page": "desk", "label": "Mail Desk",
      "group": "mail", "icon": "mail", "order": 5 }
  ],
  "operations": ["search_messages", "read_message"]
}
```

Rules enforced at discovery/validate time (re-checked at serve time):

- `entrypoint` must be relative, resolve inside the plugin directory (no
  traversal or symlink escape), end in `.js`, and must not contain a literal
  `</script` sequence or an HTML comment opener (`<!--`) - the host embeds it
  inline in a frame document.
- `pages[].id` are lowercase namespaced ids, unique within the plugin; the host
  route is `/extensions/<plugin-id>/<page-id>`.
- `navigation[].page` must reference a declared page; `group` and `icon` come
  from small host-owned enumerations (mail/automation/system;
  mail/inbox/search/list/tag/star/clock/filter/file/puzzle/sparkles/settings) so
  a manifest can never inject markup. The host controls grouping and ordering.
- `operations` is the explicit allowlist of backend tools the page may call.
  Each must name a declared tool and that tool must be read-only
  (`side_effects: "none"`). A tool may be `surface: "hidden"` - hidden means
  "not advertised to the assistant", not "unreachable"; being listed in
  `operations` is the only thing that makes it callable from a page.

Existing plugins without a `ui` block are unaffected, and every plugin still
installs **disabled** and inert until the user enables it.

## Trust boundary

The page bundle is untrusted code. Three layers isolate it:

1. **Opaque-origin frame.** The host renders the bundle in
   `<iframe sandbox="allow-scripts" srcdoc="...">`. No `allow-same-origin`,
   `allow-forms`, `allow-popups` or `allow-top-navigation`, so the frame cannot
   read the host DOM, cookies, storage, or navigate the top window. The srcdoc
   document carries its own nonce CSP:
   `default-src 'none'; script-src 'nonce-…'; style-src 'nonce-…'; img-src data:;
   connect-src 'none'; frame-src 'none'; worker-src 'none'; form-action 'none';
   base-uri 'none'; frame-ancestors 'self'`. Network, images, forms, nested
   frames and workers are all denied; only the host's own nonce'd scripts run.
2. **Host bridge.** Only the host page (same-origin, trusted) talks to the
   server. The frame talks to it with `postMessage`; the host ignores any
   message whose `event.source` is not the exact frame window and whose `sid`
   does not match the view. The host page holds a per-view session token minted
   server-side (`plugin_rt.ui_session_open`); the iframe never sees the plugin
   id as authority - the server binds the session to the plugin id + page it
   rendered and never trusts a pid from the frame.
3. **Server mediation.** Backend calls go over
   `POST /extensions/<id>/<page>/rpc` with `X-MT-Session` and `X-MT-Op`. The
   endpoint requires a same-origin `Origin`/`Referer`/`Sec-Fetch-Site` (a custom
   header cannot be sent cross-site without a disallowed preflight, and the
   opaque-origin frame cannot produce a matching Origin). The server then
   re-checks: plugin still enabled, page declared, session live and not expired,
   and the operation is in `ui.operations`. Finally the call runs through the
   normal sandbox `invoke()` path, so argument validation, grants, limits,
   strikes and the event audit all apply exactly as for an assistant call.

The browser SDK running inside the frame is convenience only - the server checks
do not depend on it. A malicious page can post arbitrary messages, but it can
only ever reach its own declared, read-only operations, and only while its
session is live.

## Page SDK (`sdk/ui.js`)

The host injects `sdk/ui.js` before the plugin bundle and calls `MTUI.start()`,
which invokes the renderer the bundle registered:

```js
globalThis.__mt_ui = {
  pages: {
    desk: {
      render: function (root, api) {
        // api.call(op, args) -> Promise<{ok, data, error, summary}>
        // api.updateUrl({q, message}, replace)   // host-mediated, bounded
        // api.on('theme'|'state'|'dispose', fn)
        // api.components: searchField, splitPane, messageList,
        //                 plainTextReader, stateView, el
      }
    }
  }
};
```

- `api.call` resolves `{ok:true, data, summary}` or rejects with an `Error`
  carrying a stable `.code` (`invalid_args`, `denied`, `forbidden`, `timeout`,
  `quota`, `disabled`, `not_found`, `internal`).
- Components render only; fetching stays in the page. All components build DOM
  with `textContent` (never `innerHTML`), so mail text cannot become markup.
- Theme tokens arrive through the bridge (`theme` message) from the host's
  computed styles, not by reading host DOM - `MTUI` applies them as CSS
  variables inside the frame.
- Layout mode is host-controlled too (`layout` message -> the frame root's
  `data-mt-layout`): the frame is often much narrower than the device, so the
  page cannot decide desktop-vs-mobile from its own media queries. The host
  re-sends it on resize.
- The page URL carries only bounded `q` and `message` state. The host validates
  and writes it with `history.pushState`/`replaceState`; `popstate` pushes the
  new state back into the page, so deep links, reload, back and forward work.
  Turbo navigations that unmount the page tear the session down.

## Lifecycle

```
GET /extensions/<id>/<page>     host mints a session, renders shell + frame
  frame loads  -> postMessage {kind:"ready"}
  host         -> postMessage {kind:"theme", theme, config}
  frame        -> {kind:"call", id, op, args}
  host         -> POST …/rpc  (session + Origin + op header)
  server       -> invoke_ui -> sandbox invoke (grants/limits/audit)
  host         -> postMessage {kind:"result", id, ok, data|error}
  frame        -> {kind:"nav", q, message}        host updates the URL
unmount / Turbo / disable / revoke
  host -> {kind:"dispose"}; aborts in-flight fetches; POST …/dispose
  server drops the session; late results are ignored
```

- Sessions expire after 30 idle minutes and are capped (oldest evicted) so the
  store stays finite.
- Disabling a plugin, changing its grants, or auto-disabling it after repeated
  failures drops all of its sessions immediately (`plugins.set_enabled`,
  `set_grants`, `disable_with_error`).
- If a page bundle is missing or unsafe to embed, the route redirects back to
  the plugin detail page with a flash; the host never serves arbitrary files.

## The demo: `plugins/mt-mail-desk`

`mt-mail-desk` is the reference UI plugin and a normal user of the public
contracts - it declares no extra keys and gets no host privileges beyond
`mailbox.read`.

- Two hidden, read-only tools: `search_messages` (capped index search) and
  `read_message` (headers/category/tags/needs-reply plus plain text), both
  backed by `ctx.mail.search`/`ctx.mail.read`.
- One page (`desk`) in the Mail navigation group. Desktop shows a split pane
  (list + reader); at <=767px it becomes a list that pushes to a reader with a
  back control. Keyboard focus is preserved.
- Search field, explicit Refresh, selection, and loading/empty/denied/error
  states. `?q=` and `?message=` deep links survive reload/back/forward.
- **Index-only, honest:** the reader shows the locally indexed text (the same
  `snippet` body the sandbox `ctx.mail.read` exposes). Attachments and rich mail
  HTML are never read, no LLM is called, and nothing is moved, sent or deleted.

## Install locally

Built-ins load from the repo `plugins/` directory. To try a user copy:

```
cp -r plugins/mt-mail-desk ~/.mail-triage/plugins/   # or <DATA_DIR>/plugins
python app.py --plugins validate ~/.mail-triage/plugins/mt-mail-desk
python app.py --plugins rescan
python app.py --plugins enable mt-mail-desk
```

Then open `/plugins/mt-mail-desk` (or the Plugins page), enable it, and use the
**Mail Desk** entry in the sidebar. Disable it to remove the entry and drop the
session. The plugin never touches real mail beyond the read-only index.
