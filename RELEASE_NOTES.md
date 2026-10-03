# Release notes

## v0.1.0-beta.1 — initial public beta (draft)

Prepared October 3, 2026. This release is a draft pending publication.

Mail Triage is a self-hosted assistant for **one mailbox**, used daily by its
author. This release packages the current application and its engineering
evidence, including an optional experimental Fusion Lab. A fully evaluated CPU
agent deployment remains a future milestone.

### Included

- Rules-first triage, guard rules, visual multi-step flows, drafts, tags, snooze,
  and a filing undo trail.
- A streaming tool-calling assistant with per-capability permissions, approval
  queues, bounded execution, and visible tool results.
- CPU hybrid retrieval: lexical + dense search, metadata filters, RRF, and
  cross-encoder reranking. No GPU is needed for the retrieval stack.
- Interpretable classifier fast paths and an initial learning lifecycle with
  provenance, versioned specialists, calibration, and shadow evaluation.
- Built-in OAuth proxy integration and a permission-gated plugin SDK/runtime.
- A 600-case model evaluation harness, committed comparison snapshots, historical
  v1 evidence, and a consolidated retrieval evaluation report.
- An experimental TinyJev + MiniCPM classification-comparison plugin and optional
  sidecar, plus a primary/fallback endpoint switch for assistant chat. The lab is
  opt-in and does not replace the default triage pipeline.

### Deployment scope

Docker + Compose is the documented deployment path. The UI binds to host
localhost; remote use needs an authenticated proxy or VPN forwarding. There is
no application login or multi-user isolation.

The app starts without an LLM. Rules and indexed search remain available;
assistant chat, LLM classification, and AI drafting need a separately configured
OpenAI-compatible endpoint. CPU retrieval does not mean CPU LLM inference has
been validated. Model and mailbox-provider compatibility require configuration.

### Known limitations

- **Model reliability:** current candidates do not satisfy the full replacement
  policy. Prompt-injection failures occurred in both the baseline and small
  models. AI auto-filing is opt-in; sending and Trash are disabled by default.
- **Learning scope:** the lifecycle is a first implemented slice, not a fully
  autonomous self-improving system. Broader routing/model kinds remain roadmap work.
- **Fusion scope:** reported classification experiments do not establish full
  acceptance or end-to-end CPU agent performance. The sidecar's 2B endpoint is
  served separately; raw spike artifacts are not distributed.
- **Reproduction:** model report snapshots are committed, but raw v2 runs are
  not distributed. The historical retrieval study used a private corpus; its
  aggregate figures cannot be recreated from a clean clone alone.
- **Resources:** CPU model downloads take several GB, and bulk indexing can
  consume significantly more RAM than steady-state queries.
- **Build reproducibility:** dependency ranges and the Python base-image tag
  are not a fully locked environment. Validate the environment you build.
- **Action reversibility:** filing has Undo; sent mail and external plugin
  effects cannot be recalled through that mechanism. The app does not permanently
  delete mail, but the mail provider can purge Trash under its own policy.

### Release preparation changes

- Made v2 the primary evaluation reference and retained v1 as historical evidence.
- Consolidated retrieval results into the published documentation.
- Replaced local worktree references on the reader-facing documentation path.
- Made the example LLM configuration blank by default; supplied the Linux Docker
  host-gateway alias used by local-endpoint instructions.
- Excluded local model caches, benchmark artifacts, and documentation from the
  app's Docker build context.
- Clarified installation, resource needs, remote access, and development setup.
- Fixed fresh-install wizard exits looping back into setup when no services are
  configured; skipping preserves the dashboard's incomplete-setup help.

### Next milestone

Evaluate **MiniCPM 2B + TinyJev fusion** for CPU agent deployment against each
component and the existing baseline. Report end-to-end task success, critical
failures, sequential latency, and RAM on named hardware before making a deployment
claim.

### Validation

| Check | Result |
| --- | --- |
| Full mock app suite | 885 passed, 0 failed on the host and in the Python 3.12 release image, including the integrated experimental fusion lab |
| Offline proxy integration suite | 26 passed, 0 failed in the release image |
| Benchmark v2 tests | 52 passed, 0 failed in the release image |
| Benchmark lint | Passed; existing truncation-boundary warnings retained |
| Snapshot/harness fidelity checks | 9 passed |
| Reader-facing documentation | 119 local links and anchors checked; no missing targets |
| Compose configuration and Docker build | Passed; the build reused cached dependency layers |
| Fresh-container smoke | HTTP 200 health endpoint, unconfigured doctor output, host alias resolution, and no broken Python requirements |
| Browser verification | 393px phone and 1440px desktop; wizard skip/exit reach the dashboard, setup help remains visible, no horizontal overflow or browser errors observed |

No real mailbox or live LLM was connected during release validation. The mock
checks establish application behavior in the tested environment, not universal
provider/model compatibility or a CPU agent performance result.

See [the release procedure](docs/releasing.md) for validation and publication.
