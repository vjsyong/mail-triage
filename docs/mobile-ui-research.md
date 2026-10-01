# Mobile Navigation & Touch Ergonomics — research brief (Flask+Jinja retrofit)

## 1. Bottom tab bar vs hamburger / drawer

- [*] NN/g quantitative study (179 participants, 6 live sites): on **mobile**, hidden navigation was used in **57%** of tasks vs **86%** for combo (part visible, part hidden); content discoverability dropped **>20%** with hidden nav; hidden-nav tasks were **15% slower** and difficulty ratings **+21%** vs visible. Recs: **≤4 top-level links → display them visibly; >4 → hide some** (smaller mobile penalty than desktop); support hidden nav with in-page links. (https://www.nngroup.com/articles/hamburger-menus/)
- [*] NN/g patterns article: tab bars are **persistent** (always visible while scrolling); nav/tab bars fit only "relatively few options — if more than 5, it's hard to fit them and keep an optimum touch-target size"; the hidden menu is "least discoverable ... best suited for content-heavy, browse-mostly sites"; task-based apps can use a hub page. (https://www.nngroup.com/articles/mobile-navigation-patterns/)
- [*] M3 navigation bar = **3–5 destinations**, bottom-anchored, each with icon + label ("All navigation items require a label text. It should be 1–2 words"). **For >5 destinations, don't use a nav bar** — use tabs or hide nav behind a menu (modal expanded navigation rail). Vertical items in compact windows (<600dp); re-tapping the active destination resets scroll to top; don't hide the bar on scroll when a screen reader is active. (https://m3.material.io/components/navigation-bar/guidelines)
- [*] M3 spec state (2025+): the "baseline" nav bar is **no longer recommended**; M3 Expressive "flexible navigation bar" replaces it (shorter, horizontal items in medium windows). Heights live in interactive token tables and could not be text-extracted from the spec page. (https://m3.material.io/components/navigation-bar/specs)
- [*] M3 numbers from official Android component docs: M3 Expressive nav bar **height 80dp → 64dp**, active indicator width **64dp → 56dp**, default active-indicator **height 32dp**, icon **24dp**, indicator 50% rounded, 4dp indicator↔label padding; label no longer bold when selected. (https://raw.githubusercontent.com/material-components/material-components-android/master/docs/components/BottomNavigation.md)
- [*] M2/M3 nav bar (retrievable page): "**height … 80dp**" container, "taller container height, active state indicator shape, no elevation shadow" in M3; label policy by count: 3 destinations → icons+labels for all; 4 → labels recommended even inactive; 5 → labels if space permits. (https://m2.material.io/components/bottom-navigation)
- [*] Apple HIG (current, "Tab bars"): iOS tab bar **floats at the bottom** above content; "use a tab bar to support navigation, not to provide actions"; keep it visible when switching sections; **avoid overflow tabs** (the iOS "More" tab "makes it harder for people to reach and notice content"); don't disable/hide tab buttons; include labels ("use single words"); "generally easier to navigate among fewer tabs". (https://developer.apple.com/design/human-interface-guidelines/tab-bars)
- [*] Thumb zone (primary source, Steven Hoober, 1,333 street observations): **49% one-handed / 36% cradled / 15% two-handed**; grip and hand position change every few seconds; reach charts show green = easily reachable, yellow = stretch, red = must shift grip — easiest zone is bottom/center, worst is top corners. (https://www.uxmatters.com/mt/archives/2013/02/how-do-users-really-hold-mobile-devices.php)
- [*] Material's ergonomics rationale for bottom placement: "The bottom navigation bar is easy to reach on a handheld mobile device." (https://m2.material.io/components/bottom-navigation)
- [*] Synthesis for this app (~9 sections): put the **4 most-used destinations in a fixed bottom bar** (e.g. Dashboard, Messages, Flows, Settings) + a **"More" destination page** (or sheet) for Rules/Classifiers/Templates/Log/Accounts — a *combo* pattern, which measured best on mobile in NN/g's study (86% usage). Keep the AI chat as a floating button/drawer, not a tab (it's an overlay by design). Hamburger-only drawer is justified if you later exceed ~9 destinations or want content-first browse.

## 2. Touch ergonomics

- [*] Apple HIG (current accessibility page): **iOS default control size 44x44 pt, minimum control size 28x28 pt**; "Consider spacing between controls as important as size … reduce the chance that someone taps the wrong control." (https://developer.apple.com/design/human-interface-guidelines/accessibility)
- [*] Material: touch targets **≥48x48 dp** ("about 9mm … recommended 7–10mm"); a 24x24dp icon needs surrounding padding to reach the full 48x48dp target; pointer targets ≥44x44dp; "touch targets separated by **8dp of space or more** promote balanced information density and usability"; the page also explicitly notes iOS 44x44pt. (https://m2.material.io/design/usability/accessibility.html) — M3's old `foundations/accessible-design/accessibility-basics` URL now **404s**, so this retrievable Material page is the citation.
- [*] WCAG 2.2 **SC 2.5.8 Target Size (Minimum), AA**: "at least **24 by 24 CSS pixels**", with 5 exceptions: spacing (a 24px-diameter circle per target must not intersect another target/circle), equivalent, inline, user-agent control, essential. Treat 24px as the absolute floor for dense UI; the doc itself says aim for the stricter 2.5.5 for important controls. (https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)
- [*] WCAG 2.2 **SC 2.5.5 Target Size (Enhanced), AAA**: "at least **44 by 44 CSS pixels**". (https://www.w3.org/WAI/WCAG22/Understanding/target-size-enhanced.html)
- [*] Tap feedback timing: 0.1 s is the limit for users to feel a system "reacting instantaneously" — pressed-state feedback should appear in **~100ms**, with the action itself following immediately (server-rendered pages → optimistic `:active` styling + disable-on-submit). (https://www.nngroup.com/articles/response-times-3-important-limits/)
- [*] Hover gating: `hover` media feature tests whether the **primary** input can hover; MDN's canonical pattern wraps hover styles in `@media (hover: hover)`; `pointer: fine/coarse` distinguishes mouse vs touch accuracy. Gate all hover-only affordances (card shadows, row action buttons) so touch users don't get sticky/phantom hover. (https://developer.mozilla.org/en-US/docs/Web/CSS/@media/hover, https://developer.mozilla.org/en-US/docs/Web/CSS/@media/pointer)
- [*] `-webkit-tap-highlight-color` is **non-standard** (default black); MDN notes the highlight "indicates to the user that their tap is being successfully recognized" — so if you set it `transparent`, you must add a replacement `:active` state. (https://developer.mozilla.org/en-US/docs/Web/CSS/-webkit-tap-highlight-color)
- [*] 300ms tap delay status: **gone** — removed for mobile-optimized pages (viewport meta `width=device-width`) in Chrome 32 (2014), Firefox & IE/Edge shortly after, and iOS 9.3 (March 2016). Fallback where the viewport tag can't be used: `touch-action: manipulation` (disables double-tap-to-zoom, so no click delay), though the 2014 article notes Safari didn't support it then; it is widely available since Sep 2019 per MDN. (https://developer.chrome.com/blog/300ms-tap-delay-gone-away, https://developer.mozilla.org/en-US/docs/Web/CSS/touch-action)
- [D][M][F][S][C] Row/card sizing derived from the above: every tappable row **≥48px tall**, adjacent actions **≥8px apart**; icon-only buttons get padding to reach 48px; anything below 44px only if spacing makes its 24px circle non-intersecting (WCAG AA spacing exception).

## 3. Copy-paste CSS

```css
/* Touch targets: 48px floor app-wide (Material 48dp; iOS 44pt) */
.btn, button, .nav-tab, .row-action {
  min-height: 48px; min-width: 48px;
  display: inline-flex; align-items: center; justify-content: center;
}
.icon-btn { padding: 12px; }          /* 24px icon + 12px×2 = 48px */
.list-row { padding: 12px 16px; }     /* keeps dense rows ≥48px tall */
```
```css
/* Gate hover behind a real pointer */
@media (hover: hover) and (pointer: fine) {
  .card:hover { box-shadow: 0 2px 8px rgb(0 0 0 / .15); }
  .list-row .row-action { opacity: 0; }   /* hover-reveal only on desktops */
  .list-row:hover .row-action { opacity: 1; }
}
```
```css
/* Pressed feedback that replaces the tap highlight */
* { -webkit-tap-highlight-color: transparent; }
.btn, .card, .nav-tab {
  transition: transform .1s ease, opacity .1s ease;  /* ~100ms = feels instant */
}
.btn:active, .card:active, .nav-tab:active { transform: scale(.97); opacity: .85; }
html { touch-action: manipulation; }   /* + keep <meta name="viewport" content="width=device-width"> */
```
```css
/* Fixed bottom tab bar shell (4–5 destinations) */
.bottom-nav {
  position: fixed; inset: auto 0 0 0; height: 64px;   /* M3 flexible bar = 64dp */
  display: flex; background: var(--surface);
  padding-bottom: env(safe-area-inset-bottom);        /* details in sibling brief */
}
.bottom-nav a { flex: 1; min-height: 48px; display: grid; place-items: center;
  font-size: 12px; }
.bottom-nav a[aria-current="page"] { color: var(--primary); }  /* active indicator */
```

## 4. Notes / caveats

- [F] Flows builder & [S] settings: dynamic rows and 55 fields → 48px rows, 8px+ gaps, and never rely on hover for reorder/delete controls; `touch-action: none` on drag handles if implementing drag (and keep pinch-zoom possible elsewhere).
- [C] Chat floating button: ≥48px, anchored bottom-right above the safe area; the bottom tab bar and the FAB can coexist (M3: FAB sits above the nav bar).
- [unverified] iOS Safari `:active` reliability without a touch listener — pattern not verified against a retrieved source this pass; test on-device.

SOURCES FETCHED:
- https://www.nngroup.com/articles/hamburger-menus/
- https://www.nngroup.com/articles/mobile-navigation-patterns/
- https://www.nngroup.com/articles/response-times-3-important-limits/
- https://m3.material.io/components/navigation-bar/guidelines
- https://m3.material.io/components/navigation-bar/specs
- https://raw.githubusercontent.com/material-components/material-components-android/master/docs/components/BottomNavigation.md
- https://m2.material.io/components/bottom-navigation
- https://m2.material.io/design/usability/accessibility.html
- https://developer.apple.com/design/human-interface-guidelines/tab-bars
- https://developer.apple.com/design/human-interface-guidelines/accessibility
- https://developer.apple.com/design/human-interface-guidelines/layout
- https://www.uxmatters.com/mt/archives/2013/02/how-do-users-really-hold-mobile-devices.php
- https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html
- https://www.w3.org/WAI/WCAG22/Understanding/target-size-enhanced.html
- https://developer.mozilla.org/en-US/docs/Web/CSS/@media/hover
- https://developer.mozilla.org/en-US/docs/Web/CSS/@media/pointer
- https://developer.mozilla.org/en-US/docs/Web/CSS/touch-action
- https://developer.mozilla.org/en-US/docs/Web/CSS/-webkit-tap-highlight-color
- https://developer.chrome.com/blog/300ms-tap-delay-gone-away


===== NEXT BRIEF =====


# Research brief: mobile safe areas/viewport (A) + forms/inputs (B) — for Flask+Jinja server-rendered retrofit

Tag legend: [D] dashboard · [M] messages · [F] flows · [S] settings · [C] chat · [*] app-wide. "Derived" = standard pattern composed from cited mechanics, not verbatim in one source.

## A. Safe areas & viewport handling

- [*] For edge-to-edge rendering use `viewport-fit=cover`; default `auto` auto-insets content; with `cover` you must then respect insets yourself. Meta: `<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">` (https://webkit.org/blog/7929/designing-websites-for-iphone-x/, https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/meta/name/viewport)
- [*] `env()` + `safe-area-inset-top/right/bottom/left` are the inset variables; `env()` works anywhere `var()` does (e.g. inside `padding`); names are case-sensitive; inset values are `0` when nothing (toolbar/keyboard) occupies viewport space, else >0. `env()` is Baseline widely available since Jan 2020. (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Values/env, webkit.org/blog/7929)
- [*] Fallback pattern: browsers without `env()` support drop the declaration containing it — always declare a plain fallback rule first, then the `env()` one (webkit.org/blog/7929). `env()` also takes a fallback 2nd arg: `env(safe-area-inset-bottom, 0px)` (MDN env()). For "minimum padding PLUS inset", WebKit's pattern: `@supports (padding: max(0px)) { .post { padding-left: max(12px, env(safe-area-inset-left)); ... } }` (webkit.org/blog/7929)
- [*] Fixed bottom tab bar (exact recipe — fallback line first, env() after):
```css
.bottom-tabs{ position:fixed; left:0; right:0; bottom:0;
  padding:8px;                                              /* fallback */
  padding-left:  calc(8px + env(safe-area-inset-left));
  padding-right: calc(8px + env(safe-area-inset-right));
  padding-bottom:calc(8px + env(safe-area-inset-bottom)); }
main, .scroll-area{ padding-bottom: calc(56px + env(safe-area-inset-bottom)); } /* clear last row */
```
(derived from webkit.org/blog/7929 + MDN env() example)
- [*] Top bar / landscape side insets: `header{ padding-top: env(safe-area-inset-top); }` and `body{ padding-left: env(safe-area-inset-left); padding-right: env(safe-area-inset-right); }` (webkit.org/blog/7929 `.post` example)
- [M][C] Sticky bottom bar (save bar / tab bar) — MDN's own pattern pads the bar itself: `footer{ position:sticky; bottom:0; padding:1em 1em calc(1em + env(safe-area-inset-bottom)); }` (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Values/env)
- [*] Opaque vs translucent under-scroll: the inset strip is filled with the page's `<body>/<html>` background-color by default (webkit.org/blog/7929). Derived practice: opaque bar → give scroll content bottom padding = bar height + inset so last row isn't covered; translucent bar (`backdrop-filter: blur()`) → content intentionally scrolls under, keep the same clearance so the last item clears the bar, and let body background show through the blur.
- [D][M] App shell height: prefer `min-height: 100dvh` (see B) over a body padded by insets; apply safe-area padding to fixed bars and to `<main>` horizontal padding, not a blanket `body{padding:...}` that fights the fixed bars. (derived)

## B. Viewport units & keyboard

- [*] `100vh` breaks on mobile: `vw`/`vh` don't change when URL/tab toolbars show-hide, so `100vh` elements bleed out / cover the viewport when toolbars are shown. New units: `svh`=small (toolbars expanded), `lvh`=large (retracted), `dvh`=dynamic (clamped between), plus `svw/lvw/dvw` etc.; sizes are stable unless viewport resizes. (https://web.dev/blog/viewport-units)
- [*] Support: Chrome/Edge 108+, Firefox 101+, Safari + iOS Safari 15.4+ (not Opera Mini / UC). (https://caniuse.com/viewport-unit-variants)
- [D][C][M] Full-height layout snippet: `.shell{ min-height:100vh; min-height:100dvh; }` — `100dvh` tracks the URL bar smoothly; use `100svh` if you want zero shift while toolbars animate. (derived from web.dev/blog/viewport-units)
- [*] Meta viewport: MDN recommends `width=device-width`; notes `initial-scale=1` is "usually unnecessary" but add it if content overflow causes unwanted shrink (MDN viewport page); WebKit's cover example uses `initial-scale=1, viewport-fit=cover` (webkit.org/blog/7929).
- [*] Do NOT use `maximum-scale=1` / `user-scalable=no`: MDN warns it blocks low-vision zoom; WCAG requires ≥2× (best practice 5×); and iOS 10+ ignores both by default anyway. Fix focus-zoom with 16px inputs (see C). (https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/meta/name/viewport)
- [*] Keyboard & viewports: mobile has a layout viewport (positions `position:fixed`) and a visual viewport (what's visible); the OSK can shrink the visual viewport without affecting the layout viewport. (https://developer.mozilla.org/en-US/docs/Web/API/VisualViewport, https://developer.chrome.com/blog/viewport-resize-behavior)
- [*] Chrome Android ≤107 resized the layout viewport on OSK; since Chrome 108 its default is `resizes-visual` (like Safari iOS): viewport units stay stable and `position:fixed` elements stay put and can be obscured by the keyboard. Group 1 (resize visual only): Safari iOS/iPadOS, Chrome iOS, Edge iOS; group 2 (resize both): Chrome/Firefox/Edge Android. (https://developer.chrome.com/blog/viewport-resize-behavior)
- [*] `interactive-widget` values: `resizes-visual` (default), `resizes-content` (resizes both layout+visual viewport; the initial containing block and viewport units then change), `overlays-content` (no resize). Supported Chrome 108+ and Firefox 132+; NOT implemented in WebKit/Safari (WebKit standards-position issue #65 open). (https://www.htmhell.dev/adventcalendar/2024/4/, https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/meta/name/viewport)
- [C] iOS Safari keyboard quirks: Safari pushes the viewport up rather than resizing it; there are no OSK events — detect via `focusin`/`focusout` on inputs or viewport-size monitoring (resize-monitoring is unreliable due to rotation/zoom/URL bar). (https://martijnhols.nl/blog/how-to-detect-the-on-screen-keyboard-in-ios-safari)
- [C][M][S] Vanilla pattern to keep a chat input / sticky save bar above the keyboard (adapt of MDN's "simulating position:device-fixed" + focus detection):
```js
const vv = window.visualViewport, bar = document.getElementById('savebar');
const fit = () => { const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
  bar.style.transform = `translateY(${-kb}px)`; };
addEventListener('resize', fit); vv.addEventListener('resize', fit);
vv.addEventListener('scroll', fit); addEventListener('focusout', () => setTimeout(fit, 50));
```
(MDN VV example: offset the bar by `visualViewport.offsetTop` and `visualViewport.height - layout height`, plus `scale(1/scale)`; MDN warns device-fixed emulation can flicker while scrolling; martijnhols for the focus-detection caveat.)
- [*] Pragmatic combo: set `interactive-widget=resizes-content` so Chrome/Android + Firefox resize layout → `dvh` and fixed bars adapt natively; keep the `visualViewport` JS as the iOS Safari fallback (Safari ignores the keyword). (derived from htmhell + Chrome blog + MDN)

## C. Forms / inputs

- [S][F][C] iOS auto-zoom: font-size ≥16px → focus is normal; ≤15px → Safari zooms the viewport into the input on focus. Fix = 16px minimum input font; `maximum-scale=1` is not acceptable (see B; also ineffective on iOS 10+). Canonical write-up: https://css-tricks.com/16px-or-larger-text-prevents-ios-form-zoom/ (behavior is a WebKit heuristic; no spec text located).
- [*] Snippet: `input, select, textarea { font-size: 16px; min-height: 44px; }` (+ buttons 44px) — covers zoom fix and target size at once; for 55-field settings, keep 16px on every device rather than media-querying below it. (CSS-Tricks + W3C below)
- [S][F] `inputmode` values (MDN): `none`, `text` (default), `decimal`, `numeric`, `tel`, `search`, `email`, `url`; it is only a keyboard hint — no validation; pair with matching `type`: email→`type=email`, tel→`type=tel`, search→`type=search`, URL→`type=url`; numeric codes → `<input type="text" inputmode="numeric" pattern="\d*">`. (https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Global_attributes/inputmode)
- [F][S] Avoid `type=number`: MDN — only for incremental numbers; not appropriate for postal codes/credit card numbers; risk of accidental increment (spinbutton role), and default `step=1` makes decimals invalid; prefer `inputmode="numeric"`/`"decimal"` + `pattern`. (https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/input/number)
- [C][F][S] `enterkeyhint` (MDN): `enter, done, go, next, previous, search, send`. Use `next` on every field except the last (flows rows, settings cards), `send` on chat input, `go`/`search`/`done` per action; UA may infer a label from `inputmode`/`type`/`pattern` when omitted. (https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Global_attributes/enterkeyhint)
- [S][F] `autocomplete` tokens (MDN): `name`, `email`, `tel`, `postal-code`, `one-time-code`, `username`, `current-password`, `new-password`; space-separated lists, `section-*` prefix to disambiguate duplicates; needs `name`/`id` + a `<form>`. GOV.UK: `autocomplete` satisfies WCAG 1.3.5 Identify Input Purpose. (https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Attributes/autocomplete, https://design-system.service.gov.uk/components/text-input/)
- [S][F] Token/API-key/username fields: `autocapitalize="none" autocorrect="off" spellcheck="false"`. `autocapitalize` values: `none/off`, `sentences/on`, `words`, `characters`; ignored for `url`, `email`, `password` (never autocapped); Chrome/Safari default = sentences. `autocorrect="off"` disables autocorrection; password/email/url are always off; editable fields are on by default. `spellcheck="false"` also has a privacy rationale (text may be sent to third-party spellchecking — "spell-jacking"). (MDN: autocapitalize, autocorrect, spellcheck pages)
- [S][F] Labels/layout: "align labels above the text input they refer to", short, sentence case, no colons, don't use placeholder as label; one question per page recommended (use label as page heading). (https://design-system.service.gov.uk/components/text-input/)
- [S][F] Single-column rows on phones: one field per row, `label{display:block}`, inputs `width:100%` — no side-by-side field pairs in flows rows or the 55-field settings grid below ~600px. (derived; GOV.UK label-above + single-question guidance)
- [*] Target size: WCAG 2.5.5 Target Size (Enhanced), AAA = "at least 44 by 44 CSS pixels"; WCAG 2.5.8, AA = "at least 24 by 24 CSS pixels". Use `min-height:44px` on inputs/buttons/row-delete controls. (https://www.w3.org/TR/WCAG22/)
- [S][F][C] Sticky save bar + open keyboard caveats: on iOS Safari (`resizes-visual` group) fixed elements "remain in place and can be obscured by the OSK" (Chrome blog); with `interactive-widget=resizes-content` a bottom bar lifts with the layout viewport on Chrome/Android+Firefox only. So: pair the sticky save bar with the `visualViewport` `translateY` pattern above, and re-run it on focusout. (Chrome blog + htmhell + MDN VV + martijnhols, derivation)
- [F] Dynamic form rows: attributes are per-element, so when JS clones a row, set `font-size` via CSS (16px), and `inputmode` / `enterkeyhint="next"` / `autocorrect="off"` come along in the cloned markup — no extra JS needed beyond cloning + fixing the last row's enterkeyhint. (derived)

SOURCES FETCHED:
- https://webkit.org/blog/7929/designing-websites-for-iphone-x/
- https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Values/env
- https://web.dev/blog/viewport-units
- https://caniuse.com/viewport-unit-variants
- https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/meta/name/viewport
- https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Global_attributes/inputmode
- https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Global_attributes/autocapitalize
- https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Global_attributes/autocorrect
- https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Global_attributes/enterkeyhint
- https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Global_attributes/spellcheck
- https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Attributes/autocomplete
- https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/input/number
- https://developer.mozilla.org/en-US/docs/Web/API/VisualViewport
- https://developer.chrome.com/blog/viewport-resize-behavior
- https://www.htmhell.dev/adventcalendar/2024/4/
- https://martijnhols.nl/blog/how-to-detect-the-on-screen-keyboard-in-ios-safari
- https://css-tricks.com/16px-or-larger-text-prevents-ios-form-zoom/
- https://design-system.service.gov.uk/components/text-input/
- https://www.w3.org/TR/WCAG22/ (Understanding/target-size-enhanced page was 403-blocked; values cited from the WCAG 2.2 Recommendation itself)


===== NEXT BRIEF =====


# Research brief: (A) mobile overlays (bottom sheets vs modals) & (B) PWA packaging
*The following is the deliverable; no local files were created or modified. All facts below are from the fetched pages listed at the end.*

## A. Overlays: modal bottom sheets vs centered dialogs

**When to use a bottom sheet vs a dialog**
- Reach for a **bottom sheet on phones**: M3 says use bottom sheets at *compact window widths < 600dp* (e.g. phone portrait); content must be "additional or secondary (not the app's main content)". (https://m3.material.io/components/bottom-sheets/overview) [*]
- **Modal vs standard sheet**: modal "appear[s] in front of app content, disabling all other app functionality… remaining on screen until confirmed, dismissed, or a required action has been taken"; standard "display[s] supplementary content without blocking access to the screen's primary content". (https://m3.material.io/components/bottom-sheets/overview) [*]
- M2 guidance still useful: modal sheets are "an alternative to inline menus and simple dialogs on mobile, providing additional room for content, iconography, and actions"; tapping outside dismisses; drag vertically to dismiss. (https://m2.material.io/develop/android/components/bottom-sheet-dialog-fragment) [*]
- Sheet geometry: **28dp top corner radius, max-width 640dp, optional drag handle with accessible 48dp hit target**. (https://m3.material.io/components/bottom-sheets/overview) [*]
- **Do NOT use a sheet for complex/long-lived UI**: NN/g — "don't use a sheet to display [long content]… a sheet is inherently a transient UI element… should not be used for displaying complex content"; "strongly recommend not using a bottom sheet to replace typical page-to-page user flows"; "use bottom sheets only for short interactions". (https://www.nngroup.com/articles/bottom-sheet/) [C][F][S][M]
- NN/g also debunks the reachability argument: "the bottom of the screen is often not the most easily reachable screen region". (https://www.nngroup.com/articles/bottom-sheet/) [*]
- **Full-screen takeover beats a sheet** when the task is complex/input-heavy: M3 full-screen dialog criteria — "components which require keyboard input, such as form fields"; "changes aren't saved instantly"; "components within the dialog open additional dialogs"; full-screen dialogs are compact-breakpoint only; the close "X" should be the only app-bar navigation. (https://m3.material.io/components/dialogs/guidelines) [C][F][S]
- NN/g: if a flow needs multiple steps, "it probably justifies dedicating a full page to it". (https://www.nngroup.com/articles/modal-nonmodal-dialog/) [F][S]
- Component mapping for this app: **[C]** AI chat (page + floating drawer): chat is long-lived, keyboard-heavy, keeps state → treat the mobile drawer as a full-screen takeover/page, not a dismissible sheet (per the two rules above). **[S]** 55-field settings and **[F]** flows builder: full-screen/page (form fields, unsaved changes, sub-dialogs). **[M]** HTML message viewer: full-screen reader; sheet only for quick peek/actions on a list row. **[D]** dashboard: sheets OK for quick filters/actions; stats themselves are main content, not sheet content. [D][M][F][S][C]

**When dialog/sheet interruption is justified (confirmations etc.)**
- NN/g: modals OK for "important warnings… critical errors", "information critical to continuing the current process", breaking a complex workflow into steps (wizards), progressive profiling; avoid for nonessential info and high-stakes flows like checkout; too many nonessential modals destroy trust. (https://www.nngroup.com/articles/modal-nonmodal-dialog/) [*]

**Close affordances & drag handle**
- NN/g: "include a visible Close (or X) button… rather than relying exclusively on the grab handle" (grab handle is easy to ignore; swipe is ambiguous with system gestures; close button aids screen-reader/keyboard). Support hardware/gesture **Back** to dismiss. Never stack sheets. (https://www.nngroup.com/articles/bottom-sheet/) [*]
- M3 accessibility: users must be able to resize without touch gestures — "Drag handles should cycle the bottom sheet through available heights when selected. If a drag handle can't be used, add a button to do this action." (https://m3.material.io/components/bottom-sheets/accessibility) [*]
- Add a visible **grabber** iff the sheet is draggable; it signals drag affordance (M3 "optional drag handle"). (https://m3.material.io/components/bottom-sheets/overview) [*]

**`<dialog>` + `showModal()` as the modern base**
- `showModal()` opens in the top layer, creates `::backdrop`, **traps focus** (focus goes to first focusable element; use `autofocus` to steer it), makes the rest of the page `inert`, closes on **Esc**; `<dialog>` opened this way is implicitly `aria-modal="true"`; "everything other than the `<dialog>` and its contents should be rendered inert using the `inert` attribute… provided by the browser" with showModal. (https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/dialog) [*]
- Backdrop-tap close is NOT default: `closedby` values `any` (light dismiss = tap outside) / `closerequest` (Esc + platform dismiss) / `none`; default for showModal is `closerequest`. (https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/dialog) [*]
- MDN: "include an explicit [close] button"; don't add `tabindex` to `<dialog>`; body scroll is a separate problem from inertness. (https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/dialog) [*]
- Adding `display:flex`/`position:fixed` layout to `<dialog>`: style the `::backdrop` for scrim; modern support is good, but verify your target iOS Safari version [not directly verified here]. [*]

## B. Background scroll lock (iOS Safari)
- `showModal()` blocks interaction/focus but "doesn't reliably prevent background scrolling across all browsers and devices". (https://blog.openreplay.com/stop-page-scrolling-dialog-open/) [*]
- `body{overflow:hidden}` works on desktop but **fails on iOS Safari**. (https://blog.openreplay.com/stop-page-scrolling-dialog-open/) [*]
- **Most reliable: position:fixed + scroll save/restore** — save `window.scrollY`, set wrapper `position:fixed; height:100%; width:100%; overflow:auto`, on close remove class and `window.scrollTo(0, savedY)`; works everywhere including iOS without jank; negative `top` offset variant risks off-screen offset on resize/orientation change — prefer scroll restore. (https://www.jayfreestone.com/writing/locking-body-scroll-ios/) [*]
- CSS-only one-liner for short dialogs: `body:has(.example-dialog[open]) { overflow: hidden; }` — but known caveat: dialog must be short / not need scrolling (also inherits the iOS caveat above). (https://chipcullen.com/how-to-stop-page-scrolling-with-open-dialog/) [*]
- `overscroll-behavior: contain` prevents *scroll chaining*, not scrolling itself; apply to `dialog` and `::backdrop`; Chrome 144+ additionally applies it to non-scrollable scroll containers, but "other browsers haven't fully caught up yet"; needs the dialog to be a scroll container (`overflow:hidden` trick). Use it *alongside* other methods, not as replacement. (https://css-tricks.com/prevent-a-page-from-scrolling-while-a-dialog-is-open/ + https://blog.openreplay.com/stop-page-scrolling-dialog-open/) [*]
- Popover API is not a modal substitute for scroll locking/interaction blocking; stick with `<dialog>`+`showModal()`. (https://blog.openreplay.com/stop-page-scrolling-dialog-open/) [*]
- Recommended combo here: `<dialog>`+`showModal()` for the base + JS scroll save/restore (position:fixed shim) while open + `overscroll-behavior:contain` on dialog/backdrop. [*]

## C. PWA-to-native packaging

**Manifest essentials (Android/Chrome side)**
- Single JSON manifest per app, typically root, linked on **all pages** your PWA can install from: `<link rel="manifest" href="/app.webmanifest">`; official extension `.webmanifest`. (https://web.dev/learn/pwa/web-app-manifest) [*]
- Serve it with correct MIME: "the response of the manifest file should return `Content-Type: application/manifest+json`"; `.json` + `application/json` generally supported; if the manifest needs credentials, `crossorigin="use-credentials"`. (https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Manifest) [*]
- Frequently used members: `name`, `short_name` (keep under 12 chars), `icons` (each `{src,type,sizes,purpose}`; at minimum 192x192 + 512x512; 512x512 is the one-size priority; 384/1024 also recommended), `start_url` (absolute path recommended; don't churn it — changing it "may be removing the browser's ability to detect that a PWA is already installed"), `display: "standalone"`, `id` (identity; falls back to start_url), `theme_color`, `background_color` (pre-CSS placeholder; iOS ignores), `scope`, `orientation`, `screenshots` (`{src,type,sizes}`, no size restrictions), `description`, `shortcuts`. (https://web.dev/learn/pwa/web-app-manifest) [*]
- Maskable icons: `"purpose":"maskable"`, main icon in a safe zone "circle centered in the icon with a radius of 40 percent of the width", ≥512px, or `"any maskable"`. (https://web.dev/learn/pwa/web-app-manifest) [*]
- Android auto-generates the splash from `theme_color`/`background_color`/icon; iOS does not use the manifest for splash screens. (https://web.dev/learn/pwa/web-app-manifest) [*]
- Chrome install criteria (what makes beforeinstallprompt fire): not already installed; engagement (user clicked/tapped ≥ once, spent ≥30s on page, ever); **HTTPS**; manifest with `short_name`/`name`, icons 192px+512px, `start_url`, `display` in fullscreen|standalone|minimal-ui|window-controls-overlay, `prefer_related_applications` absent or false. Note: **no service-worker requirement in the current (fetched) criteria list**. (https://web.dev/articles/install-criteria) [*]
- When criteria met, Chrome fires `beforeinstallprompt` (capture to show your own install button); Lighthouse flags the same manifest list. (https://developer.chrome.com/docs/lighthouse/pwa/installable-manifest) [*]

**iOS specifics (beyond the manifest)**
- iOS Manifest support: `name`,`short_name`,`scope`,`display`,`start_url` (11.3+), `theme_color` (15.0+), `icons` (15.4+), `id` (16.4+). NOT supported: `orientation`, `background_color`, `display_override`, `shortcuts`, `prefer_related_applications`; `minimal-ui` falls back to browser, `fullscreen` falls back to `standalone`; no install prompt/banner at all (Add to Home Screen via Share menu; the added icon is a Web Clip). (https://firt.dev/notes/pwa-ios/) [*]
- Keep `apple-touch-icon` link anyway — it **overrides** manifest icons on iOS; Apple's sizes guidance includes 180x180 (iPhone retina) + 152/167 (iPad); fallback lookup of `apple-touch-icon*.png` in root. (https://developer.apple.com/library/archive/documentation/AppleApplications/Reference/SafariWebContent/ConfiguringWebApplications/ConfiguringWebApplications.html + https://firt.dev/notes/pwa-ios/) [*]
- `apple-mobile-web-app-capable content="yes"` = standalone toggle (legacy; optional since 11.3 if manifest `display:standalone`), **but startup images only work if this meta tag is present**. (https://developer.apple.com/library/archive/.../ConfiguringWebApplications.html + https://firt.dev/notes/pwa-ios/) [*]
- Splash on iOS = static `apple-touch-startup-image` links, one per exact device/window size, `media="orientation: portrait|landscape"` variants; generate with PWA Asset Generator / PWA Compat. (https://web.dev/learn/pwa/enhancements) [*]
- `black-translucent` status bar = full-screen mode with status bar rendered over your content; OS always renders status icons white → keep top backgrounds light, and pad with `env(safe-area-inset-*)`. (https://web.dev/learn/pwa/enhancements) [*]
- iOS detection: `navigator.standalone` (iOS-only): `undefined` = not iOS, `false` = browser, `true` = opened from home screen; standard equivalent is `@media (display-mode: standalone)`. (https://web.dev/learn/pwa/enhancements + https://web.dev/learn/pwa/app-design) [*]
- iOS < 15.4: manifest only loaded when the Share sheet opens — it may be missed and A2HS yields a plain shortcut; keep `apple-*` tags as belt-and-braces. (https://web.dev/learn/pwa/enhancements) [*]

**theme-color meta**
- `<meta name="theme-color" content="#4285f4">`; add `media` with `(prefers-color-scheme: light|dark)` variants for dark-mode status bar. (https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/meta/name/theme-color) [*]

**What changes in standalone mode**
- No browser navigation UI (no URL bar/button bar on iOS); on mobile the status bar stays visible; `display-mode` media query lets you adapt (e.g. show an install hint only in browser mode; back button in standalone). (https://web.dev/learn/pwa/app-design) [*]
- Pull-to-refresh: "on some browsers, such as Chrome on Android, that behavior is also enabled on standalone PWAs". (https://web.dev/learn/pwa/app-design) [*]
- Use `viewport-fit=cover` + `safe-area-inset-*` env vars when going edge-to-edge. (https://web.dev/learn/pwa/app-design) [*]
- `<title>` becomes the window title/app-switcher label — make it meaningful. (https://web.dev/learn/pwa/app-design) [*]

**Minimal manifest (copy-paste; serve at /static/app.webmanifest with `application/manifest+json`)**
```json
{
  "id": "/", "name": "My App", "short_name": "MyApp",
  "start_url": "/", "scope": "/", "display": "standalone",
  "theme_color": "#111111", "background_color": "#ffffff",
  "orientation": "portrait",
  "icons": [
    { "src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png" },
    { "src": "/static/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
  ]
}
```
(`orientation` is ignored on iOS; it's for Android. `background_color` ignored on iOS.) [*]

**iOS head block (copy-paste into base template)**
```html
<link rel="manifest" href="/static/app.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="MyApp">
<link rel="apple-touch-icon" sizes="180x180" href="/static/icons/icon-180.png">
<link rel="apple-touch-startup-image" href="/static/splash/iphone-portrait.png" media="(orientation: portrait)">
<link rel="apple-touch-startup-image" href="/static/splash/iphone-landscape.png" media="(orientation: landscape)">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#111111" media="(prefers-color-scheme: dark)">
```
Keep `apple-mobile-web-app-capable` even though optional since 11.3 — startup images require it (firt.dev). [*]

SOURCES FETCHED:
https://m3.material.io/components/bottom-sheets/overview
https://m3.material.io/components/bottom-sheets/accessibility
https://m3.material.io/components/dialogs/guidelines
https://m2.material.io/develop/android/components/bottom-sheet-dialog-fragment
https://www.nngroup.com/articles/bottom-sheet/
https://www.nngroup.com/articles/modal-nonmodal-dialog/
https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/dialog
https://blog.openreplay.com/stop-page-scrolling-dialog-open/
https://www.jayfreestone.com/writing/locking-body-scroll-ios/
https://chipcullen.com/how-to-stop-page-scrolling-with-open-dialog/
https://css-tricks.com/prevent-a-page-from-scrolling-while-a-dialog-is-open/
https://web.dev/learn/pwa/web-app-manifest
https://web.dev/learn/pwa/installation
https://web.dev/learn/pwa/enhancements
https://web.dev/learn/pwa/app-design
https://web.dev/articles/install-criteria
https://developer.chrome.com/docs/lighthouse/pwa/installable-manifest
https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Manifest
https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/meta/name/theme-color
https://firt.dev/notes/pwa-ios/
https://developer.apple.com/library/archive/documentation/AppleApplications/Reference/SafariWebContent/ConfiguringWebApplications/ConfiguringWebApplications.html

**Summary:** Produced the citation-backed brief above (overlays + scroll lock + PWA packaging) from 21 fetched sources; key actionable defaults: bottom sheets only for short/quick interactions, full-screen page takeovers for chat/flows/settings, `<dialog>`+`showModal()` as the base with JS scroll save/restore for iOS, and the manifest + iOS meta blocks supplied verbatim. No files created or modified; all values verified against the fetched pages (unverified items avoided).


===== NEXT BRIEF =====


# Mobile retrofit brief — scrolling/gestures, visual polish, performance hygiene
Target: Flask+Jinja server-rendered app, vanilla JS, iPhone/Android, PWA install. Tags: [D] dashboard [M] messages [F] flows [S] settings [C] chat [*] app-wide.

## A. SCROLLING & GESTURES

- **iOS momentum: `-webkit-overflow-scrolling` no longer needed.** Apple: "Added support for one-finger accelerated scrolling to all frames and `overflow:scroll` elements eliminating the need to set `-webkit-overflow-scrolling: touch`" (Safari 13 / iOS 13+). Don't add it; it's a legacy no-op for current iOS. (https://developer.apple.com/tutorials/data/documentation/safari-release-notes/safari-13-release-notes.json) [*]
- **Nested scroll containers + scroll chaining.** Default (`auto`) chains scroll to ancestors when inner scroller hits its boundary — MDN's own chat example: `.messages { height:220px; overflow:auto; overscroll-behavior-y: contain }`. Keep one primary scroller per screen (drawer chat, message list, log page). (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/overscroll-behavior) [C,M,M/log]
- **`overscroll-behavior` values.** `auto` (default, chaining) | `contain` (bounce stays in element, no chaining; "disables native browser navigation, including the vertical pull-to-refresh gesture and horizontal swipe navigation") | `none` (no chaining AND suppresses bounce/overflow effects). One value applies to both axes; only applies to scroll containers; on a non-scrollable `overflow:hidden` container `contain`/`none` still blocks chaining to ancestors — MDN: usable "to prevent background scrolling while a dialog or overlay is open". (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/overscroll-behavior) [C,*]
- **Chrome-Android pull-to-refresh can be disabled.** `html { overscroll-behavior: none }` prevents Chrome-Android's refresh-when-scrolled-past-top (MDN example); web.dev PWA guide: use `body { overscroll-behavior-y: contain }` in the installed app (Chrome Android applies PTR in standalone PWAs too). (MDN overscroll page; https://web.dev/learn/pwa/app-design) [*]
- **overscroll-behavior support.** Chrome 63▹65, Edge 79, Firefox 59, Samsung 8.2; **Safari & iOS Safari 16.0+** (14.1–15.6 shipped disabled-by-default). MDN still labels it "Limited availability" (older Safari). Safe to ship as progressive enhancement. (https://caniuse.com/css-overscroll-behavior) [*]
- **iOS PTR reality.** Safari-tab pull-to-refresh is browser chrome; no fetched CSS source controls it on iOS [unverified]. In standalone/installed iOS web apps PTR is reported to be unavailable/already suppressed — secondary evidence only. Assume: don't fight PTR; provide an explicit in-app refresh button instead. (https://stackoverflow.com/questions/75972895/ios-pwa-how-to-re-enable-pull-to-refresh) [*]
- **Sticky header pitfalls (MDN position).** `sticky` "sticks to its nearest ancestor that has a 'scrolling mechanism' (overflow: hidden/scroll/auto/overlay) even if that ancestor isn't the nearest actually scrolling ancestor" — a decorative `overflow:hidden` wrapper silently breaks it; a `top` inset is required or it behaves as `relative`; sticky always creates a new stacking context (give the header an explicit `z-index` above page content). (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/position) [*]
- **Sticky repaint cost.** Scrolling elements with fixed/sticky content can jank ("browser may not be able to manage repaints at 60 fps"); MDN suggests `will-change: transform` on the stuck element — apply narrowly, per MDN will-change rules. (MDN position; https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/will-change) [*]
- Sticky header + anchor-offset snippet [*, S (55-field page anchors), M (viewer deep links)]:
```css
.app-header { position: sticky; top: 0; z-index: 100; background: var(--bg); }
h2[id], .field[id] { scroll-margin-top: 56px; } /* scroll target lands below sticky bar */
```
`scroll-margin-top` defines the outset used when aligning the box (Baseline Apr 2021); `scroll-padding` is the equivalent set on the scroll port. (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/scroll-margin-top; https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/scroll-snap-type)
- **Horizontal chip rows: scroll-snap.** `scroll-snap-type: x proximity` on the row + `scroll-snap-align: start` on chips; `mandatory` = "must snap to a snap position if it isn't currently scrolled", `proximity` = UA decides (safer for chips). Baseline since Apr 2022. (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/scroll-snap-type) [D,S,flows chips]
```css
.chips { display:flex; gap:8px; overflow-x:auto; scroll-snap-type: x proximity; }
.chips > * { flex: 0 0 auto; scroll-snap-align: start; }
```
- **Horizontal swipe areas: `touch-action: pan-y`** so vertical page scroll survives; Chrome guidance: "if you have a horizontal carousel consider applying `touch-action: pan-y pinch-zoom`". Value is intersected up the ancestor chain (apply on the custom-behavior element); changes mid-gesture don't apply. (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/touch-action; https://developer.chrome.com/blog/scrolling-intervention) [F,C]
- **Scroll anchoring for chat.** Browser auto-adjusts scroll to minimize content shifts; enabled by default; opt out with `overflow-anchor: none` if it fights your JS autoscroll. Baseline 2026 (newly available Sept 2026). (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/overflow-anchor) [C]
```css
.transcript { overflow-anchor: none; } /* your follow-bottom JS owns scroll position */
```

## B. VISUAL POLISH

- **System font stack = instant native feel, zero font download.** web.dev PWA snippet [*]:
```css
:root { --font-ui: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui,
        Roboto, Oxygen-Sans, Ubuntu, Cantarell, "Helvetica Neue", sans-serif; }
body { font-family: var(--font-ui); }
```
`system-ui` generic = platform default UI font; use it in `font-family` (not the `font` shorthand — vendor-prefix parsing issue) and it's "intended to make UI elements look like native apps". (https://web.dev/learn/pwa/app-design; https://css-tricks.com/snippets/css/system-font-stack/; https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/font-family) [*]
- **Dark mode plumbing.** `:root { color-scheme: light dark; }` + `<meta name="color-scheme" content="light dark">` placed before CSS (prevents wrong-color flash); `color-scheme` makes the UA adapt "canvas surface, default colors of scrollbars… form controls" — key for a 55-field settings form. Style with `prefers-color-scheme` + CSS custom properties, named semantically (`--accent-color`, not `--highlight-yellow`). Support: Chrome/Edge 76+, Firefox 67+, Safari 12.1 macOS / 13 iOS. (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/color-scheme; https://web.dev/articles/prefers-color-scheme) [*]
- **Theme-color dark variant** (tints iOS status bar / Android URL bar): two metas with `media`; runtime updates via `matchMedia('(prefers-color-scheme: dark)')` change listener. Chromium 93+/Safari 15+. (https://web.dev/articles/prefers-color-scheme; https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/meta/name/theme-color) [*]
```html
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#ffffff">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#111111">
```
- **prefers-reduced-motion — what to disable, and the safe snippet.** Disable decorative/reveal/parallax/autoplay motion; CSS reacts automatically, JS animations must listen via `matchMedia('(prefers-reduced-motion: reduce)')` and stop. Don't use `animation: none !important` (breaks code waiting on `animationend`); web.dev's global override [*, C, F]:
```css
@media (prefers-reduced-motion: reduce) {
  *, ::before, ::after {
    animation-duration: 1ms !important; animation-iteration-count: 1 !important;
    transition-duration: 1ms !important; animation-delay: -1ms !important; transition-delay: -1ms !important;
    scroll-behavior: auto !important;
  }
}
```
(https://web.dev/articles/prefers-reduced-motion)
- **State transitions.** Animate compositor-friendly properties only: "use the `transform` and `opacity` CSS properties as much as possible, and avoid anything that triggers layout or paint"; restrict to the composite stage. Keep micro-transitions short (~100–300 ms; exact range not in fetched sources [unverified]); M3 CSS easing tokens: standard `cubic-bezier(0.2, 0, 0, 1)`, decelerate `cubic-bezier(0, 0, 0, 1)`, accelerate `cubic-bezier(0.3, 0, 1, 1)`, emphasized decelerate `cubic-bezier(0.05, 0.7, 0.1, 1.0)`. (https://web.dev/articles/animations-guide; https://m3.material.io/styles/motion/easing-and-duration/tokens-specs) [*, F rows, D cards]
```css
.card, .btn { transition: transform .2s cubic-bezier(0.2,0,0,1), opacity .2s cubic-bezier(0.2,0,0,1); }
.card:active { transform: scale(.98); }
```
- **Tap highlight & focus.** Kill the default tap flash with `-webkit-tap-highlight-color: transparent` (non-standard but ubiquitous) and supply your own `:active` feedback; use `:focus-visible` (Baseline Mar 2022) so pointer taps don't show rings while keyboard/script focus still does; keep indicator ≥3:1 contrast (WCAG). (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/-webkit-tap-highlight-color; https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Selectors/:focus-visible) [*]
```css
* { -webkit-tap-highlight-color: transparent; }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
```
- **PWA extra:** disable text selection on nav/buttons (`user-select: none` + `-webkit-` prefix) — content stays selectable. (https://web.dev/learn/pwa/app-design) [*]

## C. PERFORMANCE HYGIENE

- **Passive event listeners.** Cancelable `touchstart`/`touchmove`/`wheel` listeners block scrolling until they finish ("browser needs to wait for the event to finish"); pass `{passive: true}`. Chrome 56+ auto-defaults window/document/body touch listeners (preventDefault ignored → DevTools warning "Unable to preventDefault inside passive event listener…"); prefer `touch-action` over preventDefault for gesture intent. Verify with Lighthouse audit "uses-passive-event-listeners". (https://developer.chrome.com/blog/scrolling-intervention; https://developer.chrome.com/docs/lighthouse/best-practices/uses-passive-event-listeners) [C,F,M]
```js
el.addEventListener('touchstart', onTouchStart, { passive: true });
el.addEventListener('wheel', onWheel, { passive: true });
```
- **Avoid layout thrash.** Never read geometry inside a write loop (`el.offsetWidth` per iteration forces sync layout each pass); "read style values then make style changes"; layout cost scales with DOM size (55-field settings, long lists, dynamic flow rows). Detect via DevTools "Forced reflow" insight or LoAF `forcedStyleAndLayoutDuration`. (https://web.dev/articles/avoid-large-complex-layouts-and-layout-thrashing) [F,S,M,C]
- **Batch DOM writes with `requestAnimationFrame`** (runs before next repaint; one-shot, re-request per frame; paused in background tabs) — use for flow-builder row insertions and chat appends:
```js
let queued = false;
const schedule = (fn) => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; fn(); }); } };
```
(https://developer.mozilla.org/en-US/docs/Web/API/Window/requestAnimationFrame) [F,C]
- **CSS animations over JS-driven per-frame work**, kept on transform/opacity; use `will-change` only "as a last resort" — not on many elements, not on large sections (e.g. `body`), set/remove from JS around the change; non-auto values can create a stacking context up front. (https://web.dev/articles/animations-guide; https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/will-change) [*, D]
- **content-visibility for long lists.** `content-visibility: auto` skips offscreen rendering (gains layout/style/paint containment; offscreen also size containment); **must** pair with `contain-intrinsic-size` ("acts as a placeholder size") or scrollbars jump. Offscreen content stays in DOM + a11y tree (find-in-page kept). Baseline 2024 (Sept 2024) → Safari 18+; older browsers ignore it gracefully. Best on message list / log rows, not on the pinned chat transcript; use `auto 500px`-style placeholders. (https://web.dev/articles/content-visibility; https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/content-visibility) [M, S/logs]
```css
.msg-row { content-visibility: auto; contain-intrinsic-size: auto 120px; }
```
- **IntersectionObserver instead of scroll listeners.** Async observation of a target vs root/viewport; designed for "lazy-loading…, implementing 'infinite scrolling' [and] reporting visibility", explicitly contrasted with scroll-handler intersection checks that fire constantly on the main thread (MDN's infinite-scroll example). Options: `root`, `rootMargin`, `threshold`; use it for "reached bottom" in chat/messages and lazy rendering. (https://developer.mozilla.org/en-US/docs/Web/API/Intersection_Observer_API) [M,C,D]
- **`contain` for isolated subtrees** (cards, chat bubbles, form sections): `contain: layout paint` limits layout/style/paint scope; caution — layout/paint/strict/content create a new containing block (abs/fixed descendants!), new stacking context, new BFC. Baseline Mar 2022. (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/contain) [D,S,C]
- **`touch-action: manipulation` on all tappable controls** — pan + pinch-zoom retained, double-tap-to-zoom disabled, which "removes the need for browsers to delay the generation of click events" → snappier taps; also enables instant button press feedback. (https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/touch-action) [*]

SOURCES FETCHED:
https://developer.apple.com/tutorials/data/documentation/safari-release-notes/safari-13-release-notes.json
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/overscroll-behavior
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/touch-action
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/scroll-snap-type
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/scroll-margin-top
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/overflow-anchor
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/position
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/content-visibility
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/contain
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/will-change
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/color-scheme
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/font-family
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/-webkit-tap-highlight-color
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/overflow
https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Selectors/:focus-visible
https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/meta/name/theme-color
https://developer.mozilla.org/en-US/docs/Web/API/Intersection_Observer_API
https://developer.mozilla.org/en-US/docs/Web/API/Window/requestAnimationFrame
https://web.dev/articles/avoid-large-complex-layouts-and-layout-thrashing
https://web.dev/articles/content-visibility
https://web.dev/articles/prefers-reduced-motion
https://web.dev/articles/prefers-color-scheme
https://web.dev/articles/animations-guide
https://web.dev/learn/pwa/app-design
https://developer.chrome.com/blog/scrolling-intervention
https://developer.chrome.com/docs/lighthouse/best-practices/uses-passive-event-listeners
https://caniuse.com/css-overscroll-behavior
https://webkit.org/blog/13152/webkit-features-in-safari-16-0/
https://css-tricks.com/snippets/css/system-font-stack/
https://m3.material.io/styles/motion/easing-and-duration/tokens-specs
https://stackoverflow.com/questions/75972895/ios-pwa-how-to-re-enable-pull-to-refresh

---

**Summary for parent:** Deliverable above — a 3-section, citation-tagged (D/M/F/S/C/*) brief with exact snippets (system font stack, sticky header + scroll-margin-top, overscroll behavior, reduced-motion override, passive listener, content-visibility, scroll-snap chips, M3 easing). Key verified findings: `-webkit-overflow-scrolling` is unnecessary since iOS 13 (Apple Safari 13 release notes); `overscroll-behavior` semantics + Safari 16.0 support floor (caniuse); sticky-header pitfalls per MDN (overflow ancestors, stacking context); Lighthouse passive-listener syntax/verification; content-visibility Baseline 2024 (Safari 18+) with `contain-intrinsic-size` requirement; scroll anchoring Baseline 2026. Two items flagged [unverified]/secondary: iOS PTR control in Safari tabs (forum-level evidence only) and the 100–300 ms transition duration range. Failed fetches (excluded from citations): web.dev's old passive-listener URL (404), MDN `-webkit-overflow-scrolling` page (404, now removed), Apple HIG Typography (bot-blocked). No files created/modified.