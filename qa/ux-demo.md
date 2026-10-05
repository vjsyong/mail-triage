# UX demo acceptance evidence

Validated 2026-10-05 in the `ux-demo` worktree, against the separately built
`mail-triage-ux-demo` container on `127.0.0.1:8101`.

## Automated gates

- `.venv/bin/python tests/mock_e2e.py --all`: **944 passed, 0 failed**.
- T64 invokes `tests/ux_e2e.py`: **17 behavioral cases passed**. Covers combined
  filters/counts/neighbors, literal SQL wildcards and injection strings, date
  boundaries, correction reversal and persistence, unsaved previews, guard/list
  precedence, fixed drafts, disabled edit position, validation/escaping, duplicate
  diagnostics, Turbo draft redirect, model scaler roundtrip, and empty mock folders.
- Python compilation, `node --check static/ux.js`, and `git diff --check`: passed.
- Docker build/recreation and explicit restart with the played-in database: passed.
  Health endpoint returned 200; container `healthy`, automatic restart count 0.
- Production checkout remains clean on `master`; its health endpoint returned 200.

## Browser flows

Browser automation used real Chromium at **1440×1000** and **393×852**.

| Route / action | Observed result |
|---|---|
| Messages, expand assistant | Subject column remains 485px; the assistant overlays rather than shrinking the workbench at 1440px. |
| Search “Invoice” | 20 matching messages; viewer links preserve the query. Count/list/navigation agreement is also regression-tested. |
| Message 1, Draft reply → generate → edit → Save to Drafts | Editable mock draft displayed after a 303 redirect to the canonical viewer; edited draft saved into mock Drafts with a success flash. |
| Message 1 on phone | Header action bar starts at ~323px; quoted history starts closed; no horizontal overflow. |
| Correct category, clear/re-enable Needs reply | Human correction and success feedback displayed; repeated reversals are covered by regressions and survive reclassification. |
| Snooze 1 day → Wake now | Snoozed-until flash, Wake now control, then “Back in your lists.” |
| Dashboard Undo | “Moved back to INBOX — automation will not re-file it.” |
| Rules | Duplicate invoice organizer identifies earlier rule #2. New rule has one visible condition and an Add condition control. |
| Unsaved equipment rule preview | Draft conditions match; existing Invoice organizer wins by list order. Proposed draft actions and actual winning actions are distinguished. |
| Lunch flow, change fixed draft without saving → preview recent lunch email | Report includes the unsaved text; fixed draft and tag steps are previewed without persistence. |
| Change filter after preview | Explicit “Draft changed since this preview” warning appears. |
| Phone preview fields | Full-width styled fields (320px), document width 378px inside a 393px viewport. |
| Settings | Five destinations; General only visible by default (~1031px total page height on phone, including demo/header/footer). |
| Settings deep link `#ai-search` | Search section selected; endpoint card moved into it; advanced protocol options remain collapsed. |
| Settings → AI → Messages → browser Back → Search | Correct settings page restored, tabs still work, exactly one advanced status wrapper. |
| Learning | Training provenance and AI agreement labeled; routing prerequisite precedes promotion; history disclosed. |
| Invoice finder → Use in Assistant | Editable plugin request prefilled; nothing sent automatically; enabled plugin shortcuts present. |
| Remove context | Context chip hides, `mtCtxPath()` returns an empty string, page-state attachment is suppressed. |
| Console / runtime | No new browser runtime errors in the verified final flows. |

## Review closeout

- Correctness: search predicates are parameterized and shared by list/count/neighbor;
  timestamp ordering handles returning to an old correction; guards and rule-first
  precedence apply to previews; no temporary automation is written to the DB.
- Architecture: presentation helpers/report templates in `ux.py`; shared behavior
  loaded once from BASE via `static/ux.js`; shared CSS in BASE. Docker explicitly
  includes the new module. Existing engine/store seams are reused.
- Escaping: draft names, sample text and reports use Jinja auto-escaping; raw mail
  HTML is not inserted into the preview endpoint response.
- Browser-discovered fixes: Turbo form redirects, cached Settings/Back navigation,
  removable context state, styled preview inputs. Sandbox restart additionally
  exposed and fixed the mock server's empty-folder UID tail search.
- No schema migrations, production account changes, production data mounts or
  changes to automatic-filing defaults.

## Screenshots

Local screenshots live under `qa/shots/ux-demo/` (gitignored):
`messages-desktop.png`, `messages-assistant-overlay.png`, `dashboard-desktop.png`,
`rules-desktop.png`, `learning-desktop.png`, `settings-search-desktop.png`,
`message-phone.png`, `settings-phone.png`, `learning-phone.png`, and
`flow-preview-phone.png`.

## Follow-up: sent-reply reconciliation (reply state machine)

Adds `docs/reply-resolution.md` and `replies.py`: Sent folders are scanned
incrementally (special-use detection, drafts excluded, UIDVALIDITY-aware
checkpoints + 30-day initial backfill, 100 headers/pass), replies are matched by
normalized Message-ID across In-Reply-To/References with sender/recipient/
chronology corroboration (subject alone never clears), and only newly authored
text (quoted history stripped for plain and HTML) is assessed by the configured
classifier for sufficiency. A strict contract gates the verdict: boolean
sufficient, finite confidence, explicit unanswered-request list, and a clearing
verdict additionally requires no unanswered requests and confidence >= 0.90.
Attachment evidence (names/types only) is passed to the assessment; attachment
bodies are never included. Outcomes live in `msg_events` (`thread_headers`
evidence, `reply_state` transitions) — no schema migration.

Safety behavior, each test-enforced: partial/uncertain/low-confidence results
keep the flag; a `matched` event shows "Reply check pending" when the hourly
budget is exhausted (assessment resumes on a later pass); failures cool down 15
minutes and never mark the original errored; manual Needs reply reopens and
requires a newer sent reply; manual No reply needed stays authoritative; a
manual change or settings switch flipped during an assessment prevents the
automatic clear; new incoming follow-ups do not inherit an old resolution; a
reply outcome is not a negative training label (the dataset keeps the positive
requirement).

- T65 (`tests/reply_e2e.py`): **28 cases passed** — real MIME parsing against a
  read-only mock mailbox, plus one test exercising the real `LLMClient` request
  over the suite's mock HTTP endpoint.
- Full mock suite: **945 passed, 0 failed**.
- Demo container: `reconcile` returned `{'checked': 2, 'answered': 1}` over the
  seeded threads; test-client renders show **Answered** (msg 122, links to sent
  123) and **Partial reply** (msg 124, links to sent 125), and the sent message
  links back to the request it resolved.
- Browser over Tailscale at 1440px and 393px: Reply status card renders between
  the composer and the message body; no runtime errors. Screenshots:
  `reply-answered-desktop.png`, `reply-answered-phone.png`, `reply-partial-phone.png`.
- Settings: "Detect answered mail" switch in Classification, with Advanced
  sent-folder override and sending-alias fields (alias validation rejects
  malformed entries); behavior saves verified by the UI suite.

## Demo limits

The AI and semantic index endpoints are deterministic fixtures, not a model-quality
test. The database persists; generated report caches are process-local and expire.
For launch, access, reset and a play checklist, see `docs/ux-demo.md`.

## Follow-up: visible dashboard status chips

Moved the existing Rules, LLM, Auto-filing and Settings chips out of the collapsed
historical statistics section into the top attention card. Their state values and
styling are retained, without a duplicate strip.

- UI-domain mock suite: **434 passed, 0 failed** (`--only ui`, partial run),
  including all **17 workbench regression cases**.
- Rebuilt demo and verified over its Tailscale HTTPS URL at 1440px and 393px.
- All four chips are visible with statistics closed; on phone they sit at
  ~376–435px. No horizontal overflow or browser runtime errors.
- Screenshots: `dashboard-status-desktop.png`, `dashboard-status-phone.png`.
