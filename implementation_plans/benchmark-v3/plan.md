# Benchmark v3 implementation plan (accepted WP0-WP7)

Status: **WP0-WP1 delivered in this package**; WP2-WP7 are planned and owned by
later disjoint packages. This file records the user's accepted plan, not a new
expansion of scope. Design authority: `specs/benchmark-v3.spec.md`.

Delivery sequence:

```text
WP0 → WP1 → WP2 ───────────────┐
             WP3 → WP4 → WP5 ─┴→ WP6 → WP7
```

Each work package owns disjoint paths and targets the interfaces frozen in
`benchmarks/v3/README.md`. No package may silently modify another's contract;
a shape change is a spec revision.

---

## WP0 — Specification and protocol lock  ✅ this package

**Outputs:** design specification; coverage matrix; schema examples; scoring
definitions; CPU protocol; private-data and annotation procedure.

**Delivered:** `specs/benchmark-v3.spec.md` (tracks/profiles, records,
observability, lineage, coverage matrix, scoring, statistics, CPU protocol,
review gates, FR1-FR8, acceptance).

**Acceptance:** representative records demonstrate every track, observable/
hidden evidence, ambiguity, permissions and relation type -- exercised by the
schema validators and fixtures in WP1.

---

## WP1 — Versioned schemas and application contracts  ✅ this package

**Owned areas:** `benchmarks/v3/{contracts.py,schema.py,common/,schemas/,
policy/,tests/}`, `specs/`, this plan.

**Delivered:**

- strict v3.0 artifact validation (`schema.validate_artifact`);
- native request rendering and parsing reproducing
  `engine.LLMClient.classify` (prompt, headers, `snippet[:1500]`, greedy
  regex + strict JSON, MIME salvage);
- explicit `policy_conditioned` and `full_context` profiles;
- observable-evidence projections (`observable_in`, `project_observable_fields`);
- output parsing and field provenance (`decision_only_provenance`,
  `validate_provenance`, `has_fabricated_prose`);
- gold isolation (`find_gold_leakage`, `assert_no_gold_leakage`);
- run identity, stable hashing and resume refusal (`common.identity`);
- review gates defaulting to draft, with a named human seal and a separate
  real-mail authorization.

**Parity method:** isolated AST extraction of the live `engine.py` in
`tests/test_contracts.py` -- no engine import, no `.env`, no database/worker,
no network. A more tolerant parser is a separately named diagnostic and never
receives `native` credit.

**Acceptance evidence:** `benchmarks/v3/tests/` (native 1499/1500/1501
boundaries, parser including multiple JSON objects, dynamic category enum and
owner render, no gold leakage, observability and produced-fields schema,
malformed-artifact rejection, honest draft review, stable hashing and
changed-identity resume refusal).

**Depends on:** WP0.

---

## WP2 — Scenario builder, lineage and split enforcement  (planned)

**Owned areas:** `benchmarks/v3/build/`, public development fixtures.

Implement deterministic scenario rendering; source/release catalog; relation
generation; lineage grouping; explicit split assignments; leakage and
gold-in-input checks; private/public export boundaries.

**Acceptance:** repeated builds are identical; intentionally leaked clean/
attack or counterfactual pairs fail lint.

**Depends on:** WP1.

---

## WP3 — Workflow sandbox  (planned)

**Owned areas:** `benchmarks/v3/sandbox/`, workflow fixtures/tests.

Implement the supported deterministic mailbox state machine and event log with
explicit denied, pending, successful, failed and no-op outcomes.

**Acceptance:** denied actions preserve state; writes affect subsequent reads;
repeat moves stay consistent; failures do not partially mutate state; unknown
ids and unsupported tools return explicit errors.

**Depends on:** WP1; can run alongside WP2.

---

## WP4 — Adapters, runner and run identity  (planned)

**Owned areas:** `benchmarks/v3/adapters/`, runner/CLI, manifests.

Implement the generative-model, TinyJev decision, fusion and offline-fake
adapters; required capability declarations; immutable run manifests; resumable
case execution; timing/resource collection.

Do not modify the existing fusion worktree (it has ongoing uncommitted work);
target a pinned interface/artifact and record its identity.

**Acceptance:** unsupported capabilities stay explicit; no synthesized prose
earns capability credit; changed inputs/prompts/models block resume.

**Depends on:** WP1 and WP3 for workflows.

---

## WP5 — Scoring, statistics and executable gates  (planned)

**Owned areas:** `benchmarks/v3/scoring/`, `policy/`, report generation.

Implement per-field decision scoring; relation scoring; state-based workflow
assertions; calibration and selective coverage; clustered paired statistics;
qualification logic; JSON and Markdown reports. Use the configured protocol
(default B = 4000, pre-declared NI margin 0.03).

**Acceptance:** an always-abstaining system cannot win; skipped calls cannot
satisfy required actions; critical failures cannot be offset by an average; all
comparison paths use the configured statistical protocol.

**Depends on:** WP1-WP4.

---

## WP6 — Pilot, annotation and dataset freeze  (planned)

**Outputs:** reviewed pilot, annotation agreement report, finalized coverage and
private manifests.

Procedure: independently double-review the pilot; revise ambiguous policies;
review every final gold item; double-review ambiguous/safety-sensitive cases and
a random subset of ordinary cases; determine final sample sizes; freeze
synthetic test partitions.

Real-mail intake is a separately authorized subpackage. V3 operates without it
and states when external-validity evidence is absent.

**Depends on:** WP2 and WP5.

---

## WP7 — Baseline execution and release report  (planned)

Run Gemma reference, MiniCPM5-2B alone, TinyJev alone, current fusion, and
applicable deterministic/cheap controls. Produce track eligibility matrix,
quality and robustness results, CPU resource/latency results, development
failure analysis, paired comparisons and reproducibility instructions.

Use development failures to guide future training; keep sealed-test details from
training-data generation and checkpoint selection.

**Depends on:** WP4-WP6.

---

## Review and validation

Recommended review level **STANDARD**, with independent scrutiny of
lineage/split integrity, production-contract fidelity, scoring denominators,
permission/state semantics, and manifest/resume identity.

Validation for code landing includes the offline v2 tests, new offline v3
tests, and the required mock E2E suite before landing:

```bash
.venv/bin/python benchmarks/v2/tests/run_v2_tests.py
.venv/bin/python -m unittest discover -s benchmarks/v3/tests -v
.venv/bin/python tests/mock_e2e.py --all
```
