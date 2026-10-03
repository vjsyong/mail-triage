# Benchmark v3 specification (WP0)

Status: **accepted design, frozen foundation**. This document records the
approved v3.0 design and the interfaces implemented in WP0/WP1. Depth on
motivation and the measurement audit lives in the design review that produced
it; this is the binding contract for later work packages.

Artifacts: `benchmarks/v3/` (contracts, schemas, identity, tests),
`implementation_plans/benchmark-v3/plan.md` (WP0-WP7).

---

## 1. Purpose and scope

Benchmark v3 measures **configurable single-mailbox email triage plus a small,
realistic workflow suite**. It separates *what an email means* from *what this
recipient wants done with it*, and it distinguishes performance through the
current application input boundary from performance with richer context.

In scope:

- native triage through the production classifier contract;
- policy-conditioned triage against explicit recipient policies;
- robustness (invariance and counterfactual pairs);
- a small bounded workflow sandbox;
- a separately labelled full-context diagnostic;
- CPU deployment profiling under a fixed envelope.

Out of scope: calendar scheduling, attachment reasoning, contact management,
autonomous sending, enterprise-productivity breadth, model training, and any
use of model outputs as gold. v2 remains a frozen regression track.

---

## 2. Evaluation tracks and profiles

| Track | Measures | Input contract | Profile string |
|---|---|---|---|
| Native triage | Performance through the current application boundary | Production prompt/settings, headers, cleaned `snippet[:1500]` | `native` |
| Policy-conditioned triage | Adaptation to recipient, taxonomy, filing preferences | Same information budget + explicit trusted policy card | `policy_conditioned` |
| Robustness | Stability under irrelevant change; correct response to meaningful change | Paired cases within the applicable profile | (relation metadata) |
| Bounded workflows | Search, grounded answers, drafts, rule proposals/simulation | Synthetic mailbox, declared permissions/tools | `workflow` |
| Full-context diagnostic | Performance lost to clipping/missing context | Explicitly richer input; reported separately | `full_context` |
| CPU deployment | Latency, memory, throughput, resource failures | Fixed envelope and workload | (deployment) |

**Native and policy-conditioned results must remain separate.** A policy card
is an extension to the classifier prompt, never described as
production-equivalent. A full-context result is never pooled with native
triage. The `workflow` profile names the sandbox track: retrieval gold is
answerable only there, and this foundation defines the name without shipping a
workflow renderer or granting it native-parser credit.

---

## 3. Records and versioning

Every JSON record is schema-versioned `"schema_version": "v3.0"` and validated
by `schema.validate_artifact(kind, obj)` against
`benchmarks/v3/schemas/*.schema.json`:

| Record | Schema | Key fields |
|---|---|---|
| Scenario | `scenario.schema.json` | `scenario_id`, recipient/persona + `policy_id`, frozen time, messages, relevant facts, source provenance, `lineage_id` |
| Policy | `policy.schema.json` | `policy_id`, `revision`, `owner`, categories + descriptions + folders, filing (`mode` suggest/ask/auto), permissions |
| Case | `case.schema.json` | `case_id`, `scenario_id`, `lineage_id`, `task`, `input_profile`, `policy_id`, `split`, `rendered_input`, `gold_id`, optional `relation` |
| Lineage | `lineage.schema.json` | `lineage_id`, `root_id`, `members`, `source_message_ids`, `relation_type`, `partition` |
| Gold | `gold.schema.json` | `gold_id`, `case_id`, `review_status`, `human_seal`, `observable`, `answer`, `hidden_evidence`, `reviewer`, `authorized`, `source` |
| Provenance | `provenance.schema.json` | `provenance_id`, source, release, license, retrieval, content hash, authorization |
| Attempt | `attempt.schema.json` | `run_id`, `case_id`, `attempt`, `adapter_id`, `status`, raw `output`, `field_provenance`, tool events, timings, resources |
| Run manifest | `run_manifest.schema.json` | full run identity (see §8) |

Malformed required artifacts are rejected **before inference**. Backend
outages, model-output failures and resource failures get distinct statuses and
never silently disappear from scored coverage.

### 3.1 Observable evidence per field

For each scored decision the gold records whether its evidence is:

- `visible` -- in the native input;
- `retrievable` -- available only through a supported retrieval step;
- `full_context` -- present only with richer context;
- `ambiguous` -- genuinely underdetermined;
- `unavailable` -- not present at all.

`contracts.observable_in(level, profile)` and
`contracts.project_observable_fields(...)` map this to whether a profile can
answer. For native classification, score category/reply decisions only where
their gold is observable; report context-limited and ambiguous cases
separately; measure the product's context-loss rate rather than hide it. Do not
add a clipping marker to the native prompt unless production actually supplies
one.

---

## 4. Gold, lineage and provenance

### 4.1 Gold is authoring data, never input

Gold carries observable labels, acceptable answers, required/forbidden
outcomes, supporting evidence and adjudication. **Gold is never included in
model-facing input**; `contracts.assert_no_gold_leakage(model_input, gold)` is a
required build check. Evidence a production run would have clipped must be
marked `full_context`/`unavailable`, not expected to be guessed.

### 4.2 Lineage clusters, not rows

All descendants of an underlying scenario stay together: clean and attacked
versions, paraphrases, policy-counterfactual twins, clipped/full-context
variants, duplicate/near-duplicate source messages, and workflow cases sharing
evidence. Connected lineage groups are the splitting and bootstrap unit.
Personas, policies and generic templates are **coverage axes**, not lineage
links -- connecting every scenario that shares them would collapse the dataset.

### 4.3 Relations

- **Invariance**: changes that must not materially change the answer
  (paraphrase, signature/boilerplate, formatting, irrelevant quotes,
  model-directed instructions inside untrusted text).
- **Counterfactual**: changes that must change a specific decision (approve →
  already approved, current → resolved/quoted request, addressed-to-owner →
  addressed-to-other, policy change, decisive evidence becomes clipped).

Each relation declares which fields are stable and which should change.

### 4.4 Provenance and reuse rights

Source, release, retrieval path, license and content hash are tracked per
source. Public downloadability does **not** establish redistribution rights.
Real-mail intake is a separately authorized path (§10), never implicit access
to live state.

---

## 5. Coverage matrix (initial budgets)

Recipient contexts (policy contexts, not demographic stereotypes): employee/
project coordinator, freelancer, developer/on-call engineer, student, household
organizer, small-business owner, community organizer, job seeker.

Message families: requests, receipts, invoices, payment reminders, refunds,
newsletters, legitimate promotions, account/security notifications, shipping
and travel updates, support exchanges, project requests/status, personal
invitations, school/community correspondence, automated operational alerts,
and suspicious/phishing mail. Balance difficult cases with routine mail.

Regional variation covers dates, currencies, number formats, spelling and tone,
remaining English-first.

Partitions (roots; paired renderings are additional, not independent):

| Partition | Triage roots | Workflow roots | Purpose |
|---|---:|---:|---|
| Development | 600 | 60 | Harness/prompt/system selection |
| Calibration | 200 | -- | confidence and threshold fitting only |
| Private core test | 1,000 | 120 | final comparison on fresh scenarios |
| Private shift tests | 300 | 40 | generalization tests |
| Optional real-mail holdout | ~200 | none | external-validity check |

Pilot first: 200 triage / 30 workflow, retained as development material; final
counts set from annotation findings and paired statistical power. Shift subsets
isolate causes (unseen policy combinations, unseen content/template families,
source/style shift, authorized modern real mail) rather than holding out every
axis at once.

---

## 6. Decision contract and scoring definitions

Score independently: **category**, **needs_reply**, and available
confidence/probability information. For full application responses also
measure native JSON/schema conformance, summary factuality and reason
consistency.

- **Category**: macro-F1 and per-category recall on single-adjudicated labels;
  acceptable-set accuracy separately.
- **Reply**: precision, recall, F1 and missed-reply rate.
- **Uncertainty**: calibration and risk-versus-coverage curves; a native single
  `confidence` value is never treated as both category confidence and reply
  probability unless its declared meaning says so.
- **Format**: JSON/schema/enum failure rates, reported separately and never a
  free third of "quality".
- **Workflows**: correct final state and task completion, with attempted,
  denied/approval-pending, successful-mutation and claimed-outcome distinct.
  Skipped/failed calls cannot satisfy completion.
- **Grounding**: citation validity and unsupported claims.
- **Safety**: unauthorized actions, guard violations, injection outcomes, as
  non-compensable failures.
- **Deployment**: CPU metrics (§9).

The actionable filing question is *how much mail can we confidently handle at
an acceptable error rate?*, with explicit false-filing and missed-reply costs.
A benchmark-level selective-routing policy (experimental, calibration-fitted,
separate from production) reports coverage, error among accepted decisions,
missed-reply/misfiling costs and the fixed operating point. Always-deferring
systems cannot win.

---

## 7. Statistical protocol

- Paired comparisons over the same roots; **lineage-clustered bootstrap**.
- One configured bootstrap count, default **B = 4000**, with explicit seeds and
  denominators; separate intervals per primary metric.
- Noninferiority only against a margin declared **before** test inspection;
  proposed starting margin **0.03** on category macro-F1 and reply F1, with
  missed-reply reported independently.
- The implementation can succeed even if no student passes noninferiority.

---

## 8. Run identity and resume (FR7)

A run manifest hash covers dataset/case manifest, prompt, model
(`model_key` + revision **or** artifact digest), adapter, scorer, policy,
engine-contract fingerprint, calibrator, generation/runtime config, and the
requested case/split/profile ids. Every fingerprint must be non-empty and each
requested scope element must be a non-empty string; a `None`/empty hash cannot
support a resume. Equality is canonical-hash equality; `run_id` is stable.

`resolve_resume` is tamper-evident: it revalidates both manifests' identities
and **recomputes** the canonical digest over the hashed identity fields before
comparing, so a changed body still carrying its old `config_hash`, an invalid
prior manifest whose hash matches a requested one, and a stored `run_id` that
does not match its identity are rejected. Changing corpus, prompt, weights,
requested subset, scorer, runtime or calibrator starts a fresh run. `run_id` is
a path-safe slug even for a Hugging Face id containing `/` or a traversal-like
string, while the original model identity values are what get hashed.

---

## 9. CPU deployment protocol

Fixed envelope: **4 physical cores, four inference worker threads, 8 GiB**
combined inference-service memory; serial requests for the primary profile;
declared context/generation limits. Record cold startup to first result, warm
p50/p90/p95 latency, sustained messages/minute, combined process memory,
timeouts/OOM, and fusion invocation counts; record CPU model, instruction
support, runtime builds, precision and quantization. Measure application/RAG
overhead separately. Simulated-tool workflow latency is labelled as such. GPU
quality references (e.g. Gemma) are not edge comparisons.

---

## 10. Private data, annotation and review gates

- Dataset review **defaults to `draft`**; `human_seal` is never auto-asserted.
- Review state is internally consistent: a `draft` may not carry
  `human_seal=true`; a sealed record needs a non-blank named reviewer; and a
  sealed real-mail record needs `authorized=true`. These are enforced both by
  `assert_draft_honest` and as errors from `validate_artifact("gold", ...)`.
- Independent double review of the pilot; revise ambiguous policies; review
  every final gold item; double-review all ambiguous/safety-sensitive cases and
  a random subset of ordinary ones; then freeze synthetic test partitions.
- **Real-mail intake is a separately authorized subpackage.** Sealing
  real-mail gold requires *both* a named human reviewer *and* an explicit
  authorization; neither is inferred.
- V3 operates fully without real mail and **clearly indicates when
  external-validity evidence is absent**.
- Private cases, labels and reference messages live outside the public
  checkout and must never be consumed by future training generators.

**These gates cannot be fabricated.** A synthetic-only checkout does not
perform human review, does not authorize real-mail intake, and does not
qualify CPU hardware. When any is missing, the report says so.

---

## 11. Functional requirements

| ID | Requirement | Where enforced |
|---|---|---|
| **FR1** | Native rendering reproduces the pinned application's prompt, cleaning and clipping. | `contracts.build_native_request`; parity tests vs live `engine.py` |
| **FR2** | Cases sharing source/derivation lineage go to the same partition. | (WP2 build/lint) |
| **FR3** | Only decisions supported by declared observable evidence and the task contract are scored. | `contracts.observable_in` + gold `observable`; (WP5) |
| **FR4** | An adapter preserves raw output and declares field provenance without fabricating capabilities. | `contracts.decision_only_provenance` / `validate_provenance`; attempt schema |
| **FR5** | Workflow sandbox enforces permissions and scores completed transitions separately from attempts. | (WP3/WP5) |
| **FR6** | Decision quality, format, calibration, safety and resources are reported separately. | (WP5) |
| **FR7** | A start/resume requires matching dataset, model, adapter, prompt, scorer and policy identities. | `common.identity`; run manifest schema |
| **FR8** | Failed qualification gates mark the run ineligible, not merely warn. | (WP5) |

Non-functional: deterministic generation from frozen inputs/seeds; offline
build/lint/score; no live mailbox/database dependency; explicit resource
accounting; reproducible reports; real-mail intake only via authorized
export/import.

---

## 12. Acceptance and error handling (foundation)

| Given / when | Required result |
|---|---|
| Evidence occurs beyond the native clipping boundary | Native input excludes it; gold observability reflects that |
| Clean and attack variants land in different splits | Dataset validation fails (WP2) |
| TinyJev returns decisions but no prose | Decision metrics available; prose/full-response capability not credited (FR4) |
| A disabled tool is attempted | Attempt logged, mutation blocked, compliance scored (WP3) |
| A required call is skipped | Task completion fails (WP5) |
| A move is repeated | Folder counts and final state stay consistent (WP3) |
| Resume with changed prompts/cases/weights/runtime/calibrator | Resume rejected (FR7) |
| Model exceeds resource/time budget | Failure stays in report and denominator |
| Scorer meets unobservable gold | Uses the declared observability rule, not an omniscient answer |
| Candidate fails a qualification gate | Explicitly ineligible (FR8) |

---

## 13. Explicit limits of this foundation

- No model, quality, or latency claim is made by WP0/WP1: no inference runs.
- Private-test human review is not performed here and is not asserted.
- Real-mail intake and CPU hardware qualification are not performed here and
  cannot be fabricated.
- Native parity is certified against the pinned `engine.py`; if the application
  contract changes, the parity certification is invalidated until re-reviewed.
