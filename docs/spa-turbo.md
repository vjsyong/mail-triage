# SPA navigation layer (Turbo Drive) — 2026-10-01

Goal: "make it an SPA instead for a more responsive feel". A framework rewrite
(React/Vue + JSON API) was ruled out — it would discard the server-rendered
templates, the 400-check suite and this week's page-by-page UI work for no
functional gain. Chosen: **Hotwire Turbo Drive**, vendored (`static/turbo.js`,
v8.0.12, served at `/static/turbo.js`, 7-day cache), the standard way to give a
server-rendered app SPA navigation semantics without a rewrite.

## What it does now
- Every link and form navigation is a fetch + `<body>` swap: **no full page
  reload, no white flash**, document state (JS, scroll) persists.
- History/back/forward work; Turbo's progress bar restyled to the brand (2px black).
- Links prefetch on hover/touch (Turbo 8 default) so taps feel instant.
- `<meta name="turbo-cache-control" content="no-cache">`: every visit renders
  fresh from the server (mail data must never be stale), scripts execute exactly
  like a normal render — capped at ~1 fetch per navigation.
- `data-turbo-permanent` on the assistant drawer (`#asb`): it survives
  navigation, so an open drawer (and a streaming reply) keeps working while you
  browse other pages.

## Correctness work that came with it
Re-executing base scripts per swap would have re-registered document/window
listeners. All of those are now singleton-guarded (idempotent), with live DOM
queries inside the handlers where the closure could go stale:
`__mtTicker` (freshness ticker), `__mtChatDel` (two-step delete), `__mtSide1`
(sidebar Escape + details-menu close), `__mtCopy`, `__mtKb` (keyboard-follow),
`__mtBip` (install prompt), `__mtFlOut` (flow-builder outside-click), `__mtAsbRz`
(drawer resize). Assistant drawer wiring is guarded by `asb.__wired` (element
marker — needed for permanence). The status-ticker interval stops when its
element is detached; `pollAuth` stops when its panel leaves the DOM; the two
auto-reload pollers (classify 10s, index 8s) are now path-guarded so they can
never reload a *different* page you navigated to.

## Not touched
Streaming engine (fetch/SSE — Turbo respects `preventDefault`), `location.reload`
completions (deliberate full refresh), hash links (`#anchor` handled natively,
no visit), external `target=_blank` links (Turbo skips).

## Verification (live)
Marker `window.__mark` survived tab clicks / sidebar clicks / form POST
(`Check now`) / back-button; assistant chat streamed a reply and its throwaway
session was then deleted via the two-tap trash (also under Turbo); viewer, log,
classifiers, flows, settings all swap with zero console errors; drawer marker
`__t=777` survived desktop navigations (permanence + single-binding check).
