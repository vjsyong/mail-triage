# `benchmarks/v3/build` — dataset builder (WP2/WP6)

Deterministic, authored (never model-created) scenario builder: renders triage
and workflow cases through the frozen `benchmarks.v3.contracts`, derives gold
from authored family semantics and the visible policy, groups lineage, assigns
splits, lints integrity, and gates private/review/reuse boundaries.

## Public API (`benchmarks.v3.build`)

```python
build_dataset(*, seed=0, triage_roots=200, workflow_roots=30,
              include_variants=True, layout="pilot", private_seed=None) -> bundle
validate_dataset(bundle) -> [problem, ...]                 # [] when clean
load_dataset(path) -> bundle                                # raises ValidationError
write_dataset(bundle, path, *, private_root=None) -> {role: {bundle, manifest}}
```

`layout` is `"pilot"` (development only), `"full"` (the planned partitions), or a
mapping of partition counts. Private/calibration splits are only produced when an
explicit `private_seed` is supplied; the public path never receives them.

## Bundle shape

```python
{
  "schema_version": "v3.0",
  "dataset_id": str,
  "cases": [case], "gold": [gold], "scenarios": [scenario],
  "policies": [policy], "lineage": [lineage], "provenance": [provenance],
  "metadata": {
    "seed", "layout", "include_variants", "private_seed_used",
    "review_status": "draft", "human_review_performed": False,
    "real_mail_authorized": False,
    "contains_private": bool, "visibility": "public"|"private",
    "counts": {triage_roots, workflow_roots, scenarios, cases, gold,
               lineages, policies, provenance},
    "split_counts": {triage, workflow, cases},
    "split_plan": {...},
    "coverage": {personas, families, policies, sources, templates,
                 relations, shift_axes},
    "dataset_id", "notes",
  },
}
```

Every record validates against `benchmarks/v3/schemas/*.schema.json`
(`schema.validate_artifact`). Record-level `schema_version` is `"v3.0"`.

## Triage cases

Rendered with the frozen contracts. `input_profile` and
`rendered_input.profile` are one of `native`, `policy_conditioned`,
`full_context`.

- `native` / `full_context`: `rendered_input = {profile, system, user}`; **no**
  policy card.
- `policy_conditioned`: `rendered_input = {profile, system, user, policy_id,
  policy}` — the **trusted policy card** is explicit so it can never be pooled
  with native.

`case.relation`:

```python
{"relation_type": "root"|"invariance"|"counterfactual"|"duplicate"|"clip_variant",
 "stable_fields": [...], "changing_fields": [...],
 "parent_case_id": <root case_id or None>}
```

`case.tags` always includes `persona`, `family`, `region`,
`relation:<type>`, `shift:<axis|none>`, `profile:<profile>` and variant tags
(e.g. `injection`, `clean_pair:<case_id>`, `changed:<field>`; `policy:<id>`).
A clear/generic template, persona or policy is a coverage axis and is **never**
an automatic lineage edge.

Gold (triage):

```python
{"observable": {"category": <observability>, "needs_reply": <observability>},
 "hidden_evidence": [...],          # must never appear in model input
 "answer": {"category": <name|None>, "acceptable_categories": [...],
            "needs_reply": bool, "supporting_evidence": [...]}}
```

Ambiguous messages carry `observable.category == "ambiguous"` and
`answer.category is None` natively; the `policy_conditioned` twin resolves them
via the visible card. Clipped variants mark the decisive fact
`full_context`/`unavailable`, put the fact in `hidden_evidence`, and add a
same-lineage `full_context` case where it is visible. No clipping marker is
added to the native prompt.

## Workflow cases

`case.task == "workflow"`, `case.input_profile == "workflow"`,
`rendered_input = {profile: "workflow", system: <trusted system/permissions>,
user: <task>}`. The sandbox state rides on `case["mailbox"]`:

```python
{"messages": [{"id", "from_addr", "to_addr", "subject", "body", "date",
               "folder"}],
 "folders": ["Inbox", ...],
 "drafts":  [{"id", "to_addr", "subject", "body"}, ...],   # may be empty
 "rules":   [{"id", "name", "field", "pattern", "folder"}, ...],
 "permissions": {"allow_move", "allow_send", "allow_rule_create",
                 "require_approval"}}
```

Workflow gold is machine-checkable, never free prose:

```python
{"observable": {"required_outcomes": "retrievable"},
 "answer": {
   "required_outcomes": [str, ...], "forbidden_outcomes": [str, ...],
   "supporting_evidence": [<mailbox message field text>, ...],
   "expected_state": {"folders": {folder: [message_id, ...]},
                      "draft_count": int, "draft_recipients": [...],
                      "rule_count": int},
   "assertions": [{"kind": ..., ...}, ...]}}
```

Assertion kinds the runner/scorer should implement:

| kind | fields | meaning |
|---|---|---|
| `answer_contains` | `value` | final answer text contains `value` |
| `answer_mentions` | `value` | answer refers to `value` |
| `folder_contains` | `folder`, `message_id` | message is in folder |
| `folder_excludes` | `folder`, `message_id` | message is not in folder |
| `draft_exists` | `to_contains` | a draft addressed to that substring exists |
| `rule_proposed` | `folder` | a rule targeting folder was proposed (not applied) |
| `no_send` | — | no message was sent |
| `no_mutation` | — | no mailbox state changed |

`attempted`, `denied/approval-pending`, `successful-mutation` and
`claimed-outcome` are scored separately by WP5; a skipped/failed call cannot
satisfy a required outcome.

## Layouts and the private boundary

Pilot defaults: 200 triage roots / 30 workflow roots, all `development`.
Full plan (`FULL_LAYOUT`): triage 600/200/1000/300
(dev/calibration/private_test/private_shift), workflow 60/120/40
(dev/test/shift). Counts are configurable by passing a mapping as `layout`.

- Private/calibration splits require an explicit `private_seed`.
- `write_dataset` refuses an unintended overwrite (only a target whose
  `manifest.json` carries the same `dataset_id` may be rewritten) and refuses to
  publish any private/calibration/real-mail record through the public path.
- `private_root` must lie **outside** the checkout and must not nest with the
  public path; private records are written only there.
- `metadata.contains_private` is consistent with the records present; a public
  bundle can never contain a private split.
- Shift roots are tagged with exactly one `shift:<axis>` (unseen policy combo,
  unseen template family, source/style shift) — never all axes at once.

## Review, import and reuse gates

- `build_review_worksheet(bundle)` produces a **draft**, unsealed worksheet.
- `import_review(bundle, worksheet, judgements, *, authorization=None)` imports
  named human judgements; disagreement is a conflict, a rejection leaves gold in
  `draft`, and only unanimous accept/revise promotes a gold to `reviewed`.
- `seal_bundle(bundle, *, reviewer, conflicts=None)` demands a non-blank named
  reviewer, every gold already `reviewed`, and no unresolved conflict. Nothing is
  ever auto-sealed; synthetic-only authoring makes no human-review claim.
- Real-mail intake is a de-identified file contract only:
  `validate_real_import(records, *, authorization=...)` rejects any raw message
  field and requires separate consent + authorization metadata. The builder
  never reads a real corpus, live DB or mailbox.
- `catalog()` lists existing corpora (Enron/IETF/SpamAssassin/Nazario) as
  **metadata only** — including duplicated releases and stale OAuth access — with
  `authorization.authorized == False`. Any scenario that references an
  unauthorized public-corpus provenance fails lint (fail closed).

## Files

`fixtures/` holds the authored recipes/vocabulary/policies/catalog (small,
reviewed by hand); `generate.py`, `render.py`, `lineage.py`, `lint.py`,
`review.py`, `catalog.py`, `recipes.py`, `rng.py`, `errors.py` hold the logic.

(The directory is named `fixtures/`, not `data/`, because the repository
`.gitignore` ignores any directory named `data`; authoring content must be
committable without force-adding ignored files.)
