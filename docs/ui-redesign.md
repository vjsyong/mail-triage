# UI rebuild — design brief (research + decisions)

Commissioned goal: the UI is functional but awkward. Research current front-end/UI/UX
best practices, then rebuild the interface to match, keeping the Vercel-style light
theme (grey base, white cards, black panels, blue links, square corners, Geist fonts).

## Sources consulted (2026-10)

- NN/g: *10 Usability Heuristics*; *Website Forms Usability: Top 10 Recommendations*
- uxpatterns.dev *Data Table Pattern*; Pencil & Paper *Enterprise Data Tables*;
  UX Design World *Actions in Data Tables*; Eleken *Bulk action UX*
- AI UX Playground *Designing better AI chat*; Setproduct *AI chat anatomy/patterns*;
  thefrontkit *AI Chat UI Best Practices 2026* (+ the in-repo chat checklist)
- 5of10 *Dashboard Design Best Practices*; UXPin *Dashboard Design Principles*;
  UX Planet *Sidebar UX Best Practices*
- TetraLogical / WCAG 2.2: target size >= 24x24 CSS px (or spacing), focus appearance
- SaaSUI *Loading & Skeleton UX*; Aufait / Vendasta *Empty state design*
- Vercel Geist (typography scale: label 12/13/14/16, copy 13/14/16; mono pairing)

## Decisions (patterns -> how this app implements them)

### Shell & navigation
- Persistent left sidebar (232px) grouped into Mail / Automation / System; active item
  gets accent left rail + bold weight + tinted background; hover + focus-visible states.
- Mobile (<1024px): sidebar becomes a slide-over drawer opened from a compact topbar.
  Skip-link to content; `aria-current="page"` on the active item.
- Page header component: title + one-line description + contextual primary action(s).
- No global search box (single-user app); status chips (worker, proxy) live at the
  sidebar foot and on the dashboard.

### Tables & lists (messages, datasets, logs)
- Sticky header row; hover row highlight; text left / dates right; consistent formats.
- Selection: always-visible checkboxes (min 24px, 44px on touch), select-all in the
  header with total counts; contextual bulk toolbar appears only with a selection
  ("N selected", Clear, actions) and stays pinned while scrolling.
- Row actions: primary click opens the record; secondary actions on hover (desktop)
  with a persistent affordance (kebab) for touch; destructive actions confirmed.
- Pagination with totals ("page X of Y", Older/Newer/Last, per-page selector).
- Mobile: message list becomes stacked cards (no horizontal scrolling).

### Forms & settings
- Single-column field flow inside cards; grouped sections (LLM endpoint, RAG,
  Behaviour, Connection); label above field; help text under label; no placeholder-only.
- Save actions per card (sticky at card bottom for long cards); server-side validation
  messages inline in the form area (flash banner + field-level where practical).
- Secrets: masked inputs, explicit "Clear" checkbox, "blank keeps" hint.
- Test buttons return inline result chips (success/failure + timing).

### Feedback, loading, empty states
- Loading: skeletons not spinners for page-level; inline spinners for buttons
  (submit-disable while in-flight); honest progress with counts for the classify job.
- Empty states: icon + title + one-line reason + primary action, everywhere
  (messages filters, rules, classifiers, templates, accounts, log, chat).
- Errors: specific message + single recovery action; never a bare "failed".
- Toasts: top-right, auto-dismiss, role=status, used for AJAX actions; flash banners
  for server redirects.

### Dashboard
- F-pattern: status strip (worker state, last pass, run-now) -> KPI row (6 metrics,
  each with label + context) -> two-column: recent messages (hero) + index card;
  activity feed full-width below. All timestamps have "last updated" context.

### Assistant (chat)
- Scrollable conversation container + pinned composer; user right/accent, assistant
  left/neutral with avatar + label + timestamp; ~75% max bubble width.
- States: queued shimmer, thinking collapsible (auto-open while streaming, collapses
  to "Reasoning · Ns"), streaming with caret + status label ("thinking"/"writing"/
  "running <tool>" + elapsed), stopped keeps partial text, specific errors.
- Tool calls as compact chips with status; copy button per assistant message;
  empty state with 4 suggestion chips; Enter sends, Shift+Enter newline; Stop button
  while streaming; auto-scroll only when near bottom, plus "Jump to latest".
- aria-live=polite on the streaming container; twin markdown renderers stay in sync.

### Accessibility & responsive
- focus-visible: 2px accent ring (offset) on all interactive elements; WCAG 2.2
  target sizes; colour never the only signal (text labels/badges paired with colour).
- prefers-reduced-motion honored; contrast >= 4.5:1 for body text.
- Breakpoints: 1200/1024/768/640. Tables collapse to cards; grids collapse to 1 col.

### Theme
Kept: palette, square corners (deliberate brand), Geist fonts, black panels.
Added: hover/active/focus tokens, --line-strong, status dot colours, toast styles.

## Test constraints (mock_e2e asserts exact strings)
Keep: "not authorised", "LLM endpoint", "RAG / semantic search", '<details class="think"'
(closed by default), "thinking", "why: <reason>", "LLM summary: ...", "classifier
thinking", "page X of Y" + "Older" + "per page", '<div class="toast">' + "moved to",
"keep in place (guard)", "classified this as", "dataset review", "Similar rule exists".
Run the full suite after each page rebuild.


## Second pass: Settings & Accounts regrouping (2026-10, same day)

Follow-up research: Eleken "Settings Page UI Design Guide" (group by the tasks users come
to do; labeled sections; helper text under each control; reveal rarely-used fields on
demand; show integration status plainly; set risky actions apart; save feedback) plus
connection-status patterns (Shopify "account connection", Stripe/Auth0 connected-accounts
status docs). Inventory of the old pages: Settings had 53 fields in 4 flat forms whose
headings mixed tasks (Behaviour / LLM endpoint / RAG / Mail connection / Runtime);
Accounts buried the status-driven action (Authorise) among four equal-weight buttons.

Settings regrouped into task sections, each card a scoped save:
  Mailbox (Mail source · Checking) — Sorting & classification (Rules & classifiers ·
  LLM classification · LLM endpoint · Assistant) — Filing & drafts (Categories & folders)
  — Search (Index · Endpoints) — General (You) — Status (effective values).
Every control got a one-line helper; external-server fields reveal only in external mode;
the behaviour form was split into scoped partial saves (handler already skipped absent
keys, so each card saves only its own fields); flashes name the saved scope.

Accounts regrouped around status: compact proxy strip (badges + listener dots + restart
+ log), then per-account cards with a status-driven primary action (Authorise /
Re-authorise), Edit, and a More menu (copy password/redirect URI, reset tokens, remove —
destructive last), a "reading" badge on the account the app actually uses, an external-mode
notice, Setup details kept behind a disclosure, and the explainer collapsed.
