# FORM/EDITOR pages — per-page layout brief (research + recommendations)

Scope: the five form/editor page types —
**Settings** (`/settings`), **Rule editor** (`/rules/new`, `/rules/<id>/edit`),
**Flow builder** (`/flows/new`, `/flows/<id>/edit`), **Template editor**
(`/templates/new`, `/templates/<id>/edit`), **Account forms** (`/accounts/new`,
`/accounts/<id>/edit`).

Method: read the live templates (`app.py` …) first, then fetched current
authoritative guidance (NN/g, GOV.UK Design System, Baymard, W3C/WAI, Smashing
Magazine, Eleken) on 2026-10-01; every recommendation below names its source.
Quantitative UI research cross-referenced from in-repo briefs
(`docs/mobile-ui-research.md`, `docs/ui-redesign.md`).

**Design-language invariants (do not change):** Vercel-style light theme, square
corners (deliberate), Geist, black panels, blue links; mobile shell = topbar +
bottom tab bar, ≤767px is the phone layout; 16px input font on phones (iOS zoom);
sticky savebars; per-card partial saves on Settings.

Citation keys (full URLs in §Sources):
`[NNg-forms]`, `[NNg-pd]` (progressive disclosure), `[GOV-input]`, `[GOV-error]`,
`[Baymard]`, `[Smashing]`, `[WCAG-2.5.7]`, `[Eleken]`; `[repo]` = in-repo docs/tests.
Line references are to `/home/xrim/mail-triage/app.py` at time of writing.

---

## 0. Cross-cutting rules (all five page types)

1. **One question per section, label above the field, one column on phones.**
   "You should align labels above the text input they refer to" and "do not use
   placeholder text in place of a label" `[GOV-input]`; on mobile the label belongs
   above the field so the input can use the **full width** — "users must be able to
   see their entire input" `[Baymard]`; multiple columns "interrupt the vertical
   momentum" `[NNg-forms]`. The app already collapses `.grid2`→1 col and `.grid3`→
   `1fr 1fr` with the value input going full width at ≤767px (lines 536-538, 726) —
   keep that; never re-introduce side-by-side *unrelated* pairs.
2. **Group by the task the user came to do, with a one-line helper under each
   item.** "Break one long menu into smaller sections named after the task" and
   "add a short line of helper text under each setting" `[Eleken]`; "visually group
   related labels and fields … if your form asks about two different topics,
   section it" `[NNg-forms]`. This is the model Settings already uses (6 groups,
   `.setrow` = name+helper left / control right, 4166-4173).
3. **Progressive disclosure for rarely-used fields — one extra level, clearly
   labelled.** "Hiding the advanced settings … helps novice users avoid mistakes",
   but "it must be obvious how users progress from the primary to the secondary
   disclosure levels", and "designs that go beyond 2 disclosure levels typically
   have low usability" `[NNg-pd]`; "hide rarely used fields and reveal them only
   when someone needs them" `[Eleken]`. In-app: the external-server block `#extf`
   (4407) and the accounts "Custom provider details" block (4912) are correct
   examples; use `<details>` for *sub*-groups inside a card (e.g. the LLM fallback
   fields, 4247-4255) and keep disclosure to one level.
4. **Validation: never lose input, summarise + mark + focus.**
   "Always show an error summary when there is a validation error, even if there's
   only one", "move keyboard focus to the error summary", "link to each of the
   answers that have validation errors", and make the summary wording "the same as
   those which appear next to the inputs" `[GOV-error]`. Errors must not rely on
   colour alone — "outline the field AND use red text AND a heavier font"
   `[NNg-forms]`. **Current gap:** rule/flow/account POSTs re-render via
   `redirect(url_for(...))` after a flash (2164-2166, 2182-2184, 2484-2489,
   2508-2513, 5071-5073, 5108-5110), so the user's typed values are **discarded**;
   re-render the template with the submitted context + an inline `msg err` summary
   instead.
5. **Save pattern: scoped savebar → scope-named feedback → return to the card.**
   Per-scope saves with a sticky save bar are the settings convention
   `[Eleken]` + `[repo: ui-redesign §2/§7]`; the app's `.savebar` (509, mobile
   offset 680) and `scope`-named flashes ("Mail source saved.") already implement
   half of it. The missing half: the POST ends with
   `redirect(url_for("settings"))` (4559) which dumps the user at the **top** of a
   55-field page — redirect back to the saved card (`/settings#ai-model`, or a
   `next` hidden field), since `scroll-margin-top:70px` is already set for anchors
   (498). Feedback itself: give the flash container `role="status"`/`aria-live`
   (790-792 currently announces nothing; the toast container at 813 already is
   `aria-live="polite"`).
6. **Secrets and "is it set?" state: show state in visible text, not a
   placeholder.** "Avoid placeholder text" for hints — it vanishes while typing,
   "not all screen readers read it out", and its contrast is often too low
   `[GOV-input]`/`[NNg-forms]`. The password fields already hint via placeholder
   ("set — type to replace", 4236); move the *state* ("Currently set — type to
   replace") into the row's visible `.sub` helper and keep `autocomplete=
   "new-password"`.
7. **Destructive / high-consequence actions: set apart, name the consequence,
   use a specific verb, and require extra effort for the worst.** Danger belongs
   in a dedicated place — "at the bottom of the settings/account page" or its own
   page — with only "truly critical actions there" `[Smashing]`. Confirmation copy
   must name the entity and the effect, use a danger icon (works for colour-blind
   users), and the button verb must say what happens ("Delete API Key", not
   "Confirm") `[Smashing]`. Note dangerous ≠ delete only: sending mail and
   granting permissions count `[Smashing]` — this applies to the Agent-permissions
   selects (4353-4385) and to `perm_send` most of all.
8. **Touch: ≥44px targets on coarse pointers, ≥8px gaps, never hover-only, and
   reorder by buttons — never drag.** `[repo: mobile-ui-research §2]`; and per
   WCAG 2.2 AA, anything drag-operated needs a "single pointer [non-drag]
   alternative — e.g. a sortable list provides adjacent up/down controls"
   `[WCAG-2.5.7]`. The flow builder's ↑/↓ buttons already satisfy the reorder
   requirement; they just need labels (below).
9. **Partial-save contract (Settings, hard rule).** One card = one `<form>` = one
   `section`/`scope` pair = one `.savebar`. The server writes only keys present in
   the POST (`if k in f`, 4104-4152; `if has(k)`, 4048+ for the behavior handler)
   so a card can never overwrite a field it does not render. Consequences: never
   duplicate a field name into a second card; when a field moves card, move it
   inside that card's `<form>` **and** add it to that handler; keep the
   `checkbox`+`hidden` twin pattern for booleans (e.g. `render_images` at 4216)
   with the hidden input **after** the checkbox, because `request.form.get()`
   returns the first value; the only JS-serialised payload (`steps_json`, flow
   builder) must stay inside its own form. Add a regression test to
   `tests/mock_e2e.py`: post one card and assert every other setting is unchanged.
10. **Dirty state is not signalled anywhere.** The savebars are always visible and
    there is no unsaved-changes warning; leaving the flow/account/rule/template
    editors (Back/Cancel links, or closing the tab) silently drops edits. The
    sticky save bar that "appears when there are unsaved changes"
    `[Eleken]` plus a lightweight `beforeunload` guard on dirty forms (derived,
    test on-device) would close the loop; at minimum keep the savebar and the
    Cancel link visually distinct so Save is the obvious exit
    `[NNg-forms: Cancel less prominent than Submit]`.

---

## 1. SETTINGS — `/settings` (SETTINGS_TMPL, 4155)

**The question this page answers:** *"Where do I change X — and did it stick?"*
Not "show me every value": 55 fields behind a 216px sticky sub-nav, six task
groups (General · AI Settings · Mail & connection · Sorting & filing · Search
index · System status), each card a scoped save.

### Phone (≤767px)
- Keep the task groups and cards; on phones the sub-nav collapses to a wrapped
  link list before the first control (4173) — 15 links is a wall. Replace it with
  a horizontal, snap-scrolling chip row (reuse the `.tchips` pattern already
  established in `[repo: mobile-ui-research §scroll-snap]`) or tuck it into a
  `<details>` "Jump to…"; both keep the "labeled sections" rule `[Eleken]`.
- `.setrow` already goes single-column at ≤900px (4173): name+helper, then
  control — label above the field, which is the mobile default `[Baymard]`. Make
  sure the control sits directly under its helper (≤8px) and that the textarea in
  "Query instruction prefix" (4319) keeps a full-width line.
- 16px inputs are set twice (648-658 for coarse pointers, 728-730 for ≤767px) but
  the 44px min-height for inputs/buttons lives **only** in the coarse-pointer
  block (648-657) — a narrow mouse window at 360px keeps 34px buttons. That still
  clears the WCAG 2.2 AA floor of 24px, but if the touch-first phone layout is the
  target at that width, extend `min-height:44px` into the ≤767px block too.
- Savebar per card, sticky above the tab bar (`bottom:calc(72px +
  env(safe-area-inset-bottom))`, 680) — correct; nothing overlaps it (per the
  shell z-order — tab bar 180, sheets 190, drawer 220 `[repo: assistant-mobile.md
  §Overlay z-order]` — any new overlay must sit above 180).
- After saving, land back on the card: `redirect(url_for("settings"))` (4559) →
  add the anchor (`#sort-filing`, etc.). On phones this matters most — the flash
  is otherwise off-screen.
- State badges near the thing they describe (proxy-mode badge in the Mail-source
  card head, 4394-4396; "Test" buttons next to LLM/embeddings, 4258-4262/4339-
  4343) match "show integration status plainly" `[Eleken]` — keep, and keep
  "tests the saved settings" beside them (they test stored config, not the
  unsaved form: the hint says so).

### Desktop
- Keep the 216px sticky in-page nav + sections `[Eleken]` ("groups under labeled
  sections; when there are a lot, break them across tabs"). Add an active-section
  highlight (scrollspy setting `aria-current="true"` on `.setnav a`) to match the
  shell's active-item convention `[repo: ui-redesign §Shell]`.
- Keep `.setrow` two-column (name+helper left, control right) — for shorter
  desktop forms GOV.UK still wants labels above, but the repo's row pattern is a
  deliberate settings idiom and remains scannable; the *mobile* collapse covers
  the Baymard case `[Baymard]`.
- The Fallback-endpoint sub-group (4247-4255) is rarely used: wrap it in a
  `<details>` inside the LLM card (one disclosure level) `[NNg-pd]`.
- Numeric rows ("Timeout", "Max calls/hour") do not need 300px of control width;
  size fields to the expected input where cheap `[NNg-forms: match fields to type
  and size]`.

### Concrete changes
- [ ] Redirect saves back to the card anchor; keep scope-named flashes.
- [ ] `role="status"` on the flash loop (790-792).
- [ ] Phones: `.setnav` → scroll-snap chip row or `<details>`.
- [ ] Move secret "set/not set" state from placeholder (4236, 4253, 4313, 4332,
      4417) into the row helper; keep `autocomplete="new-password"`.
- [ ] `<details>` around Fallback endpoint; keep external-server disclosure `#extf`
      as is (one level, JS-driven, 4407).
- [ ] Agent permissions: keep the danger-in-its-own-card grouping and the
      confirm() guard (4372-4385), but upgrade the confirm to a `<dialog>` whose
      copy names the capability and whose button verb is the action ("Allow
      auto-send"/"Allow auto-delete"), with the ⚠ line and icon; keep Off as
      default `[Smashing]`.
- [ ] Add the partial-save regression test (cross-cutting rule 9).

---

## 2. RULE EDITOR — `/rules/new` · `/rules/<id>/edit` (RULE_EDIT_TMPL, 2059)

**The question:** *"What does this rule match, and what does it do to the mail it
matches?"* (context that matters: rules run first, in list order, first match
wins — already in the page-desc, 2063; keep it).

### Phone
- Three cards: Basics / Conditions / Actions (2068-2114) — matches
  "section it into groups" `[NNg-forms]` and the app's own third-pass decision
  `[repo: ui-redesign §3]`.
- Basics: Name + Match-mode collapse to one column (536-538); the match-mode
  helper stays under the label `[Eleken]`; the Enabled checkbox stays in Basics
  `[repo §3]`.
- Conditions: 5 always-visible rows (2086-2099). Each row is a select+select+
  input; on phones `.cond-head` is hidden (718: users lose the
  field/operator/value captions — each control keeps its `aria-label`, 2089/2093/
  2097, so screen readers are fine) and the value input drops to its own
  full-width line (726). Two improvements: (a) show only the first ~2 rows on
  phones with a clear "+ Add another condition" / "Show 3 more" control — empty
  rows are visual debt and "keeping forms short" is the top form guideline
  `[NNg-forms]` while "reveal rarely used fields" is the settings idiom
  `[Eleken]`; hidden rows still submit empty values, which `_rule_from_form`
  already ignores (2128-2131), so no server change is needed. (b) On phones add a
  tiny visible caption per row ("Condition 2") so empty rows are distinguishable
  without the hidden header row.
- Actions: "Move to folder" + two checkboxes; on phones stack them (grid2
  collapse) and keep the guard-rule hint directly under the card head `[GOV-input:
  hints before input]`.
- **Validation (biggest fix):** a POST with no condition flashes "Add at least
  one condition with a value." then redirects (2164-2166) and every typed value is
  gone. Re-render the template with the posted values + an error summary
  (`<div class="msg err" role="alert">` linking to `#conds`) `[GOV-error]`, and
  make the inline message next to the Conditions card identical in wording
  `[GOV-error]`.
- Savebar at the end (2115): keep Save primary-first, Back as secondary —
  "give Cancel significantly less visual prominence than Submit" `[NNg-forms]`;
  sticky-above-tab-bar behaviour is inherited (680).

### Desktop
- Keep the three stacked cards; `.grid3` (150/130/1fr) aligns the condition rows
  with their header — good scan pattern; keep the header row visible here.
- Keep the whole-word-matching hint (2101) — "explain any input or formatting
  requirements" `[NNg-forms]`.
- Consider a rules-list preview of this rule's current match count on edit
  (reuse the dry-run machinery at 2032-2049) — deferred, not required.

### Concrete changes
- [ ] POST error → re-render with values + error summary + focus (`[GOV-error]`).
- [ ] Phone: progressive condition rows (2 + "Add condition") `[NNg-pd]`.
- [ ] Keep `aria-label`s on every condition control; add per-row caption on phones.
- [ ] Optional: `enterkeyhint="next"`/`inputmode` on condition value inputs
      `[repo: mobile-ui-research §C]`.

---

## 3. FLOW BUILDER — `/flows/new` · `/flows/<id>/edit` (FLOW_EDIT_TMPL, 2255)

**The question:** *"When a message matches … then what happens, in order?"*
(WHEN → THEN, linear — the proven shape for scoped automation
`[repo: ui-redesign §7]`; deliberately not a canvas).

### Phone
- Three cards (Flow / WHEN / THEN) — one-column flow, group-per-concern
  `[NNg-forms]`; add and keep a bottom savebar (2320).
- WHEN: as rules — collapse to one column, value full width, and add the same
  progressive "add condition" treatment only if you also do it for rules (keep
  the two editors visually identical; they share the condition UI by design).
- THEN step cards are the part to fix on phones. Current `.stepcard` is a fixed
  `26px | minmax(0,1fr) | auto` grid (2257) with `.steptools` (↑ ↓ ✕, 2262) in
  the third column; at ≤767px each tool is 44px wide on coarse pointers (650), so
  three of them + the number leave the field column cramped. Recommended:
  `@media(max-width:767px){ .stepcard{grid-template-columns:26px minmax(0,1fr)}
  .steptools{grid-column:2;justify-content:flex-end;margin-top:6px} }` — controls
  move under the step content, still adjacent to their step (the WCAG non-drag
  example is exactly "adjacent controls … tapping … up or down"
  `[WCAG-2.5.7]`).
- **Label the tool buttons.** They render as bare glyphs (`↑`, `↓`, `✕`) with a
  `title` only on remove (2371-2377). Add `aria-label="Move step 2 up"`,
  `"... down"`, `"Remove step 2"` `[WCAG-2.5.7 example + repo a11y conventions]`.
- `render()` rebuilds `#steps` with `innerHTML` (2331) — focus and screen-reader
  context are lost on every add/reorder/remove. Steer focus deliberately after a
  change (new card's type select after add; the moved step's button after a move)
  and announce with a polite live region ("Step 3 added — 4 steps"), the same
  focus-management discipline GOV.UK requires on validation errors
  `[GOV-error]` + `[repo: toasts aria-live, 813]`.
- Add an empty state inside `#steps` ("No steps yet — add one below") `[repo:
  ui-redesign §Feedback, loading, empty states]`; save-time validation currently
  flashes + redirects and loses the built steps (2484-2489) — apply the
  re-render-with-values fix `[GOV-error]`.
- `.stepfields` is a 120px-label | control grid (2260): "labels to the left" is
  the desktop idiom; on phones switch to one column so step field labels sit
  above their inputs `[Baymard]`.
- Add-step buttons (2312-2316): keep them after the last step; if the 5 buttons
  wrap into a tall block on a 320px screen, accept it or compact to a single
  "+ Add step" that appends a Move step — either way keep "obvious how users
  progress" `[NNg-pd]` and 44px tall `[repo]`.

### Desktop
- `.stepcard` 3-column grid with the numbered square and right-hand tools; keep
  the visual order cue (`.stepnum`) — it is the cheapest way to make "in order"
  legible `[NNg-pd: label expectations]`.
- `steps_json` is a hidden field synced only by JS (2326-2328); add a
  `<noscript>` line telling users the builder needs JavaScript, or the save will
  submit an empty list.

### Concrete changes
- [ ] `aria-label`s on ↑ ↓ ✕; keep `disabled` at the ends.
- [ ] Phone: tools move under step content; `.stepfields` single column.
- [ ] Focus + live-region announcement after add/move/remove.
- [ ] Empty state in `#steps`; POST-error re-render keeps steps.
- [ ] Keep buttons (not drag) as the reorder mechanism — already WCAG-safe; if
      drag is ever added, the buttons stay as the required alternative
      `[WCAG-2.5.7]`.

---

## 4. TEMPLATE EDITOR — `/templates/new` · `/templates/<id>/edit` (TEMPLATE_EDIT_TMPL, 2593)

**The question:** *"What does this reusable reply say, and which parts get
substituted from the message?"*

### Phone
- Two cards, single column: Template (name + optional subject) and Body
  (2594-2615) — the simplest page of the five; `grid2` already collapses
  (536-538). Keep `(optional)` on Subject — "clearly label optional fields"
  `[NNg-forms]`.
- **Give the body textarea a programmatic label, and move the placeholders
  legend above it.** Two fixes in one card: (a) the textarea (2611) is visually
  introduced by the `<h3>Body</h3>` but has no `<label>`/`aria-label` — GOV.UK is
  unambiguous that "all text inputs must have labels, and in most cases the label
  should be visible" `[GOV-input]`, so add `aria-labelledby` to the card heading
  (or a visually-hidden `<label for="t-body">`); (b) the
  `{sender} {subject} {date} {my_name}` legend currently sits *below* the field
  (2612) — help that explains an input belongs before it `[NNg-forms: explain
  input requirements]`/`[GOV-input: hint text]`: put it under the "Body" heading,
  give it an `id`, and wire `aria-describedby` on the textarea.
- Textarea: base CSS gives it the mono stack and 16px on phones (403, 728-730) —
  fine for placeholders; keep `rows` tall enough to write in without pushing the
  sticky savebar out of reach.
- Optional (nice-to-have): a small "Preview" toggle that substitutes the
  placeholders with sample values — matches the intent ("the LLM adapts it") and
  catches typos without changing storage. Preview-before-commit is a recommended
  settings trick `[Eleken]`.

### Desktop
- Keep name/subject side by side (short, logically related fields are the allowed
  exception to single-column `[NNg-forms]`); body full width at a readable
  measure.
- Keep the "keep it short — the LLM adapts it" helper (2610) `[Eleken: helper
  text]`.

### Concrete changes
- [ ] `aria-labelledby` on the body textarea; placeholder legend above the body +
      `aria-describedby`.
- [ ] Optional Preview toggle.

---

## 5. ACCOUNT FORMS — `/accounts/new` · `/accounts/<id>/edit` (ACCOUNT_NEW_TMPL 4860, ACCOUNT_EDIT_TMPL 4976)

**The question (new):** *"How do I connect this mailbox — and what do I do next?"*
(On the Accounts list the page then answers "is it authorised?"; the edit page's
question is *"how do I fix or update this connection?"*.)

### Phone — new
- Sectioned cards: Account / OAuth app / Login flow / (Custom details) — group by
  task `[Eleken]`, single column `[NNg-forms]`; `grid2` collapses (536-538) and
  inputs are 16px/44px on coarse pointers (648-658).
- The conditional blocks are already textbook progressive disclosure: custom
  provider fields hidden until `provider=custom` (4912, 4952-4957), Thunderbird
  reuse row only for Outlook (4888-4895, 4958-4966) — keep; label expectations
  "obvious how users progress" `[NNg-pd]` (the note text already explains it).
- The page is step 1 of a 3-step staged flow (add → register redirect URI →
  Authorise); staged disclosure "is useful when you can divide a task into
  distinct steps" `[NNg-pd]`. Strengthen the page-desc into a 3-line numbered
  hint (or a small `<ol>` with the three steps) so nobody wonders why nothing
  connects after saving.
- Local password (4880-4882): a generated credential in a plain text input with a
  click-to-reset `<span>`. Add a copy button (the Accounts page already ships
  `cp()` shared JS — reuse it, 4784-4791), make the control a real
  `<button type="button">` so it is keyboard reachable, and call it
  "regenerate" rather than "reset" — form-reset semantics are an anti-pattern
  users have been trained to fear `[NNg-forms: avoid Reset/Clear buttons]`; add
  `autocomplete="off" spellcheck="false" autocapitalize="none"` `[repo:
  mobile-ui-research §C]`.
- **Client secret should be a password field.** Both templates use `type="text"`
  (4898, 5008); Settings treats API keys as `type="password"` with
  `autocomplete="new-password"` (4236). Match that, and surface "set/not set" as
  visible helper text rather than a bare placeholder `[GOV-input]`.
- Validation (5071-5073): same re-render-with-values fix as the rule/flow editors
  `[GOV-error]`; keep the native `required` on provider (4872) and email (4879) as
  a progressive enhancement, not the only check.

### Phone — edit
- Cards: Account / OAuth app / Server (4985-5027) — keep; the savebar's
  "Listener: 127.0.0.1:…" note (5028) is useful context, keep it visible.
- Readonly email (4988) renders like an editable input; add a muted style or a
  `.kv` line so it reads as information, not a broken field `[NNg-forms:
  distinguish states]`.
- "blank = keep current" is stated on Client secret (5008) but **not** on Local
  password (4995) even though the server keeps a blank password (5102-5103) — add
  the same hint `[GOV-input]`.
- Secret handling as above (type=password + visible state).

### Destructive actions
- Account removal already lives away from the edit form — in the Accounts list
  card's "More" menu, destructive last, with a confirm naming the account and
  the consequence (4726-4728). That satisfies "keep only truly critical actions"
  in a bounded, labelled place `[Smashing]`; keep it there and do **not** add a
  delete control to the edit form. Keep "Reset tokens" adjacent to it (same
  reversibility class) and keep both visually danger-styled (`.menu-item.danger`
  exists, 507).
- The removal confirm text should keep naming the effect ("tokens and config
  entry are deleted") — specific verbs over "Are you sure?" `[Smashing]`.

### Concrete changes
- [ ] New page: 3-step arrow/ordered hint in the page-desc area; keep cards.
- [ ] Local password: copy affordance + real "Regenerate" button + no-autofill
      attributes; "blank = keep current" on the edit form.
- [ ] Client secret → `type="password"`, `autocomplete="new-password"`, visible
      set/not-set helper.
- [ ] POST errors re-render with values + error summary `[GOV-error]`.
- [ ] Edit: style readonly email as text, not an input.

---

## Sources (fetched 2026-10-01, keyless web via hound)

- `[NNg-forms]` NN/g — *Website Forms Usability: Top 10 Recommendations* —
  https://www.nngroup.com/articles/web-form-design/ (single column; labels above
  on mobile; group related fields; no placeholder text; optional/required;
  formatting requirements; error messages not colour-only; avoid Reset/Clear).
- `[NNg-pd]` NN/g — *Progressive Disclosure* —
  https://www.nngroup.com/articles/progressive-disclosure/ (keep the initial
  display small; make progression obvious and well-labelled; max ~2 levels; chunk
  advanced features).
- `[GOV-input]` GOV.UK Design System — *Text input* —
  https://design-system.service.gov.uk/components/text-input/ (label above,
  visible, sentence case; no placeholder-as-label/hint; one question per page).
- `[GOV-error]` GOV.UK Design System — *Error summary* —
  https://design-system.service.gov.uk/components/error-summary/ (always
  summarise; link each error to its field; identical wording; move focus;
  "Error:" in the page title).
- `[Baymard]` Baymard Institute — *Field Label UX: place labels above the field*
  — https://baymard.com/research-articles/mobile-form-usability-label-position
  (full-width fields so input is fully visible; room for required/optional and
  descriptions; landscape exception).
- `[Smashing]` Smashing Magazine — *How To Manage Dangerous Actions In User
  Interfaces* (Victor Ponamarev, 2024-09) —
  https://www.smashingmagazine.com/2024/09/how-manage-dangerous-actions-user-interfaces/
  (danger zones at the bottom of settings/account pages; specific verbs; danger
  icons; extra effort; dangerous ≠ delete-only: sending mail, granting
  permissions).
- `[WCAG-2.5.7]` W3C/WAI — *Understanding SC 2.5.7 Dragging Movements (AA)* —
  https://www.w3.org/WAI/WCAG22/Understanding/dragging-movements.html (any drag
  needs a single-pointer non-drag alternative; sortable lists provide adjacent
  up/down controls).
- `[Eleken]` Eleken — *Settings Page UI Design Guide with Real Examples* —
  https://www.eleken.co/blog-posts/settings-page-ui (group by task with labeled
  sections; helper text under each setting; hide rarely used fields; set
  destructive actions apart; split across tabs when a page holds too many
  settings; save feedback; sticky save bar that appears when there are unsaved
  changes).
- In-repo (established, keep): `docs/ui-redesign.md` (per-card saves, sticky
  savebar, sectioned editors, flows = linear WHEN/THEN), `docs/mobile-ui-research.md`
  (48px rows/8px gaps, no hover for reorder, 16px inputs, CSS-Tricks iOS-zoom,
  inputmode/enterkeyhint/autocomplete), `docs/assistant-mobile.md` (overlay
  z-order, ≤767px shell), `docs/dashboard-mobile.md` ("name the one question the
  screen answers").

## Second-pass notes / open questions

- Fetches that failed and were therefore **not** used: GOV.UK "Add another"
  pattern pages (both known slugs return 404 as of 2026-10-01 — the repeated-row
  guidance above is instead grounded in `[NNg-pd]` + `[WCAG-2.5.7]` +
  `[NNg-forms]`), Shopify Polaris ContextualSaveBar docs (URL now redirects to a
  generic index; the dirty-state save-bar behaviour is cited from `[Eleken]`).
- Between 768–900px the Settings grid collapses but the 44px/16px control rules
  (648-658) only apply on coarse pointers — verify on a real tablet before
  changing anything.
- The condition-row UI is shared between the rule editor and the flow builder:
  whatever progressive-row treatment is chosen must land in both templates
  (2086-2099 and 2290-2303) or the two editors will drift.
- Two server-side refactors unblock most of the validation UX: (1) POST/redirect
  → POST/re-render on validation failure for rules, flows and accounts; (2)
  redirect-to-anchor after successful Settings saves. Both are small, local
  changes in `app.py` and carry no template-contract risk.
