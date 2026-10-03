# Onboarding and the setup wizard

The first-run experience for new installs. Research + decisions for the `/welcome`
flow, written before the rebuild (2026-10-02). The earlier version was a flat
checklist rendered as a regular app page; it read like a settings screen, not a
setup experience.

## What the sources say

Nielsen Norman Group, *Wizards: Definition and Design Recommendations*:
- Use wizards for novice users and infrequent processes such as configuration or
  setup - exactly this case. For repeat use, offer a faster path elsewhere.
- Communicate a clear mental model: show the list of steps and highlight the
  current one, so users know how long the process is.
- Enforce a clear sequential order; allow going back, not jumping ahead.
- Label navigation by what happens next, not bare "Next".
- Let users exit midway and resume later; steps should not lose work.
- Help text lives NEXT TO the wizard, never covering it.

ui-patterns.com, *Wizard design pattern*:
- Break one goal into dependable sub-tasks, one screen each; keep screens few
  (but do split screens that overflow).
- Show progress and which steps are completed; Previous/Next with Finish at the
  end; keep a separated Cancel; keep content and navigation above the fold.
- Plain language for untrained users; summarize choices near the end; prefill
  good defaults.
- The wizard complements, never replaces, the full interface.

Living examples (self-hosted products):
- **Jellyfin** setup wizard: sequential full-screen steps (language, admin,
  libraries, metadata, networking), skippable steps ("click Next without adding
  anything"), and a "Next steps" list at the end.
- **Home Assistant** onboarding: takes over the whole screen, one decision per
  screen, then Finish -> dashboard.
- LogRocket (when NOT to use a wizard): wizards cost extra clicks and are not
  gracefully interruptible - so state must auto-detect and resume, and advanced
  users must be able to skip the whole thing.

## Decisions for Mail Triage

1. **Full-screen takeover.** `/welcome` renders without the sidebar, topbar,
   assistant rail, or mobile tab bar (body class `setup`). First-run is a mode,
   not a page among pages. An "Exit setup" action is always visible, and the whole
   wizard is skippable.
2. **Three steps, two bookends.** Steps: Mailbox, LLM, Search index. Bookends: a
   welcome hero (only when nothing is done) and a finish recap. The rail lists
   the three steps and marks the current one; bookends show the rail dimmed
   (fresh) or complete (done).
3. **Resume by auto-detect, not stored progress.** Step state is computed from
   real system state (accounts, LLM endpoint, index counts), so the wizard
   always opens at the first incomplete step and every step can complete
   out-of-band (e.g. OAuth finished on the Accounts page). No half-filled state
   is ever lost because none is kept - re-entry is always safe.
4. **Work happens in place where possible.** The LLM step edits the same
   settings the Settings page would (same save path + a `next` redirect back
   into the wizard); its `Save & test` button saves the typed values and probes
   the endpoint in one step, so testing never checks a stale configuration.
   The index step triggers the real indexer and polls live counts. Only the
   mailbox step links out (the OAuth dance owns the Accounts page).
5. **Language.** Plain words, no internal jargon; each step answers "what is
   this, why should I care, what happens if I skip it". The safety promise
   (never deletes mail) sits in the wizard footer.
6. **Skip affordances.** Every step is deferrable ("Do this later"); the banner
   on the dashboard is the persistent resume entry, the More page carries the
   same link, and a fresh install lands on the wizard automatically (only when
   nothing is configured: no accounts, no LLM, no mail). Explicitly exiting or
   skipping sets `welcome_skipped`, preventing another automatic takeover while
   keeping the incomplete-setup banner. `welcome_done` separately hides the banner;
   resetting setup clears both preferences.
7. **It complements the app.** Everything the wizard does exists on the normal
   pages; the wizard is a guided path, per ui-patterns' "allow alternatives".
8. **Narrow screens.** The same flow, single column; the rail stays a compact
   strip at the top; transitions are directional (forward slides in from the
   right, back from the left) and respect `prefers-reduced-motion`.

## Implementation map

- `app.py`: `WELCOME_TMPL` (the wizard), route `/welcome` (`?s=N` deep links,
  POST `skip`/`dismiss`/`reset`), `POST /welcome/test-llm` (save + probe in one step),
  `GET /welcome/state.json` (live step/state polling
  for the index step), `render(..., setup=True)` (chrome-free shell),
  `setup_state()` (3 steps), `_fresh_install()` + dashboard redirect, `next`
  redirects on the settings / test-llm / index endpoints.
- Suite: T51 pins the wizard structure, start-step logic, state.json, `next`
  redirects, banner, skip/dismiss/reset, and the fresh-install redirect, including
  a regression check for exiting with no mailbox or LLM configured.
