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
- [../RELEASE_NOTES.md](../RELEASE_NOTES.md) - initial public release scope and limitations
- [releasing.md](releasing.md) - release validation and publication procedure
- [onboarding.md](onboarding.md) - research + decisions behind the setup wizard
  (`/welcome`)
- [assistant-page.md](assistant-page.md) - research + decisions for the assistant
  page layout (reading column, disclosure, rail)
- [app-inventory.md](app-inventory.md) - page-by-page inventory + the app-wide layout campaign
- [ux-demo.md](ux-demo.md) - isolated UX workbench demo, play checklist and reset instructions

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
- [plugin-pages.md](plugin-pages.md) - plugin browser pages as built
  (2026-10-02): the `ui` manifest block, the sandboxed opaque-origin frame + nonce
  CSP, the host bridge / session / CSRF model, the page SDK components, lifecycle,
  and the read-only `mt-mail-desk` demo
- [plugins-ui.md](plugins-ui.md) - the Plugins page: the research (progressive
  disclosure, toggle patterns, VS Code / Chrome / Home Assistant precedents) and
  the applied list + detail redesign
- [model-bench-plugin.md](model-bench-plugin.md) - the in-app model benchmark
  (`mt-model-bench`): frozen eval subset, slice/resume under the 30s sandbox
  clock, severity scoring, reference anchors (2026-10-02)
- [model-evaluation.md](model-evaluation.md) - current model-evaluation summary,
  comparison snapshots, and links to the historical study
- [../benchmarks/README.md](../benchmarks/README.md) - model benchmark findings;
  600-case v2 methodology, acceptance policy, and offline reproduction
- [rag-lite-report.md](rag-lite-report.md) - consolidated RAG evaluation:
  retrieval ablations, CPU tradeoffs, live validation, and limitations
- [fusion-lab.md](fusion-lab.md) - TinyJev+MiniCPM local fusion (try-out rig):
  the A/B Fusion Lab plugin, the fusion sidecar, the assistant
  primary/fallback switch, and the measured v2 numbers (2026-10-03)
- [flows-builder-v2.md](flows-builder-v2.md) - flow canvas (trigger -> filters -> steps)
- [flows-fuzzy-classifier.md](flows-fuzzy-classifier.md) - AI category + about-topic
  conditions for flows
- [undo-triage-snooze.md](undo-triage-snooze.md) - undo trail, triage queue, snooze,
  log tools
- [reply-resolution.md](reply-resolution.md) - sent-reply reconciliation: matching,
  sufficiency assessment, and when the needs-reply flag clears
- [dashboard-recent-mail.md](dashboard-recent-mail.md) - the dashboard recent-mail
  stacked feed (why the 5-col table became a list; activity-row flow fix)
- [spa-turbo.md](spa-turbo.md) - SPA navigation layer (Turbo Drive), and why not a
  framework rewrite
- [ui-redesign.md](ui-redesign.md) - simulator / dry-run report page design
- [ui-conventions.md](ui-conventions.md) - the app-wide interaction standard: armed
  deletes, switches for on/off, flash vs toast split (`ok`/`warn`/`err`), row-action
  laws, empty-state anatomy, exceptions register

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

Historical benchmark v1 is preserved under
[../benchmarks/legacy/](../benchmarks/legacy/README.md). Its scores are not
comparable with v2.
