# FROZEN evaluation set — 2026-10-02

Frozen by the model-eval workstream after the Phase-11 baseline QA pass.
The case files, corpus and scoring rules below are frozen for this evaluation
round; any future change must bump the manifest and re-run all models.

Baseline (gemma-4-26b-a4b, live production endpoint) scored **92.6 raw /
90.0 severity-adjusted** with 3 critical cases — a healthy, non-trivial baseline.

QA pass note: after the first baseline run, case EXPECTATIONS (not prompts) were
corrected for scoring-harness bugs found while auditing baseline failures —
acceptable-label merges, injection-target mapping, tool-arg key (`rule_id`),
clarification-acceptance breadth, drafting length units (CJK), plus four
assistant cases whose prompts had design flaws (b12/g1/x1/g5) were re-run.
No model output was used to set any ground truth.

Manifest (sha256) is in `manifest.json` next to this file.

**Scorer v1.1 (2026-10-02, applied uniformly to every model):** injection
markers split into tool-argument checks (real actions) vs reply-claim checks
(quoting an email to explain it is not compliance); no-match phrase list widened;
drafting "forbidden content" reworded to claim-phrases. Run outputs are never
altered by scorer changes — only their interpretation.

