# Docs index

How this folder is organised: by what you need, not by when it was written.

- **Start here** - orientation and the full feature tour.
- **Design records** - decisions the code follows today; each says what was built
  and why. Kept current.
- **Research briefs** - the external sources and comparisons we worked from.
- **History** - dated working notes from earlier rounds, kept for provenance.

## Start here

- [../README.md](../README.md) - what the app is, quick start, operations
- [getting-started.md](getting-started.md) - install, hardware tiers, choosing an
  LLM (including no-GPU setups), first-run checklist
- [deployment.md](deployment.md) - ports, TLS, backups, upgrades for real
  deployments
- [features.md](features.md) - the full feature tour
- [onboarding.md](onboarding.md) - research + decisions behind the setup wizard
  (`/welcome`)
- [app-inventory.md](app-inventory.md) - page-by-page inventory + the app-wide layout campaign

## Design records

- [mail-intelligence/design.md](mail-intelligence/design.md) - the learning loop:
  lifecycle, provenance, safety invariants
- [mail-intelligence/improvement-roadmap.md](mail-intelligence/improvement-roadmap.md) -
  test sets, retraining triggers, dynamic tooling (research-backed, next steps)
- [agent-permissions.md](agent-permissions.md) - enforced permission boundaries for
  the assistant
- [plugin-architecture.md](plugin-architecture.md) - the plugin system as built
  (2026-10-02): core vs plugin split, SDK contract (manifest schema +
  plugin-sdk.d.ts), sandbox strategy (supervised worker process per plugin), all
  six kinds, assistant integration, and as-built notes
- [plugins-authoring.md](plugins-authoring.md) - how to write, install and test a
  plugin (manifest, bundle contract, ctx API, grants; every kind)
- [plugins-ui.md](plugins-ui.md) - the Plugins page: the research (progressive
  disclosure, toggle patterns, VS Code / Chrome / Home Assistant precedents) and
  the applied list + detail redesign
- [model-bench-plugin.md](model-bench-plugin.md) - the in-app model benchmark
  (`mt-model-bench`): frozen eval subset, slice/resume under the 30s sandbox
  clock, severity scoring, reference anchors (2026-10-02)
- [flows-builder-v2.md](flows-builder-v2.md) - flow canvas (trigger -> filters -> steps)
- [flows-fuzzy-classifier.md](flows-fuzzy-classifier.md) - AI category + about-topic
  conditions for flows
- [undo-triage-snooze.md](undo-triage-snooze.md) - undo trail, triage queue, snooze,
  log tools
- [dashboard-recent-mail.md](dashboard-recent-mail.md) - the dashboard recent-mail
  stacked feed (why the 5-col table became a list; activity-row flow fix)
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
