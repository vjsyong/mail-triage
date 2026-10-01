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

## Transitions (added same day)
"Nice smooth transition animations" — the View Transitions API, which Turbo wraps
renders in automatically when the head carries
`<meta name="view-transition" content="same-origin">` (verified in the vendored
turbo.js: `prefersViewTransitions` reads exactly that meta, and
`renderChange()` calls `document.startViewTransition`).

Design: **chrome stays put, content breathes.**
- `.side`, `.topbar`, `.bottom-nav`, `#asb` get `view-transition-name` groups:
  they morph in place (identical pixels = invisible crossfade; the sidebar's
  active state crossfades smoothly instead of sliding with the page).
- The content (root group) fades: old out at 140ms, new in at 240ms with a
  subtle 8px rise (`cubic-bezier(.2,.7,.3,1)`).
- `prefers-reduced-motion: reduce` kills all transition pseudo animations
  (instant swap), and the feature CSS is itself wrapped in `no-preference`.
- Redirect navigations (e.g. the Assistant tab, which 302s to its session URL)
  run two chained transitions, but both render the same final body so the
  second is invisible — verified frame-by-frame, no blank flash.

Verified live: `startViewTransition` hook fired once per navigation (twice on
redirect navs), zero console warnings (no duplicate-name aborts), slowed-down
frames show a clean crossfade with sidebar/topbar crisp and already updated.

Side-fix found by the slowed-down frames: the desktop Messages DATE column was
only 55px wide and wrapped "10-01 18:30" into three fragments; `white-space:nowrap`
on the date cell (and the classifiers "updated" column) — column now 101px,
single line.

## Direction-aware mobile transitions (final)
Mobile now pushes horizontally like a native app, direction follows the navigation:
- **Forward** (link/tab tap, form nav): old page slides off left, new page enters
  from the right (0.28s matched-ease so the two layers move as one).
- **Back** (history back/forward, and any `.backlink` ("← Parent") click): exact
  mirror - old exits right, new enters from the left.
- Direction is decided in a singleton listener: `turbo:visit` reads
  `e.detail.action` ("restore" = back) and a capture-phase click listener flags
  `.backlink` anchors as back (`window.__mtBack` consumed on the next visit).
  The result lands on `html[data-vt-dir]` which flips the CSS animation set.
- Desktop keeps the subtle fade + 8px rise (>=768px media); the horizontal set
  is <=767px only. Reduced-motion still disables everything.

Verified with slowed-down frames on a phone viewport: forward = old exiting left
while new enters from the right over a clean vertical split; backlink and browser
back = the exact mirror; top bar and bottom tab bar pinned in both. Desktop
re-verified as fade+rise with zero horizontal displacement. Also classed the
message viewer's "← Messages" link as `.backlink` (it was the last unclassed
parent link, so it now slides from the left too).

## Direction fix (same day, v3) - page-order model
Symptom: "always right to left" - every tab-to-tab tap animated the same way, because
Turbo reports ALL link clicks as action=advance, so the action-based rule never
reversed on sibling navigation.

Rule now: pages sit in a fixed order in the base script (`VTPOS`: tab-bar order
dashboard(0) < messages(10) < viewer(11) < assistant(20) < rules(30/31) <
classifiers(40/41) < flows(50/51) < templates(60/61) < accounts(70/71) <
settings(80) < more(90) < log(91) < proxy(92)). Moving to a LATER page slides in
from the right; moving to an EARLIER page slides in from the left - regardless of
how the navigation was triggered. Backlink clicks stay forced to 'back'; unknown
page pairs fall back to Turbo's action (restore = back).

Verified live on mobile (attr + slowed-down frames): assistant->messages now
slides in from the LEFT (the reported bug), messages->assistant unchanged
(from the right); messages<->viewer, messages<->dashboard and history-back all
correct. When adding a page, add its prefix to VTPOS or it falls back to
always-forward for its pairings.

## Redirect double-render fix (same day, v4) - /assistant renders directly
Symptom: tapping the Assistant tab played the slide animation TWICE. Cause found by
wrapping startViewTransition + listening to turbo:visit/before-render: Turbo's
followRedirect() renders the redirected response (transition 1), then proposes a
second "replace" visit to the final URL (transition 2, serialized after the first
by Turbo's ViewTransitioner) - two visits, two renders, two SVTs for one tap.

Fix: GET /assistant no longer 302-redirects. It renders the assistant page directly
(shared `_assistant_page(sid)`, called by both /assistant and /assistant/s/<sid>);
the template's first script replaceState()s the URL to /assistant/s/<sid>. One fetch,
one render, one transition - verified by the same trace (1 SVT / 1 render / canonical
URL at render time). Back/forward history behaves identically to the redirect era.

Note: form-submission redirects (settings saves, rule saves, etc.) were ALREADY
single-transition - Turbo hands the response to the replace visit in that path, so
.no second render. The double only affected cross-location GET redirects, of which
/assistant was the only one in this app.
