# Messages — mobile layout redesign (2026-10-01)

Trigger: "too many stray elements and weird element spacing". Same first-principles
frame as the dashboard pass: the list is the product; controls serve it, not the
reverse (Fuselab: subtraction before decoration; UX Pilot: hierarchy over equality,
Gestalt grouping).

## Inventory → what changed (≤767px only; desktop verified unchanged)
1. **Toolbar was one wrapping flex row**: 6 filter chips + 2 action buttons wrapped
   into 4 ragged rows with unbalanced gaps and mixing filters with actions.
   → Now: chips = ONE horizontally scrollable row (`scrollable`, no wrap, active
   chip black); the two actions = their own full-width row (Classify all / Learn
   rules, shortened labels on phones via `.mhide` spans).
2. **Cards stacked every cell vertically** (timestamp / sender / subject / snippet /
   badge / classification = 6 stray lines + checkbox floating in a gap).
   → Now a native mail-row layout (flex, reordered with `order`):
   line 1 = sender (600 weight, ellipsis) + timestamp right; line 2 = subject;
   line 3 = snippet clamped to 2 lines; line 4 = badges row (status left,
   classification right). Checkbox = clean absolute top-right (20px target).
   Scoped to the messages form (`#bulk`) so the dashboard's `mcards` table keeps
   its own layout.
3. **Pager** on phones: buttons half-width each side by side, "page X of Y" centered
   underneath; "Last »" + per-page chips hidden on phones (desktop keeps them; the
   test-pinned strings stay in the DOM).
4. Empty-cell cleanup (`.tbl.mcards td:empty{display:none}`) kills hollow gaps.

## Kept
Bulk-selection flow (dark bar appears below the toolbar, verified), pagination
strings, filters semantics, desktop table.

## Verification
Emulated 393×852: chips scroll (616px content in a 351px row), action row split
evenly, four-line cards, checkbox top-right, bulk bar clean; desktop 1440×900
regression-checked visually — table + toolbar unchanged. `tests/mock_e2e.py`:
new T32 hooks + full suite green.
