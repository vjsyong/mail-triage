# List & table pages — per-page layout brief (research + recommendations)

Scope: `/rules`, `/flows`, `/classifiers`, `/classifiers/<id>/dataset`, `/templates`,
`/accounts`, `/log`, `/proxy/log`. Companion to `docs/ui-redesign.md`,
`docs/dashboard-mobile.md`, `docs/messages-mobile.md` — this brief keeps the
established design language (Vercel-style **light** theme, **square corners**
(deliberate — never round them), Geist/Geist Mono, black log panels, blue links
`#0070f3`, WCAG 2.2 AA) and the established responsive shells (phone ≤767px:
topbar + bottom tab bar, one-line horizontally scrollable chip rows, compact
pagers; desktop: 236px left sidebar + data tables).

Method: two passes. First pass drafted from the core sources (NN/g *Mobile
Tables*, NN/g *Confirmation Dialogs*, M3 *Lists*, M3 *Chips*, Pencil & Paper
*Enterprise Data Tables*); second pass added empty-state, gesture, reordering
and logging sources (all fetched 2026-10-01, listed at the bottom). Every
recommendation names one source; template/line references are to
`app.py`.

## Shared rules (apply to every page here)

1. **Table → cards on phones, never a horizontally squeezed table.** At
   ≤767px each `.tbl.mcards` row becomes a card; keep 2–3 meaningful values per
   card line and fold the rest into one dim meta line — wordy entries fit only
   ~2 legible columns on a phone (NN/g, *Mobile Tables*). Horizontal scroll is
   only acceptable inside genuinely wide technical content (the proxy log
   `pre`), and then the first column should stick (same source).
2. **Row anatomy: one fact per line, ~3–4 lines max before top-alignment.**
   L1 identity (name/subject) + timestamp; then state (badge); then meta.
   Top-align once a card passes ~3–4 lines (Pencil & Paper, *Enterprise Data
   Tables*); keep element order identical from row to row (M3, *Lists*).
3. **Row actions on touch: one visible primary + overflow menu; no swipe-only
   actions.** Touch has no hover, and `.rowacts` already becomes permanently
   visible under `@media(hover:none)`. Secondary actions belong in a persistent
   kebab/`More` menu (Pencil & Paper, *Enterprise Data Tables*; uxpatterns.dev
   *Data Table Pattern*, via `docs/ui-redesign.md`). Swipe is not a substitute:
   gestures are invisible, so interfaces must visually signal that a gesture
   exists — anything reachable by swipe must also be reachable by a visible
   control (Material Design, *Gestures*).
4. **Destructive actions: confirm only the irreversible; name the object and
   the consequence; verb-labelled buttons; no default Yes.** (NN/g,
   *Confirmation Dialogs Can Prevent User Errors*.) Where the action is
   reversible (dataset remove → re-include), skip the dialog and use the
   existing flash toast — the guidance prefers action + undo over warning
   fatigue (same source; the dataset page already behaves this way).
5. **Order is semantics** (rules and flows run top-to-bottom, first match wins).
   Show the position number, and keep precise move controls (⤒/↑/↓ buttons)
   rather than drag-and-drop: drag-and-drop is not accessible and WCAG 2.2 SC
   2.5.7 (Dragging Movements, AA) requires every drag operation to have a
   non-dragging alternative; the button-only approach is one click and works
   for mouse, touch and keyboard (Darin Senneff, *Designing a reorderable list
   component*). Drag-and-drop may only ever be an optional bonus on top
   (Pencil & Paper, *Drag & Drop UX*). The existing `.seg` move group is right.
6. **Filters are chips in one non-wrapping, horizontally scrollable row**
   (phone), with an `active` state — the messages toolbar convention
   (`docs/messages-mobile.md`). Chips are the standard container for "tags or
   descriptive words to filter content", an alternative to toggles (M3,
   *Chips*).
7. **Empty states are teaching moments:** they should communicate system
   status, increase learnability and deliver a direct path to the key task
   (NN/g, *Empty States in Application Design: 3 Guidelines*); structure =
   headline → one-line explanation → one prominent CTA, and they must match the
   moment (first-run vs cleared vs no-results) (Eleken, *Empty state UX*). The
   app already does this on rules/classifiers/templates/accounts — keep the
   skeleton for every new empty case, and keep CTAs keyboard-usable with
   contrast (same source: never colour-only, no jargon).
8. **Counts carry context.** Every truncated list says what it shows
   ("showing the first N of Y", "page X of Y") — pagination-with-totals is an
   established repo convention (uxpatterns.dev, via `docs/ui-redesign.md`) and
   context-around-numbers is the dashboard rule (`docs/dashboard-mobile.md`).
9. **Bulk actions (shared pattern, only where multi-item acts exist):**
   selection is explicit (always-visible checkboxes ≥24px, 44px on touch); the
   contextual bulk toolbar appears only once something is selected and stays
   pinned while scrolling (Pencil & Paper, *Enterprise Data Tables*). The
   messages page already implements this (`docs/messages-mobile.md`); these
   pages currently have no multi-item act, so do not add selection speculatively.

## /rules — rule list + tester

**The one question: "What happens to my mail, in which order — and is the rule
set doing what I think?"** Order and effect are the product; management actions
serve them.

Desktop (verify unchanged): 5-column table (`#`, rule, matches, actions,
row-actions). Keep the sticky header (Pencil & Paper, *Enterprise Data
Tables*); the position `#` is the rule's priority — make that legible as a
plain numeral (Darin Senneff: ordered lists carry explicit meaning in their
order); disabled rows stay dimmed at `.55` opacity with the `disabled` badge
(existing; opacity alone is not the signal — badge text carries it, WCAG via
`docs/ui-redesign.md`). Keep the `.seg` move group; keep `delete` last and
right-aligned.

Phone (≤767px):
- Row becomes a 4-line card: **L1** `#1 · name` + `disabled` badge; **L2**
  match summary (mono, wraps — it is a sentence, not ellipsis fodder); **L3**
  action summary (dim); **L4** action row. Fewer, legible facts per card is the
  whole point (NN/g, *Mobile Tables*).
- Action row: `⤒ ↑ ↓` stay as 44px targets beside the labelled
  `enable/disable` button (reordering is the frequent precision task here);
  `edit` + `delete` move into a `details.menu` overflow so five controls never
  wrap into ragged rows (Pencil & Paper, *Enterprise Data Tables*: secondary
  actions behind the 3-dot; WCAG 2.2 target size via `docs/ui-redesign.md`).
- Tester: put "Test against last N messages" in its own full-width action row
  under the header on phones (the messages toolbar split: filters vs actions,
  `docs/messages-mobile.md`), and render the results card **above** the list —
  it is the reason the user tapped (NN/g, *Mobile Tables*: bring the requested
  subset into view).
- Dry-run results need count context: state "of the last {{ test_limit }}
  messages" in the card sub-line (dashboard convention: never an isolated
  number, `docs/dashboard-mobile.md`).

Concrete changes (`RULES_TMPL` ~1705):
- Scope phone CSS `#rules .tbl.mcards …` under `@media(max-width:767px)`: make
  the `#` + name cells share a flex line (`td:nth-child(1),td:nth-child(2)`
  inline), keep `.rowacts{flex-wrap:wrap;min-height:44px}`.
- Wrap `edit`/`delete` in `<details class="menu">` (reuse the ACCOUNTS_TMPL
  pattern ~4718) — desktop keeps them inline via a `@media(min-width:768px)`
  override or by rendering both.
- Keep `aria-label`/`title` on move buttons (present but inconsistent —
  unify), and keep the delete `confirm('Delete rule …?')` verb label.
- Do not rename any strings: `tests/mock_e2e.py` pins exact copy ("Dry-run
  test", "not authorised", etc. — see §Test cautions).

## /flows — automation list

**The one question: "Which WHEN→THEN automations exist, what exactly do they
do, and are they on?"** The summary sentence is the row.

Desktop: keep one card per flow (few items; a grid would fragment the
sentence). Keep `name + badge`, then the `IF … → …` summary, then `last ran`.
Keep actions top-right and persistent — with a short list, learnability beats
cleanliness and hover-reveal hides state changes (M3, *Lists*: consistent,
scannable format; Pencil & Paper: discoverability of row actions).

Phone:
- Same card, re-cut: L1 name + `disabled` badge; L2 the IF→THEN sentence
  wrapped (never ellipsised — it is the entire value of the row); L3
  `last ran …`; L4 action row (M3, *Lists*: items short and easy to scan).
- Action row: `Enable/Disable` labelled button stays visible; `↑ ↓ Edit
  Delete` fold into the overflow menu (Pencil & Paper, *Enterprise Data
  Tables*). Move buttons must render `disabled` at list ends — a disabled
  control explains the model better than a no-op tap (NN/g, *10 Usability
  Heuristics*: visibility of system status).
- "last ran" is honest-status trivia worth keeping on phones (exception-first
  thinking, `docs/dashboard-mobile.md`) — keep it even though it competes with
  the summary.

Concrete changes (`FLOWS_TMPL` ~2219):
- Replace the five-button `.row` in `.card-h` with `.seg` (↑↓) + toggle button
  + `details.menu` (Edit, then Delete as `menu-item danger`) — mirror
  ACCOUNTS_TMPL ~4715-4731.
- Add server-side `disabled` at first/last row for the move forms (the
  `flow_move` handler can keep no-op'ing; UI should prevent it).
- Phone CSS: `.card-h{flex-wrap:wrap}` handling so title + actions don't
  collide; reuse `.badge`/`.sub`.
- Keep the empty state copy ("A flow is 'when a message matches… then do
  several things, in order'") — it is a model empty state (Eleken: explain the
  concept at the empty moment).

## /classifiers — heuristics list

**The one question: "Which trained heuristics exist, are they healthy, and what
do I do next — review dataset, retrain, disable?"** Samples/labels/confidence
are the decision data; `kind` and `matches on` are reference.

Desktop: keep the 8-column table but treat two columns as secondary: if it
feels wide, move `kind` + `matches on` into an expandable row-detail (Pencil &
Paper, *Enterprise Data Tables*: secondary data belongs in a row-details view).
Keep the sticky header; `updated` stays last and dim; name + badge first.

Phone:
- Collapse to a card: L1 `name` + `disabled`/`weak` badges; L2 meta line
  `category · N samples (−M removed) · min conf …`; L3 `updated`; L4 actions
  (NN/g, *Mobile Tables*: only ~2 legible columns' worth of text per card).
- `description[:170]` must not render as its own phone line: clamp with the
  messages 2-line `-webkit-line-clamp` pattern or hide on phones — it is the
  longest cell and duplicates `kind`/`category` (`docs/messages-mobile.md`
  clamp convention; NN/g, *Mobile Tables* on wordy cells).
- Actions: `dataset` stays a visible labelled link (the primary review path);
  `enable/disable`, `retrain`, `delete` go in the overflow (Pencil & Paper:
  one primary + overflow; M3, *Lists*: find item → act on it).
- `retrain` is a slow submit: give it an in-flight disabled state (repo
  convention: submit-disable while in-flight, `docs/ui-redesign.md`) and keep
  the retrain flash ("Classifier '…' retrained.") as the success signal.
- `delete` keeps its confirm — irreversible (NN/g, *Confirmation Dialogs*).

Concrete changes (`CLASSIFIERS_TMPL` ~1766):
- Add `.mhide` spans so `kind`, `labels`, `matches on` collapse into one dim
  meta line under 767px (same technique the messages pass used for shortened
  labels, `docs/messages-mobile.md`).
- Add phone CSS scoped `#classifiers .tbl.mcards` (flex-wrap rows, 44px
  `.rowacts` targets, clamp class on the description).
- Wrap `enable/disable`, `retrain`, `delete` in a `details.menu`; keep
  `dataset` inline as `.btn small`.
- Keep the empty-state and its "Go to messages" CTA (NN/g, *Empty States*:
  direct pathway to the key task).

## /classifiers/<id>/dataset — sample table with inline actions

**The one question: "Is this classifier trained on the right examples — and can
I fix individual samples fast?"** Review + corrective editing is the whole job;
the label select is the core control.

Desktop: keep the two section tables (In the set / Out of set — the
precision/recall split is real information). Recommendations: sticky section
headers as lists scroll (Pencil & Paper, *Enterprise Data Tables*); action
column right-aligned; `select` gets a visible label (it has one — keep it) and
submit-on-change is acceptable for a single low-risk field (M3, *Lists*:
consistent action format per row).

Phone:
- Each sample = card: L1 subject link (clamp 2 lines); L2 `from` (dim); L3 the
  reclassify control on its own full-width line, prefixed with visible text
  `Label:` + the select; L4 `remove` / `re-include` action (WCAG 2.2 target
  size; Pencil & Paper: generous row height/tap targets on touch).
- Removed rows: keep opacity `.45` **and** the explicit `· removed` text —
  opacity/colour must never be the only signal (WCAG via `docs/ui-redesign.md`;
  Eleken accessibility note).
- No confirm dialog for `remove`: it is reversible (`re-include` restores), and
  the flash already explains "retrain to apply" — NN/g says prefer undo to
  warning dialogs (NN/g, *Confirmation Dialogs*).
- The footer "showing the first N of Y" needs a real affordance on phones: a
  compact pager or "View all" link, not a dead end (repo pagination + totals
  convention, `docs/ui-redesign.md`).
- If bulk sample editing is ever added, use the messages bulk bar pattern
  (contextual, pinned, explicit selection) (Pencil & Paper, *Enterprise Data
  Tables*).

Concrete changes (`CLASSIFIER_DATASET_TMPL` ~1864):
- Add `.mhide` handling so the `from` and meta cells stack; set
  `select{width:100%}` for `#dataset .tbl.mcards` under 767px.
- Improve per-row a11y: `aria-label="Reclassify sample — {{ subject }}"` so
  screen readers hear which sample the control belongs to (NN/g, *10
  Usability Heuristics*: help users recognise context).
- Keep the toast markup verbatim (`<div class="toast">` + ✓ + subject + "moved
  to the in-set/out-of-set") — test-pinned per `docs/ui-redesign.md`.
- Keep both empty states ("No positive samples yet" / "No negative samples
  yet") — they teach how to fill the set (NN/g, *Empty States*).

## /templates — draft templates list

**The one question: "What reusable drafts does the assistant have, and how do I
manage them?"** Small list (single digits) → readability over density.

Desktop: keep the 3-column table (name, preview, actions); keep the ~120-char
preview clamp; keep the sticky header only if it ever scrolls (Pencil & Paper,
*Enterprise Data Tables*).

Phone:
- Card: L1 name (link); L2 preview clamped to 2 lines; L3 `edit` + `delete`
  both visible. With only two actions, an overflow menu adds taps without
  saving space — the item should be directly actionable (M3, *Lists*; NN/g,
  *Mobile Tables*: fewer elements per card).
- `delete` keeps its confirm (irreversible) (NN/g, *Confirmation Dialogs*).

Concrete changes (`TEMPLATES_TMPL` ~2557):
- Already `.tbl.mcards`; add the messages-style 2-line clamp to the preview
  cell and ensure 44px targets in the phone action row.
- Keep the empty state + "New template" CTA (NN/g, *Empty States*: first-run
  pathway).

## /accounts — account connection cards

**The one question: "Which accounts are connected and working, and what needs
my action right now?"** Status drives the page (already regrouped in
`docs/ui-redesign.md` §Second pass — keep that structure).

Desktop (keep): proxy strip (badges + listener dots + Restart + Proxy log),
then per-account cards: `email · provider` + `reading` badge, status badge
(`authorised` / `not authorised` / `authorising…`), status-driven primary
action (`Authorise`/`Re-authorise`), `Edit`, and a `More` menu with
destructive items last. This is the "account connection" pattern: show
integration status plainly and set risky actions apart (Shopify/Stripe
connected-account docs, via `docs/ui-redesign.md`).

Phone:
- The auth/paste panel must reflow: the paste row is `flex-wrap:nowrap`
  (~4758) and will overflow — wrap it below 767px (WCAG reflow; target sizes).
- Card: L1 `email` (+ `reading` badge, ellipsis if long); L2 status badge +
  expiry; L3 primary action (Authorise/Re-authorise) full-width and reachable;
  L4 `Edit` + `More`. The primary action must stay large and reachable — it is
  the "fix it" control (Fitts, via `docs/dashboard-mobile.md`; M3, *Lists*:
  the item so the user can act on it).
- Keep `Setup details` collapsed by default — progressive disclosure is the
  right pacing on phones (`docs/dashboard-mobile.md` principle; `docs/ui-redesign.md`
  second pass).
- Destructive items (`Reset tokens`, `Remove account…`) stay last inside
  `More`, with their existing confirms (NN/g, *Confirmation Dialogs*).

Concrete changes (`ACCOUNTS_TMPL` ~4667):
- Under 767px: `.spread{flex-wrap:wrap}` for account cards; force the paste
  row `.row{flex-wrap:wrap}`; bound `.menu-pop{max-width:calc(100vw - 24px)}`
  so the overflow can't clip off-screen.
- Proxy strip on phones: keep badge + listeners on line 1; let `Restart proxy`
  + `Proxy log` drop to line 2 (secondary to status).
- Keep every status string — `mock_e2e` pins "not authorised" (per
  `docs/ui-redesign.md` §Test constraints).

## /log — activity log (dense feed)

**The one question: "What did the app just do — and did anything fail?"**
Exception-first reading; the feed is reference.

Desktop + phone (shared):
- Filters: the four level chips + debug toggle already use `.chip` + counts —
  correct for filtering by descriptive words (M3, *Chips*), and counts are the
  context rule (NN/g, *Dashboards* via `docs/ui-redesign.md`). On phones the
  chip group must become one non-wrapping, scrollable row — the messages
  convention (`docs/messages-mobile.md`) — instead of wrapping inside
  `.page-head`.
- Feed rows: keep one line per event: mono timestamp → level badge (text +
  colour; never colour-only) → message. Consistent line structure is the
  readability rule for dense logs: correct levels, meaningful messages,
  uniform timestamps (Better Stack, *Logging Best Practices* #1-3; SentinelOne,
  *Log Formatting*).
- No zebra striping and no per-row menus: extra grey levels fight hover/
  disabled/selected states in dense lists (Pencil & Paper, *Enterprise Data
  Tables*).
- Keep `Refresh` a labelled control (it is an action, not a filter) (NN/g,
  *10 Usability Heuristics*: consistency; repo convention).
- Make the filter bar sticky while scrolling a long feed so context and
  controls stay reachable (Pencil & Paper: sticky control panel row).

Phone:
- Message text may wrap; keep the timestamp+badge as a stable leading segment
  and don't drop below .8rem — dense ≠ unreadable, and body contrast must stay
  ≥4.5:1 (`docs/ui-redesign.md` accessibility rules).
- Optional, low-cost: day separator lines (`sub`) once the feed spans >1 day
  — helps scanning without new components (NN/g, *Mobile Tables*: group blocks
  to make long data navigable).

Concrete changes (`LOG_TMPL` ~5179):
- Give the chip row a scroll container under 767px (reuse the messages
  `.toolbar`/scrollable classes if hoisted; otherwise
  `#log .page-head .row{overflow-x:auto;flex-wrap:nowrap}` plus padding).
- Keep the `.logpanel`/`.logrow` markup and every string ("Nothing logged at
  this level yet.", debug toggle labels).
- Sticky: scope `position:sticky;top:0` to a phone filter bar wrapper only if
  the bottom tab bar doesn't collide (verify at 393×852).

## /proxy/log — raw proxy log

**The one question: "What is the embedded OAuth proxy doing, and what did it
just say?"** Raw text; fidelity beats prettiness.

Keep: the black `pre.log` panel, server-side tail, no soft-wrapping — wrapping
destroys log line structure, and horizontal scroll inside a panel is the
acceptable fallback for genuinely wide content (NN/g, *Mobile Tables*; log
formatting consistency, SentinelOne).

Phone:
- The `200/500/2000` n-links stay `.chip`s with an `active` state, placed in a
  non-wrapping scrollable row (they are the page's primary control) (M3,
  *Chips*; `docs/messages-mobile.md`). Keep `Accounts` as the tertiary link.
- Optional: drop the `pre` font to `.78rem` under 640px so more line fits
  before scrolling; do not introduce wrapping or ellipsis.

Concrete changes (`PROXY_LOG_TMPL` ~5037):
- Chip-row scroll wrapper for phones (same as `/log`).
- Keep `pre.log{border:0;margin:0}`; ensure the panel is the only scroll
  container (page itself shouldn't double-scroll).
- Security note in the page description if the tail ever contains
  auth-adjacent material: log guidance is "don't log sensitive data" —
  redact server-side rather than hiding on screen (Better Stack, *Logging Best
  Practices* #10). Point the user at `/log` for triage history instead.

## Test/consistency cautions

- `tests/mock_e2e.py` asserts exact strings. Do not rename buttons/headings
  without grepping the suite. Known pinned strings (per `docs/ui-redesign.md`):
  "not authorised", "dataset review", "page X of Y", "Older", "per page",
  `<div class="toast">` + "moved to", "keep in place (guard)".
- All phone styles go behind `@media(max-width:767px)` (640px for fine
  tuning), like the dashboard/messages passes; desktop must stay
  pixel-identical for these pages. Verify at 393×852 + 1440×900.
- Reuse existing primitives only: `.card`, `.card-h`, `.tbl.mcards`,
  `.rowacts`, `.seg`, `.chip`, `.details.menu`/`.menu-pop`, `.empty`,
  `.badge`, `.sub`, `.mhide`. Square corners, 1px `--line` borders, no
  shadow creep; black panels stay black; Geist + Geist Mono only.

## Sources

Fetched 2026-10-01 for this brief:
1. Nielsen Norman Group — *Mobile Tables: Comparisons and Other Data Tables*
   (A. Schade, 2017) — https://www.nngroup.com/articles/mobile-tables/ ;
   column legibility, sticky first column, horizontal scroll as fallback,
   accordions/grouping.
2. Nielsen Norman Group — *Confirmation Dialogs Can Prevent User Errors*
   (J. Nielsen, 2018) — https://www.nngroup.com/articles/confirmation-dialog/ ;
   confirm serious/irreversible only, specificity, verb buttons, no default
   Yes, prefer undo.
3. Nielsen Norman Group — *Empty States in Application Design: 3 Guidelines*
   (K. Kaplan; video, with the related article *Designing Empty States in
   Complex Applications*) —
   https://www.nngroup.com/videos/empty-states-in-application-design-guidelines/ ;
   empty states communicate system status, teach the system, and give a
   direct path to key tasks.
4. Material Design 3 — *Lists* — https://m3.material.io/components/lists/overview ;
   find-and-act lists, logical order, short scannable items, consistent
   format, selection treatment, 56/72/88dp heights, top-alignment for 3+ lines.
5. Material Design 3 — *Chips* —
   https://m3.material.io/components/chips/overview ; filter chips for
   filtering content by descriptive words; alternative to toggles.
6. Material Design 2 — *Gestures* —
   https://m2.material.io/design/interaction/gestures.html ; interfaces must
   visually signal when a gesture is available; swipe actions commit on a
   threshold — hence never swipe-only.
7. Pencil & Paper — *Data Table Design UX Patterns & Best Practices* —
   https://www.pencilandpaper.io/articles/ux-pattern-analysis-enterprise-data-tables ;
   sticky headers, contextual bulk toolbar on selection, secondary actions via
   3-dot/row detail, top-alignment past 3–4 lines, grey-level pitfalls.
8. Pencil & Paper — *Drag & Drop UX Design Best Practices* —
   https://www.pencilandpaper.io/articles/ux-pattern-drag-and-drop ;
   discoverability of drag affordances is hard; a non-drag version is
   essential.
9. Darin Senneff — *Designing a reorderable list component* —
   https://www.darins.page/articles/designing-a-reorderable-list-component ;
   drag-and-drop is not accessible (WCAG 2.2 SC 2.5.7 Dragging Movements);
   button-only reordering is one click and works for mouse/touch/keyboard;
   don't move items on input.
10. Better Stack — *Logging Best Practices: 12 Dos and Don'ts* —
    https://betterstack.com/community/guides/logging/logging-best-practices/ ;
    correct log levels, meaningful entries, canonical lines, retention,
    don't log sensitive data.
11. Eleken — *Empty state UX examples and design rules that actually work* —
    https://www.eleken.co/blog-posts/empty-state-ux ; informational / action /
    celebratory types, "say exactly why", headline → explanation → one CTA,
    accessibility.
12. (unavailable) Apple HIG *Lists and tables* — blocked by the site's bot
    protection; not relied on. SentinelOne *Log Formatting* was reviewed via
    search summary only.

Carried over from the repo's earlier research (already cited in
`docs/ui-redesign.md`, not re-fetched): uxpatterns.dev *Data Table Pattern*;
Eleken *Bulk Actions UX* and *Settings Page UI Design Guide*; UX Design World
*Actions in Data Tables*; NN/g *10 Usability Heuristics* and *Dashboards:
making charts easier to understand*; TetraLogical/WCAG 2.2 target size ≥24px;
Vercel Geist type scale.
