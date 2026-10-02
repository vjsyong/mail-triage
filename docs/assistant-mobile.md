# Assistant mobile chat — research + what was built (2026-10-01)

Goal: the assistant page should feel like a native AI chat app on phones.
Trigger: Sean's review — history rail at the top, boxed empty state, and the
composer pushed below the fold ("this assistant UI is not it").

## Sources
- Setproduct — *Designing AI chat interfaces: Anatomy, patterns, pitfalls* (Roman
  Kamushken, 2026-05; fetched 2026-10-01). Key mobile rules adopted:
  - Mobile = single column, full bleed; conversation list becomes a slide-in drawer
    behind a top-left button.
  - **Composer docked, never floating** — a floating composer that overlaps the last
    message is "the single most common mobile UX bug in AI chat"; dock it and give the
    message stream bottom padding equal to the composer height.
  - Send/stop large (44px+) and thumb-reachable; hide non-essential header chrome.
  - Assistant messages: flat, full-width — SMS-style bubbles signal "messenger" and
    most serious AI chat (Claude/ChatGPT/Cursor) moved away from them.
  - Empty state: small greeting + suggestion chips, not a wall of text.
  - Streaming: caret/alive signal, stop button near the composer, auto-scroll only
    within ~100px of the bottom with a Jump-to-latest affordance.
- In-repo prior work: `docs/ui-redesign.md` (AI UX Playground, thefrontkit AI Chat UI
  Best Practices 2026, in-repo chat checklist) and `docs/mobile-ui-research.md`
  ([C]-tagged items: scroll anchoring, `overscroll-behavior: contain`, MDN patterns).

## Applied (assistant page, ≤767px)
1. **History → slide-in sheet.** The page rail is moved at runtime into a left
   slide-in panel opened from a ☰ header button (full-width tap rows, delete confirm
   unchanged). Desktop keeps the inline rail. No JS ⇒ rail stays inline (graceful).
2. **Slim chat header.** `☰ History  ·  Assistant  ·  ✚ New chat` replaces the
   page-head on phones (the tab bar already names the section).
3. **Composer, single row.** Textarea grows; Send (and Stop while streaming) inline at
   the right; docked above the tab bar. The shell is a fixed-height flex column
   (`100dvh` minus chrome) so the composer is always visible — the below-the-fold bug
   is structurally impossible now. `enterkeyhint="send"` on both composers.
4. **Message stream** flexes between header and composer (internal scroll); all
   auto-scroll / Jump-to-latest / streaming behavior unchanged. Assistant messages
   render flat full-width; user messages stay right-aligned; avatars hidden on phones.
5. **Empty state** compact: small icon, title, suggestion chips; long description
   hidden on phones.

## Not changed
Desktop layout, streaming engine, tool chips, rule-proposal cards, permission/approval
UI, drawer (its composer inherits the same single-row treatment).

## Verification
Emulated phones (393×852 and 360×740): composer above the tab bar, document height ==
viewport height, history sheet opens/closes with the rail inside (reviewed: reads like
the ChatGPT/Claude drawer and covers the tab bar), drawer covers the full screen,
empty state vertically centered under one slim header. `tests/mock_e2e.py` green.

## Gotcha fixed along the way
The page's inline script ran during parse — BEFORE BASE_TMPL defines
`window.assistantChat` — so the page chat binding threw and every page conversation
silently fell back to a plain form POST (no streaming). Init is now deferred to
`DOMContentLoaded`; keep that deferral.

## Overlay z-order (mobile)
topbar 30 < fab 60 < bottom-nav 180 < sheet 190 < drawer 220 < toasts 300.
Any new full-screen overlay must sit above 180, or the tab bar punches through it
(this bit both the history sheet and the assistant drawer during review).


### Keyboard-safe composer (2026-10-01, evening)
Report: on iPhone, focusing the composer buried the input behind/below the tab bar
with the keyboard up. Old mechanism lifted `#aform` with translateY but the
container (`height: calc(100dvh - 115px)`) never shrank, `.jumpwrap` kept a
`min-height:420px` floor, and iOS keyboard events could arrive late.

Now: while the keyboard is up (`body.kb-open`), `.assistant-main` is resized to the
visual viewport (`--kb-h` = vv.height - rect.top, computed in `fit()`), so the
composer - last child of the flex chain - always lands exactly on the keyboard line;
`.jumpwrap{min-height:0}` releases the floor; the tab bar hides. `#dform`/`.savebar`
keep their translateY lift. iOS flakiness covered with focusin retries (120/400ms)
and a 250ms poll for ~3s after focus. `window.__mtKbFit` is exposed for tests:
emulate a keyboard by overriding `window.visualViewport` ({height, offsetTop}) and
calling `__mtKbFit()` - verified composer bottom == keyboard line, nav none, restore
clears everything.


### Page context (same evening): "this email" means the email on screen
- Client: a base-script tracker (`window.mtCtxPath` / `mtLastCtx` in sessionStorage)
  remembers the last meaningful page (any non-/assistant path). Chat sends from the
  drawer AND the assistant page attach `path=...` to the /assistant/stream POST;
  when on the Assistant tab the last meaningful page is used instead, so tapping
  over from a message keeps the email context.
- Server: `engine.assistant_page_context(path)` resolves the page to a system-prompt
  block: message (subject/from/to/date/folder/status/action/verdict + "pass id N to
  tools"), flow (full conditions+steps text), rule (same), classifier, template,
  messages list, known static pages; account pages stay generic (NO secrets ever).
  The block is appended in AssistantAgent.stream() after _assistant_context().
- UI: `.dw-ctx` (drawer) and `.am-ctx` (assistant page) chips show
  "Context: message · <subject>" via /assistant/context.json; hidden when empty;
  truncated with ellipsis.
- Verified live: drawer chat from a message returned the subject+sender from the
  page block; the chip carried across to the Assistant tab; flow/rule contexts
  resolve with real ids.


### Contextual intelligence (same day): scoped sessions + proactive prefill
- **Sessions scope to the page.** `assistant_page_context` now returns a compact
  context KEY (message:3563, flow:15, rule:2, page:/settings). The base tracker
  publishes it (`window.__mtCtxKey` + `mt:ctxkey` event); the drawer compares it to
  `mtSessCtx` (sessionStorage) and silently starts a fresh session when the context
  changes to a non-empty, non-streaming chat. Old conversations stay in History.
  Empty chats just re-scope (no session spam). First load adopts (resumes).
- **Entity suggestion chips.** `_suggestions_for_path` gained entity-level lists:
  on a flow/rule page the first chip is "Test it in the simulator →" — a LINK chip
  (new: chips may carry `href` instead of a prompt) straight to
  `/simulate?flow=N` / `?rule=N`; the rest are explain/conflict prompts.
- **Simulator prefill (nothing to type).** `engine.example_draft_for(kind, gid)`
  writes ONE example email that satisfies the rule/flow conditions — model-written
  (JSON mode) with a deterministic fallback built from the conditions. `/simulate`
  accepts `?flow=N` / `?rule=N` / `?t=flow:N` (also via the "Prefill from" select +
  "Generate an example draft" GET button inside the draft card); the form arrives
  filled with a note explaining where the draft came from. Nothing runs until Run.
- **After saving a flow**, `/flows` redirects with `?test=<id>` and shows a banner:
  "Flow saved. … Simulate a draft that tests it →" (one click to the prefilled sim).
- Verified live: message → chat → switch to a flow page = fresh scoped session
  (DB-confirmed session count +1 per switch, send + reply verified); flow page chips
  render the simulator link; click-through lands on a model-written prefilled draft
  ("PhD Inquiry: Research Opportunities in your Lab" for the real PhD flow).

### Simulator page: the assistant must fill, not fumble (2026-10-02)
Report: asking the drawer (on /simulate, chip "Context: simulator") to help fill the
draft made it search mail/drafts and ask "which email?" - the generic one-line page
block ("the user is on the simulator") taught it nothing. Fixes:
- A real `/simulate` context block: what the page is (dry-run draft -> rules/flows/
  classifier), the prefill affordances ("Prefill from" select + "Generate an example
  draft", `/simulate?flow=<id>` / `?rule=<id>`), and the expected behaviour when the
  user wants to test a flow/rule: find it (list_flows / list_rules), then EITHER give
  concrete From/To/Subject/Body values that exercise it, OR point at the prefill with
  a link like `[Test "<name>" ->](/simulate?flow=<id>)`. Explicitly: no mailbox or
  draft searches, do not ask which email they mean; reply with the values or the one
  line of guidance, not a tool play-by-play.
- Simulator-shaped empty-state chips (`ASSIST_SUGGESTIONS["simulate"]`): fill a draft
  to test a flow / to test a rule / how this page works.
- Chat markdown now renders SAME-ORIGIN relative links (`/simulate?flow=3`) as in-app
  links in BOTH renderers (python `md_to_html` + JS `mdRender`) so the prefill link is
  one click; protocol-relative (`//host`) and `/\host` stay blocked (only `https?://`
  or a single-slash path renders).
- Checks: simulator chips replace the default set on /simulate; the context block
  names the prefill path; relative links render in-app while `//evil.com` does not.
