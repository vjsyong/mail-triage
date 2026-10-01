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
