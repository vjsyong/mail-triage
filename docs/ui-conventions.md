# UI conventions — the interaction standard

Design record, 2026-10-02. One page, one way to do each recurring interaction. When a
page disagrees with this document, the page is wrong; fix it or add an entry to the
exceptions register below. The standard came out of an app-wide sweep that found four
different delete mechanisms, five enable/disable presentations, three feedback
channels, and unstyled/duplicated primitives.

## Where shared things live

- All CSS/JS used by 2+ pages lives in `BASE_TMPL`. A page `<style>` block only ever
  carries styles exclusive to that page (the simulator reasoning trace overflowed for
  a week because shared classes shipped in a page-only block — see `ui-redesign.md`).
- Shared primitives named here are defined once in `BASE_TMPL`; never re-implement
  them locally (the Accounts page used to carry its own copy of `cp`).

## Action grammar

### Item deletes — armed, two-step

Deleting an entity listed in the UI (rule, flow, classifier, template, chat) is a
two-step, in-place action:

- Class `arm-del` on the button (chat keeps `chat-del`), plus
  `data-arm-label="Press again to delete <thing> <name>"` and a matching
  `aria-label="Delete <thing> <name>"` on the initial state.
- First activation *arms* it: red (`--err`) background, text becomes `Confirm delete`
  (chat shows the trash glyph), aria-label announces the arm. It auto-disarms after
  4 s, on Escape, on any outside click, and when its `details.menu` closes.
- Second activation performs the delete. No `confirm()` dialog, ever, for these.
- Inside a menu, the item carries `menu-item danger arm-del`; the menu stays open
  through arming so the second click can land.
- After the delete: a redirect flashes its result (page variant) or the drawer toasts
  it. Armed is not a substitute for feedback.

Implementation: the `window.__mtArmDel` delegate in `BASE_TMPL`.

### One-way / high-stakes actions — `confirm()`

Actions that are irreversible, security-relevant, or affect more than the clicked
item keep a native `confirm()` with a sentence that names the object and the
consequence. The exhaustive list:

| Action | Consequence copy must say |
|---|---|
| Remove account | tokens + config entry are deleted |
| Reset cached OAuth tokens | you will need to authorise again |
| Rebuild the search index | mail itself is untouched |
| Clear a chat | clears this chat only |
| Enable assistant auto-apply (`llm_apply`) | it will act on its own |
| Approve a trusted plugin browser view | it can transmit mail text by navigating itself |

`onsubmit="return confirm('…')"` on the form; keep the string apostrophe-safe (do not
interpolate names into a single-quoted JS string).

### Reversible mutations — no confirmation, show the reverse

Edits that can be undone in place (dataset sample removal ⇄ re-include, snooze ⇄
wake now, tag ⇄ untag, needs-reply clear) need no confirmation and no danger styling.
Put the reverse control next to the action.

### Never

No action deletes mail. Worst case the app files it into a folder; guard rules keep
matching mail in place.

## Feedback

- **Server redirect** → flash banner at the top of the content. Categories are
  exactly `ok` (green), `warn` (amber), `err` (red). `info` is retired — use `ok` for
  benign outcomes and `warn` when something only partly worked.
- **In-place JS / fetch** → the shared `toast(msg, kind)` at top-right (bottom on
  phones). Same three kinds. One toast at a time.
- **No page-local toast markup, no query-param toasts** (the classifier dataset page
  used to render its own `?toast=` div; it now flashes like everything else).
- **Announce every mutation** unless the resulting state is self-evident in the same
  viewport (row reorder, paging, filters). Wording: past tense, name the object
  ("Rule 'News' disabled.", "Chat deleted.", "Sample moved to the in-set — Promo.").
- Toggles and deletes must announce even when the control itself communicates the
  state — the confirmation that the server persisted it matters.

## In-flight state

- The shared submit guard disables a form's submit button the moment a real
  submission starts (after any `confirm()`), and resets before Turbo caches the page
  so Back never restores a stuck button. Opt a form out with `data-no-busy`.
- Long-running jobs (classify-all, index build) also disable their trigger while
  running and show honest progress with counts.
- No second guard is needed inside forms that already disable their own button.

## State controls (on/off)

- Use the `.px-sw` switch for every boolean enable/disable of an entity (rules,
  flows, classifiers, learners, plugins). It auto-submits on change and the route
  flashes the new state.
- Verbs are only **Enable** / **Disable** — never Pause/Resume or On/Off variants.
  `title` and `aria-label` name the entity.
- Status transitions that are not booleans (Retire, approve, save permissions) stay
  text buttons.
- On coarse pointers `.px-sw` gets a 44px hit area.
- The switch needs JS (same as plugins have always worked). The entity editors'
  "Enabled" checkboxes remain the no-JS path.

## Lists & row actions

- Clicking a row opens the record; secondary actions live in `.rowacts` (revealed on
  hover on desktop, always visible on touch).
- Desktop: relevant actions inline with `.ra-inline`; the rest in a `details.menu`
  with `.ra-menu`. Touch (≤767px): `.ra-inline` hides, `⋯` menu shows. Every
  `.rowacts` ships both variants.
- Dense tables use the `.tbl.mcards` collapsed card anatomy; card lists (flows,
  accounts) may show their few actions inline.
- Menu items are sentence case (`Edit`, `Delete…`); destructive items carry
  `menu-item danger arm-del`.
- Bulk actions live in the `.bulkbar`, which appears only with a selection; triggers
  live in `.tactions`.

## Save bars & editors

- Editors end in a `.savebar`: primary **Save <thing>** + **Cancel**. The way out of
  an editor is Cancel; "Back" is only for navigation backlinks.
- Server-side validation renders at the top of the form and receives focus
  (`#form-err` pattern).
- Settings cards keep scoped partial saves; a card must never submit fields it does
  not own.

## Empty states

- `.empty` anatomy: optional icon, `h4` title, one-line reason, at most one CTA
  (`.btn primary`). Used for any list/card with nothing to show.
- Micro-empties inside activity/log feeds may be a single `.sub` line
  ("Nothing logged at this level yet.").
- Never a bare blank area or a bare "failed": specific reason + one recovery action.

## Copy

- Use the shared `cp(text, el)` or a `.copy[data-copy]` element from `BASE_TMPL`.
- Never call `navigator.clipboard` from a page template (no fallback, inconsistent
  labelling).

## Auto-submit selects

- A control that mutates exactly one value may auto-submit (`onchange`), and when it
  does it must announce via flash/toast. Rows with several controls keep an explicit
  Save.

## Labels & accessibility

- Sentence case for buttons, links, and menu items.
- Name the object in destructive labels and `aria-label`s.
- Focus-visible ring everywhere; 44px touch targets; colour is never the only signal.

## Testing the standard

- `tests/mock_e2e.py` section **T58** pins armed delete markup, switches,
  consequence confirms, toggle/delete flashes, and the shared guards. Extend it when
  you add a pattern.
- Pinned strings that must survive refactors: `chat-del`, `.chat-del.armed`,
  "Press again to delete this chat" (chat two-step), and the assistant empty state.
- Rule: add checks for new behaviour; never weaken or delete a check to make a
  change pass.

## Exceptions register (deliberate deviations)

| Exception | Why |
|---|---|
| Log toolbar `Pause`/`Resume` | Live-view control, not an entity's enabled state |
| Row reorder (rules/flows up-down) is silent | The new order is visible in place |
| Dataset sample `Remove` is plain, not armed | Fully reversible via `Re-include` next to it |
| Settings/editor `Enabled` checkboxes | Form context: the Save bar is the commit step |
| `confirm()` for the six high-stakes actions above | Arming cannot carry the consequence |

## Review checklist (new or changed pages)

1. Delete/remove: armed (entity) or `confirm()` (high-stakes) or reverse control
   (reversible) — and it announces afterwards.
2. On/off: `.px-sw`, Enable/Disable, route flashes.
3. Feedback: flash on redirect, `toast()` in place, three kinds only.
4. New form: submit guard works, Save/Cancel labels.
5. Row actions: `.rowacts` with `.ra-inline` + `.ra-menu`; sentence case.
6. Empty state: `.empty` anatomy with one primary CTA.
7. Copy: shared `cp`/`.copy`.
8. Strings pinned by the suite updated rather than weakened.
