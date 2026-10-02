# Mail Desk (mt-mail-desk)

A focused, **read-only** desk over the local mail index, shipped as the reference
browser-UI plugin. It exercises only the public plugin contracts: a sandboxed
`execute` bundle plus one browser page, with no host privileges beyond
`mailbox.read`.

## What it does

- **Search** the local index (capped at 50 results) by sender, subject or
  snippet.
- **Select** a result and read headers, category, tags and needs-reply state,
  plus the locally indexed plain text.
- **Deep links**: `?q=` and `?message=` survive reload, back and forward.
- Desktop shows a two-pane list/reader; at <=767px it becomes a list that pushes
  to a reader with a back control.
- **Honest, index-only caveat:** the reader shows only what the local index
  stores (the same body text the sandbox `ctx.mail.read` exposes). Attachments
  and rich mail HTML are never read. No LLM is called and nothing is moved, sent
  or deleted.

## How it is wired

- `manifest.json` declares two hidden, read-only tools
  (`search_messages`, `read_message`) and a `ui` block whose `operations` list
  allowlists exactly those two tools for the browser page.
- `dist/plugin.js` is the sandbox bundle; it calls `ctx.mail.search` /
  `ctx.mail.read` and catches host errors, returning typed
  `{ok:false, error:{code}}` envelopes.
- `ui/mail-desk.js` is the browser bundle; it uses only the host page SDK
  (`MTUI`) and never fetches directly.

## Install / try it

Built-ins load from the app's `plugins/` directory automatically (disabled by
default). Enable it from the Plugins page or:

```
python app.py --plugins rescan
python app.py --plugins enable mt-mail-desk
```

Then use the **Mail Desk** entry in the sidebar. See
`docs/plugin-pages.md` for the trust boundary and authoring details.
