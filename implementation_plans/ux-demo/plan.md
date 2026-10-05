# Plan: UX workbench demo

## Objective
Implement the full UI sweep as a playable, isolated build on `ux-demo`.

## Requirements
- Reachable message drafting, snooze, reversible reply correction and category correction.
- Readable mail list with optional assistant, keyword search and sender/category/folder/date filters.
- Task-first dashboard with pending approvals, recent mail, compact undo and disclosed health/history.
- Test unsaved rules/flows against recent or entered mail without persisting or executing actions.
- Incremental rule conditions and exact duplicate/shadow diagnostics.
- Five Settings sections, advanced disclosure, concise Learning cards and explicit prerequisites.
- Plugin-to-chat shortcuts, removable context and clear action/Trash copy.
- Independent demo image/volume/loopback port with synthetic mail and deterministic mock AI.
- Sent-reply reconciliation clears the needs-reply flag when a matched sent reply
  sufficiently addresses the request; manual intent stays authoritative.

## Constraints And Dependencies
- Existing Flask/string templates, shared primitives, current schemas and guard rules.
- Preserve existing tests. No live accounts/OAuth or production state in the demo.
- Worktree only; production stays on master. No merge or production deployment requested.

## Acceptance Criteria
- [x] Draft/snooze/correction actions accessible without scrolling through a thread at 393px.
- [x] Search/filter/count/pagination/viewer navigation agree and SQL remains parameterized.
- [x] Corrections persist, are auditable and reversible, and survive reclassification.
- [x] Unsaved automation preview names precedence and proposed actions; no rows or mail change.
- [x] Settings shows one of five sections, supports legacy deep links and scoped saves.
- [x] Learning exposes provenance, meaningful metrics, next action and routing prerequisites.
- [x] Plugin shortcuts prefill editable chat; detached context is not sent to the assistant.
- [x] Full mock suite green; desktop/phone workflow and console checks pass.
- [x] Separate built container healthy, accessible and documented for play/reset.
- [x] Sent replies matching + sufficiency assessment clear the flag auditably;
      manual corrections/reopens outrank automatic outcomes.

## Recommended Risk And Review
- Tier: STANDARD
- Reason: Broad UI changes plus query, correction and simulation seams; no schema migration.
- Default: one bounded self-review and full integration/browser validation.

## Scope And Packages
- WP1: demo isolation and plan.
- WP2: message workbench, search/corrections and dashboard.
- WP3: automation/editor previews, diagnostics, settings/learning and shortcuts.
- WP4: regression checks and browser evidence.
- WP5: isolated demo build and handoff.

## Open Questions
- None blocking. Synthetic demo data is chosen to keep all interactions playable independently.

## Out Of Scope
- Production deployment, account changes, schema migrations and real model evaluation.
