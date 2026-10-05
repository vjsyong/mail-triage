# AGENTS.md

Guidance for AI coding agents in this repo. `README.md` is the human front page; this
file is the agent front page. Depth lives in `docs/` (`docs/README.md` indexes the
design records and research briefs; read the relevant one before a significant change).

What this is: self-hosted triage for a single mailbox. Deterministic rules sort most
mail, a local LLM classifies the rest, and a learning loop compiles repeated LLM
reasoning into small auditable models. One container runs the Flask UI, the worker,
and an embedded OAuth mail proxy; the index and database live on this host.
Classification defaults to a local model; a cloud fallback, when configured in
`.env`, runs the same prompts and therefore sees message snippets. The app never
deletes mail.

## Feature workflow: one feature = one branch + one worktree

**Never develop a feature in the main checkout.** The main checkout stays on
`master` and is the source every deploy builds from. (`git worktree list` shows
existing ones.)

Every mail-triage worktree lives under a shared `mail-triage-wt/` directory beside
the main checkout, one subdirectory per feature.

```bash
# 1. start a feature (from the main checkout)
mkdir -p ../mail-triage-wt
git worktree add ../mail-triage-wt/mail-triage-<name> -b <name>

# 2. work there; tests need the shared venv, and ragmodels/ is gitignored
cd ../mail-triage-wt/mail-triage-<name>
ln -s <main-checkout>/ragmodels ragmodels
.venv/bin/python tests/mock_e2e.py                            # must be ALL GREEN

# 3. land it (from the main checkout) once the suite is green and work is committed
git merge <name>
git push origin master
rm -f ../mail-triage-wt/mail-triage-<name>/ragmodels    # untracked symlink blocks worktree removal
git worktree remove ../mail-triage-wt/mail-triage-<name>
git branch -d <name>
```

- Naming: branches short, lowercase, hyphenated (existing: `mail-intelligence`,
  `rag-lite-eval`). The worktree dir sits under `mail-triage-wt/` with a SHORT dir
  name that may abbreviate the branch (`~/mail-triage-wt/mail-triage-intel` runs
  `mail-intelligence`, `~/mail-triage-wt/mail-triage-rag` runs `rag-lite-eval`).
- CodeGraph: a tracked `githooks/post-checkout` builds a per-worktree index when a
  worktree is added. Enable it once per clone with `git config core.hooksPath githooks`.
- If `master` moved while you worked: merge `master` into your branch inside the
  worktree, resolve there, then merge back.
- Trivial fixes (typo, one-liner) may commit straight to `master`; anything with
  behaviour changes goes through the worktree flow.
- Push after every merge (`git push origin master`) so GitHub stays current. Normal
  push only; never force-push (`master` is append-only history).
- Deploy only after merging, only from the main checkout, only with a clean tree.

## Commands

- **Tests** (required before any code commit; docs-only changes may skip):
  `.venv/bin/python tests/mock_e2e.py` — full mock E2E (mock IMAP + mock LLM +
  mock TEI), ~70 s, ~730 checks. On a dirty tree it auto-selects the domains the
  changed files touch (`engine.py` -> core+assistant, template-only `app.py`
  hunks -> ui, `rag*.py` -> rag, `plugins/` -> plugins, ...); a clean tree runs
  everything. Sections are tagged `base/core/ui/assistant/rag/learning/plugins/
  proxy/bench`; `base` fixtures and each group's prerequisites always run.
  - `--all` forces the full suite; `--only core,rag` / `--skip ui` pick domains
    explicitly; `--list` shows every section's groups. Partial summaries say
    `[partial run]` and list skipped sections. Run `--all` before merging to
    master. Add checks for new behaviour; never weaken or delete one to make it
    pass.
- **Rebuild + deploy**:
  `docker compose build && docker compose create --force-recreate mail-triage && docker start mail-triage`
  then poll `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8097/healthz`
  until 200. There is no source mount: code edits need a rebuild to take effect.
- **Health**: `docker exec mail-triage python app.py --check`
- **UI**: http://127.0.0.1:8097 (bound to loopback; put it behind a reverse proxy
  or VPN for remote access). If you run the app live: keep restarts brief, and
  verify before declaring anything done.

## Repo map

- `app.py` - Flask routes + ALL page templates as string constants (`BASE_TMPL` is the
  shared shell; page templates like `DASH_TMPL` carry their own CSS).
- `engine.py` - scanning, rules, flows, classification, assistant agent.
- `store.py` - SQLite layer (`data/triage.db` is live state; never delete it).
- `rag.py` / `rag_lite.py` - search backends (lite = CPU ONNX, default).
- `learning.py`, `heuristics.py` - learning loop + fast-path classifiers.
- `proxy.py` - embedded email-oauth2-proxy manager.
- `tests/mock_e2e.py` - the suite; `docs/` - design records + research briefs.

## Conventions that bite

- UI behaviour follows `docs/ui-conventions.md`: entity deletes arm in place (never a
  confirm popup), on/off is always a `.px-sw` switch, feedback is flash (`ok`/`warn`/
  `err` only) on redirect or `toast()` in place.
- CSS shared by 2+ pages lives in `BASE_TMPL`; page-exclusive styles may ride in the
  page template's own `<style>` block (a page-scoped block never ships elsewhere, so
  cross-page reuse silently breaks). `BASE_TMPL` is a raw string: `\uXXXX` renders
  literally.
- The Dockerfile COPYs files explicitly; a new module needs a Dockerfile line or the
  container fails only at runtime.
- Never hand-edit `data/emailproxy/emailproxy.config` (generated from the DB).
- UI changes: verify in a real browser (phone 393px + desktop widths); the mock suite
  cannot see CSS/JS placement mistakes.
- Commits: descriptive single-line subjects, mention the suite count. Parallel agent
  sessions share this repo: stage ONLY the files you touched, never `git add -A`,
  never stage, revert, or rewrite another session's uncommitted work.

## Boundaries

- Always: green suite before code commits; rebuild to verify a deploy; preserve the
  never-delete-mail guarantee; keep LLM auto-filing opt-in.
- Ask first: turning on `llm_apply` or live rule actions that move real mail; schema
  changes; anything touching accounts/OAuth.
- Never: commit `.env` or secrets; hand-edit generated config; force-push or rewrite
  `master`; bypass guard rules; delete the owner's data as a shortcut.

---
*Written 2026-10-02 from the AGENTS.md standard (agents.md), GitHub's "lessons from
2,500 repositories", and aihero.dev's progressive-disclosure guide. Keep it small;
add depth to `docs/`, not here. (Claude Code: `CLAUDE.md` symlinks to this file.)*
