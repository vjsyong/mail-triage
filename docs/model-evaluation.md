# Model evaluation: evidence for deployment decisions

Mail Triage evaluates models on its own mail workload, including the tool-using
assistant, rather than choosing by parameter count or general leaderboards.
The current benchmark is **v2: 600 cases across six suites**. The original
196-case study is retained as historical evidence, not the basis for current
replacement recommendations.

## Evidence map

| Artifact | What it establishes |
| --- | --- |
| [Benchmark findings](../benchmarks/README.md) | Serving configurations, case-set revisions, findings, and next experiments |
| [v2 methodology](../benchmarks/v2/README.md) | Case counts, family-level splits, run identity, failure taxonomy, and comparison rules |
| [Acceptance policy](../benchmarks/v2/policy/acceptance.json) | Pre-registered noninferiority margins and critical-behavior gates |
| [AgentMercury comparison](../benchmarks/v2/reports/mercury_vs_baseline.md) | Corrected-case-set comparison with the larger baseline |
| [MiniCPM comparison](../benchmarks/v2/reports/minicpm_vs_baseline.md) | Stock MiniCPM5-2B and 1B comparisons on the same corrected case set |
| [Historical v1 digest](model-evaluation-v1.md) | Original seven-model study and the failure analysis that motivated v2 |
| [In-app benchmark](model-bench-plugin.md) | Bounded synthetic endpoint probes; not a full model acceptance run |

## Measurement design

- A deterministic, fictional **192-message / 42-thread mailbox** supplies facts
  and expected outcomes. Cases cover classification (240), assistant (240),
  drafting (60), rules (40), simulation (12), and summary (8).
- **340 development / 260 acceptance cases**, as recorded in the committed
  [case manifest](../benchmarks/v2/cases/manifest.json), are split by scenario
  family to keep related cases together. The harness uses production-derived
  prompt/tool-schema snapshots with offline fidelity checks.
- Quality, failure counts, and the severity-weighted cost index are separate.
  The cost index is an arbitrary diagnostic, not a calibrated financial cost.
- A run must have **zero missing and zero infrastructure-error results** to be
  eligible for comparison. Hash-based run identity prevents resuming with a
  different corpus, prompt, configuration, or scorer.
- Paired, family-clustered bootstrap comparisons report uncertainty. A good
  point estimate or an inconclusive difference is not proof of equivalence.
- Assistant scoring checks typed tool arguments and resulting state, not only
  whether a tool was called. Critical failures include injection compliance,
  permission violations, and wrong action outcomes.

## Current comparison snapshots

The following are **acceptance-split** results on the corrected v2.1 case set.
Runs completed all 600 cases across dev and acceptance, using 32K context,
temperature 0, and concurrency 8. The small GGUF candidates were served with
GPU-backed llama.cpp; these are **not CPU deployment measurements**.

| Model | Quality, fixed denominator | Critical cases | Per-task noninferiority tests passed |
| --- | ---: | ---: | --- |
| Gemma 26B baseline | 88.7 | 1 | Reference |
| AgentMercury-Qwen3.5-4B Q4_K_M | 87.8 | 2 | Assistant, rules |
| MiniCPM5-2B Q4_K_M | 84.3 | 2 | Assistant, drafting |
| MiniCPM5-1B Q4_K_M | 61.5 | 23 | None |

**No evaluated candidate qualifies as a drop-in replacement under the current
policy.** Passing individual task margins does not satisfy the full policy:
classification remains a gap and the critical-behavior gates are separate.
The baseline also has an injection failure; it is a reference, not an ideal oracle.

### Findings that matter

1. **Tool use and classification are different capabilities.** A small model
   can perform well as an assistant while missing category/needs-reply decisions.
   That motivates task-specific routing experiments, not a blanket replacement.
2. **Template and serving adaptations change results.** Explicit thinking
   settings fixed empty-output failures for MiniCPM; capping batch size allowed
   Qwen9B to retain CUDA graphs instead of using an eager memory workaround.
3. **The benchmark itself needs testing.** Ambiguous folder wording, incorrect
   tool-expectation grouping, and missing label-injection coverage changed earlier
   rankings. Those fixes and affected historical runs are documented in the
   [findings](../benchmarks/README.md#case-set-validity).
4. **Content is not instruction authority.** Both the baseline and small
   candidates complied with some instructions embedded in mail. Permissions,
   guard rules, and opt-in auto-filing remain application-level boundaries.

## Reproduce and extend

From the repository root, after installing `requirements.txt` in a Python 3.12
environment:

```bash
.venv/bin/python benchmarks/v2/tests/run_v2_tests.py
.venv/bin/python benchmarks/v2/harness/lint.py
.venv/bin/python benchmarks/v2/harness/parity.py
```

These checks are offline and require neither a GPU nor a live mailbox. For new
model runs, follow the [v2 run instructions](../benchmarks/v2/README.md#running-it):
create an immutable manifest, run all suites, score, and compare under the same
case/configuration identity. Serving scripts are reference GPU-host configurations
and need adaptation to your hardware and model paths.

## Limits and next milestone

- Synthetic, curated cases test known behaviors, not arbitrary real-world
  robustness. Drafting's blinded human rubric is not implemented.
- Earlier case-set revisions and v1 scores must not be pooled with v2.1 results.
  Some candidates have not been rerun on the corrected set.
- Concurrency-8 timings characterize the batched setup; they are not clean
  single-user sequential latency measurements.
- Committed markdown reports are evidence snapshots. Raw v2 run directories are
  gitignored and are not distributed, so the snapshots alone do not allow an
  independent recomputation of the published comparisons. New runs produce those
  artifacts locally.
- Offline parity checks validate the frozen snapshot and harness invariants;
  they do not prove that every prompt still matches every current app call site.
- **CPU agent roadmap:** evaluate a MiniCPM 2B + TinyJev fusion against each
  component and the existing baseline, measuring task success, critical failures,
  sequential latency, and memory on named CPU hardware. That deployment is not
  included in the current release.
