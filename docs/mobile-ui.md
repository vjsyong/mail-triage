# Mobile UI rework — design (2026-10)

Make the app feel like a native mobile app at phone widths. Research record with
citations: `docs/mobile-ui-research.md` (NN/g, M3, Apple HIG, web.dev, MDN, WCAG 2.2,
UXmatters thumb-zone study — all verified against fetched pages).

Design decisions below are tagged **[apply]** (do it), **[adapt]** (pattern adjusted for
this app), or **[skip]** (deliberate deviation, with reason).

## 1. Navigation

- **[apply] Bottom tab bar < 768px** with 4 destinations + More: `Dashboard / Messages /
  Assistant / More` (NN/g: combo visible+hidden navigation measured best on mobile, 86%
  usage vs 57% hidden; M3: 3–5 destinations, icon + 1–2 word label, ≥48px targets;
  Apple HIG: floats at the bottom, always visible, single-word labels).
  - The tab is fixed, `height: 56px + env(safe-area-inset-bottom)` (M3 flexible bar is
    64dp; 56px + inset fits our compact aesthetic — see §2 for the exact padding recipe).
  - `aria-current="page"` marks the active tab; re-tapping the active tab scrolls to top.
- **[apply] "More" page** (`/more`): list of the remaining destinations (Rules, Flows,
  Classifiers, Templates, Accounts, Log, Settings) as ≥48px rows. Active state applies
  when the current path is one of them.
- **[adapt] The hamburger/drawer stays for 768–1024px tablets**; ≥1024px keeps the
  sidebar unchanged. On phones the hamburger is hidden (the bar replaces it).
- **[apply] Assistant stays a floating FAB + drawer** (M3: FAB sits above the nav bar);
  on phones the drawer becomes a full-screen takeover (§5). FAB bottom offset clears the
  bar + safe area.
- **[skip] Hub-page-only navigation** — we keep the bar because our sections are
  task-based and few.

## 2. Safe areas & viewport

- **[apply] Meta:** `width=device-width, initial-scale=1, viewport-fit=cover,
  interactive-widget=resizes-content` (WebKit cover guide + Chrome 108 behaviour; Safari
  ignores the keyword harmlessly). Never `maximum-scale`/`user-scalable=no`.
- **[apply] Fixed bottom elements** get the WebKit recipe (plain fallback line first,
  then `env()`):

```css
.bottom-nav{ position:fixed; left:0; right:0; bottom:0;
  padding:6px 8px; /* fallback */
  padding-left:  calc(8px + env(safe-area-inset-left));
  padding-right: calc(8px + env(safe-area-inset-right));
  padding-bottom:calc(6px + env(safe-area-inset-bottom)); }
```

- **[apply] Clearance:** `main` bottom padding = bar height + inset + 12px on phones so
  the last row is never covered.
- **[apply] Top insets:** topbar/header `padding-top: env(safe-area-inset-top)` (notch in
  landscape / standalone status bar).
- **[apply] Full-height shells use `min-height:100dvh`** (fallback `100vh` first), not
  the current `calc(100vh - 250px)` assistant hack — recompute with dvh.
- **[apply] Keyboard (iOS):** `visualViewport` translateY JS on the chat composer and
  sticky save bars (`resizes-content` covers Chrome/Android natively; Safari needs the
  JS). Pattern from brief B (MDN VisualViewport): offset by
  `innerHeight - vv.height - vv.offsetTop`, re-run on focusin/focusout.

## 3. Touch ergonomics

- **[apply] Targets:** interactive elements ≥44px (Apple HIG floor) and ≥48px for tab
  bar / primary controls (Material), with ≥8px spacing; apply under
  `@media (pointer: coarse)` so desktop density is unchanged.
- **[apply] Pressed feedback replaces tap highlight** (~100ms = "instant" per NN/g):

```css
* { -webkit-tap-highlight-color: transparent; }
.btn, .card, .nav-tab { transition: transform .1s ease, opacity .1s ease; }
.btn:active, .card:active, .nav-tab:active { transform: scale(.97); opacity: .85; }
html { touch-action: manipulation; }
```

- **[apply] Hover gating:** wrap hover-only affordances (`.rowacts` reveal, card hover
  shadow) in `@media (hover:hover) and (pointer:fine)`; on touch, row actions are
  always visible. (App already has a `@media(hover:none){.rowacts{opacity:1}}` rule —
  normalize to the canonical gating.)
- **[apply] Mobile icons/buttons in tables** get 44px boxes; dense chips stay as-is
  (WCAG 2.5.8 24px spacing exception) but never below 24px.

## 4. Forms

- **[apply] `input, select, textarea { font-size:16px }` under `@media (pointer:coarse)`
  or max-width 767px** — kills iOS focus auto-zoom; applies to Settings (55 fields),
  Flows builder, composer. Never `maximum-scale=1`.
- **[apply] `min-height:44px`** on inputs/buttons in the same media query; single-column
  rows under 600px (settings grid already collapses at 900px — tighten).
- **[apply] Keyboard hints:** `enterkeyhint="send"` on the chat composer;
  `enterkeyhint="next"` on ordered form fields; `inputmode="numeric"` for numeric
  fields (avoid `type=number`; use `inputmode` + `pattern` where validation allows);
  `autocapitalize="none" autocorrect="off" spellcheck="false"` on token/key/URL fields
  (llm_api_key, embed/rerank keys, folder names).
- **[apply] Sticky save bars** pair with the visualViewport JS (§2) so they stay above
  the keyboard.

## 5. Overlays

- **[apply] Assistant drawer = full-screen takeover on phones** (NN/g: sheets are for
  short interactions only; chat is long-lived/keyboard-heavy; M3 full-screen dialog
  criteria: keyboard input, unsaved state). `width:100vw; height:100dvh`, safe-area
  top padding, own scroll with `overscroll-behavior: contain`.
- **[adapt] The assistant page rail** (chat history) collapses under a "History" toggle
  button < 900px (keeps the transcript full-width).
- **[skip] New bottom-sheet component for row actions** — existing inline action rows
  and native `confirm()` cover v1 needs; add sheets only when a quick-action surface
  actually needs them.
- **[apply] Scroll lock** for any dialog/drawer that needs it: `<dialog>`+`showModal()`
  base + iOS position-fixed scroll save/restore shim (body overflow hidden alone fails
  on iOS Safari); `overscroll-behavior: contain` on scrollable overlay content.

## 6. PWA-to-native feel

- **[apply] Manifest** at `GET /manifest.webmanifest` (`application/manifest+json`):
  `id:"/"`, `name:"Mail Triage"`, `short_name:"MailTriage"` (≤12 chars),
  `start_url:"/"`, `scope:"/"`, `display:"standalone"`, `theme_color:"#fafafa"`,
  `background_color:"#fafafa"`, icons 192 + 512 + 512-maskable (maskable safe zone =
  40% radius circle), `description`, `shortcuts` (Messages, Assistant).
- **[apply] Icons**: brand tile — black square (#0a0a0a), white "MT" wordmark, zero
  radius (matches the app identity). Generate 180 (apple-touch, overrides manifest icon
  on iOS), 192, 512, 512-maskable via browser screenshot pipeline; store in `static/icons/`.
- **[apply] iOS head**: `apple-mobile-web-app-capable=yes` (kept — startup images
  require it), `status-bar-style=black-translucent`, `apple-mobile-web-app-title=Mail
  Triage`, `apple-touch-icon` 180, `theme-color #fafafa`. `viewport-fit=cover` already
  required for edge-to-edge.
- **[apply] PTR:** `html{overscroll-behavior-y:contain}` so Chrome-Android
  pull-to-refresh doesn't fight the app in standalone; keep explicit refresh buttons
  (Dashboard "Check now") — iOS PTR is browser chrome we can't control.
- **[skip] Service worker** — not required by current Chrome install criteria (verified);
  the container is LAN/tailnet-served so offline is not a goal. Revisit if offline is
  ever wanted.
- **[skip] iOS splash images** (per-device PNG matrix — high upkeep, low value here).
- Detection helpers if needed later: `@media (display-mode: standalone)`,
  `navigator.standalone` — use only to hide "install hint" affordances.

## 7. Scrolling & polish specifics

- **[apply]** `overscroll-behavior: contain` on the chat transcript, drawer, message
  viewer column; `overflow-anchor:none` on the chat transcript (JS owns the bottom
  stick logic — audit and keep); scroll-snap chips: `.chips` rows (messages filters,
  settings subnav) `scroll-snap-type:x proximity` + `scroll-snap-align:start`.
- **[apply] Sticky-header anchor offset:** `[id] { scroll-margin-top: 64px }` so
  settings subnav anchors land below the sticky elements.
- **[apply] Reduced motion:** audit existing block; ensure it covers the new :active
  transforms (web.dev global override pattern, `animation-duration:1ms` etc.).
- **[adapt] `content-visibility:auto` + `contain-intrinsic-size: auto Npx`** on log
  rows and message-list rows where the markup allows (skip inside tables if it
  misbehaves; verify scrollbar stability).
- **[apply] Passive listeners** for any new touch/wheel handlers; rAF-batch DOM writes
  in the flow-builder row logic and chat appends if not already.
- **[skip] System font stack** — the app self-hosts Geist as brand identity (already
  instant-loading, SF-like); swapping to `system-ui` would change the design for no
  perceptible gain.
- **[skip] Dark mode** — the app is light by design (Vercel-style); set
  `color-scheme: light` so OS dark mode cannot repaint form controls, and keep the
  light theme-color only.

## 8. Per-page passes

| Page | Changes |
|---|---|
| Shell | bottom tab bar, More page, safe-area paddings, dvh, hamburger hidden <768px, FAB offset; main clearance |
| Dashboard | metric tiles stack 2-up (exists), keep "Check now" prominent; card spacing touch-tuned |
| Messages | table → stacked rows <768px (hide thead, block rows, subject line strong, meta line dim, badges inline; row ≥56px); bulk action bar docks above tab bar; always-visible row actions on touch |
| Viewer | header actions wrap to ≥44px buttons; emailbody media full-width; column sticky → static (exists at 1023px, verify) |
| Assistant | full-screen drawer (phone), rail collapses under History toggle, composer + visualViewport keyboard fix, pending panel spacing, chips for tools wrap |
| Settings | subnav = horizontal scroll-snap chips (sticky under topbar); anchors scroll-margin; inputs 16px/44px; savebar above keyboard |
| Flows | step cards single column, ≥44px buttons, enterkeyhint=next, drag-reorder not hover-only |
| Log/Accounts | chips scroll-snap; rows ≥48px; account action buttons wrap |

## 9. Verification

- CDP device emulation (iPhone 15 Pro 393×852 DPR3 + Pixel 8 412×915) over
  `browser_exec`; screenshots + `vision_analyze` per page; checklist above walked item
  by item; then the mock E2E suite stays green (add: /manifest.webmanifest 200 +
  content-type; /more renders; viewport meta).
- Real-device smoke (Tailscale host) — installed-to-home-screen check.

### Keyboard-follow, Android correction (2026-10-02)
Bug: on Android the tab bar stayed visible over the keyboard and the composer was
pushed out of view. Cause: `interactive-widget=resizes-content` (chosen above) means
Chrome/Android shrinks the LAYOUT viewport with the keyboard, so the detection
formula `kb = innerHeight - visualViewport.height` read ~0 - `kb-open` never fired,
the tab bar never hid, and `.jumpwrap{min-height:420px}` (zeroed only under
`kb-open`) shoved the composer below the fold. Fix (JS only, meta unchanged):
track `baseH` = the largest innerHeight seen as the no-keyboard baseline (width
changes from rotation reset it, and innerHeight growth adopts a new base), then
`kb = baseH - vv.height - vv.offsetTop` with the open-threshold at 120px (above
Chrome-Android URL-bar resizes ~56px, below any keyboard). iOS semantics are
unchanged (baseH stays == innerHeight there). Verified in emulation both ways:
Android-style (layout shrinks to 516/852) and iOS-style (vv stub 500/852) both hide
the tab bar and land the composer at the visual bottom.
