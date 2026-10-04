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
departments, role mailboxes, and a **catalog** of the goods/services/documents/
projects/courses it can reference), people per org, the eight account owners and
their **real colleagues**, venues, events and a calendar. `identity.org_slug`
strips legal suffixes and connectors (``"Cedar & Co."`` -> ``cedar``);
`identity.org_domain` builds ``<slug>.<tld>``; role mailboxes (`billing@`,
`support@`, `orders@`, `no-reply@`, `accounts@`, ...) live on that org's domain.
**TLD policy** is centralized in `fixtures/world/config.json`: the owner
approved realistic commercial-looking domains, so `tld_profile: "commercial"`
maps to `.com`. Every organization, person and address is **fictional** and
generated only; no mail is sent and no real domain is contacted. The `reserved`
profile (`.example`) remains as a switch, and the domain/person lint derives its
checks from the configured suffix. Sender, recipient, signature person, host and
reply-to all
resolve to world entities.

**Selection fails closed** (`World.eligible_orgs`): a sender must match one of
the family's declared industries **and** hold one of its declared roles, or the
build raises. There is no "matching or any org" fallback and no silent role
substitution, so marketing uses offers/newsletter roles, invoices and reminders
use billing/accounts, security uses security/no-reply. A `meeting_request`
colleague is a genuine member of the owner's org/domain (constructed, not a
vendor person with a rewritten domain), and every scenario records an explicit
relationship (customer/tenant/client/student/parent/colleague/friend).

**B. Bottom-up scenarios** (`world.World.build_scenario`, `generate.py`). Each
family maps to a sender kind and to a temporal window (`temporal.WINDOWS`). The
scenario's object (item/service/document/project/course) is drawn from the
**sender org's declared catalog** and bound to the family's purpose and the
recipient relationship, so a lettings document request asks for a tenancy/lease
document and a software vendor references its licence/plan -- never a telecom
data plan from a retailer or a software licence from a landlord. A world event
(purchase->receipt, invoice->reminder, order->confirmation, shipping->update,
event->invitation/registration, meeting->request, ...) drives the message.

**Temporal engine** (`temporal.py`). No date string lives in any fixture. Every
message has an absolute ISO datetime; named weekdays and deadlines are derived
from it; deadlines are `send +` a bounded business-day window (1-14 for routine
requests, bounded family exceptions up to 60); receipts carry a transaction date
before send; a payment reminder may carry a past due date. **Event seasons come
from the actual held date**, not the send date (a fair announced in autumn is
held in autumn), and social events (fairs, parties) may fall on a weekend while
business deadlines stay on business days; RSVP/registration dates precede the
event they answer for. Locale date/currency formats come from the world region.

**C. Corpus-guided style** (`style.py`, `fixtures/style.json`,
`tools/mine_corpora.py`). The offline miner samples a bounded number of messages
from `/home/xrim/datasets/email-corpora` (Enron maildir, IETF/Nazario mboxes,
SpamAssassin dirs) and derives **aggregate** shapes only -- greeting/sign-off
shapes, subject prefixes, body-length bands, quoting rate -- with checksums and
license provenance in the fixture. No body, name, address or domain is copied.
Runtime and tests never read the corpora; `style.restyle_greeting` uses the
derived greeting pool, and the `source_style_shift` axis reserves a disjoint
style profile and a disjoint authored org cohort (`pinnacle`, `beacon`).

**D. Plausibility lint** (`plausibility.py`, enforced by `lint.validate_dataset`
and before render in `generate._build_triage_root`). Two layers:

*Structured facts + source message* (`check_scenario`):

| check | rejects |
|---|---|
| day-of-month spread (no day > 25%) | clustered dates |
| `due/event/...` after send, 1..60 business days, txn before send, rsvp/registration before the event | temporal contradictions |
| event season equals the season of the **held** date | wrong-season event names |
| sender mailbox domain is its own org's domain; membership; no cross-org sender/recipient domain for non-personal families | identity + domain defects |
| sender role is a role the org actually holds; catalog object is in the org's catalog | role/industry/object defects |
| host person == signer identity | host/signer mismatch |

*Rendered model input per case* (`check_case`): the header date must equal the
send datetime; `From` must equal the declared sender and `To` the declared
recipient (or its team alias); every printed calendar date must match a declared
fact and a printed weekday must match that date; the signer and the bound
catalog object must appear. Clip/quote variants declare a per-message
**projection** (`case.audit` plus `clip_quoted_weekdays`) so authored quoted
boilerplate is not falsely rejected -- the lint is never disabled. Lint failures
name the offending scenario/case and field. `test_build.py` reproduces each
defect first and then asserts the lint rejects it, including rendered-only
mutations while the metadata stays correct.

**Semantic gold (AR-1)** (`fixtures/semantics.json`, `recipes.resolve_semantics`).
Gold is no longer resolved by "first category whose role intersects a generic
action role" (which produced Incident for a developer's social invite, Coursework
for an ISP support email, Appointment for a tenancy document). Each family has an
authored semantic intent; each policy category has authored `covers`/`by_name`
grounded in its own description. Resolution:

- exactly one covering category, and (the policy card is visible **or** the
  category name is self-evident) -> `visible` with that category;
- several categories fit, or the family is inherently ambiguous, or a native run
  cannot see the description that would disambiguate -> `ambiguous` with an
  acceptable set and `answer.category = null`;
- no category covers the intent -> `unavailable` with `reason = taxonomy_gap`
  and `answer.resolution_reason = taxonomy_gap` (never an invented first label).

A category's explicit `roles` (e.g. the marketing-merge twin) takes precedence
over the name map, so policy twins honor only their declared mapping change.
Coverage and the number of taxonomy gaps are recorded in
`metadata.coverage.taxonomy` by profile and persona (a diagnostic, not a quality
claim). No production category contract is modified.

**Context claims + reply intent (AR-2)** (`fixtures/situations.json`,
`world.build_scenario`). Context clauses are purpose-scoped (request/billing/
news/notice/social) and never assert an unbacked copy/CC/team/workstream/prior-
exchange fact; the lint rejects such a phrase unless the scenario declares the
corresponding fact. A template may declare its own `needs_reply` (e.g. the
payment family ships both an automated pay-only reminder, needs_reply false, and
a variant that explicitly asks for a reply, needs_reply true); gold derives the
reply intent from the rendered template, so a payment/action message is never
scored as a reply request unless it actually asks for one.

**Revisions**: `BUILDER_REVISION`/`DATA_REVISION` are `3.5-draft-semantic`; the
content generator changed, so earlier draft datasets and previews (including
`83ed0ac`) are incompatible and the dataset ids differ.

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
