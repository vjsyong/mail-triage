# QA — unified automation workspace (WP4 acceptance)

Integrated acceptance run for the `unified-automation` feature. Source tree:
`/home/xrim/mail-triage-wt/mail-triage-auto` at base
`da78d28375bdee5108f94eaa05166ba5cd63ccd8` plus the WP4 acceptance fixes in the
same commit as this file. The browser sandbox image was built from that working
tree; the image/container are removed after the run (rebuild with the commands
below). No production (`8097`) or prior demo (`8101`) container, volume, port or
data was touched.

## Artifacts

- Browser evidence: `qa/unified-automation/*.png` (desktop 1440px and phone 393px).
- Receipts (host scratch, not committed): `/tmp/opencode/wp4/`
  (`ux1.log`, `mock_focus.log`/`.exit`, `mock_all.log`/`.exit`, `build.log`,
  `build_receipt.log`, `no_js_save.py`, `docker_stop.log`, `docker_rm.log`,
  `docker_rmi.log`).

## Automated gates

| Gate | Command | Result |
|---|---|---|
| TEST1 | `.venv/bin/python tests/ux_e2e.py` | PASS — 59 tests, exit 0 |
| TEST2 | `.venv/bin/python tests/mock_e2e.py --only core,ui,assistant` | PASS — 720 passed, 0 failed, exit 0 |
| TEST3 | `.venv/bin/python tests/mock_e2e.py --all` | PASS — ALL PASS, 1002 passed, 0 failed, exit 0 (final frozen tree) |
| TEST4 | `node --check static/ux.js`; `git diff --check`; `git diff master...HEAD --check` | PASS — exit 0, no whitespace/syntax errors |
| TEST4 | `git diff master...HEAD -- store.py engine.py` review | `store.py` untouched; `engine.py` changes are assistant page-context text/branches only |
| TEST5 | Isolated real browser (below) | PASS — matrix below |
| TEST6 | This document + docs review | PASS — docs updated; no stale Settings-editor claims |

Mock gates were serialized with `flock -w 900 /tmp/opencode/mail-triage-e2e.lock`
(shared suite lock) so parallel agent worktrees never ran a shared server/resource
gate at once.

## Isolated browser sandbox (TEST5)

Preflight: port `8102` free; production `8097` and demo `8101` healthy (both `200`
before and after); container name `mail-triage-automation-qa` free.

```
docker build -f Dockerfile.demo -t mail-triage-automation-qa:local .            # exit 0
docker run -d --name mail-triage-automation-qa -p 127.0.0.1:8102:8097 \
    --tmpfs /data:rw,size=256m mail-triage-automation-qa:local                  # exit 0
# health 200 after ~20 s
# ... browser matrix via agent-browser (real Chrome, console + page-error capture) ...
docker stop mail-triage-automation-qa   # exit 0
docker rm  mail-triage-automation-qa    # exit 0
docker image rm mail-triage-automation-qa:local  # exit 0 (re-runnable from the tree)
```

No production `.env`, volume or account directory is mounted; the demo uses its own
mock mailbox/AI and `--tmpfs /data`. Live filing was never enabled; `llm_apply`
stayed `false` throughout (verified before/after). No mail action was performed.

### Browser findings (observed, not planned)

| Check | Result | Evidence |
|---|---|---|
| Single Automation entry (desktop sidebar + More); no separate Rules/Flows/Templates entries | PASS | `.ws-nav` = 6 tabs; sidebar `Automation*` active; More rows = Automation/Simulator/Learning/Accounts/Log/Settings/Plugins |
| Local nav active state on every list/editor (`/rules`,`/flows`,`/automation/categories`,`/templates`,`/automation/controls`,`/flows/new`,`/flows/1/edit`,`/rules/new`,`/templates/new`) | PASS | exactly one `aria-current` each; sidebar Automation active |
| Category rows: configured + map-only, add-row (JS), save, preservation | PASS | JS add → `original=""`, `configured="1"`, `mapping_present="0"`, no remove control; Save → 303, flash `Categories and filing saved.`, new row persisted, `LegacyPromo` map-only preserved |
| Referenced removal (flow + classifier) blocks and focuses error | PASS | 422; `#form-err` focused; "Cannot remove 'Action': still referenced by flow #2 … classifier #1 …" |
| Two-tab conflict | PASS | stale submit → 409, read-only (no Save, name readonly), reload link, server state unchanged |
| Default-filing opt-in cancel leaves off | PASS | submit handler calls `confirm(...)`; cancel resets checkbox and `llm_apply` stays `false`; no navigation |
| Scoped control save | PASS | Classification form → `llm_batch_per_cycle` 5→3 only; `classify_concurrency`, `llm_suggest`, `rules_apply`, `flows_apply`, `llm_apply` unchanged; flash `Classification saved.` |
| Rendered control/destination forms post to `/settings` | PASS | four Controls forms + filing + drafting forms have `action="/settings"` in the DOM |
| Drafting: destination + draft-producing flows + per-mode links | PASS | destination form action `/settings`; "Lunch drafts" listed; Draft destination link present for fixed/template/LLM/plugin step modes |
| Category seed: mapped / blank mapping / invalid / blank | PASS | `?category=Receipt` disabled + one move step; `?category=Action` no step + "Add a step before saving"; malicious/blank → 303 to Categories & filing |
| Seeded untrusted folder DOM | PASS | `window.__xss` undefined, no raw `<script>window.__xss` in DOM, `\u003c/script` escaped present |
| "Create flow" from Categories → seeded disabled editor | PASS | click lands on `/flows/new?category=Receipt`, disabled, banner, Manage categories link |
| Settings anchored deep links | PASS | `#sort-filing`/`#sorting`/`#sort-rules` → section sorting; `#ai-classify`/`#ai-classifiers`/`#ai-perms` → section ai; `#ai-search` → searchidx; cards link to `/automation/categories` and `/automation/controls` |
| Turbo Back/Forward + drawer, no duplicated handlers | PASS | click → categories, Back → /automation, Forward → categories; `#asb` drawer present; console/errors empty |
| Keyboard focus | PASS | Tab reaches nav links with visible `outline: solid 2px` |
| No-JS scoped save | PASS | plain server-side POST of the rendered `#cat-form` (urllib, no JS) → 303 + flash `Categories and filing saved.` |
| 393px layout | PASS | no page horizontal overflow on Overview/Categories/Controls/Drafting/More; `.ws-nav` scrolls internally; bottom tab bar present |

Console messages and page errors were empty after every interaction above.

### Screenshots

`desktop-automation-overview.png`, `desktop-more.png`, `desktop-categories.png`,
`desktop-categories-refs.png`, `desktop-categories-added.png`,
`desktop-categories-conflict.png`, `desktop-controls.png`, `desktop-drafting.png`,
`desktop-seeded-flow.png`, `desktop-settings-landing.png`,
`mobile393-automation.png`, `mobile393-categories.png`, `mobile393-controls.png`,
`mobile393-drafting.png`, `mobile393-more.png`.

## Acceptance criteria matrix

| AC | Statement | Automated | Browser |
|---|---|---|---|
| AC1 | One primary Automation destination; six local tabs with exactly one selected; existing URLs/query banners work | TEST1 (`test_automation_workspace_chrome_and_single_nav`, `test_vtpos_orders_workspace_sections`), TEST2 (T30 updated) | Single entry; active states on all lists/editors; Turbo nav |
| AC2 | Old Settings anchored links show destination cards; unchanged forms usable; old scoped POST transport compatible | TEST1 (`test_settings_landmarks_and_reply_detection`, `test_controls_forms_own_exact_keys`) | `#sort-*`/`#ai-*` land on sections; cards link to Automation; `/settings` POSTs still work |
| AC3 | No-op save preserves order/names/map-only/blank-vs-absent/spelling; atomic; 409/422 no writes | TEST1 (round-trip, conflict, duplicate, referenced removal, map-only) | Save preserved rows; two-tab 409 read-only; referenced 422 focused |
| AC4 | One canonical form per moved key; no cross-key writes; cancel leaves opt-in off; defaults unchanged | TEST1 (`test_category_save_does_not_touch_live_switches`, `test_controls_forms_own_exact_keys`) | Scoped classification save; opt-in cancel leaves off |
| AC5 | Drafting keeps templates + destination override + draft flows; every draft mode links destination | TEST1 (`test_drafting_page_destination_and_flow_cards`, `test_flow_editor_draft_modes_link_shared_destination`) | Destination form + draft flow listed; link in all four modes |
| AC6 | References identify stored flows/classifiers incl. disabled, no duplicates; removal fails visibly; no artifact rewrite | TEST1 (`test_category_rows_order_map_only_and_presence`, `test_removal_reference_protection_and_no_mutation`) | Ref chips → `/flows/2/edit`, `/classifiers/1/dataset`; removal error names both |
| AC7 | Seed GET disabled/unsaved/escaped/server-derived; invalid no mutation; mapped 1 step, blank none; Save preserves map/globals; Cancel saves nothing | TEST1 (`test_seed_flow_*`, `test_seed_flow_blank_category_redirects`, `test_seed_flow_escapes_untrusted_values`, `test_seed_flow_save_enables_without_global_or_map_change`) | Seed states + malicious DOM + blank-category 303 |
| AC8 | Guards/keep/first-match/two-phase/preview/fallback semantics preserved; no delete/send/schema/default/runtime change; no probe on GET | TEST1 (`test_workspace_get_is_read_only_and_probe_free`), TEST2, TEST3 full suite | No probe; modes unchanged; `store.py` untouched |
| AC9 | Assistant names real pages; editor fill tools intact; docs/QA accurate; desktop/393/keyboard/focus/Turbo/no-JS recorded | TEST1 (`test_assistant_context_new_automation_pages`) | Matrix above + screenshots |

## Deviations / limitations

- No substantive contract change was needed; the two inspected WP3 followups were
  fixed within acceptance scope (`flow_new` now treats a present-but-blank
  `category=` as an invalid seed → 303; the flow filter node links to Categories &
  filing without touching condition/step serialization).
- T30 in `tests/mock_e2e.py` was updated to pin the actual Automation href and the
  absence of separate Rules/Flows/Templates More hrefs, replacing the artificial
  title-case word list; the natural More subtitle was restored.
- `pointer:coarse` 44px switch targets are enforced by CSS but the desktop browser
  session does not emulate coarse pointer, so that specific media query was not
  exercised in-session; keyboard focus outlines were verified.
- The pre-existing `{{ cfg_json|safe }}` plugin-nav JSON island was left untouched
  (out of scope); the flow-editor script now uses `|tojson`.
- No production deploy/merge/push was performed; the sandbox container and image
  were removed and `8097`/`8101` verified healthy after cleanup.
