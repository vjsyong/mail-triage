# App shell & consistency — research brief (2026-10-01)

Scope: app-wide shell/consistency questions for mail-triage, used mostly on a phone via
Tailscale and installed as a PWA. Topics: (1) page heads on mobile, (2) the More hub,
(3) empty states, (4) loading/progress/freshness/feedback placement, (5) PWA polish.

Method: fresh web sources fetched 2026-10-01 (hound, keyless; list at the end), plus
first-hand reads of `app.py` (BASE_TMPL ~L307, MORE_TMPL ~L1321) and the existing briefs
(`ui-redesign.md`, `mobile-ui.md`, `mobile-ui-research.md`, `assistant-mobile.md`,
`dashboard-mobile.md`, `messages-mobile.md`) for conventions that must not be
contradicted. Tags: **[apply]** do it, **[adapt]** adjusted for this app, **[skip]**
deliberate deviation, **[derived]** composed from cited mechanics (not in one source).

Non-negotiables carried over: Vercel-style LIGHT theme, zero border-radius (brand),
Geist, black panels, WCAG 2.2 AA, bottom tab bar <768px (Dashboard / Messages /
Assistant / More), sidebar ≥768px, assistant = full-screen takeover with the topbar
hidden on phones (single-header precedent), no service worker (LAN/tailnet, offline
not a goal).

---

## 1. Page heads, titles, actions, back navigation

### What exists
- `.page-head` (BASE_TMPL L367–369): `title + desc` left, actions right, wraps.
- Phones already drop the description (`.page-desc{display:none}`, L682) except the
  message viewer, which re-enables it because it carries sender/date/folder
  (`.page-desc.msgfrom`, L717) — the model for "essential meta only".
- The assistant page hides the shell topbar and uses one slim chat header
  (`body:has(.assistant-main) .topbar{display:none}` L704; `.chat-head` L687).
- Phone actions were moved into content on prior passes, per page: dashboard actions
  live in the hero card (`.dashactions`), messages actions get their own full-width
  row (`.tactions`). It works, but it isn't a shell-level pattern yet.
- Detail pages: the message viewer has `← Messages` (L2997) above the H1. Editors all
  have a `Back` button **top-right of the page-head** (rules L2065, classifier dataset
  L1876, template L2599, accounts L4866/L4982) plus a `Cancel` (or `Back`) in the
  savebar (L2115, L2320, L2614, L4932, L5028). Flows is the outlier: savebar `Cancel`
  only (L2320), no head back.
- `<title>` is the static "Mail Triage" on every page (L317).

### Phone recommendations
- **Keep the H1 on every page.** On phones it is the only place the page name exists:
  the topbar shows the product, the tab bar shows the section, and the tab labels
  don't change per page. Hiding it "because the tab names the section" is only
  defensible on the assistant page, where the section *is* the whole page (existing
  single-header). Don't extend that exception to list/detail pages.
- **Keep the description hidden by default; allow exactly one operational meta line**
  (viewer pattern). Page-head should be: back link (if any) → H1 → ≤1 meta line →
  actions row.
- **Actions belong in a full-width row under the title, not the top-right corner.**
  The top corners are the worst reach zone on a phone; the easiest zone is
  bottom/center (Hoober, 1,333 street observations: 49% one-handed — via
  `docs/mobile-ui-research.md`, which also cites Fitts/Material reach guidance in
  `dashboard-mobile.md`). Dashboard and messages already made this move; make it a
  shell rule so new pages inherit it.
- **Back affordance = one "← Parent" link at the top of the page-head, not a
  breadcrumb trail.** NN/g's mobile breadcrumb guidance says don't let trails wrap,
  don't shrink them (touch targets need ~1cm), and *"consider shortening the
  breadcrumb trail to include only the last level(s)… a single breadcrumb pointing up
  a level may be all that is necessary"* — while desktop, where there is room, shows
  the full trail (https://www.nngroup.com/articles/breadcrumbs/, guidelines 9–11).
  This app's hierarchy is one level deep (list → detail/editor), so a single parent
  link is the correct pattern on both breakpoints; a full trail is **[skip]** even on
  desktop (NN/g #7: breadcrumbs aren't useful for flat 1–2 level hierarchies — the
  sidebar + active state already answers "where am I").
- **The back link must target the parent list, not history** (NN/g #2: location-based,
  not path-based). `← Messages` → `/messages` (default view), not the last filtered
  URL; system/browser back remains the session-back tool. The viewer already behaves
  this way — keep it.
- **Don't rely on the tab bar for back.** Tapping the Messages tab goes to the
  unfiltered list and can't return to the item you came from; it is navigation, not a
  back action. Every sub-page needs its own parent link.
- **Normalize where the parent link lives.** Move editors' `Back` from the top-right
  button into the same `.sub` link row the viewer uses (promote a `.backlink` class),
  keep `Cancel`/`Back` in the savebar as-is, and give flows the missing head link.
  Reach: a text link under the title is nearer the thumb than the top-right corner.
- **Detail-page action density stays ≤1–2 controls beside the title.** M3: an app bar
  carries "content and actions related to the current page… 1–2 essential actions",
  the leading button is "a back arrow, which returns to the previous screen", and
  secondary actions go in a toolbar rather than an overflow menu
  (https://m3.material.io/components/app-bars/guidelines). The viewer satisfies this
  via its Actions card; status badges in the head are fine (not controls).
- **Hardware/gesture Back should close overlays, not exit the PWA.** The drawer and
  history sheet open without a history entry, so Android back leaves the app instead
  of closing them. Push a state on open + close on `popstate` (BASE_TMPL shell JS
  L835–853). Grounding: NN/g's sheet guidance — support Back to dismiss, and always
  keep a visible close, never gesture-only
  (https://www.nngroup.com/articles/bottom-sheet/, already recorded in the repo's
  overlays brief).
- **List → detail stays full-page navigation** with real `<a href>` links (the
  server-rendered viewer). Not a sheet: sheets are for short interactions;
  scroll-heavy reading gets a page (NN/g, same source; the repo overlays brief already
  mapped the viewer to full-screen reader).
- **Set a per-page document `<title>`** ("Rules · Mail Triage"): in standalone the
  title is the window/app-switcher label, and a constant "Mail Triage" makes parallel
  windows indistinguishable (web.dev, PWA app design:
  https://web.dev/learn/pwa/app-design — "make it meaningful").

### Desktop recommendations
- Page-head keeps title + one-line description + actions right (as designed in
  `docs/ui-redesign.md` §Shell). No breadcrumbs (flat hierarchy). Keep the viewer's
  smaller H1 override for long subjects.
- Top-right Back buttons are fine for mouse; still normalize them into the link row
  for one consistent component.

### Changes (mapped)
- BASE_TMPL ≤767 block (L666+): add
  ```css
  .page-head{flex-direction:column;gap:8px}
  .page-head .acts{display:flex;gap:8px;width:100%}
  .page-head .acts .btn{flex:1;min-height:40px;justify-content:center}
  ```
  and give `.dashactions` / `.tactions` the `acts` class (or add them to the selector
  list) so dashboard/messages/editors share one mechanism.
- BASE_TMPL: promote the viewer's back row (L2997) to a `.backlink` class; use it in
  `RULE_FORM_TMPL` (above L2065's H1), `FLOW_EDIT_TMPL` (missing today),
  `TEMPLATE_EDIT_TMPL`, `ACCOUNT_*_TMPL`, `CLASSIFIER_*`; keep the savebar buttons.
- BASE_TMPL: accept an optional `title` per page in `render()` (L1294) and emit
  `<title>{{ title or 'Mail Triage' }}</title>`; set it in each page route.
- BASE_TMPL JS: `pushState`/`popstate` for `#drawer` + history sheet.
- No breadcrumbs anywhere; record the [skip] with the NN/g #7 rationale.

---

## 2. The More hub (`/more`, MORE_TMPL L1321)

### What exists
One `.more-list` of 7 rows (Rules, Flows, Classifiers, Templates, Accounts, Log,
Settings), 54px, name + one-line description + chevron. The tab bar marks More active
for `morepaths` (L808) — but the More anchor lacks `aria-current` (the other three
have it). The sidebar groups the same destinations into Mail / Automation / System
with icons; the hub has no groups, no icons, no status. `/more` has `w`, `pend`, `tz`,
`info`, `cfg` in scope via `render()`.

### Recommendations (phone and desktop)
- **Keep the hub; it is not redundant with the tab bar.** The bar carries the 4
  primary task destinations (M3: 3–5 destinations, icon + 1–2 word label; Apple HIG:
  avoid overflow tabs — both in `docs/mobile-ui-research.md`). The hub carries the
  long tail. NN/g: >5 options don't fit a tab/nav bar with usable targets, and the
  *navigation hub* — "a page that lists all the navigation options" — "can work well
  in task-based websites and apps, especially when users tend to limit themselves to
  using only one branch of the navigation hierarchy during a single session"
  (https://www.nngroup.com/articles/mobile-navigation-patterns/). mail-triage is
  task-based with short sessions; the hub is the sanctioned pattern, and the repo
  already measured the combo approach (visible bar + hub) as the best-performing
  mobile navigation in NN/g's study.
- **Phone: keep it a single-column rows list, not a grid.** Rows are what let each
  destination carry icon + name + description (+ optional status) at ≥48px; the
  hub-as-list is NN/g's own description of the pattern, and 7 destinations don't
  need grid density. Current 54px rows are right.
- **Phone: add section labels inside the list, mirroring the sidebar exactly** —
  "Automation" (Rules, Flows, Classifiers, Templates) and "System" (Accounts, Log,
  Settings), same words/order/icons. One mental model across sidebar, tab bar, and
  hub. Grounding: group by the tasks users come to do, with labeled sections (Eleken
  settings guide, recorded in `docs/ui-redesign.md` §Second pass).
- **Add the sidebar's icons to the rows** (reuse the exact SVGs from L756–770): the
  tab bar already reuses sidebar icons; consistency + recognition.
- **Statuses only where the row's state is the reason to visit, always with text**
  (colour never sole signal — repo convention, WCAG). Cheap and honest with the
  context `render()` already supplies: a hub footer line `● Last check 2m ago ·
  Times in <tz>`, matching the sidebar foot (L772–779) and topbar. Heavier per-row
  badges (parked-errors count on Log) are [later]: they'd need a store call in the
  `/more` route and would duplicate the dashboard's alerting.
- **Desktop: same page, same list; the sidebar remains the primary navigator.** No
  desktop-specific chrome; reach isn't an issue with a mouse and the hub costs
  nothing. Keep row order stable (Rules first, Settings last) for muscle memory.
- **The hub is the right home for the one app-level action missing today: "Install
  app"** (see §5). Not Settings — it's a meta action, and the hub is already the
  "everything else" surface.
- Revisit the hub only if the app ever drops to ≤5 total destinations (then Settings
  could absorb it) or grows past ~12 (then groups + a filter row); neither is near.

### Changes (MORE_TMPL)
- Split `.more-list` into two groups with `.nav-label` headers ("Automation",
  "System"), same styling as BASE_TMPL L345.
- Prefix each row name with its sidebar SVG (16px, `flex:none`, muted like the
  sidebar).
- Footer: status dot + `Last check {{ w.last_ok_r }}` + `Times in {{ tz }}`, reusing
  `.side-foot` markup/styles.
- Add the "Install app" row (§5), hidden unless installable / browser mode.
- Add `aria-current="page"` to the More tab when `p.startswith(morepaths)` (L809).

---

## 3. Empty states

### What exists
`.empty` (BASE_TMPL L462–466): centered, optional icon, `h4` title, `p` reason,
optional one `.btn`. `ui-redesign.md` fixed the anatomy: icon + title + one-line
reason + one primary action. Current audit:
- With action: Rules "New rule" (L1757), Flows "Build the first flow" (L2250),
  Classifiers "Go to messages" (L1804), Templates "New template" (L2586), Accounts
  "Add account" (L4770), viewer "Body unavailable" (specific recovery, L3031).
- **Without action: Messages** (L2753–2756) — both the first-run and the filtered
  variant.
- Dataset empties are intentional learning cues, no action (L1917, L1943).

### Recommendations (phone and desktop; phone only tightens padding)
NN/g's three purposes: **communicate system status**, **provide learning cues**,
**provide direct pathways for key tasks** — and a loading process must never
masquerade as "no records", which users read as breakage
(https://www.nngroup.com/articles/empty-state-interface-design/). Classify every
`.empty` into one of three flavors and make the copy match:

1. **First-run** (nothing yet): say why it's empty and what will populate it; end in
   the action that populates it. Messages is the gap — it currently explains the
   watcher but offers no button; add **[Check now]** (POST `/check`; route
   `check_now`, L1674). One sentence of learning cue max.
2. **Filtered / no results** (data exists, filter matched nothing): repeat the filter
   in words + offer the reset. NN/g's canonical example: "There are no records to
   display for the selected date range" with a way forward. Messages already says
   "No messages match this filter yet" — add **[Show all messages]**
   (`url_for('messages')`).
3. **Empty after a process / error**: specific reason + single recovery (viewer
   model). Keep exactly as is.

Rules of thumb:
- One action maximum, and it must be the thing that populates the container or clears
  the filter.
- `.empty` is never a loading state: loading gets progress/skeleton (§4); NN/g warns
  a premature "No records" while work runs is the harmful case.
- Copy: 1 title + ≤2 lines, no "Oops", no exclamation.

### Changes (mapped)
- MESSAGES_TMPL empty block (L2753–2756): add buttons — `[Check now]` when
  `filt == 'all'`, `[Show all messages]` otherwise.
- Sweep confirms no other page needs an action; leave datasets as learning cues.
- ≤767 CSS: `.empty{padding:28px 14px}`; action button full-width on phones.

---

## 4. Loading, progress, freshness, feedback placement

### What exists
- Classify job: card with "classifying…", `done/total · failed · current`, percent
  `.progress` bar, Stop, and a **full-page reload every 10s** (L2693–2707).
- Dashboard index run: "indexing…" line + **full-page reload every 8s** (L1629).
- OAuth flow: 2s `fetch()` of a JSON status endpoint, button state machine — the good
  polling pattern already in the codebase (L4804–4835).
- Freshness: `w.last_ok_r` in the topbar (L787, bare value) and sidebar foot (L777);
  dashboard: "checked X ago · next in ~Ns".
- Feedback: flash banners at the top of `.content` (L790–792) for post-redirect
  results; JS toasts top-right (`.toasts` L484, `toast()` L815–825, dismiss
  4.6–5.2s) for AJAX.

### Progress recommendations
- **Percent-done bars are correct for both jobs.** NN/g: looped animations
  (spinners) for 2–10s; percent-done for ≥10s because it shows how much is left and
  lets users decide to wait or stop
  (https://www.nngroup.com/articles/progress-indicators/). Keep the counts + current
  item; add a rough time-left line when cheap.
- **Make the bars accessible**: `role="progressbar"` + `aria-valuenow` +
  `aria-label` (min/max default 0/100). MDN caveat: descendants of a `progressbar`
  are presentational, so the accessible name must be on the element and visible text
  must live outside it; MDN prefers a native `<progress>`
  (https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Reference/Roles/progressbar_role).
  Either is acceptable; ARIA on the existing `.progress` div is the smaller diff.
- **Stop reloading the page under the user.** `location.reload()` on a timer throws
  away scroll position, text selection, and reading context — worst on Messages,
  where the user browses while classification runs. Replace with the app's own OAuth
  pattern: `fetch()` a small status fragment (HTML or JSON) and update only the
  progress card, plus an explicit "Refresh list" chip. [derived from NN/g's
  feedback/status guidance + the existing L4804 pattern.] If a reload fallback stays,
  gate it on `document.visibilityState === 'visible'` and restore scroll.
- **No indicator under ~1s** (NN/g): don't add skeleton screens for instant
  navigations. If a server-rendered page ever visibly drags past ~1s, skeleton
  screens are the sanctioned pattern for full-page loads (<10s) — but don't add them
  pre-emptively ("Skeleton Screens 101",
  https://www.nngroup.com/articles/skeleton-screens/).

### Freshness / "Ns ago" recommendations
- **Always label what the age refers to** — "Last check 2m ago", not bare "2m ago".
  Cloudscape: "timestamps should always be accompanied by a label that clearly and
  consistently describes what event the timestamp references"
  (https://cloudscape.design/patterns/general/timestamps/). Fix the topbar (L787) and
  sidebar foot (L777); the dashboard is already good.
- **Relative-first, absolute one hover/tap away**: wrap in
  `<time datetime="ISO" title="absolute">` (same source) — pass `last_ok_iso`
  through `render()` alongside `last_ok_r`.
- **Tick the labels client-side (~30s)** so an open page's "2m ago" doesn't drift;
  pause on `visibilitychange:hidden` [derived].
- **Staleness = text + colour.** When `w.err`, the topbar dot goes red *and* the
  label becomes "last check failed" (sidebar already does this; the topbar shows only
  the dot). Colour never without text (repo convention, WCAG AA).
- **Make "Last check …" the tap-to-refresh target on phones** (Desktop: keep explicit
  buttons) [derived].

### Toasts vs flash recommendations
- **Model: flash = durable post-redirect result (top of content, both breakpoints,
  unchanged). Toast = transient AJAX feedback.** M3: snackbars are low priority,
  auto-dismiss, "shouldn't interrupt", one at a time; and on the web an
  auto-dismissing snackbar is inaccessible unless the same information is also
  available inline — so a toast must never be the only record of something important
  (https://m3.material.io/components/snackbar/guidelines).
- **Phone: dock toasts to the bottom, above the tab bar.** M3: place snackbars "at
  the bottom of a UI, in front of the main content", nudged up to clear docked
  toolbars/FABs, and "avoid placing a snackbar in front of frequently used touch
  targets or navigation" — the tab bar is exactly that. Today's top-right toasts sit
  over the topbar/notch area and far from the thumb.
- **Assistant page exception**: the composer is docked at the bottom; either offset
  toasts above the composer or keep them top-anchored on that one page.
- **Desktop: keep top-right** (existing convention; out of the reading path).
- **One at a time**: new toast replaces/queues, never stacks (M3: "only one snackbar
  may be displayed at a time").
- **Auto-dismiss ~4–7s**: web.dev's transient-UI guidance for install snackbars says
  4–7s (https://web.dev/learn/pwa/installation-prompt); M3 allows 4–10s. Current
  4.6–5.2s is fine.
- **Never double-report**: pick flash *or* toast per event, never both.

### Changes (mapped)
- MESSAGES_TMPL classify card (L2693–2707): add `role="progressbar"` +
  `aria-valuenow` + `aria-label`; replace `setTimeout(location.reload, 10000)` with a
  status-fragment fetch (e.g. `GET /messages/classify/status`) updating the card +
  "Refresh list" chip.
- DASH_TMPL index line (L1629): same; until the fragment exists, keep the 8s reload
  visible-tab-only.
- BASE_TMPL: topbar/sidebar relabel to "Last check …", wrap in `<time>`, add ~30s
  ticker; `render()` passes `last_ok_iso`.
- BASE_TMPL ≤767: `.toasts{top:auto;bottom:calc(72px + env(safe-area-inset-bottom));
  left:12px; right:12px}` + full-width 2-line `.toast2`; `toast()` replaces the
  previous toast instead of appending; assistant-page offset override.

---

## 5. PWA polish across pages

### What exists
Manifest `/manifest.webmanifest` (L1355): id/scope/start_url/standalone/icons/
shortcuts/description. iOS metas + 180px touch icon. PTR contained
(`overscroll-behavior-y:contain`). Safe areas: topbar (L660), tab bar (L669–671),
content bottom (L678), savebar (L680). Overlay z-order documented in
`assistant-mobile.md` (topbar 30 < fab 60 < bottom-nav 180 < sheet 190 < drawer 220 <
toasts 300). Service worker deliberately [skip]. No install affordance anywhere.

### Safe areas — what's missing (fix first)
- **Assistant single-header has no top inset.** `.chat-head` (L687) is first in the
  page with the topbar hidden; under `black-translucent` in standalone iOS the status
  bar draws *over* content, so the header collides with the clock. Add
  `padding-top:calc(env(safe-area-inset-top) + 8px)` ≤767.
- **Full-height overlays**: history sheet header `.sheet-h` (L712) and drawer head
  `.dw-head` (L630) sit at `top:0` — same inset needed.
- **Toasts**: top placement ignores `env(safe-area-inset-top)`; the bottom move (§4)
  fixes that with the bottom inset.
- **Landscape notches**: `.content` side padding is fixed 14px ≤1023 (L534); use
  `padding-left/right: max(14px, env(safe-area-inset-left/right))` [derived from the
  WebKit `max()` recipe in `docs/mobile-ui-research.md` §A].
- **Keyboard**: `interactive-widget=resizes-content` (meta L309) only reaches
  Chrome/Android + Firefox; iOS Safari needs the `visualViewport` translateY for the
  composer/savebar (repo plan, `mobile-ui.md` §2). Verify it's wired — if not, this
  is the same class of bug as the missing insets.

### Install prompt
- **Add an "Install app" row to /more** with per-platform behavior:
  - Chromium (Android/desktop): capture `beforeinstallprompt` with
    `preventDefault()`, store it, and only reveal the row **after the event fired**;
    tapping calls `deferredPrompt.prompt()`. Keep it dismissible and remember the
    choice. Grounding: https://web.dev/learn/pwa/installation-prompt and
    https://web.dev/articles/promote-install ("keep promotions outside of the flow of
    your user journeys… only show the promotion after the `beforeinstallprompt`
    event has fired").
  - iOS: no prompt/banner and no `beforeinstallprompt` exists
    (https://firt.dev/notes/pwa-ios/ — both ❌). Show static "Share → Add to Home
    Screen" instructions instead, rendered only in browser mode via
    `@media (display-mode: browser)` — web.dev's documented fallback, which also
    hides itself once installed.
- **Manifest polish**: add `screenshots`. With `description` + `screenshots`, Chrome
  Android upgrades the install dialog from a small info bar to a richer
  app-store-like sheet (web.dev, installation-prompt). Keep `id`/`start_url`/`scope`
  stable — churning them breaks installed-state detection (web.dev PWA learn).

### Standalone display quirks
- `black-translucent` remains the only way to fullscreen on iOS, and the status bar
  renders over content (firt.dev) — hence the insets above; keep top surfaces light
  (the system draws white status glyphs; repo research).
- iOS ignores `orientation`, `background_color`, `display_override`, `shortcuts`
  (firt.dev) — the manifest shortcuts are Android-only. Harmless; don't build iOS
  features on them.
- iOS has no link capturing (only a push can open an installed PWA) and opens
  out-of-scope links in an in-app browser (since 12.0, firt.dev) — expected; don't
  promise "opens in app" anywhere.
- Detect install state via `@media (display-mode: standalone)` (web.dev;
  `navigator.standalone` is the legacy iOS check) — use it only to hide install
  affordances.
- **Fetch/refresh**: offline is a non-goal (no SW, [skip]); freshness is server-side.
  PTR is deliberately contained on Android and absent on iOS — never rely on it; keep
  explicit refresh affordances (dashboard "Check now", list reloads, tap-to-refresh
  "Last check …"). On return to a visible tab, if data is older than the poll
  interval and no form has unsaved input, refresh in place or offer a "Refresh" chip
  instead of silently navigating away [derived; matches web.dev PWA app-design /
  repo `mobile-ui.md` §6].

### Changes (mapped)
- BASE_TMPL ≤767: top insets for `.chat-head`, `.sheet-h`, `.dw-head`; landscape
  `max()` padding on `.content`; bottom dock + safe area for `.toasts` (§4).
- BASE_TMPL head/scripts: `beforeinstallprompt` listener + dismissal memory; nothing
  else new in the head beyond existing metas.
- MORE_TMPL: "Install app" row (hidden in standalone via `@media`; iOS tap shows
  instructions).
- Manifest route (L1355): add `screenshots` (light theme, phone + desktop).

---

## Priority order (smallest diff, most clarity first)
1. Safe-area top insets on assistant header / sheet / drawer (§5) — tiny, fixes a
   real standalone collision.
2. "Last check …" labels + `<time>` + staleness text (§4) — tiny, high clarity.
3. Messages empty-state actions + `.empty` padding on phones (§3) — small.
4. Phone toast docking + single-toast behavior (§4) — small, removes overlap.
5. Page-head `acts` normalization + `.backlink` on editors (flow editor first) (§1) —
   medium.
6. More hub labels/icons/footer + Install row + `aria-current` on the tab (§2/§5) —
   medium, low risk.
7. Replace reload-polling with status fragments for classify/index (§4) — largest,
   do last.

## Deliberate deviations [skip]
- No breadcrumbs (flat 1-level hierarchy; NN/g #7) — one back link is the mobile
  pattern (NN/g #11).
- No rounded corners, no dark mode, no service worker — existing brand/product
  decisions, unchanged.
- Desktop keeps top-right toasts and top-right editor Back buttons (mouse reach is a
  non-issue); only phones dock toasts / move back links into the flow.

## Sources fetched (2026-10-01)
- https://www.nngroup.com/articles/breadcrumbs/ — mobile rules 9–11, hierarchy not history
- https://www.nngroup.com/articles/mobile-navigation-patterns/ — navigation-hub pattern
- https://m3.material.io/components/app-bars/guidelines — 1–2 actions, leading back arrow
- https://www.nngroup.com/articles/empty-state-interface-design/ — 3 guidelines
- https://www.nngroup.com/articles/skeleton-screens/ — <10s skeleton; no frame-only
- https://www.nngroup.com/articles/progress-indicators/ — 1s rule, ≥10s percent-done
- https://m3.material.io/components/snackbar/guidelines — bottom placement, one at a time, inline equivalent
- https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Reference/Roles/progressbar_role — role/aria-valuenow; prefer <progress>
- https://web.dev/articles/promote-install — show only after beforeinstallprompt; dismissible
- https://web.dev/learn/pwa/installation-prompt — capture/defer, display-mode fallback, screenshots
- https://firt.dev/notes/pwa-ios/ — iOS: no prompt, no beforeinstallprompt, no link capturing
- https://cloudscape.design/patterns/general/timestamps/ — relative + label + `<time datetime title>`

Reused, already verified in-repo (not re-fetched): `mobile-ui-research.md` (Hoober
thumb zones; M3 nav bar 3–5; HIG tab bars; WebKit safe-area recipes), `mobile-ui.md`
(shell decisions, PTR containment), `assistant-mobile.md` (single-header precedent,
z-order), `ui-redesign.md` (page-head, empty-state anatomy, flash/toast split),
`dashboard-mobile.md` ("last updated X ago beats a spinner"). Apple's HIG pages block
fetching, so HIG points go through the repo's earlier verified briefs.


---

## Applied (2026-10-01, same day)
Shipped from this brief:
- **Safe areas** (priority 1): top insets on `.chat-head`, `.sheet-h`, `.dw-head`;
  landscape `max(14px, env(safe-area-inset-left/right))` on `.content`; toasts dock
  bottom on phones (82px + bottom inset) with top-anchor exception on the assistant
  page; single-toast-at-a-time (`box.innerHTML=''` before append).
- **Freshness** (priority 2): topbar + sidebar now say "Last check Ns ago" wrapped in
  `<time data-rel datetime>`; 30s client ticker recomputes the label; topbar shows
  "last check failed" text (not colour alone) when errored; `last_ok_iso` passed
  through `render()`.
- **Empty states** (priority 3): messages empty block gets [Check now] (empty) /
  [Show all messages] (filtered); `.empty` padding + full-width action on phones.
- **Back links** (priority 5): `.backlink` promoted; editors (rules, flows,
  templates, accounts new/edit, dataset) now carry a "← Parent" link above the H1
  and dropped the top-right Back buttons; flow editor got its missing link.
- **More hub** (priority 6): grouped rows (Automation/System) with the exact sidebar
  icons, hub footer "Last check … · Times in …", "Install app" row
  (beforeinstallprompt-gated, iOS instructions fallback, hidden when standalone),
  `aria-current` on the More tab.
- **Progress** (priority 7, partial): classify `.progress` got
  `role="progressbar"` + aria-value*; both reload-pollers (classify 10s / index 8s)
  are now visibility-gated so they never reload a hidden → background tab; the
  status-fragment replacement is deferred.
- Not done (deliberate): generic `.page-head .acts` (per-page mechanisms already in
  place; risk of breaking chip rows), tap-to-refresh on the freshness label,
  manifest `screenshots`, no breadcrumbs (per [skip] above).
