# Assistant page: layout pass

Research + decisions for the `/assistant` page layout (2026-10-02). Two drivers:
a real bug (the mobile chat header rendered stacked at every width above 767px)
and a layout that did not read as a modern AI chat.

## What the sources say

setproduct.com, *Designing AI chat interfaces: anatomy, patterns, pitfalls*:
- The anatomy is fixed: conversation list, slim header (never competes with the
  message area), the message stream as the product, composer, per-message
  actions, optional follow-up chips.
- Reading width is capped - Claude ~768px, ChatGPT ~768, Perplexity ~720.
  Beyond that, long answers become unreadable.
- The composer grows with content up to a cap; Enter/Shift+Enter defaults.
- A **disclosure surface** (what the model is, what it can see, what it is
  allowed to do) must be always-visible but should not be a wall: identity near
  the input, details on demand.

aiuxdesign.guide, *Anatomy of a Chat Interface*:
- User messages right, assistant left - universal convention. Max width 60-75%
  of the container.
- Spacing carries grouping: 8-12px between messages of the same sender,
  16-20px when the sender changes.
- The empty state is the most important screen: 1-2 sentences of welcome, 3-4
  suggested prompts, no feature dump - get the user typing within seconds.
- The first message should say what the assistant is for, what it can already
  see, and one thing worth trying.

NN/g, *Designing Empty States*: communicate status, teach in-context, provide
direct pathways (buttons). Never leave dead space.

uxdesign.cc, *The forgotten conversation problem*: even title-only search beats
none; conversation lists should group by recency and be filterable.

## Findings on the current page

1. **Bug**: `.chat-head` (the mobile ☰ / title / + header) was only ever styled
   inside the <=767 block; above 767 it fell back to `display:block` and its
   icon buttons stacked vertically - 91px of broken chrome on desktop/tablet.
2. The composer hint carried the whole permission wall as one run-on line:
   "runs on ... agent permissions: may do directly: classify, flag, ...".
3. The "Context: ..." indicator floated as a low-contrast line above the
   composer instead of reading as part of the input area.
4. The rail was cramped, had no grouping, no filter, and used absolute
   timestamps ("10-02 14:25") that scan poorly.
5. The empty state's description ran to two long sentences.
6. The message stream had no reading-width cap (~850px wide at desktop), and
   consecutive same-role messages used the same 18px gap as sender changes.

## Decisions

1. `.chat-head` is `display:none` at every width by default; the <=767 block
   re-enables it as flex (mobile only). Desktop keeps the slim page-head.
2. **Reading column**: the thread, pending panel, and composer are capped at
   820px and centered inside the assistant column; bubbles stay <=75% of that.
   The composer hint row stays single-purpose.
3. **Disclosure surface** becomes a collapsed `<details>` under the composer:
   a model chip + "assistant details" that expands to grouped permission chips
   (Can do directly / Asks first / Off) plus the edit link. No JS.
4. **Context indicator** moves INSIDE the composer form as a small chip above
   the textarea (identity/context near the input, per the research).
5. **Rail**: grouped by Today / Yesterday / Earlier (display timezone),
   compact times (HH:MM today, MM-DD before), a title filter input, a clearer
   active row (inset bar + white on hover), and a friendly empty state.
6. **Empty state**: one short sentence, four chips, no marketing.
7. **Spacing**: consecutive same-role messages pull together (-10px) so
   role changes read at the researched 16-20px and same-role at ~8px.

Files: `app.py` (BASE_TMPL + ASSISTANT_TMPL + `_assistant_page` context), suite
pins in T52. All styling uses the existing design tokens; zero radius stays.

## Follow-up: message affordances (2026-10-02)

- **Regenerate**: the newest assistant reply carries an ↻ control (rendered in
  the server fragment and added by the live stream on done). It re-runs the
  same user turn through `/assistant/regenerate`, which replaces the trailing
  reply in place: the agent streams with `store_user=False`, so the user
  message is never duplicated, and only the newest reply offers the control.
- **Bubbles**: the assistant bubble carries a black border on desktop and
  mobile; the user bubble stays black-filled. Mobile no longer overrides
  `.crow.user` with `justify-content:flex-end` - inside a `row-reverse` flex
  that packs LEFT, the opposite of the documented user-right convention.
