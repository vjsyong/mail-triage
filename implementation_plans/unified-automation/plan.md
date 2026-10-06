# Expanded Implementation Plan: Unified Automation Workspace

## Handoff
- Status: **READY FOR IMPLEMENTATION**
- Plan path: `implementation_plans/unified-automation/plan.md`
- Implementer entrypoint: `plan-implementer`
- Requested plan name: none explicitly supplied; derived title/slug: Unified Automation Workspace / `unified-automation`.
- Recommended branch and PR base: `unified-automation`, based on current `master`; use the repository's feature worktree rule under sibling `mail-triage-wt/`. Do not implement in the main checkout. The implementer supplies actual worktree, branch and base SHA at dispatch.
- Gate base ref and command: compare against the integration base on `master`; `git diff master...HEAD --check` for committed changes and `git diff --check` for working changes. The behavioral merge gate is `.venv/bin/python tests/mock_e2e.py --all`, not its dirty-tree auto-selection. No separate repository PR/base-ref gate was found.
- Package waves: wave 1 WP1; wave 2 WP2; wave 3 WP3; wave 4 WP4. **Serialize all packages**: `app.py`, `ux.py`, and `tests/ux_e2e.py` are shared resources. Do not dispatch simultaneous edits to them.
- Recommended PR risk/review: **HIGH**, with bounded independent re-review. This is not an engine rewrite or schema migration, but exposes mail-moving switches in a new location and changes editing/persistence of the shared classification vocabulary and destination map. Review preservation of defaults, scoped saves, map-only entries, failed saves, escaping and opt-in transitions. Implementer reassesses actual diff and follows repository/user policy.
- Reconnaissance baseline: checkout on `master`, observed HEAD `6fcd762`; `git status --short` was empty. Another worktree exists for `style-date`; it is outside this feature. Recheck user changes before implementation; line anchors below are reconnaissance anchors, not permission to overwrite later changes.
- Planning only: no application changes, tests, deployment, mailbox access, branch creation or commits performed during expansion.

## Objective

Give the owner one discoverable workspace for answering **“what happens to my mail, why, and where do I change it?”** Replace scattered primary Rules / Flows / Templates destinations and Settings' legacy “Filing & drafts” card with an **Automation** home, persistent local navigation, structured category/default-filing rows and connected drafting defaults.

Completion means the new workspace and old bookmarked editors both work, settings remain losslessly compatible, category-to-flow creation is review-before-save, explanatory copy reflects the existing two-phase engine, and automated plus desktop/393px browser evidence is green. It does not mean enabling any automation on live mail.

## Requirements

### Functional
- F1: One primary **Automation** navigation item replaces primary Rules, Flows and Templates entries in desktop navigation and mobile More. Keep Simulator and Learning as existing neighboring destinations; link them from Automation Overview. Preserve plugin navigation injection.
- F2: Use six local sections in this order: **Overview**, **Rules**, **Flows**, **Categories & filing**, **Drafting**, **Controls**. Show the same local navigation on rule/flow/template list and editor pages. Existing editor URLs remain canonical.
- F3: Move category vocabulary and category destination editing into Categories & filing. Replace comma-separated categories and map textareas with labeled structured rows. Retain category names/order and mapping distinctions on an unchanged save.
- F4: Put the sole editable `llm_apply` switch next to default filing. Controls displays its state and links there, not a second editable copy. Rules/flows have independent live/preview switches; there is no global “enable everything” control.
- F5: Drafting contains the existing templates list, shared Drafts-folder override and a list of flows with draft steps. Every draft step links back to the shared destination preference. Blank override still means auto-detect.
- F6: Category rows link to flows and fast-path classifiers that reference the category, including disabled records, and offer **Create flow for this category**. Do not imply these counts exhaust specialist/plugin/historical-label usage.
- F7: Category-to-flow creation opens the existing new-flow editor with a category filter and optional mapped move step. It never saves, activates a flow, changes the map or changes global switches on GET.
- F8: Overview shows enabled/total rule and flow counts, separate live/preview states, automatic-classification/default-filing states, precedence explanations, and **Test on a message** linking to the existing simulator. No new testing engine.
- F9: Settings retains system configuration and backward-compatible landmarks; moved cards become concise links to Automation. Preserve old `/settings` POST consumers and anchored bookmarks.
- F10: Update assistant page context and current feature documentation so neither directs the owner to the removed filing editor.

### Non-Functional
- NF1: No engine execution-order, matching, retry, idempotency, guard, undo, learning, draft-generation, sending or account/OAuth behavior change. `engine.py` changes are confined to presentation text in `assistant_page_context`.
- NF2: Never delete mail. Never enable `llm_apply`, `rules_apply` or `flows_apply` as a side effect of navigation, map saves, prefill or deployment. Existing default values remain untouched.
- NF3: Keep server-rendered Flask templates and vendored Turbo; no frontend framework, new dependency, vendored-library edit, new schema, migration or new deploy module.
- NF4: Shared UI/CSS resides in `BASE_TMPL`; use existing `.px-sw`, `.savebar`, `.empty`, `.arm-del`, scoped saves, flash `ok/warn/err`, submit guard and mobile row actions. Server validation is visible and focused. New labels and navigation remain usable without JS; dynamic additions are enhancement.
- NF5: Use isolated tests/demo only. Do not inspect secrets, alter live data, touch generated proxy configuration or rebuild/restart production to test this feature.

### Data And Contracts
- Existing keys/types are retained: `categories: list[str]`, `category_folders: dict[str,str]`, `drafts_folder: str`, `llm_apply/rules_apply/flows_apply/llm_suggest/heuristics_enabled/heuristic_autorefine: bool`, existing integer budget/batch/concurrency keys.
- No conversion of map entries to stored flows; no automatic category rename/cascade over messages, labels, models, plugin config or rules/flows.
- New structured save validates before writing and saves category list/map together in one SQLite transaction. Use optimistic conflict detection for those two keys without schema changes; legacy `/settings` POST behavior remains compatible.
- Category removal is explicit; v1 does not provide rename of an existing category. Existing names display read-only. Adding a replacement is not advertised as a rename.

## Existing System Map

| Area | Exact anchors and behavior | Consumers / blast radius |
|---|---|---|
| Shared shell | `app.py:389` loads `static/ux.js?v=1`; `BASE_TMPL` navigation at `app.py:1031–1073`; mobile More at `app.py:2195–2256`; `VTPOS` at `app.py:1354–1371` | All pages, Turbo direction, drawer, extension navigation. Shared styles must not be defined only on Settings. |
| Rendering | `_render_src` `app.py:2150` compiles/caches string templates; `render` `app.py:2160` wraps body with `BASE_TMPL` | Add workspace chrome once here, using route classification, not copies in each template. Preserve `setup=True` onboarding. |
| Rules | `RULES_TMPL` `app.py:2813`; `rules` `app.py:4792`; `RULE_EDIT_TMPL` starts around `app.py:4827`; preview helper `ux.py:26–48` | Existing test, CRUD, reorder, guard labels, editor previews remain. Dashboard/assistant deep links target `/rules`. |
| Flows | `FLOWS_TMPL` `app.py:5011`; `FLOW_EDIT_TMPL` `app.py:5069`; `_flow_edit_context` `app.py:5798`; `flows` `app.py:5821`; `flow_new` `app.py:5833`; `flow_edit` `app.py:5849`; draft fields in `fieldsFor` around `app.py:5400–5464` | Four editor render paths use context helper. Forms own `cond_kind_i/cond_field_i/cond_op_i/cond_value_i/cond_score_i` plus `steps_json`; keep URLs so `fill_flow` still recognizes editor context. |
| Templates | `TEMPLATES_TMPL` precedes `app.py:5940`; `TEMPLATE_EDIT_TMPL` `app.py:5955`; `templates` `app.py:5996`; `template_new` `app.py:6002`; `template_edit` `app.py:6014`; `template_delete` `app.py:6030` | Retain CRUD and infill plugin hints; list becomes Drafting content without duplicating template actions. |
| Existing settings UI | `SETTINGS_TMPL` around `app.py:8660`; section links `app.py:8700–8707`; classification card `app.py:8806–8829`; classifier switches `app.py:8836–8847`; sorting/cards `app.py:9030–9062` | Filing vocabulary/map/destination separate from `llm_apply` in AI Classification. Reply detection shares that classification card and must be retained when splitting it. |
| Settings persistence | `_save_behavior_settings` `app.py:8375–8433` writes only submitted keys; `_settings_anchor` `app.py:9289`; `settings` `app.py:9303–9346` accepts local `next` and otherwise redirects to an anchor | Old forms/scripts and tests rely on `/settings` and checkbox-before-hidden ordering. Endpoint test-before-save paths are outside scope. |
| Settings JS | `static/ux.js:98–135` uses five section IDs; `initialize` `:137`; cache cleanup `:160–170` | Keep `sorting`, `ai-classify`, `ai-classifiers`, `sort-rules`, `sort-filing` landmarks as link cards. Do not send an external link through the hash-section click handler. |
| Store | `store.py:10–38` defaults; `get_setting` `:537`, `set_setting` `:548`, `all_settings` `:554`; `list_flows` `:622` order position/id; `list_heuristics` `:697`; heuristic category field `:712`; `list_templates` `:972` | Category vocabulary feeds LLM, corrections, training/UI pickers. Map-only keys can exist and must not be silently dropped by a UI round trip. |
| Execution phases | `_needs_verdict` `engine.py:894`; `flow_matches` `:974`; scan `_process_folder` `:1627`; `classify_verdict` `:2417`; `classify_and_store` `:2442–2545` | Rules and non-category flows operate in scan phase; category-bearing flows wait for a verdict. Verdict comes from user correction/heuristic/LLM path, not necessarily an LLM call. |
| Classification filing | `engine.py:2457` records suggested folder regardless of `llm_apply`; guard/keep handling `:2484–2496`; category flow handling `:2496–2520`; fallback `:2521–2538` | A successfully recorded category flow, even dry-run or without a move step, suppresses fallback filing. A flow exception may leave fallback eligible. Do not promise unconditional suppression merely because a filter matched. |
| Draft destination | `save_draft` `engine.py:2839–2851` uses override, then `\\Drafts` special use, then `Drafts`, and closes connection in `finally` | No provider discovery during workspace GET. Show “Auto-detect” unless an override is saved; do not claim a resolved provider folder without querying it. |
| Assistant | `assistant_page_context` `engine.py:3998`; existing rule/flow/template paths `:4039–4112`, list descriptions `:4157–4194`, obsolete Settings description `:4195–4210`; `fill_flow` path gate around `:5640` | Add new workspace contexts, retain editor paths, do not add a settings-mutation tool or expose secrets in context. |
| Tests | `tests/ux_e2e.py:20` `WorkbenchTests` isolates DB and disables auto-filing; preview/guard tests `:101–143`; `tests/mock_e2e.py` T26 `:2847`, T28 `:3124`, T33 `:3403`, T34 `:3499`, T42 `:3856`, T58 `:5697`, T64 `:6111` | Extend isolated unittest file; T64 already runs it in the merge suite. Preserve all existing assertions/coverage. |
| Shipping / safe QA | `Dockerfile:8–15`, `Dockerfile.demo:5–12` already ship `ux.py` and static files; `docker-compose.demo.yml` isolates data/network and binds `8101`; `docs/ux-demo.md` | No Dockerfile changes required if existing modules are used. Do not take over the existing demo container/port; use the isolated validation command below. |

Binding references read: root `AGENTS.md` (no nested AGENTS or CONTRIBUTING found), `README.md`, `docs/README.md`, `docs/ui-conventions.md`, `docs/flows-builder-v2.md`, `docs/flows-fuzzy-classifier.md`, `docs/spa-turbo.md`, `docs/features.md`, `docs/ux-demo.md`. Keep feature worktree rules and full-suite merge requirement.

## Contracts

### C1: Routes, workspace chrome and compatibility

New endpoints (names are proposed, not pre-existing symbols):

| Method/path | Proposed endpoint | Result |
|---|---|---|
| GET `/automation` | `automation` | Overview, 200; read-only |
| GET `/automation/categories` | `automation_categories` | Category/default-filing editor, 200; read-only |
| POST `/automation/categories` | `automation_categories` | Validates C2, then 303 to same GET with success flash; invalid form 422 with focused error and submitted rows; conflict 409, no writes |
| GET `/automation/controls` | `automation_controls` | Scoped control forms, 200; read-only |
| Existing GET `/rules`, `/flows`, `/templates` and their editors | unchanged | 200 with workspace chrome; no redirect aliases or relocated mutations |

Drafting tab targets existing `url_for('templates')`; Rules/Flows tabs target existing endpoints. Overview/Categories/Controls use new endpoints. Do not create `/automation/drafting` as a second canonical list or rewrite existing POST redirects. `/templates` renders existing list plus destination/flow-reference cards, under page title **Drafting**; template editors retain their titles and return URLs.

Introduce pure `ux.automation_section(path) -> str | None`: exact known new GET paths and existing rule/flow/template list/editor paths resolve to section IDs `overview/rules/flows/categories/drafting/controls`; `/automation/preview`, unrelated `/automation/*`, Settings, Learning, Simulator and setup pages return None. It governs shell active state and local navigation, not authorization. In `render`, prepend one rendered workspace header/navigation to body only on recognized pages and when not `setup`.

Local navigation is a semantic `<nav aria-label="Automation sections">` of ordinary links, with one `aria-current="page"`; it is not a JS ARIA tab widget. Allow horizontal scrolling at phone width without overflowing the page. Keep existing page titles beneath it. Add workspace URLs to `VTPOS` with sensible sibling ordering (overview 30, rules 31/editor 32, flows 33/editor 34, categories 35, drafting 36/editor 37, controls 38; move Simulator to 39, leave Learning/classifiers later). Prefix match order must keep `/automation/categories` and `/automation/controls` ahead of exact `/automation` where appropriate. No vendored Turbo modification.

Settings keeps its five-section navigation and the following stable IDs as non-editing landing cards:
- `sorting` and `sort-rules` link to Controls and Rules/Flows.
- `sort-filing` links to Categories & filing and Drafting.
- `ai-classify` links to Controls; retain reply detection in a separate editable **Reply detection** card within AI.
- `ai-classifiers` links to Controls plus Learning/classifier management.
Keep all other settings cards and anchors. Old Settings POST fields continue through existing `_save_behavior_settings` and `next` validation; default redirect/flash semantics are unchanged. Fix `_settings_anchor` to recognize existing “Filing & drafts” scope as `sort-filing` without dropping older matching scopes. Do not blanket-redirect `/settings`, since URL fragments are invisible to the server.

### C2: Lossless category/default-filing editor and save

Introduce pure `ux.category_rows(settings, flows, heuristics) -> list[dict]` and `ux.parse_category_rows(form, current_settings, references) -> (categories, mapping)`; raise `ValueError` for validation failures. Here `form` is the posted MultiDict; `references` is `{'rows': category_rows(...), 'invalid_flows': list[{'id': int, 'name': str, 'reason': str}]}` computed from current stored records. Introduce pure `ux.automation_references(settings, flows, heuristics) -> dict` to construct that object, including type/shape validation of decoded condition arrays. Implementation may factor private helpers within `ux.py`; retain these public helper signatures for tests.

Row schema: `name: str`, `configured: bool`, `mapping_present: bool`, `folder: str`, `flow_refs: list[{id:int,name:str,enabled:bool}]`, `classifier_refs: list[{id:int,name:str,enabled:bool}]`. Keep configured categories in their stored order, then map-only entries in stored map order. Missing mapping and explicit `""` are distinct. Blank folder means **Keep in current folder**, not “protect from all automation”. When auto-filing is off, mappings still supply suggestions. All rows render even if their category is not currently in the classification vocabulary.

Form contract: `row_count` integer; each row index `0..row_count-1` has `original_i`, `name_i`, `folder_i`, `configured_i` (`0/1`), `mapping_present_i` (`0/1`), and optional `remove_i=1` or `restore_i=1`; hidden `settings_version` from the current two-key snapshot. `original_i` is the decimal index of that row in the server's current `category_rows` result, or empty for a new row; do not use the category name as a unique row ID. All original indices must appear exactly once, even for rows marked removed; reject missing/repeated/out-of-range originals and unexpected repeated form field values. Existing names are read-only; metadata is not authority—match original rows against server current state. Configured flags on existing vocabulary rows cannot be unset except by explicit removal. Map-only rows show **Legacy mapping · not offered for classification**; a separate `restore_i` checkbox opts that row back into the vocabulary, appended after surviving vocabulary rows in displayed legacy-row order. Do not pair that checkbox with a same-name hidden input: an absent checkbox means false. Reject simultaneous restore/remove and restore on a non-legacy row. Mapping-presence/configured metadata flags cannot be changed by a posted hidden value; use server originals when parsing. New rows have empty original, configured=1, mapping_present=0. Supply three blank new rows server-side, progressively add more using JS; ignore wholly blank new rows. A nonblank new category may have a blank folder. Existing rows are removed only by an explicit checked **Remove on save** control, not by accidental missing inputs or blanking a label.

Parse every row before any write. Reject noninteger/incomplete row structures, inconsistent original metadata, changed existing names, duplicate new names (case-insensitive against existing/new names), destinations without names, newline/control characters in new/edited category/destination values, and new category names containing comma or `=` (legacy transport ambiguity). Existing names/values outside these new-entry rules must round-trip unchanged, with explanatory legacy styling; don't normalize them. Reject changing a nonempty vocabulary to empty; an already-empty legacy vocabulary may round-trip with a warning. Preserve current configured duplicates/case variants on an unchanged save; do not deduplicate or recase existing data. For exact duplicate configured names, render their folder/removal controls read-only and reject changes to those rows in v1 rather than inventing ambiguous removal/update semantics; explain that duplicate cleanup needs a separate repair. Case-variant but distinct existing names remain separate mapping keys. Offer no new case-ambiguous names. Trim new names and edited folder values; preserve unchanged existing values exactly. Do not insert blank mapping keys for previously unmapped rows unless a destination is entered; preserve explicit blank map keys. Removing an editable row removes that exact category from vocabulary and map, but never changes historical message labels or trained artifacts.

Block explicit removal if any stored flow category condition (case-insensitive, including disabled flows) or fast-path heuristic `category` references it; show the offending links and require the owner to edit those consumers first. This is bounded protection, not a claim to enumerate specialists/plugins/historical labels. Include copy that old labels/trained specialists/plugin settings are not rewritten. Map-only row removal follows the same rule. If malformed flow JSON prevents reliable removal-reference checking, allow read-only display but reject removal and identify the flow to repair. No background repair or silent skip of an unknown dependency.

Introduce in `store.py`:
- `settings_version(values: dict) -> str`: SHA-256 over deterministic JSON for exactly `categories` and `category_folders`, using effective defaults when absent; dictionary key ordering canonical, list order significant.
- `save_category_settings(categories: list[str], mapping: dict[str,str], expected_version: str) -> bool`: open a connection, `BEGIN IMMEDIATE`, read effective values of those two keys, compare digest, return False/rollback on mismatch; upsert both JSON values and commit together on match, return True. Any exception rolls back and propagates. No DDL, no new default/key, no generic caller-supplied SQL. Reuse existing settings JSON encoding/default semantics.

Route checks snapshot mismatch before parsing against current settings, and repeats atomically in store helper to cover concurrent saves. A conflict renders submitted values read-only alongside a **Reload current categories** link, states nothing was saved, and does not silently rebase/retry. Validate references against current records; no system-wide locking of other editors is introduced. Legacy `/settings` remains last-write-wins for its old transport, but any such intervening category/map update causes a structured form's snapshot comparison to fail. No snapshot includes secrets.

On 422, show editable submitted values and `#form-err` focused; on 409, require reload before editing/saving again. DB failures yield normal logged server error behavior and no partial category/map update; do not flash success or attempt an IMAP/LLM call. Repeat POST with stale token is a harmless conflict.

### C3: Scoped controls and drafting ownership

Forms remain POST `/settings`, `section=behavior`, appropriate human `scope`, and fixed local `next`:
- Categories & filing owns a **separate** `llm_apply` switch form; next `/automation/categories`.
- Controls owns separate rule-live and flow-live switch forms and a Classification form with `llm_suggest`, `max_llm_per_hour`, `llm_batch_per_cycle`, `classify_concurrency`; it also owns a Fast-path classifiers form with `heuristics_enabled` and `heuristic_autorefine`; next `/automation/controls`.
- Drafting owns `drafts_folder` only; next `/templates#draft-destination`.
- Reply detection stays in Settings AI and owns `reply_tracking_enabled`, `reply_sent_folder`, `reply_identity_addresses` only, with its existing parsing/copy.
- Mail watching/poll/lookback remain Settings Mail; endpoints, assistant model/permissions, learning promotion and plugin settings remain where they are.

Use existing integer clamps: hourly cap min 0, batch min 1, concurrency 1–16. Preserve checkbox value `1` before paired hidden `0` in legacy Settings control transports, so existing `f.get()` keeps working. All enabled-state booleans use `.px-sw`, named aria-labels, visible state text, and existing submit busy/flash behavior. Multi-field forms commit with explicit Save; single-setting forms also provide a visible Save button for the no-JS path. No auto-save of unsaved category/map rows when a neighboring switch changes. Form-edit removal/restore selectors are ordinary labeled checkboxes, not entity enable switches or immediate entity-delete actions.

The `llm_apply` form prompts on false→true with consequence copy **“Enable automatic default filing? Future classifications may move mail using the category destinations. Protective rules still apply.”** Cancel leaves stored value and checkbox off; true→false needs no prompt. Implement on that form's submit path (not a second submit delegate), prior to the shared busy guard. Other explicit live-mode switches preserve current confirmation policy. No newly constructed request automatically supplies a checked live switch. Add `<noscript>` guidance about submitting settings switches; do not claim a server-enforced confirmation policy for the preserved legacy POST API.

`ux.drafting_flows(flows) -> dict` returns `{'flows': list[{'id': int, 'name': str, 'enabled': bool}], 'invalid_flows': list[{'id': int, 'name': str, 'reason': str}]}`. Return each flow once if any action has type `draft`, including fixed/template/llm/plugin modes; malformed JSON or invalid decoded action-array shape produces a visible diagnostic rather than crashing the list. Drafting displays saved override or **Auto-detect**, not a live resolved destination. Its flow list links to existing editors, with disabled status. Preserve template CRUD/infill/delete patterns. Draft step UI shows destination text and a link to `/templates#draft-destination` for every mode. Do not intercept or mutate step serialization to add a folder field.

### C4: References and safe category-to-flow prefill

Reference discovery uses JSON `conditions` with `kind=category` (case-insensitive category comparison, same as engine), and heuristic `category` field; deduplicate a flow with repeated category conditions. Links use existing `/flows/<id>/edit` and classifier management/dataset paths. Include disabled status; do not call all listed references “active”. Render names with Jinja escaping and safe JS text nodes.

`GET /flows/new?category=<urlencoded-name>` is the only new flow-creation entry contract. Resolve the query to an exact configured stored category or, when unambiguous, a case-insensitive configured match. Unknown/ambiguous/map-only category: flash `err`, redirect 303 to Categories & filing, no writes. Folder is looked up server-side; never trust a `folder`, `enabled`, `steps_json`, or template query parameter for this entry.

Valid seed:
- name: `File <category>` when mapped, otherwise `<category> automation`.
- match_mode `all`.
- conditions `[{'kind':'category','value': canonical_name,'min_confidence':0.0}]`; do not add a new default confidence policy.
- steps `[{'type':'move','folder': saved_folder}]` only if saved mapping value is nonblank; otherwise `[]` and a visible **Add a step before saving** instruction. Never manufacture a move to INBOX/current folder.
- enabled `False` for this seeded draft only; ordinary unseeded new-flow behavior stays unchanged.
- helper `_flow_edit_context` supplies the context just as for existing editor paths. POST uses `_flow_from_form` unchanged and ignores seed query parameters in favor of submitted fields.

Show explanatory banner: successful category-flow processing takes precedence over default filing, including preview-mode flows and flows without move steps; leaving it disabled preserves present routing. Global `flows_apply` is independent of entity Enabled. The owner reviews and may explicitly enable before Save. Saving this form must not remove its default mapping. Cancel and repeated GET leave flow count, settings, logs of mutations, and mail unchanged.

New seed JSON must go through safe Jinja JSON serialization, including steps/categories/template metadata, rather than interpolating raw `json.dumps(... )|safe` into executable script. Handle names/folders containing quotes, backslashes, `<`, `&`, `</script>` safely. Keep `window.mtFlowFill`/`window.mtCtxState` and assistant editor path gates intact. Existing validation error renders must retain entered fields, Enabled value and local navigation.

### C5: Overview and assistant explanation

Overview is an explanatory UI, not a unified reorderable pipeline. Explain two stages:
1. **Mailbox scan:** rules first; remaining eligible mail may match non-category flows (text/topic). Matching guard rules protect mail; topic filters use embeddings.
2. **After classification:** user correction/fast-path/LLM supplies a verdict; eligible category-bearing flows are evaluated in order; default filing is fallback when enabled and no flow has been recorded as handling the message. Kept/already-filed/guarded mail is protected by existing checks.

Explicit caveats: preview is not the same as Disabled; a preview category flow can still suppress default filing; a flow action failure may leave fallback eligible under current engine behavior; blank default destination means no fallback move, not a global guard. Off `llm_apply` does not prevent a live flow from moving mail. Suggestion maps exist even when default filing is off. Do not call every category verdict an LLM result.

Read state from saved settings and lists, no live model or mailbox probes. Cards show honest zero/empty states and links to the corresponding section. Controls displays read-only default-filing state and link; Overview links to Simulator (`/simulate`) for Test on a message and to Learning for classifier review. Existing preview/simulator reporting is unchanged.

`engine.assistant_page_context(path, live_state='')` keeps its four-value return. New pages have nonsecret descriptive blocks and keys `page:automation`, `page:automation/categories`, `page:automation/controls`. `/templates` description now mentions Drafting and the shared destination; `/settings` points category/filing/drafting/automation controls to Automation and describes actual retained system cards. Preserve message/rule/flow/template keys and `fill_flow` behavior. No new mutation tool.

## Implementation Work Packages

### WP1: Lossless presentation and category persistence foundations
- Purpose: implement C1 section classification, C2 row handling and atomic conflict-safe category save, C3/C4 reference helpers without introducing routes yet.
- Depends on: none. Wave 1. Own shared files exclusively until tests pass.
- Owned paths: `ux.py`, `store.py`, `tests/ux_e2e.py` (pure-helper/store tests only in this package). No generated-source exceptions.
- Execution contract: Add the proposed helpers from C1–C4 inside existing shipped modules; no schema/default alteration or engine dependency in `ux.py`. Reuse `WorkbenchTests` temporary DB. AC3/AC4/AC8 → TEST1 helper/store tests. Runtime: shared repo venv with installed requirements, no servers or live credentials. Reference anchors: `ux.rule_diagnostics:5`, `store.get_setting:537`, `store.set_setting:548`, `store.list_flows:622`, `store.list_heuristics:697`. Stop/report if malformed persisted settings have an incompatible top-level type: expose an actionable error, do not coerce/drop data. Report helper signatures, test count/command, transaction/conflict behavior and any unsupported malformed fixtures. Do not touch route/templates to make tests pass.
- Implementation checklist:
  - [ ] Implement exact section-path classification and its positive/negative route table tests.
  - [ ] Build configured plus map-only rows, stable ordering, explicit map presence, bounded references and malformed-JSON diagnostics.
  - [ ] Parse the indexed scoped form per C2; preserve original metadata from server state; provide deterministic actionable `ValueError` messages.
  - [ ] Implement canonical snapshot version and two-key atomic `save_category_settings`; rollback on mismatch/error.
  - [ ] Implement draft-producing flow discovery once per flow and expose diagnostics without fabricating references.
- Edge cases:
  - [ ] Test missing/explicit-blank mappings, map-only keys, disabled/duplicate references, legacy category names and duplicates, blank vocabulary, duplicate new entries, blank spare rows, malformed payloads and unchanged-save exact equality.
  - [ ] Test stale snapshot caused by a legacy settings update; inject a failure after first upsert and prove both keys roll back.
  - [ ] Test malformed flow JSON blocks removal, not all read-only rendering; removal never mutates historical message rows.
- Tests/checks: TEST1 and `git diff --check`.
- Done when: pure helper and store tests pass, no schema/default changes, exact round-trip and all-or-nothing save are proven.

### WP2: Build the workspace and relocate canonical controls
- Purpose: deliver C1/C2/C3/C5 navigation, overview, category editor, controls, drafting destination and Settings link landmarks.
- Depends on: WP1. Wave 2; serialize `app.py`, `ux.py`, `tests/ux_e2e.py` ownership after WP1.
- Owned paths: `app.py`, `static/ux.js`, `ux.py` (presentation helpers only if required), `tests/ux_e2e.py` (workspace/forms/compatibility tests).
- Execution contract: Use existing string-template/render architecture and proposed GET/POST routes in C1; retain canonical rule/flow/template URLs. AC1/AC2/AC3/AC4/AC5/AC8 → TEST1/TEST2/TEST4 plus WP4 browser verification. No new JS package, engine edit, live API or schema. Reference anchors: `render:2160`, `BASE_TMPL:1031`, `MORE_TMPL:2195`, `SETTINGS_TMPL:8700–9062`, `settings:9303`, `static/ux.js:98`. Shared styles and route-aware header go in base shell, not a Settings-only block. Stop/report if preserving existing page navigation or forms requires changing a POST contract outside C1–C3; do not broaden scope. Report route/status matrix, each form's owned keys, conflict/error UX and focused checks. Do not run live toggles during development.
- Implementation checklist:
  - [ ] Add shared workspace header/local navigation rendered once and selected via C1 helper; replace Rules/Flows/Templates primary shell and More entries with Automation while preserving neighboring/plugin entries.
  - [ ] Update `VTPOS`, provide scrollable accessible local links and shared structured-row styles; bump first-party UX script query version from `v=1` to `v=2` to avoid cached old behavior.
  - [ ] Implement Overview with current counts/modes, two-phase explanation, exception caveats and Simulator/Learning links.
  - [ ] Implement category editor route, row inputs/spare rows/add-row enhancement, reference links, lossless parsing/save, 422 validation render, 409 conflict render and 303 success.
  - [ ] Give `llm_apply` its separate canonical form on Categories; implement false→true consequence confirmation without double submission or auto-saving map fields.
  - [ ] Implement Controls with exact C3 field groups and scopes; retain current defaults/clamps and single-field ownership.
  - [ ] Extend `/templates` with Draft destination and draft-producing flow cards; preserve list actions and infill hints; change list title to Drafting.
  - [ ] Replace only moved Settings controls with landmark/link cards. Split out Reply detection and keep its existing fields/parsing; preserve Settings General/Mail/AI endpoints/search/permissions.
  - [ ] Keep old `/settings` behavior transports and Settings hash selection; add the missing Filing & drafts anchor mapping.
- Edge cases:
  - [ ] No rules/flows/templates; map-only entries; malformed flows; saved unknown override; server unavailable on POST; canceled auto-filing enable; unsaved category edits while using separate switch; nested/local nav on validation pages.
  - [ ] Preserve old `/rules?tested=1`, `/flows?test=<id>`, template redirects and root/mobile links. Settings external destination links must not be swallowed by its hash-navigation handler.
- Tests/checks: TEST1, TEST2, TEST4; add read-only GET assertions by patching mail/model access to fail and comparing relevant DB state before/after.
- Done when: canonical editors have one consistent workspace shell, moved controls have one editor each, compatibility and scoped-save tests pass, no unseen state activation.

### WP3: Connect category flows, draft-step defaults and assistant context
- Purpose: implement C4 seed flow and contextual explanations/cross-links; update assistant's UI model without changing runtime behavior.
- Depends on: WP2. Wave 3; serialize shared files.
- Owned paths: `app.py` (flow editor/new GET only plus contextual links), `engine.py` (`assistant_page_context` presentation branches only), `tests/ux_e2e.py` (seed/context tests), `tests/mock_e2e.py` (additive T34/T42/T58 assertions only if needed).
- Execution contract: Keep `/flows/new` and all existing editor POSTs/path gates; seeded GET is disabled and side-effect free. AC6/AC7/AC8/AC9 → TEST1/TEST2/TEST3 and TEST5 browser flow. Runtime shared venv, mock fixtures. Reference anchors: `_flow_edit_context:5798`, `flow_new:5833`, `fieldsFor` draft modes around `5400–5464`, `assistant_page_context:3998`, fill-flow path gate `engine.py:5640`; regression T34/T42/T58. Any change to `_process_folder`, `classify_and_store`, `_process_flow`, draft append, `fill_flow` permissions, or settings defaults is BLOCKED/out of scope: report rather than make it. Report seed form schema, escaping cases, cancel/POST behavior, touched engine ranges, and checks. No automatic map-to-flow conversion.
- Implementation checklist:
  - [ ] Link configured category rows to seeded new-flow GET, and show existing consumer edit links with disabled state.
  - [ ] Resolve category canonically from saved settings; lookup folder server-side; prepare C4 disabled initial form; ignore untrusted additional seed parameters.
  - [ ] Show seed precedence banner and empty-action instruction where map value is blank; use existing POST validator for Save.
  - [ ] Serialize new query-derived form data safely; update affected flow script JSON insertion points so all seed-derived values cannot terminate a script element.
  - [ ] Add shared Draft destination text/link to all four draft modes; add category-management link next to the category filter controls without mutating steps/conditions.
  - [ ] Update assistant page-context descriptions for new routes, Drafting and Settings; preserve existing entity context IDs/editor form tools.
  - [ ] Add regression coverage without weakening existing assertions.
- Edge cases:
  - [ ] Names/folders with quotes, backslashes, HTML and `</script>`; invalid/case-ambiguous/map-only seed; empty destination; invalid POST retaining disabled/edited state; repeated GET/Cancel; user explicitly enables entity but globals stay unchanged.
  - [ ] Disabled flow cannot suppress current default filing merely by opening/saving its seed; a saved enabled flow still follows pre-existing dry-run/guard/fallback behavior.
- Tests/checks: TEST1, TEST2, TEST3, TEST4. Test untouched seed GET with patched `MailClient`/LLM and settings/list/log snapshots. Test saved disabled seed uses expected condition/action JSON and preserves mapping/global booleans.
- Done when: seed preview/save/cancel contract and injection tests pass; assistant names the real UI; engine diff contains presentation-context changes only.

### WP4: Close regression, documentation and browser evidence
- Purpose: integrated acceptance verification, current feature documentation and inspectable QA evidence.
- Depends on: WP1–WP3 integrated. Wave 4. Freeze source before final gates. This package may add tests and fix acceptance bugs only in the explicit owned source paths below; any substantive contract change returns to the owning package for approval/revalidation.
- Owned paths: `tests/ux_e2e.py`, `tests/mock_e2e.py`; bug-fix-only `app.py`, `static/ux.js`, `ux.py`, `store.py`, `engine.py` (same presentation-only restriction); `docs/features.md`, `docs/flows-fuzzy-classifier.md`, `docs/flows-builder-v2.md`, `docs/ux-demo.md`, new `qa/unified-automation.md`, optional browser-evidence images under new `qa/unified-automation/` only. Evidence images are approved QA outputs, not generated runtime source; no Docker/config/schema/secret changes.
- Execution contract: AC1–AC9 → complete TEST1–TEST6. Record observed commands/counts and browser evidence, not planned successes. Use isolated sandbox port 8102; never take over 8097/8101 or enable live filing. Runtime: venv, Node, Docker for browser demo and agent-browser skill/tooling; check port/container/resource availability first. Reference anchors: `AGENTS.md:58–77,90–115`, `docs/ui-conventions.md`, `docs/spa-turbo.md`, `docs/ux-demo.md`, T64 existing subprocess integration. Stop/report environment blockers, failed engine invariants, or unowned file fixes; do not weaken tests or claim READY-to-merge without browser evidence. Report AC matrix, command outputs/counts, screenshots/evidence references, cleanup status, review findings and any remaining blockers. No production deploy/merge/push is part of this package without explicit implementation-stage authorization.
- Implementation checklist:
  - [ ] Add missing route, scoped-save, lossless/no-op, stale-form, invalid-form, seed-escaping and safety regressions to `WorkbenchTests`; ensure T64 still executes them.
  - [ ] Keep existing mock suite assertions for guards, flow dedupe/dry-run, classification filing, draft save, page context and UI conventions; extend rather than replace coverage.
  - [ ] Update current user instructions to Automation destinations, fallback precedence and shared drafting preference. Keep historical claims dated; add follow-up notes rather than rewriting original design history.
  - [ ] Update `docs/ux-demo.md` walkthrough from old Settings-only filing to the new workspace and mention legacy anchors still lead to links.
  - [ ] Execute TEST1–TEST6; use real-browser desktop and 393px, keyboard, Turbo back/forward and canceled toggle checks.
  - [ ] Save QA report with actual evidence and cleanup only the new sandbox resources; verify no production/default/data changes.
- Edge cases: empty lists, small screens, long category/folder/flow names, no-JS row saves, validation focus, double submit under Turbo, old anchors, unsaved editor state with drawer and cross-links.
- Done when: full merge suite reports zero failures and full/nonpartial run, browser matrix passes, code diff remains within boundary, QA report maps all ACs to evidence and re-review findings are closed.

## Runtime Flow
1. Owner opens Automation. Server reads settings and lists, computes display references/counts and renders Overview plus shared local links. No mail connection, LLM probe, worker job or mutation is started.
2. Owner navigates ordinary links to Rules/Flows/Drafting or new Categories/Controls pages. Turbo swaps server-rendered body; singleton delegates remain idempotent. Existing assistant drawer/session/editor path identity remains intact.
3. Category/map form carries a version of just those two settings and row metadata. Server compares snapshot, validates all rows/reference removals, then atomically compares and saves both values. Concurrent editing yields 409/no writes; validation yields 422/no writes; success redirects 303 with flash. No automatic retry or hidden repair.
4. Each control/draft-destination form posts only its own keys through established Settings parsing and redirects to its fixed local workspace destination. Opt-in toggle is separate from the category form and visibly confirms false→true in the UI. Existing worker observes settings on its normal future reads; saving preferences does not immediately process mail.
5. Create flow for a category performs read-only GET. Existing editor receives a disabled unsaved draft. Cancel does nothing. Save follows existing creation POST and list redirect, with the category map/global modes left intact.
6. Actual mail execution is unchanged: scan rules/non-category flows; verdict classification; guarded/kept/already-filed checks; ordered category-flow processing; optional default-folder move when still eligible. Current flow exception/fallback and dry-run semantics remain, and explanations accurately describe them.
7. Draft creation continues to use current shared override/special-use/fallback logic, closes connections and records events as before. Workspace only edits the override and displays links; no draft is sent by this change.
8. Rollout requires only normal image rebuild after merge/authorization. No migration/backfill. Rollback to prior code reads the same settings types; new stored flows are ordinary disabled flows compatible with old code. Existing keys survive untouched by navigation. Preserve a clean deployment tree; this planning task does not authorize deployment.

## Validation Plan

Commands run in the implementation worktree. If `.venv` is not exposed there, use `/home/xrim/mail-triage/.venv/bin/python` for the identical commands rather than creating/installing dependencies ad hoc. Shared `ragmodels` access must follow repo worktree rules. No tests were executed during planning.

### TEST1: Focused isolated helper/route/contract regressions
- Command: `.venv/bin/python tests/ux_e2e.py`.
- Targets: `WorkbenchTests` additions from WP1–WP3, temporary DB only; patch mail/model entrypoints to raise for workspace GET/seed/scoped-save tests.
- Expected: unittest exits 0 and every old/new test passes. Cases include lossless map/vocabulary round trip, legacy mappings, rollback injection, stale snapshot, removal references, GET nonmutation, old settings transports/anchors, exact control ownership, seed disabled/escaped/cancel/save and assistant descriptions.
- Covers: AC1–AC9 at contract level.
- Pass/fail rule: zero errors/failures; stale/invalid route responses exactly match C1/C2 and DB snapshots prove no accidental writes.

### TEST2: Focused repository domains
- Command: `.venv/bin/python tests/mock_e2e.py --only core,ui,assistant`.
- Targets: T26, T28, T33, T34, T42, T58, T64 plus automatically included prerequisites.
- Expected: exit 0, no failed checks. Partial label is expected here only, not final merge gate.
- Covers: AC1/AC2/AC4–AC9; existing guards, dry-run, draft, editor/context and UI patterns.
- Pass/fail rule: no regression or deleted/weakened assertion. Do not treat focused run as full-suite proof.

### TEST3: Full integration / merge gate
- Command: `.venv/bin/python tests/mock_e2e.py --all`.
- Expected: exit 0, zero failures, final summary ALL GREEN with actual check count recorded; all domains run and no `[partial run]` acceptance.
- Covers: AC8 plus cross-system compatibility for AC1–AC9.
- Pass/fail rule: mandatory after final source freeze and before code commit/merge per AGENTS. Rerun after any later acceptance fix.

### TEST4: Static and scope checks
- Commands: `node --check static/ux.js`; `git diff --check`; at final committed handoff `git diff master...HEAD --check` and review `git diff master...HEAD -- engine.py store.py`.
- Expected: syntax/whitespace commands exit 0. Inspect inline flow JavaScript in browser too; Node checking the external script does not validate string-template JS. Store diff has no DDL/default changes; engine diff only modifies assistant page-context text/branches.
- Covers: AC2/AC3/AC7/AC8/AC9.
- Pass/fail rule: no syntax/whitespace error, no unauthorized operational change, no new module/config dependency.

### TEST5: Isolated real-browser matrix
- Preflight: verify 8102 and container `mail-triage-automation-qa` are free (`docker ps` and a loopback health request are sufficient; if occupied, stop/report instead of taking over). Load `agent-browser` skill for implementation-stage browser automation.
- Commands, after checking repository directory and Docker availability:
  1. `docker build -f Dockerfile.demo -t mail-triage-automation-qa:local .`
  2. `docker run -d --name mail-triage-automation-qa -p 127.0.0.1:8102:8097 --tmpfs /data:rw,size=256m mail-triage-automation-qa:local`
  3. Poll `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8102/healthz` until 200 with bounded startup wait; fail/report if unavailable after two minutes.
  4. Open `http://127.0.0.1:8102/automation` in real browser; desktop 1440px and phone 393px.
  5. After evidence capture: `docker stop mail-triage-automation-qa` then `docker rm mail-triage-automation-qa`. Do not remove other containers/images/volumes.
- Checks: single Automation entry on desktop/More; every list/editor local link/active state; category field layout/add/remove/new rows; map-only preservation; referenced removal error focus; conflict in two tabs; destination auto-detect/override; draft-step links in fixed/template/LLM/plugin modes; seeded disabled flow versus blank mapping; no-XSS query fixture; scoped save/cancel false→true toggle; old `/settings#sort-filing`, `#sorting`, `#sort-rules`, `#ai-classify`, `#ai-classifiers` navigation; unrelated old hash like `#ai-perms` unchanged; Turbo navigate/back/forward and drawer; no duplicated handlers/submits/console errors; keyboard navigation/focus and 44px targets; page no horizontal overflow except intended local nav; form save with JS disabled for existing/spare rows.
- Expected: all controls usable and located correctly, saved state matches requested key group, canceled enable remains off, no production interaction. Demo already uses mock endpoints/own data; do not inject production env or volume mounts.
- Covers: AC1/AC2/AC4–AC9.
- Pass/fail rule: all manual checks pass at both sizes with evidence. Existing live switches stay off in sandbox except explicit mock-only targeted tests; restore fixture state afterwards. Screenshots may be placed under `qa/` with feature-specific names by implementer, not included in this planner's artifact writes.

### TEST6: Traceability/documentation/review closeout
- Check: review `qa/unified-automation.md` AC table and updated docs against actual UI; verify settings before/after unchanged navigation tests and source ownership; record independent review/fix closeout required by risk recommendation.
- Expected: each AC has observed automated/browser evidence or clearly marked blocker; no stale instructions saying Filing & drafts editor or flow live mode is in Settings.
- Covers: AC1–AC9.
- Pass/fail rule: no unverified acceptance claim, no production toggle/deploy implied, all review findings resolved or handoff marked blocked.

## Acceptance Criteria
- [ ] **AC1:** Desktop and mobile More expose one primary Automation destination, and all six sections plus rule/flow/template editors have exactly one consistent, correctly selected local navigation. Existing list/editor/CRUD URLs and query-driven test banners work. TEST1/2/5.
- [ ] **AC2:** Old Settings anchored links still show meaningful destination cards, unchanged system forms remain usable and old scoped `/settings` POST transport remains compatible. TEST1/2/5.
- [ ] **AC3:** Unchanged structured save preserves category order/names, map-only entries, absent/explicit-blank map distinction and folder spelling; category/map write is atomic, stale saves return 409 without writes, invalid saves return 422 without writes. TEST1/4.
- [ ] **AC4:** Each moved key has one canonical editable form; category/map/draft saves cannot change live switches or unrelated fields; enable-default-filing cancel leaves off. All initial/default/global values stay unchanged unless the owner explicitly submits that control. TEST1/2/5.
- [ ] **AC5:** Drafting provides existing template functionality, destination override and draft-producing flows; blank destination remains auto-detect and every draft-step mode links to it without changing action serialization. TEST1/2/5.
- [ ] **AC6:** Category references identify stored category flows/fast-path classifiers without duplicate or hidden-disabled counts; removal of referenced entries fails visibly and historical labels/artifacts are not rewritten. TEST1/5.
- [ ] **AC7:** Category prefill GET is disabled, unsaved, safely escaped and server-derived; invalid seeds do not mutate anything; mapped seed has one move step, blank mapping has none; Save preserves map/global switches and Cancel saves nothing. TEST1/2/4/5.
- [ ] **AC8:** Explanations and regressions preserve guards/keep/first-match/two-phase/preview/fallback semantics, no mail deletion or sending change, no schema/default/runtime engine change, no model/mail probes on workspace GET. Full suite passes after freeze. TEST1/2/3/4/5.
- [ ] **AC9:** Assistant describes actual new pages while existing editor fill tools still work; docs/QA accurately name destinations; desktop/393px, keyboard, validation focus, Turbo/back and no-JS scoped-save checks have recorded passing evidence. TEST1/2/4/5/6.

## Open Questions
- None blocking for this first-pass consolidation. The choices below are deliberately bounded implementation defaults; do not broaden them silently.
- Nonblocking future design: full specialist/plugin category usage analysis, explicit category renames/migrations, unified cross-rule/flow reordering, richer category descriptions and an inline all-automation tester. These require separate plans.

## Assumptions
- The user's request to expand the suggestion authorizes planning the recommended first pass, including the category-to-flow convenience action and reference links; it does not authorize implementation or enabling live actions.
- Retaining canonical `/rules`, `/flows`, `/templates` paths is preferable to moving APIs merely to make URLs look unified; the shared shell/navigation constitutes the single roof.
- Existing category names are stable identifiers in v1; add/remove is supported with bounded consumer checks, but rename/cascade is not. This avoids inventing a destructive category migration.
- Legacy map-only and odd/duplicate stored category values may exist. No-op preservation takes precedence over normalization. Incompatible top-level settings data must be reported, not repaired automatically.
- Optimistic conflict detection covers vocabulary/map only; other Settings card updates keep established scoped last-write-wins behavior. There is no new global lock across flow/classifier/plugin editors.
- Existing no-built-in-auth deployment boundary remains; route additions have the same access posture as existing Settings. No new authentication, CSRF framework or policy is introduced in this feature.
- The complete implementation fits current shipped modules; if a worker proposes a new runtime module, stop for ownership/Dockerfile-plan revision rather than silently adding it.

## Out Of Scope
- Engine refactor, collapsing rules and flows into one stored type, changing matching/ordering/confidence thresholds, automatic map→flow conversion, setting a global master switch, learning promotion, plugin permission changes.
- Schema migrations, category IDs/descriptions, historical label rewrites, changing defaults, auto-enabling filing, touching account/OAuth identity configuration or generated proxy config.
- New preview engine, live provider folder discovery on workspace GET, redesigning Simulator/Learning/assistant tools, frontend framework or vendored Turbo changes.
- Production operations, deployment, live mail actions, branch/worktree/commit/PR creation during planning. Implementation-stage operations require the appropriate explicit authorization and repository workflow.
