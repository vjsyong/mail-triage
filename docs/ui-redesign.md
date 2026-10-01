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

## Third pass: every remaining page, small items (2026-10, same day)

Page-by-page sweep of the pages that had only one pass:
- Message view: status-driven action cluster (Classify when unclassified; File primary
  once a folder is suggested; Re-classify secondary), sticky right rail on desktop,
  cleaner reply card with its own hint, wrapping long sender lines.
- Rule editor: sectioned cards (Basics / Conditions / Actions), whole-word matching hint,
  enabled toggle promoted to Basics, save bar; rules list move-buttons grouped as a
  segmented control. `.savebar`/`.seg` promoted to the base stylesheet.
- Account add/edit: sectioned cards (Account / OAuth app / Login flow / Custom details /
  Server), helper text, Cancel affordances; all field names + toggle JS unchanged.
- Templates editor: Template card + Body card with placeholder legend.
- Log: level filter chips (All/Errors/Warnings/Info) with counts, debug toggle preserved;
  proxy log n-buttons get proper active states.

## Fourth pass: dashboard (2026-10, same day)

Research round (Domo dashboard guide; Improvado guide; ClearPoint KPI rules; NN/g
"Dashboards: making charts easier to understand"):
- This is an OPERATIONAL dashboard: big status indicators, minimal clutter, current
  state first ("is anything broken?"), not analysis.
- Five-second rule; F-pattern (most critical top-left); inverted pyramid.
- 5-9 metrics max, each with context (share of total, severity), never isolated numbers.
- Visual hierarchy by size/weight, not equal-weight tiles; primary element 2-3x larger.
- Colour = status only (green/amber/red), neutral grey elsewhere; alerts/exception-first.
- Avoid the "democratic layout" (six identical black tiles) and page duplication.

Rebuild: (1) system strip - Triage / Proxy / Index / LLM each with a status dot and
live detail, error lines with actions fold in (parked -> Retry, queue -> View);
(2) metrics card replacing the black tiles - "need a reply" as the oversized primary
number linking to its filter, parked errors red when >0, each metric carrying context
(% of seen, queue length), runtime one-liner at the bottom; (3) grid: recent mail (10)
left, search index + terminal-style activity feed (scrollable) right.

## Fifth pass: message viewer (the "ugly mess" fix)

Research round (Superhuman UX teardowns, NN/g reading/scanning research):
- Header first, F-pattern: sender + date + folder immediately under the subject;
  users scan the top before reading.
- Avoid tiny text / exotic fonts (a cited Superhuman complaint) - body at ~15px,
  generous line height, and never clip or box the mail content.
- Reading measure ~50-75 characters per line for prose.
- Quoted raw falls off: collapse it behind a "show quoted text" disclosure (the
  standard reply-trimming pattern) instead of showing a wall of ">".

Root cause of the raw-MIME mess: for multipart mail with base64 parts, an old code
path stored the raw body (boundary + part headers + base64) as the snippet. The
viewer's repair path could not refetch because by then the message had ALSO been
moved on the server (external or auto-file) while the DB row kept a stale
folder/uid. Salvage then failed because the stored text's line breaks had been
collapsed, which the old base64 decoder could not handle.

Fixes shipped:
1. engine: run-based base64 decode (canonical lines, buffered across short tail
   lines; space-collapsed runs; runs after part headers) + MIME scaffold stripping
   in salvage paths; junk detector also flags raw headers (Received:, DKIM...).
2. app: viewer re-fetch falls back to a Message-ID search across folders when the
   stored folder/uid is stale, caches the repaired body back and relocates the row.
3. engine/app: every move the app performs (rules, LLM auto-file, manual file,
   assistant) now records the destination folder + new UID (from the IMAP COPYUID
   response) on the row - no more stale folders for app-filed mail.
4. Viewer layout: 72ch measure, .94rem/1.65 typography, linkified URLs, quoted
   tails collapsed, pretty date in the header, explicit "body unavailable" state.


## Sixth pass: proper email renderer (2026-10, same day)

Request: "render images and text formatting properly". Built:
- HTML body extraction (depth-first MIME walk with IMAP section numbers) + cid
  image map, sanitized ONCE at store time with nh3 (scripts/styles/forms gone;
  layout, tables, inline styles kept - the Superhuman lesson: never clip or box
  the mail, give it a document canvas).
- Viewer: `.emailbody` canvas (white sheet, 1px frame, overflow-x for wide tables,
  images max-width 100%); inline cid images via /messages/<id>/part/<n> (fetched
  from IMAP as two small literals, transfer-encoding undone, disk-cached); remote
  images blocked by default with a per-message "Load images" button and an
  always-load setting, served through an SSRF-guarded proxy (verified fetch,
  documented unverified retry for leaf-only CDN chains) with disk cache;
  "View plain text" / "View formatted" toggle; nosniff + CSP sandbox headers on
  all served mail media.
- Bulk: `app.py --extract-html` fills the renderer cache for the existing mailbox
  (phase 2 rescues stale rows via the Message-ID index); new scans extract at
  scan time, so fresh mail renders with no extra fetch.


## Seventh pass: settings subgroups, flows builder, assistant drawer (2026-10, same day)

**Settings** - regrouped into six top-level groups with a sticky sub-nav (General / AI
Settings / Mail & connection / Sorting & filing / Search index / System status); every
control is now an item row in a list (name + helper left, control right), grouped under
sub-cards (Language model, Classification, Classifiers, Embeddings & reranker,
Assistant...). All field names and per-card partial saves unchanged.

**Flows builder** - research said the proven pattern for scoped automation is the linear
trigger->actions builder (Zapier "when/then", Asana rules), not a canvas: canvas tools
(Make/n8n) trade clarity for freeform routing we don't need. Built exactly that: WHEN =
condition list (reusing the rules condition UI, all/any), THEN = ordered step cards
(add/reorder/remove, type-specific fields, hidden JSON payload). Engine runner applies
steps in order with move-tracking between steps, per-message dedupe (flow_runs by
msgid), dry-run mode, and event-log entries.

**Assistant** - frontier-lab chat pattern: sessions table + per-chat transcript,
history rail on the page, "Assistant" nav click always starts a fresh chat (recent
empty chat reused so they never pile up), resume/delete from the rail. A shared chat
engine (one JS implementation + one Jinja conversation fragment) powers both the page
and a global right-side drawer (FAB button, collapsible, remembers the open state and
the active chat per browser). Streamed turns announce their session first.
