# Dashboard "Recent mail": the stacked feed (design record, 2026-10-02)

Trigger report: *"the recent mail container in the dashboard is way too squished — it
works fine when the width is wide enough on the messages page."* This note records the
diagnosis, the research it was built from, and the contract for future edits.

## Diagnosis (measured, not eyeballed)

The card sits in `.dashgrid`'s `1.6fr` column. That column is **218–673px wide** across
real configurations (assistant rail open/collapsed × window width; `.content` caps at
1160px, so it never gets wider), while the 5-column table it held has a natural width of
**686px** (its min-content floor — tables cannot shrink below it):

| config | card width | table | result before |
|---|---|---|---|
| 1280, rail open | 376px | 686px | 310px hidden behind a horizontal scrollbar; rows 62–304px tall |
| 1440, rail open | 474px | 686px | overflow 686/472; subject crushed to a ~150px sliver, wrapping 2–4 words per line |
| 1024, rail open | 218px | 686px | overflow 686/216; the worst case — a sliver of columns |

Mechanism (the recurring class): a row grid of mostly fixed columns in a container
narrower than the content — the payload column (subject) receives the leftover sliver
and every cell wraps mid-phrase. On `/messages` the same table gets ~1000px+ and reads
fine; it is the dashboard column that can never fit it.

## Research (fetched 2026-10-02)

- **CSS-Tricks, "Responsive Data Tables"** — the canonical reflow answer: below its
  minimum comfortable width, a table should stop behaving like a table; rows become
  blocks and each record keeps its meaning through layout instead of column position.
  (Also lists the alternatives: swap for a graphic, mini-table link, hide non-essential
  columns — all rejected here because the widget is a preview of records, not a dataset.)
- **Material 3 lists** — two-line anatomy: primary text + supporting text, keep items
  short and easy to scan, consistent slots for icons/text/actions; tallest element in
  the item sets its height.
- **App's own bar** (`docs/ux-benchmarks.md` §1): row = sender + subject + snippet +
  time. The dashboard widget is a *recent-items feed*, not a data table — the full
  table belongs on Messages.

## As built

- The card renders `.mfeed` > `a.mrow` rows (dashboard-only classes; markup in
  `DASH_TMPL`):
  1. sender (dim, ellipsis) + time (right, mono)
  2. subject (link colour, one line, ellipsis; `title` keeps the full text)
  3. LLM summary (dim, one line, only when present)
  4. meta line: status badge + LLM verdict (`Notification (97%) · needs reply`)
- The **whole row is the link** (feed convention: Gmail/Linear/Vercel rows; target
  height 93–114px, well past the 44px minimum). Clips: from 44 / subject 90 /
  summary 140 chars, CSS ellipsis as the backstop.
- Same-disease fix next door: dashboard **Activity** rows had `[time][badge][message]`
  inline, leaving the message a **~63px sliver** (13 wrapped lines per event). Scoped to
  `.actwrap`, rows are `display:block` so the message flows full-width; `/log` is
  untouched. (Lesson repeats: in a narrow card, meta goes on its own line and the
  payload gets full width.)
- Phone (≤767px) still shows the first 4 rows (`.mfeed .mrow:nth-child(n+5)`); no
  horizontal overflow at 393px either.

## Contract for future edits

- Do **not** reintroduce a `.tbl`/`.tbl.mcards` table into the dashboard recent-mail
  card. The column cannot fit it; the feed is the answer.
- Tests pin this: T31 "recent mail renders as a stacked feed, not a squished table"
  (mfeed present, ≥3 rows, `<th>when</th>` absent) and "feed rows keep subject link +
  status/LLM meta".
- Meta strings must keep agreeing with `/messages` (short status badge text, same
  LLM verdict string). Numbers/labels across pages are a test-consistency rule.

## Verification

- CDP probes: after = no overflow at 1280/1440/1024/393 (`document.scrollWidth ==
  innerWidth` and `.mfeed` never overflows); every row line single-line (heights 20/23/19/22px);
  card height 1717px → 1107px at 1280/1440 while showing the same 10 rows.
- Suite: `tests/mock_e2e.py` 526 passed, 0 failed (2 new checks).
- Side note fixed en route: the T15 ordering fixture used a hardcoded near-term date
  ("Fresh message latest") that rots once the wall clock passes it (undated rows fall
  back to `processed_at` and out-rank it). Now a relative now+1d date; it flaked to red
  this morning for everyone.
