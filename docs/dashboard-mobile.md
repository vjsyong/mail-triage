# Dashboard — mobile rethink (2026-10-01)

Brief: don't reflow the desktop cards into a phone frame; rethink the layout from
first principles. Research: Fuselab *Mobile Dashboard Design: What to Leave Off the
Screen* + UX Pilot *12 Dashboard Design Principles* (both fetched 2026-10-01), plus
the earlier desktop pass (Domo / Improvado / ClearPoint / NN-g — see
`docs/ui-redesign.md` §Fourth pass).

## First principles (cited)
- **Name the one question the phone screen answers**, then remove everything that
  doesn't serve it — "responsive design decides layout, not what earns the first
  position" (Fuselab). Named failure modes: shrinking a desktop screen; designing
  outward from the data model instead of inward from the task.
- Opening view ≈ **3–5 chunks** (Cowan/NN-g progressive disclosure via Fuselab;
  UX Pilot cites Miller/Cowan — working memory holds 3–5 items).
- Attention **decays left→right / top→bottom** across equal-weight tiles (Tableau
  via UX Pilot) — rank by size, not democracy.
- Fitts: primary controls large and reachable; timestamps can sit in corners;
  drill-downs/filters near the center band (Hoober via Fuselab).
- Progressive disclosure: one entry point per concern, detail a tap away.
- Exception-first + honest state ("last updated X ago" beats a spinner);
  colour = status only and never alone (text labels; WCAG 4.5:1).

## The question this screen answers on a phone
**"Is everything running, and is there mail waiting for me right now?"**

## Inventory — every item and where it lands
1. Triage status (dot, last check, next-in) — was the system strip → inside
   **System status** (collapsed one-liner when healthy).
2. Proxy status + listener ports — system strip → System status.
3. Search-index status (chunks/folders/last run) — system strip → System status.
4. LLM status + calls-per-hour + fallback — system strip → System status.
5. Parked-errors alert + Retry parked — was a conditional strip line → stays in
   System status, and **forces it open** (exception-first).
6. Queue-length alert + View queue — conditional strip line → System status.
7. **needs-reply count — was the primary metric tile → THE HERO** (largest element,
   taps into the filtered Messages list).
8. Parked-errors metric tile → demoted (the #5 alert owns it); hidden on phones.
9. Waiting-for-LLM tile → demoted (#6 owns it); hidden on phones.
10. Sorted-by-rules + 11. LLM-classified + 12. rules-enabled tiles → folded into the
    hero's one-line context (percent of seen / rule count).
13. Rules/LLM/auto-file mode line + "change" → kept, small, under the hero.
14. Check now / Open messages — was page-head corner (worst reach) → moved into the
    hero card.
15. Recent mail (10 rows) — hero card → top 4 + "All messages →" (rest on Messages).
16. Search-index card (badge, stats, Index now / Rebuild) — right column → card
    hidden on phones; buttons fold into System status; stats already in #3.
17. Activity feed (15 events) — right column → collapsible, closed by default on
    phones (auto-reload while indexing kept).
18. Dashboard auto-reload while indexing — kept.

## Mobile opening view (≤767px), top → bottom
1. **System status** — one line: ● + "All systems normal" (+ chevron). Auto-opens
   whenever anything is off (`data-alert=1` rendered server-side).
2. **The number** — needs-reply, oversized, tappable; context line
   ("sorted by rules · classified % of seen · rules active"); **[Open messages]
   [Check now]**.
3. **Recent mail** — 4 rows.
4. **Activity** — collapsed.
Everything else is one tap away (expand System status, Messages, Log).

## Desktop
Visually unchanged: system strip fully open, six metric tiles, index card, activity
feed open. All new affordances are additive markup + mobile-scoped CSS
(`summary` rows hidden ≥768px; collapse JS is `matchMedia`-guarded).

## Verification
Emulated 393×852: collapsed system line (healthy), hero + context + actions, four
recent rows, activity closed; expanding System status reveals the four subsystems +
index actions; with `data-alert=1` it stays open. Desktop 1440×900 unchanged.
`tests/mock_e2e.py` — new T31 structure checks + full suite green.


### Hero card refinement (2026-10-01, evening)
The two run-on prose lines ("Rules act live ... — change" and "2479 sorted by
rules ...") wrapped mid-phrase on phones ("— change" dangling; "12 / rules active"
split). Replaced with:
- `.dstat` key-value rows (mobile only; desktop keeps its metric grid):
  label left, right-aligned tabular numbers ("2,479"), percent as dim suffix
  ("99% of 3,559"); "Rules active" value still links to /rules.
- `.dsc` status chips: Rules live / LLM on / Auto-filing ON / Settings ->, each an
  atomic unit (a chip can wrap whole, never mid-phrase). Green square dot for
  on-states, dim for off; the Settings chip is the "change" affordance now.
  On phones the chips render as a deliberate 2x2 grid (single line of four at
  393px left "Settings ->" orphaned on line two at 375-430px; 2x2 is deterministic
  at any width). Desktop keeps one flex line.
