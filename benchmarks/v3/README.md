# mail-triage benchmark v3

Versioned, reproducible evaluation of **configurable single-mailbox triage and
bounded assistance**. v3 supersedes v2 as the basis for generalization and
deployment decisions; v2 stays frozen as a historical regression track and is
never imported at runtime.

This directory holds the **v3 benchmark implementation**: the specification
(`specs/benchmark-v3.spec.md`), the application contracts (native parity,
profiles, provenance, gold isolation), the v3.0 artifact schemas, the run
identity, the dataset builder (WP2), the workflow sandbox (WP3), the adapters
and resumable runner/CLI (WP4) and scoring/statistics/gates (WP5).

> Real-mail intake, human review/seal, CPU hardware qualification and the
> private test set are **not** performed by this synthetic-only checkout, and
> no model or latency claim is produced here. The tooling to run them exists
> (below) but the human/authorization/qualification gates remain pending until
> a named reviewer, an explicit authorization and a verified hardware receipt
> are supplied. A synthetic-only tree never asserts any of them.

## Status

| Work package | State |
|---|---|
| WP0–WP1 spec, schemas, contracts, identity | ✅ implemented |
| WP2 dataset builder / lineage / lint / review gates | ✅ software implemented |
| WP3 workflow sandbox | ✅ implemented |
| WP4 adapters, runner, CLI | ✅ implemented (no real inference run yet) |
| WP5 scoring, statistics, gates | ✅ software implemented |
| WP6 pilot, annotation, review, freeze | ⚠ tooling available; **human review/seal pending** |
| WP7 baseline execution and release report | ⚠ tooling available; **actual qualification/report pending** |

No model has been run under v3: the shipped runs are offline fakes and unit
fixtures. CPU qualification, cold-start and latency numbers are therefore
explicitly absent (see *CPU deployment protocol*).

## Working commands

```bash
# build a small development pilot (draft), validate, run the offline mock
.venv/bin/python -m benchmarks.v3.cli build --triage-roots 6 --workflow-roots 2 \
    --out /tmp/v3pilot
.venv/bin/python -m benchmarks.v3.cli validate /tmp/v3pilot
.venv/bin/python -m benchmarks.v3.cli run /tmp/v3pilot --adapter offline-fake \
    --out /tmp/v3runs --predictions /tmp/pred.json --allow-draft
.venv/bin/python -m benchmarks.v3.cli score /tmp/v3pilot /tmp/v3runs
.venv/bin/python -m benchmarks.v3.cli review /tmp/v3pilot \
    --export-worksheet /tmp/ws.json
```

The full plan (`--layout full --private-seed N`) generates the planned
partitions; calibration/private/real splits are written only to an explicit
`--private-root` **outside** the checkout. `run` refuses an unverified model
identity unless `--allow-unverified-model` is passed for a declared development
probe, and records every externally managed endpoint (including a loopback GPU)
as external/warm and CPU-unqualified. `score --fit-calibrator` fits from the
`calibration` split only; `review --import`/`--seal` demand a named human
reviewer, and a de-identified real-mail import additionally demands
`--authorization`.


## Layout (frozen)

```
benchmarks/v3/
  __init__.py        SCHEMA_VERSION / BENCHMARK_VERSION
  contracts.py       native/policy/full-context rendering, parsing, provenance
  schema.py          v3.0 artifact validation + gold review/seal gates
  common/            stdlib-only primitives: hashing, identity, validation, mime
  schemas/           JSON Schemas (source of truth), all schema_version "v3.0"
  policy/default.json  a synthetic default policy card
  tests/             stdlib unittest suite (no network, no live DB)
  build/             (WP2) catalog, rendering, lineage, splitting, lint, review
  sandbox/           (WP3) mailbox state machine and tools
  adapters/          (WP4) generative / TinyJev / fusion / offline-fake
  runner.py, cli.py  (WP4) immutable manifests, resumable execution, CLI
  scoring/           (WP5) decisions, relations, workflows, calibration, gates
```

`build/`, `sandbox/`, `adapters/`, `runner.py`/`cli.py` and `scoring/` are all
implemented against the frozen interfaces below. The semantic request hash
(`contracts.rendered_input_hash`) is shared by adapters, the runner and the
scorer so an attempt's recorded `request_sha256` and its recomputation can never
diverge.

## Profiles (never silently mixed)

| Constant | `profile` string | Input contract |
|---|---|---|
| `contracts.NATIVE_PROFILE` | `native` | Production prompt/settings, headers, cleaned `snippet[:1500]` |
| `contracts.POLICY_PROFILE` | `policy_conditioned` | Same budget + an explicit trusted policy card |
| `contracts.FULL_CONTEXT_PROFILE` | `full_context` | Explicitly richer input; diagnostic only |
| `contracts.WORKFLOW_PROFILE` | `workflow` | Sandbox mailbox, declared permissions/tools; WP3 owns execution |

`native` and `policy_conditioned` results are reported separately and never
pooled; a `full_context` result is never pooled with native triage.
`native` reproduces the pinned `engine.LLMClient.classify`; a more tolerant
parser is a separately named diagnostic and never receives `native` credit.
`workflow` names the sandbox profile so retrieval gold is only answerable there;
WP3/WP4 own its renderer, tools and execution.

## Frozen `contracts` API

Signatures and return shapes later packages may rely on:

```python
build_native_request(msg, categories=None, owner="") -> {
    "profile": "native", "owner": str, "categories": [str, ...],
    "system": str, "user": str,
    "params": {"json_mode": True, "max_tokens": 4096,
               "retry_max_tokens": 8192, "full": True},
}
# msg keys used: from_addr, to_addr, subject, date, snippet
# user = "From: ...\nTo: ...\nSubject: ...\nDate: ...\n\n" + body[:1500]

build_policy_request(msg, policy, categories=None, owner="") -> {..., "profile":
    "policy_conditioned", "policy_id": str, "policy": {card}}
# raises ContractError when policy_id is missing
# defaults category names to policy["categories"] ({name,...} or strings)

build_full_context_request(msg, full_body, categories=None, owner="") -> {
    ..., "profile": "full_context"}   # body up to FULL_CONTEXT_LIMIT=6000, not 1500

rendered_input_hash(rendered, mailbox=None, tools=None) -> sha256
# THE semantic request hash shared by adapters, runner and scorer: covers
# profile/system/user/owner/categories/params + trusted policy + (workflow)
# mailbox/tools.  An adapter's wire-payload digest is recorded separately.

parse_native_response(content) -> dict        # mirrors classify()
native_output(result) -> {category, needs_reply, confidence, summary, reason}

decision_only_provenance(category=..., needs_reply=..., confidence=...) -> {
    "category": "produced|derived|missing", "needs_reply": ...,
    "confidence": ..., "summary": "missing", "reason": "missing"}
validate_provenance(prov) -> [problem, ...]   # [] when valid
has_fabricated_prose(prov) -> bool            # decision-only must stay False

find_gold_leakage(model_input, gold) -> [term, ...]     # [] when clean
assert_no_gold_leakage(model_input, gold) -> True | raises GoldLeakageError

observable_in(level, profile) -> bool
project_observable_fields({field: level}, profile) -> {field: bool}
# retrievable evidence is true only in WORKFLOW_PROFILES; triage profiles never
# claim retrieval
```

Constants: `SNIPPET_LIMIT=1500`, `FULL_CONTEXT_LIMIT=6000`, `DEFAULT_CATEGORIES`,
`NATIVE_MAX_TOKENS`, `NATIVE_RETRY_MAX_TOKENS`, `FIELD_PRODUCED/DERIVED/MISSING`,
`OBSERVABILITY_*`, `NATIVE_PROFILE`, `POLICY_PROFILE`, `FULL_CONTEXT_PROFILE`,
`WORKFLOW_PROFILE`, `TRIAGE_PROFILES`, `BENCHMARK_PROFILES`, `WORKFLOW_PROFILES`,
`ContractError`, `NativeParseError`, `GoldLeakageError`.

**Parser parity is intentional, including failure.** `parse_native_response`
uses the same greedy `re.search(r"\{.*\}", content, re.S)` + strict
`json.loads` + category check. Two concatenated JSON objects raise
`json.JSONDecodeError`; no-JSON and missing-category raise `NativeParseError`
(a `RuntimeError`, as in production).

## Frozen `schema` API

```python
schema.SCHEMA_VERSION                  # "v3.0"
schema.ARTIFACT_KINDS                  # scenario/policy/case/lineage/gold/
                                       # provenance/attempt/run_manifest
schema.validate_artifact(kind, obj) -> [error, ...]
schema.validate_artifact_or_raise(kind, obj) -> True | raises ValidationError
schema.validate_run_manifest(manifest) -> True | raises ValidationError

schema.new_gold(case_id, gold_id, **fields) -> gold   # review_status="draft"
schema.can_seal(gold) -> bool
schema.seal_gold(gold, reviewer, authorized=False) -> sealed | raises SealError
schema.assert_draft_honest(gold) -> True | raises SealError
```

Enums exported for builders/scorers: `OBSERVABILITY`, `FIELD_PROVENANCE`,
`REVIEW_STATUS`, `PROVENANCE_SOURCES`, `PROFILES` (includes `workflow`),
`SPLITS`.

`validate_artifact` returns error strings (never raises on shape) for a
wrong-shaped `observable` or `field_provenance`, applies the gold review gate
below, and appends identity problems for a run manifest.

## Run identity (`common.identity`)

`build_manifest(**fields)` returns a manifest with `config_hash` (canonical
hash over the identity fields) and a stable `run_id`, or raises
`IdentityError`. `resolve_resume(manifest, existing)` returns `"fresh"` /
`"resume"` and raises on any identity difference or a prior manifest without a
usable hash.

Required non-empty fingerprints: `dataset_id`, `dataset_sha256`,
`case_manifest_sha256`, `prompt_revision`, `prompt_sha256`, `model_key`,
`adapter_id`, `adapter_revision`, `scorer_revision`, `policy_revision`,
`policy_sha256`, `engine_contract_sha256`, `calibrator_revision`.
Model identity needs `model_revision` **or** `model_artifact_sha256`.
`generation_config` and `runtime_config` are required mappings; the requested
`requested_case_ids`, `requested_splits` and `requested_profiles` are required
non-empty lists of non-empty strings and are part of the hash. A `None`/empty
hash can never support a resume.

Changing the corpus, prompt, weights, requested subset, scorer, runtime **or
calibrator** changes `config_hash` and blocks resume.

`resolve_resume` is tamper-evident: it revalidates both manifests' required
identities and **recomputes** the canonical digest over `HASHED_FIELDS` before
comparing. A changed body still carrying its old `config_hash`, an invalid prior
manifest whose hash matches a requested one, and a stored `run_id` that does not
match its identity are all rejected. `run_id` is a path-safe slug
(`safe_run_component`); a Hugging Face id like `openbmb/MiniCPM5-2B` becomes
`openbmb-MiniCPM5-2B` and `../../etc/passwd` never escapes, while the **original**
model identity values are what get hashed (a slashed and a hyphenated id hash
differently).

## Statistics protocol (frozen defaults; WP5 implements)

- Paired comparisons over the same roots; **lineage-clustered bootstrap**.
- Bootstrap count **B = 4000** with explicit seeds; separate intervals per
  primary metric.
- Noninferiority only against a **pre-declared margin**; proposed starting
  margin **0.03 (three percentage points)** on category macro-F1 and reply F1.
  Final margins are settled in the pilot, before test results are inspected.
- Always-deferring systems never win: deferred cases stay in the denominator
  and coverage is explicit.

## CPU deployment protocol (frozen reference; WP7 measures)

- **4 physical cores / 8 GiB** combined inference-service memory; four
  inference worker threads; serial requests for the primary profile.
- Report cold startup, warm p50/p90/p95 latency, sustained messages/minute,
  peak memory, timeouts/OOM, and component invocation counts; record CPU model,
  instruction support, runtime build, precision and quantization.
- Measure application/RAG overhead separately; simulated-tool workflow latency
  is labelled as such. GPU quality references are **not** edge comparisons.
- No model/latency claim is made by this foundation package.

## Gates: format, quality, safety, and human review are separate

- Format/schema validity, decision quality and safety are reported
  **separately**; format compliance is never a free third of "quality".
- Gold is **never** part of model-facing input: `assert_no_gold_leakage` is a
  required build check.
- Dataset review **defaults to `draft`**. `human_seal` is never auto-asserted;
  `seal_gold` and `can_seal` demand a non-blank named human reviewer, and
  real-mail material demands a **separate authorization**. Neither gate is
  inferred from downloadability.
- Review state is internally consistent: `human_seal=true` with
  `review_status=draft`, a blank/missing reviewer on a sealed record, and a
  sealed real-mail record without `authorized` are rejected by
  `assert_draft_honest` **and** returned as errors by
  `validate_artifact("gold", ...)`.
- Private-test human review, real-mail intake authorization and CPU hardware
  qualification **cannot be fabricated** by a synthetic-only tree; when they
  are absent the report says so.

## Running the foundation tests

```bash
# from the repo root, shared venv
.venv/bin/python -m unittest discover -s benchmarks/v3/tests -v
.venv/bin/python benchmarks/v2/tests/run_v2_tests.py
```

Contract tests certify native parity against the live `engine.py` by **isolated
AST extraction** -- the engine is never imported, so no `.env`, database,
worker or network is touched.
