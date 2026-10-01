# Critique round 1 — process + findings (2026-10-01)

Process: two independent critic subagents were dispatched with 8 screenshots each
(mobile + desktop) and the research briefs as their criteria. Both ran the full
600s budget doing deep verification crops and never wrote their deliverable
files. Their transcripts were recovered and mined, every candidate finding was
re-verified live against the deployed app, and the confirmed defects were fixed.

## Fixed (verified live after the fix)
1. **Rules/classifiers phone row showed a stray red `delete` next to `⋯`.**
   Root cause: the delete `<form>` carries `class="inline ra-inline"` and the base
   rule `form.inline { display:inline }` (0-1-1) outranked `.ra-inline { display:none }`
   (0-1-0) — the edit link hid but the delete form didn't. Fix: `.rowacts .ra-inline`
   (0-2-0). Caught by critic A, confirmed via DOM dump + before/after crops.
2. **Settings helper rendered "timestamps show as ."** — `{{ tz }}` was never passed
   into the settings template scope. Fix: `tz=tz_label()` in the settings render.
   Caught by both critics from source + screenshot.
3. **Desktop rules table: two-digit `#` numbers wrapped** ("1" over "0") in the
   34px column. Fix: 44px column + `white-space:nowrap` on the cell.
4. **Settings sub-nav had no active state (desktop)** — brief §1 asked for it.
   Fix: IntersectionObserver scrollspy marking `.setnav a.on`.

## Checked, judged fine (no change)
- Assistant suggestion chips "look rounded": false alarm — `border-radius:0
  !important` is global; pixel crop of the chip corner shows a hard 90° step.
- "Show 3 more conditions" missing: present and visible on /rules/new ≤767
  (the critic's crop band simply missed it).
- Log `Refresh` lives at the end of the horizontally scrollable filter row:
  consistent with the established chip-row pattern; kept.

## Found, accepted as-is (documented)
- Messages desktop TAG column renders empty when no tags exist (reserved column).
- Desktop rules/testers/etc keep top-right buttons for mouse reach (per the
  shell brief's [skip] decisions).
- Empty-state and progress polish from the shell brief already shipped; the
  status-fragment refactor for classify/index polling remains deferred.
