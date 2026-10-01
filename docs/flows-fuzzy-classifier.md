# Flows: fuzzy AI conditions + instructed drafts (design record, 2026-10-01)

## Why

The flow builder was IFTTT-shaped: exact fields in, fixed steps out. That misses the
point of having an LLM in the loop - the model should act as a *fuzzy classifier*
that routes mail by MEANING, while the flow stays a deterministic, auditable pipeline.
User asks that triggered this:

- "if content is <type> then it automatically creates <something>"
- "if it's related to <domain> the draft will reply using that something"

## What mature products do (researched 2026-10)

**n8n Text Classifier node** - the canonical "classify then route" pattern:
- Categories are defined in plain language; the DESCRIPTION is the prompt. Descriptions
  must name the boundary ("what it excludes") or accuracy collapses; 3-6 categories is
  the workable range.
- Each category becomes its own output branch: structural routing, no IF afterwards.
- Unmatched items go to an explicit fallback branch - never silently discarded;
  the fallback is "your radar that categories no longer cover reality".
- Classifying costs a model call per item: filter cheaply FIRST (deterministic
  conditions), classify only the ambiguous remainder. It is a router, not a labeller.
- Log every classification decision to a database (audit + drift checks).

**Shortwave** - "custom AI filters": standing rules you write in plain English that
auto label/star/archive; natural-language AND/OR search queries; metered as a scarce,
valuable resource per plan.

**Swfte / market summary** - AI email filters work by intent, not keywords: semantic
conditions ("looks like a sales pitch"), classify by topic/urgency, then take action.
Layered ON TOP of deterministic filters rather than replacing them; corrections feed
back to improve the classifier.

**Zapier/Make** - AI steps inside otherwise deterministic workflows; copilots compile
NL requests into a workflow a human reviews before enabling.

## Decisions for mail-triage

Two fuzzy condition kinds, both first-class in flows (engine + builder + assistant):

1. **AI category** (`{kind:"category", value:"Invoice", min_confidence?:0-1}`) -
   matches the app's OWN classification verdict (heuristic first, LLM fallback).
   Zero extra LLM calls: the verdict already exists. Evaluated right after
   classification; the first matching flow runs and SUPPRESSES the plain
   category->folder filing (flows win over the default map). Guard rules still block.
2. **About (topic)** (`{kind:"topic", value:"<description with boundary>", threshold?:0.2-0.95}`) -
   semantic match via embeddings (local embed endpoint): cosine(message subject+snippet,
   description) >= threshold. Evaluated at scan time. No LLM contention, cheap,
   deterministic-ish. Default threshold 0.55.

Plus: **instructed LLM drafts** - draft steps with mode "llm" accept free-text
`instructions` ("thank them and ask for the PO number") so "reply using that
something" is expressible; template stays as optional guidance.

Design rules taken from the research:
- Deterministic conditions evaluate first and short-circuit (failing from/sender
  checks skip the embedding cost entirely).
- Topic descriptions ARE the prompt: the builder tells users to include the boundary
  ("NOT marketing") and the assistant writes them that way.
- Everything stays auditable: flow runs are logged with the reason
  ("[topic match 0.71]" / "[AI category Receipt 90%]"), flow_runs dedupes, dry-run
  still works (flows_apply).
- No silent fallback loss: an unmatched message simply continues down the normal
  pipeline (classify -> suggest), nothing is discarded.

## Wiring

- `engine._needs_verdict(flow)` splits the phases; scan (`_process_folder`) evaluates
  deterministic + topic conditions; `classify_and_store` evaluates category flows
  after the verdict, before the category-map move.
- `engine.flow_matches(flow, fields, ctx)` - ctx carries `{verdict}` and the
  message text; `_topic_matches` embeds on demand with a query-vector cache.
- Assistant: `propose_flow` conditions accept `kind: field|category|topic`
  (+`min_confidence`, `threshold`); llm draft steps accept `instructions`. The
  prompt teaches when to use each (category = "a kind of mail the list covers";
  topic = "related to / about X" fuzziness; deterministic first).
- Builder UI: per-condition `kind` selector + `min score` column; the steps editor
  grows a "Reply instructions" textarea for LLM drafts.
