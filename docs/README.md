# Docs index

How this folder is organised: by what you need, not by when it was written.

- **Start here** - orientation and the full feature tour.
- **Design records** - decisions the code follows today; each says what was built
  and why. Kept current.
- **Research briefs** - the external sources and comparisons we worked from.
- **History** - dated working notes from earlier rounds, kept for provenance.

## Start here

- [../README.md](../README.md) - what the app is, quick start, operations
- [features.md](features.md) - the full feature tour
- [app-inventory.md](app-inventory.md) - page-by-page inventory + the app-wide layout campaign

## Design records

- [mail-intelligence/design.md](mail-intelligence/design.md) - the learning loop:
  lifecycle, provenance, safety invariants
- [mail-intelligence/improvement-roadmap.md](mail-intelligence/improvement-roadmap.md) -
  test sets, retraining triggers, dynamic tooling (research-backed, next steps)
- [mail-intelligence/audit-store.md](mail-intelligence/audit-store.md),
  [audit-classification.md](mail-intelligence/audit-classification.md),
  [audit-feedback.md](mail-intelligence/audit-feedback.md),
  [audit-data.md](mail-intelligence/audit-data.md),
  [audit-rag.md](mail-intelligence/audit-rag.md) - pre-build audits of the existing
  pipeline (what data exists, what was missing)
- [agent-permissions.md](agent-permissions.md) - enforced permission boundaries for
  the assistant
- [flows-builder-v2.md](flows-builder-v2.md) - flow canvas (trigger -> filters -> steps)
- [flows-fuzzy-classifier.md](flows-fuzzy-classifier.md) - AI category + about-topic
  conditions for flows
- [undo-triage-snooze.md](undo-triage-snooze.md) - undo trail, triage queue, snooze,
  log tools
- [spa-turbo.md](spa-turbo.md) - SPA navigation layer (Turbo Drive), and why not a
  framework rewrite
- [ui-redesign.md](ui-redesign.md) - simulator / dry-run report page design

## Research briefs

- [ux-benchmarks.md](ux-benchmarks.md) - the commercial bar: what good mail UIs do
- [research-shell.md](research-shell.md) - app shell, navigation, page frames
- [research-lists.md](research-lists.md) - list/table page patterns
- [research-forms.md](research-forms.md) - forms and editor patterns
- [mobile-ui-research.md](mobile-ui-research.md) - mobile patterns before the phone pass

## History (dated working notes)

- [critique-round1.md](critique-round1.md) + [critiques/](critiques/) - independent
  critic rounds with screenshots (2026-10-01)
- [mobile-ui.md](mobile-ui.md) - the mobile pass as built
- [dashboard-mobile.md](dashboard-mobile.md), [messages-mobile.md](messages-mobile.md),
  [assistant-mobile.md](assistant-mobile.md) - per-page mobile rethinks (2026-10-01)

Related: the RAG evaluation report lives on the `rag-lite-eval` branch
(`docs/rag-lite-report.md` there).
