# mail-triage — app-wide layout campaign (2026-10-01)

Goal: at once — inventory every page, research the best layout per page from UI/UX
first principles, implement across the app, then have an independent critic nit it.

## Surfaces & their one question

| # | Route | Job it does (one question) | State | Treatment |
|---|-------|---------------------------|-------|-----------|
| 1 | `/` Dashboard | Is everything running, and is mail waiting? | mobile redone (649900c) | reference; keep |
| 2 | `/messages` | What's in my mailbox? | mobile redone (69bc11f) | reference; keep |
| 3 | `/messages/<id>` Viewer | What does this mail say; what do I do with it? | partial pass (a0d20a5) | review vs research (reading layout, actions reach, reply) |
| 4 | `/assistant` (+drawer) | Chat with my mailbox | redone (b9a058d) | reference; keep |
| 5 | `/rules` | Which rules file my mail; are they right? | legacy | lists brief → rework |
| 6 | `/rules/new`, `/rules/<id>/edit` | Compose a rule | legacy | forms brief → rework |
| 7 | `/flows` | Which automations run on my mail? | legacy (builder got a pass) | lists brief → rework |
| 8 | `/flows/new`, `/flows/<id>/edit` | Compose an automation | partial mobile pass | forms brief → rework |
| 9 | `/classifiers` | Which learned classifiers exist; train more | legacy | lists/forms brief → rework |
| 10 | `/classifiers/<id>/dataset` | Is this classifier trained on the right samples? | legacy | lists brief → rework |
| 11 | `/templates` + editor | What draft templates exist; edit one | legacy | lists/forms brief → rework |
| 12 | `/accounts` (+new/edit, `/proxy/log`) | Is my mailbox connection healthy; manage it | legacy | lists/forms brief → rework |
| 13 | `/settings` | Change how the tool behaves | regrouped desktop pass | forms brief → rework |
| 14 | `/log` | What has the system been doing? | legacy | lists brief → rework |
| 15 | `/more` | Reach everything else on a phone | shell pass | shell brief → rework |

## App-wide (shell) questions
- Page-heads per page vs slimmed/hidden on phones; detail-page back navigation.
- Empty / loading / progress / stale states; toast & flash placement on phones.
- Consistent list-row anatomy, form anatomy, destructive-action treatment.
- Desktop stays the reference for density; phone is the priority for these passes.

## Process
1. **Research** — three cited briefs (running as background subagents):
   `docs/research-lists.md`, `docs/research-forms.md`, `docs/research-shell.md`.
2. **Implement** — lists group → forms group → shell/viewer, each: edit → deploy →
   emulated QA (393×852 + 1440×900) → vision review → commit.
3. **Critic** — independent subagent reviews the live site, produces a prioritized
   nit list; fix the accepted nits; final QA + report.

## Conventions to hold (from the redone pages)
- Square corners, light Vercel theme, Geist — never recommend rounding.
- Mobile: bottom tab bar, safe areas, 44px targets, 16px inputs, one-line scroll
  filter rows, docked composers, page-desc hidden ≤767, overlay z-order: nav 180 <
  sheet 190 < drawer 220.
- Partial-save safety on Settings (never submit fields a card doesn't own).
- Keep test-pinned strings in the DOM (hidden via CSS is fine).
