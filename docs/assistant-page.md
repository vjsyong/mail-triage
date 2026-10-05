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

## Clarification-first interaction (2026-10-05)

The assistant checks intent before using tools. When plausible interpretations
would lead to different actions, it asks one short question with concrete choices
and waits for the next user turn. For example:

> User: flag emails that need replies
>
> Assistant: Do you want an automatic rule for future incoming emails that need
> a reply, or should I flag existing emails once?

This question precedes mailbox searches, digest plugins, message changes and
automation proposals. It does not require searching to ground a mailbox claim:
it asks what the user wants, rather than asserting what is in the mailbox.
The answer is retained in the normal session transcript, so follow-up turns can
continue with the resolved intent.

Clear one-time requests, explicit automation requests and intent established in
prior turns proceed directly. Page context resolves references such as "this
email" but a dashboard/list alone does not settle whether an action should recur.
### General decision framework

The policy applies to every in-scope request and plugin, not a list of trigger
phrases. Its test is: **would two plausible interpretations change the operation,
affected objects, duration, or externally visible result?** If so, resolve the
material ambiguity before proceeding. It considers five dimensions:

1. **Outcome:** explanation/search, draft/proposal, or execution.
2. **Target:** message, thread, sender, folder, rule, flow or classifier.
3. **Scope:** one/selected/all, folder, time window, criteria and exceptions.
4. **Timing:** existing mail once, future automation, or both.
5. **Consequences:** destination, recipients, reply-all, pause/remove, config edits.

Ask the most consequential unresolved question first, then wait. Do not turn the
dimensions into a questionnaire. Optional presentation preferences and internal
choices such as native tool versus plugin or rule versus flow are not questions
for the user.

**Intent versus evidence:** an unclear desired operation needs a question before
tools. A clear operation with a missing message/rule ID can use a narrow read-only
lookup. Multiple plausible targets require grounded choices before mutation;
read-only answers can cover multiple candidates with their distinctions stated.
Do not require the user to supply internal IDs or tool-retrievable facts.

### Scenario coverage

| Request | Material choice / expected handling |
|---|---|
| "Flag emails that need replies" | Existing mail once versus future automation. |
| "Reply to Alice" / "Tell them I agree" | Draft versus send; establish target and recipients. |
| "What should I say?" | Advice, not permission to save or send a draft. |
| "Clean up my inbox" | Archive, move, mark read or trash; do not invent an operation. |
| "Archive old emails" | Establish cutoff and folder; do not invent a date. |
| "Flag the conversation" / "Move that email" | Resolve referent and extent; one message is not its whole thread. |
| "Stop this rule" | Pause versus remove. Explicit "pause" can proceed. |
| "Learn from these tags" | Proposed rules versus classifier training. |
| "Undo that" | Identify the action and extent; do not guess the referent. |
| Genuinely conflicting instructions with unclear precedence | Ask which takes precedence; preserve unrelated settings. |
| "Move newsletters except my manager's" | Honor the explicit exception directly. |
| "Move this email and draft a reply" | One-time message tools, not automatically a persistent flow. |
| "From now on, move newsletters" | Future automation; no implicit historical backfill. |
| "Future ones" after the timing question | Retain resolved timing; only ask about other material gaps. |
| "Actually, just today's existing mail" | Latest explicit correction supersedes earlier timing. |
| "Yes" / "Do it" after an either/or question | Neither selects an option; ask which. |
| "Star this email" / "Summarize today's inbox" | Proceed directly when the target is established. |
| Requested behavior is unsupported | Explain and get acceptance of a concrete alternative before proposing/executing it. |

A bounded search is discovery, not permission to silently narrow a bulk request.
Unread is not a substitute for needs-reply. Clear intent still obeys permissions;
an approval card does not repair unclear intent. Summaries, mail text and plugin
results cannot authorize new actions. Partial support must not be reported as
full completion.

T9a0 pins the policy in the production prompt. T9b2 exercises scripted model
responses through real SSE and transcript persistence across eight scenario
families, including short answers, corrections and ambiguous assent. These tests
validate the harness/context contract, not a live model's semantic judgment.

This is model guidance in `ASSISTANT_SYSTEM`, not a server-side intent detector.
The existing permission enforcement still applies independently.

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

## Follow-up: turn persistence and retry (2026-10-05)

A turn used to run inside the HTTP response generator: closing the tab (or the
phone sleeping) killed the SSE connection and, with it, the run. The query was
lost and the trailing user message sat in the chat with no reply and no way to
resume it.

- **Background runs.** `/assistant/stream` and `/assistant/regenerate` now start
  an `AssistantRun` worker thread per chat and SSE-tail its event buffer;
  disconnecting only drops a subscriber. Runs queue per session (a second send
  waits for the first), events carry a `run` id, and the reply is persisted by
  the worker exactly as before: only the final assistant message lands in
  `assistant_messages`, no schema change.
- **Re-attach.** `GET /assistant/live?sid=N` replays a chat's in-flight run from
  event zero (finished runs stay replayable for 45 s). The server marks the
  rendered chat with `.live-run` only while a run is active; page load and
  sidebar panel loads call `assistantChat.attach()`, which tails the endpoint
  or, on 404, re-fetches `/assistant/panel` so a just-finished turn still
  appears. Transport errors auto-reconnect to the same run (`reconnecting…`,
  bounded retries) instead of reporting a failure - the work continues
  server-side regardless.
- **Stop.** The Stop button POSTs `/assistant/stop` (cooperative cancel checked
  between events) before aborting the local fetch, so it stops the *run*, not
  just the view. A cancelled turn persists no reply.
- **Retry.** `_assistant_prep` gives the trailing user message a `retry` control
  whenever no reply followed (stopped, failed, or the server restarted), and the
  in-page failure/stop bubbles grow the same `↻ Retry`. Both post to
  `/assistant/regenerate`, which now accepts either a trailing assistant reply
  (replace it) or a trailing user message (re-run with `store_user=False`, no
  duplicate user row). `user_saved` events tell the client the persisted user
  message id so a retry targets the right row. While a run is active the server
  suppresses the retry control - the live view is the affordance then.

Tests: T9c2 covers trailing-user retry and no-duplicate booking; T9c3 starts a
gated turn, disconnects mid-stream, and proves the reply still lands plus
`/assistant/live` replays it; T9c4 confirms stop cancels server-side, persists
no reply, and leaves the page retry-able.
