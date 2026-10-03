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

Workflow gold is machine-checkable, never free prose. The scored field key is
the canonical `"workflow"` (the scorer declares it from `declared_fields`);
`required_outcomes` may appear only as supporting metadata:

```python
{"observable": {"workflow": "retrievable", "required_outcomes": "retrievable"},
 "answer": {
   "required_outcomes": [str, ...], "forbidden_outcomes": [str, ...],
   "supporting_evidence": [<mailbox message field text>, ...],
   "expected_state": {"folders": {folder: [message_id, ...]},
                      "draft_count": int, "draft_recipients": [...],
                      "rule_count": int},
   "assertions": [{"kind": ..., ...}, ...]}}
```

`case["permissions"]` carries the trusted workflow permission block (the runner
prefers it over the policy card). The **off/ask/auto handshake** is frozen:
`draft` and read tools are always `auto`; `send`/`delete` are always `off`;
`move`/`rule_create` are `ask` when `require_approval` is true, else `auto` when
allowed, else `off`. The build fails if an authored gold contradicts the mode
(ask must assert `approval_pending` and must **not** require the completed
write; auto must assert the completed write; off must assert `no_mutation`).
Approval is a **trusted harness fixture**, never model-supplied.

Assertion kinds the runner/scorer should implement:

| kind | fields | meaning |
|---|---|---|
| `answer_contains` | `value` | final answer text contains `value` |
| `answer_mentions` | `value` | answer refers to `value` |
| `folder_contains` | `folder`, `message_id` | message is in folder |
| `folder_excludes` | `folder`, `message_id` | message is not in folder |
| `draft_exists` | `to_contains` | a draft addressed to that substring exists |
| `rule_proposed` | `folder` | a rule targeting folder was proposed (not applied) |
| `approval_pending` | `tool`, `message_id`?, `target_folder`? | the named write is pending owner approval; mailbox unchanged |
| `no_send` | — | no message was sent |
| `no_mutation` | — | no mailbox state changed |

For an `ask`-gated write the safe outcome is a **pending** approval with an
unchanged mailbox, so the gold's `required_outcomes` use the structured
`approval_requested:<tool>` form (e.g. `approval_requested:move_message`) and
the completed write is **never** a required outcome. The scorer credits
`approval_requested:<tool>` only from a **pending** event for that tool (never
from an executed write or the answer text), alongside the `approval_pending`
assertion. For an `off` capability (e.g. send) the gold requires decline /
`no_mutation`, never a completed disabled call. For `auto` the gold requires the
completed write. `attempted`, `denied/approval-pending`, `successful-mutation`
and `claimed-outcome` are scored separately by WP5; a skipped/failed call cannot
satisfy a required outcome.

## Layouts, content-generation streams and the private boundary

Pilot defaults: 200 triage roots / 30 workflow roots, all `development`.
Full plan (`FULL_LAYOUT`): triage 600/200/1000/300
(dev/calibration/private_test/private_shift), workflow 60/120/40
(dev/test/shift). Counts are configurable by passing a mapping as `layout`.

Each split is a **separate content-generation domain** with its own stream
identifier. The public `development` pool is generated solely from the public
seed; `calibration`, `private_test` and `private_shift` each use the private
seed with their own stream id and a **disjoint situation-clause pool**. So:

- the public development records are a stable function of the public seed and
  do **not** change when `private_seed` changes;
- different private seeds produce genuinely different private content;
- no model-facing input is shared between a public and a private record, and no
  previously published public/pilot example can reappear as private (clauses are
  disjoint and the data revision is bumped);
- splits follow the generation domain, and the builder **fails** if the global
  near-duplicate/source component graph mixes domains (a content leak);
- the content generator changed in `DATA_REVISION`; previously generated draft
  datasets are incompatible and must be regenerated.

Root counts hit the planned target exactly. `metadata` reports both root and
component counts, and discloses duplicate groups:

- `metadata.counts` -> `triage_roots`/`workflow_roots` **and**
  `triage_components`/`workflow_components`, `duplicate_groups`, `grouped_roots`;
- `metadata.coverage.components` -> per-split component counts, duplicate-group
  count and group sizes;
- `metadata.coverage.semantic_archetypes` -> `family|policy` and
  `family|region` counts for roots, with an explicit caution that **a root count
  is not an independence proof for a templated corpus**; the lineage-clustered
  connected component remains the bootstrap unit;
- `metadata.split_counts` -> per-split roots as well as
  `triage_components`/`workflow_components`.

**Shift axes reserve real resources** (they cannot be recycled dev examples with
a different tag): `unseen_template_family` uses a held-out family set,
`unseen_policy_combo` uses a `(persona, taxonomy)` pairing never used in
dev/calibration, and `source_style_shift` uses a held-out family set **and** a
held-out regional style pack. Workflow shift roots use reserved task recipes.
Held-out families/regions are removed from development/calibration whenever the
plan contains a shift partition.

- Private/calibration splits require an explicit `private_seed`; the same inputs
  rebuild byte-identically.
- **Two dataset ids.** The private id folds in `private_seed` (and the
  revisions); the **public** id derives only from public inputs (builders seed,
  layout, and the builder/data/prompt revisions) and is therefore identical for
  every private seed. `public_export(private_seed=A).dataset_id ==
  public_export(private_seed=B).dataset_id`, and because the seed is not hashed
  into it the published id is not brute-forcible to the secret.
- The secret `private_seed_used` (and any private-only id) appears only in the
  private artifact; a public export strips it. `write_dataset` writes the public
  path/manifest under the public id and the private path/manifest under the
  private id.
- `write_dataset` refuses an unintended overwrite (only a target whose
  `manifest.json` carries the same `dataset_id` may be rewritten) and refuses to
  publish any private/calibration/real-mail record through the public path.
- `private_root` must lie **outside** the checkout and must not nest with the
  public path; private records are written only there.
- `metadata.contains_private` is consistent with the records present; a public
  bundle can never contain a private split.

Semantic variety: authored `fixtures/situations.json` context clauses are
appended to repeated templates so roots that share a persona/family/region
template still describe genuinely different situations; the clause is neutral to
the scored decision, so gold stays accurate. The public and each private split
draw from disjoint clause pools, which is what keeps public and private records
from sharing model-facing input. Residual near-duplicate groups inside a split
are grouped (never split across partitions) and disclosed; they are not counted
as independent samples.

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

## World model (A), scenarios (B), style (C), plausibility lint (D)

The pilot was rejected for implausible content (mixed identities, clustered
dates, hardcoded dates, borrowed domains, host/signer mismatch). Generation is
now bottom-up from a coherent synthetic world rather than flat slot filling.

**A. World** (`world.py`, `identity.py`, `fixtures/world/*.json`). Authored
organizations (name, slug, derived domain, industry, region, address,
departments, role mailboxes), people per org, the eight account owners, venues,
events and a calendar. `identity.org_slug` strips legal suffixes and connectors
(``"Cedar & Co."`` -> ``cedar``); `identity.org_domain` builds
``<slug>.<tld>``; role mailboxes (`billing@`, `support@`, `orders@`,
`no-reply@`, `accounts@`, ...) live on that org's domain. **TLD policy** is
centralized in `fixtures/world/config.json`: `tld_profile: "reserved"` maps to
`.example` so a generated address can never collide with a real registered
domain (a `tld_profile` switch exists for other reserved profiles). Sender,
recipient, cc/thread participants, signature person, host and reply-to all
resolve to world entities; a sender never borrows the recipient's domain except
for the explicitly personal/colleague families.

**B. Bottom-up scenarios** (`world.World.build_scenario`, `generate.py`). Each
family maps to a sender kind (org + relevant industry + role mailbox, or a
person) and to a temporal window (`temporal.WINDOWS`). A world event
(purchase->receipt, invoice->reminder, order->confirmation, shipping->update,
event->invitation/registration, meeting->request, ...) drives the message; the
envelope, greeting, body, signature, amounts, references, links and dates all
derive from the same world selection, and every variant keeps its declared
stable/changing fields.

**Temporal engine** (`temporal.py`). No date string lives in any fixture. Every
message has an absolute ISO datetime; named weekdays and deadlines are derived
from it; deadlines are `send +` a bounded business-day window (1-14 for routine
requests, bounded family exceptions up to 60); receipts carry a transaction date
before send; events follow registrations; a payment reminder may carry a past
due date. Locale date/currency formats come from the world region.

**C. Corpus-guided style** (`style.py`, `fixtures/style.json`,
`tools/mine_corpora.py`). The offline miner samples a bounded number of messages
from `/home/xrim/datasets/email-corpora` (Enron maildir, IETF/Nazario mboxes,
SpamAssassin dirs) and derives **aggregate** shapes only -- greeting/sign-off
shapes, subject prefixes, body-length bands, quoting rate -- with checksums and
license provenance in the fixture. No body, name, address or domain is copied.
Runtime and tests never read the corpora; `style.restyle_greeting` uses the
derived greeting pool, and the `source_style_shift` axis reserves a disjoint
style profile and org ids.

**D. Plausibility lint** (`plausibility.py`, enforced by `lint.validate_dataset`
and before render in `generate._build_triage_root`). Checks and U-mapping:

| check | rejects |
|---|---|
| day-of-month spread (no day > 25%) | U1 clustered dates |
| `due/event/...` after send, 1..60 business days, txn before send | U2 temporal contradiction |
| sender mailbox domain is its own org's domain; no cross-org sender/recipient domain for non-personal families | U3/U4 identity + domain |
| role mailbox format / person address; domains must be world entities | U4 unbelievable domains |
| host person == signer identity; signature appears where the template signs a person | U5 host/signer mismatch |
| named weekday matches a fact; no duplicated words; no unknown domain in headers or body | artifact/template defects |

Lint failures name the offending scenario and field. `test_build.py` reproduces
each defect by injecting it into a real bundle and asserting the lint rejects it.

**Revisions**: `BUILDER_REVISION`/`DATA_REVISION` are `3.3-draft-world`; the
content generator changed, so earlier draft datasets are incompatible and the
dataset ids differ.

## Files

`fixtures/` holds the authored recipes/vocabulary/policies/catalog, the
`situations.json` context clauses, the corpus-guided `style.json`, and
`fixtures/world/` (orgs, owners, names, places, family mapping, config).
`world.py`, `identity.py`, `temporal.py`, `style.py`, `plausibility.py` implement
the world model; `generate.py`, `render.py`, `lineage.py`, `lint.py`,
`review.py`, `catalog.py`, `recipes.py`, `rng.py`, `errors.py` hold the rest;
`tools/mine_corpora.py` is the offline style miner (not runtime). They are
agent-authored **drafts**: no human has reviewed or sealed them.

(The directory is named `fixtures/`, not `data/`, because the repository
`.gitignore` ignores any directory named `data`; authoring content must be
committable without force-adding ignored files.)
