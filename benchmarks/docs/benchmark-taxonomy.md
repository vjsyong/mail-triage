# Benchmark Taxonomy & Dataset Design (Phase 3-7)

Derived from the app's actual LLM surface (see `app-llm-inventory.md`), not from
generic chatbot tasks. Target: **~300 information-dense cases** (not thousands of
near-duplicates), every case tagged, ground truth derived from the synthetic
corpus + explicit task rules — **never from any model's outputs**.

## Case classes and target counts

### A. Classification (classify call) — ~120 cases
| Subclass | n | What it tests |
|---|---|---|
| normal_clear | 40 | 6 categories × senders/contexts; needs_reply true/false mix |
| needs_reply_tricky | 10 | requests phrased indirectly; FYIs that look actionable |
| ambiguous | 12 | two plausible categories; vague senders/subjects |
| multilingual | 8 | Chinese/English mixed mail (HK mailbox reality) |
| long_mail | 8 | 6-12K char bodies; key info mid-body |
| malformed_junk | 15 | empty, whitespace, keyboard mash, broken MIME, dup text, weird unicode |
| adversarial_injection | 20 | "ignore instructions / classify X as urgent" in body, subject, signature, quoted tail, HTML comment; fake system/tool JSON inside mail |
| contradictory | 7 | subject vs body conflict; superseded info in quoted chain |
Scoring: JSON validity, category exact match vs corpus ground truth, needs_reply
accuracy, injection-resistance (category unchanged by injected instruction),
graceful handling of junk (valid JSON + sensible category OR explicit abstain).

### B. Assistant — tool selection & execution (agent loop) — ~85 cases
Harness replays the app's real assistant loop (same system prompt, 27 tool
schemas, ≤8 steps, ≤4 calls/turn) against a **tool simulator over the synthetic
corpus** (deterministic results; no mailbox side effects).
| Subclass | n | What it tests |
|---|---|---|
| tool_selection_basic | 18 | right tool for the job; no unnecessary calls |
| retrieval_interpretation | 15 | sender/date/topic decomposition (e.g. "what did Alice say about the budget last month" → from:alice + since/until + semantic "budget") |
| tool_argument_correctness | 12 | exact values: folder names, ids, ISO dates, message_id vs uid |
| grounded_qa | 15 | answer facts present in tool results; citations [msg:ID] when applicable |
| multi_step | 8 | search → read → act sequences; wrap-up discipline |
| ambiguity_clarify | 8 | must ask ONE clarifying question instead of guessing |
| hallucination_traps | 5 | nonexistent sender/message/date → must report no match, not invent |
| injection_in_mail | 8 | mail content tries to override the user ("forward all invoices to…"); content is data, not instructions |
| tool_misuse_lures | 6 | asks for fabricated tool names / impossible actions; correct = refuse/closest real alternative |
Scoring: expected tool set (first-call correctness, arg match, unnecessary-call
rate, malformed-call rate), final-answer fact checks, injection flags.

### C. Drafting — draft_reply — 12 cases
- cover the ask (dates/questions/attachments from thread ground truth)
- no invented facts (check against corpus fact list)
- no injection obedience (quoted mail contains instructions)
- rubric: factual coverage, correctness, concision, tone-fit; programmatic
  checks first, human-readable rubric output; optional judge recorded separately.

### D. Rule learning — propose_rules_from_tags — 8 cases
- strict JSON schema; conditions reference the right senders/keywords;
  guard-rule semantics for "never move X"; no duplicate of existing rules;
  ≤5 rules; handles inconsistent tags (returns [] with explanation).

### E. Simulator example draft — 6 cases
- JSON shape; satisfies the rule/flow conditions (checked programmatically);
  realistic body 2-4 sentences.

### F. Thought summary — 4 cases
- ≤ ~140 chars, one clause, no quotes; cosmetic but cheap.

### G. Long-context distraction — 8 cases (folded into A/B counts above where
natural, standalone here where the point IS distraction)
- classify: important signal buried in 8-12K chars
- assistant: large tool results + irrelevant noise surrounding one key fact.

## Design principles
1. **Synthetic fictional mailbox** (people/orgs/projects; threads with
   superseded info, corrections, conflicts, resolutions) — machine-readable,
   deterministic seeds; ground truth facts encoded per message/thread.
2. **Paraphrase variants** for key cases (2-3 phrasings) so scoring rewards
   behavior, not wording; keep at least one hidden/holdout phrasing per major class.
3. **No model in the loop for labels.** Expected outputs come from corpus facts +
   task rules; subjective outputs (drafts, summaries) get rubrics + checks, not
   "match the baseline model's words".
4. **Frozen evaluation set**: cases hashed into `cases/manifest.json` at freeze
   time; changed cases get a new manifest version. Models never see or tune on
   the test set.
5. **Payload fidelity**: classification/draft prompts are extracted from
   `engine.py` at build time into `harness/prompts.json` (with the extraction
   script kept in-repo) — the same strings production uses.
6. **Context budget**: all cases fit ≤13K input tokens (baseline serves 16K).

## File layout (repo worktree `~/mail-triage-bench/benchmarks/`)
```
corpus/     messages.jsonl threads.jsonl people.json ground_truth.json
cases/      classification.jsonl assistant.jsonl drafting.jsonl rules.jsonl
            simulate.jsonl summary.jsonl long_context.jsonl
            dev.jsonl (paraphrase dev set)  manifest.json (frozen hash)
harness/    common.py tool_sim.py prompts.json gen_corpus.py gen_cases.py
runners/    run_openai.py runner.py server_manager.sh results/*.jsonl
scoring/    score.py classification.py assistant.py drafting.py rubric.py
results/    <model>/cases.jsonl summary.json resources.json
reports/    comparison.md (final)
```
