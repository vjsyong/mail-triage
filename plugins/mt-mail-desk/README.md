# Mail Desk (mt-mail-desk)

A focused, **read-only**, **composed** plugin page over the local mail index -
the reference for the sandboxed UI tier. It runs no browser plugin code and gets
no host privileges beyond `mailbox.read`.

## What it does

- **Search** the local index (capped at 50 results) by sender, subject or
  snippet; **select** a result to read headers, category, tags and needs-reply
  state plus the locally indexed plain text.
- **Refresh**, and loading/empty/denied/error states with retry.
- **Deep links**: `?q=` and `?message=` survive reload, back and forward.
- Desktop shows a split list/reader; at <=767px the host switches to a
  list-then-reader layout with a back control (host-controlled layout).
- **Honest, index-only caveat:** the reader shows only what the local index
  stores. Attachments and rich mail HTML are never read. No LLM is called and
  nothing is moved, sent or deleted.

## How it is wired (composed)

- `manifest.json` declares `ui.mode: "composed"`, one page, two hidden
  read-only tools, and `mailbox.read`.
- `dist/plugin.js` exports synchronous `uiOpen` / `uiDispatch` / `uiClose`; it
  builds a data-only component tree with the `MTUIB` helpers and calls
  `ctx.mail.search` / `ctx.mail.read`. The host validates the tree and renders
  it; controller execution gets a read-only host capability subset (net, LLM,
  kv writes and action proposals are denied).

## Install / try it

Built-ins load from the app's `plugins/` directory automatically (disabled by
default). Enable it from the Plugins page or:

```
python app.py --plugins rescan
python app.py --plugins enable mt-mail-desk
```

Then use the **Mail Desk** entry in the sidebar. See `docs/plugin-pages.md` for
the composed/trusted contract.
