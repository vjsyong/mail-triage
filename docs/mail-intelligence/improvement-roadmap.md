
# Improvement roadmap: test sets, continuous training, dynamic tooling

Sources reviewed (Oct 2026): Langfuse "Golden dataset evaluation"; datarekha
"Retraining & continual learning" (MLOps); arXiv 2412.03700 "Good practices for
evaluation of machine learning systems"; MLflow + Snowflake continuous-training
guides; Voyager / self-improving-agent literature for dynamic tooling.

## Shipped now (ninth pass)

1. **Frozen golden test set** (`eval_items`, set `golden`). ~50 messages sampled
   once, stratified; labeled blind (no model output shown while labeling);
   stored separately from `labels`; **excluded from every training window**
   (`build_dataset` skips them); metrics grade BOTH the specialist and the stored
   AI verdict against the human answers, including "when the two disagree, who
   matched you more often" - the number the other metrics cannot give.
   Rationale: every other metric here is computed against AI weak labels, so it
   measures imitation of the AI, not correctness (arXiv: "results on data used
   for development are optimistic"; Langfuse: golden sets are "walled off from
   any data the model learned on").
2. **Contamination hint**: models trained before the set was created carry a
   "retrain so they never see it" prompt until retrained.

## Suggested next changes (in order)

3. **Extend exclusion + scoring to the fast-paths** (decision_list/naive_bayes).
   Their "self-check accuracy" is in-sample; the golden set should exclude them
   from tag-training and score them on it too. Then the fast-path cards can drop
   the honesty caveat.
4. **Promotion rule written in advance** (datarekha: "write the promotion rule
   before viewing results"). Suggested gate, recorded in design.md and enforced
   in code when promoting validated -> active:
   - golden non-regression: human-check accuracy not materially below the
     incumbent (with the +- Wilson bound shown), and
   - shadow agreement floor vs the AI on recent mail, and
   - no unlabeled growth in the golden set (retire items deliberately, never
     relabel to improve a score).
5. **Retrain triggers** (continuous training): keep the manual button, add a
   counter - "N new corrections since this model was trained" - and retrain when
   it crosses a threshold (e.g. 25) OR the golden score regresses. Auto-retrain
   should land only in `validated`; promotion stays human-gated (datarekha:
   "automate monitoring/data validation/model evaluation; automate promotion
   only when evidence and rollback are equally trustworthy").
6. **Feedback loop guard** (already enforced): model predictions never mint
   labels. Keep the test that pins this. The golden set additionally protects
   against drift-by-imitation: it never grows from model outputs.
7. **Active learning for labelling efficiency**: grow a *separate* "hard cases"
   bucket from shadow disagreements and user corrections (most informative
   items); keep the golden set's random sample intact for unbiased metrics
   (Langfuse: maintenance = turn production failures into items, but keep the
   frozen core for comparability).
8. **Evaluation hygiene in the UI**: golden scores display n + +- points;
   with n < 20 the page should mark scores as "early". Add per-class rows for
   the category task (arXiv: report slices, not just aggregate).
9. **Dynamic tooling (discover -> propose)**: the "What can be trained next"
   list is the static ancestor. Next milestone: a scheduled error-analysis job
   where the LLM reads recent disagreements + corrections and writes STRUCTURED
   proposals (task, features, evidence, expected gain) - never code, never
   state changes. The constrained pipeline (validated -> shadow -> human
   promotion) is the safety property that separates this from unbounded
   self-modification (Voyager-style skill libraries work precisely because
   skills are validated before adoption).
10. **Rolling monitoring**: keep the existing live agreement stats; add a
    monthly reconcile pass (already CLI) that flags "degraded" when the golden
    or shadow picture falls off, and logs it for review.

## Non-goals (kept)

- No online/A-B randomization: single-user mailbox cannot support counterfactual
  measurement; shadow + golden + human promotion is the honest maximum here.
- No warm-starting: full retrains keep versions reproducible and comparable.
- No auto-promotion: promotion remains a one-line human decision with a tested
  rollback (deploy the previous version).
