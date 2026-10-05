# UX demo — branch `ux-demo`

Separate sandbox at **http://127.0.0.1:8101** on this host. The production app is
still on 8097. Demo image: `mail-triage-ux:demo`; container: `mail-triage-ux-demo`;
Compose project: `mail-triage-ux`. No production database, `.env`, account or token
directory is mounted into it. It has its own Compose network and named data volume.

**Tailscale URL:** https://gpu-vm1.bigscale-snapper.ts.net:8101/

Available only within the tailnet, through a persistent Tailscale Serve HTTPS
proxy. The container stays bound to loopback. Enable/disable this demo route:

```bash
sudo tailscale serve --bg --https=8101 http://127.0.0.1:8101
sudo tailscale serve --https=8101 off
```

Existing Serve routes, including production 8097 and OAuth callbacks, are preserved.

## Start / rebuild

From `/home/xrim/mail-triage-ux`:

```bash
docker compose -p mail-triage-ux -f docker-compose.demo.yml up -d --build
curl http://127.0.0.1:8101/healthz
```

The port is loopback-only. Use your existing reverse proxy or an SSH tunnel for
another device: `ssh -L 8101:127.0.0.1:8101 <this-host>` then open
`http://127.0.0.1:8101` on your computer.

## What to try

1. **Dashboard:** attention first with Rules / LLM / Auto-filing status chips always
   visible, recent mail next, Undo beside a seeded filing, statistics and system
   health collapsed. One pending approval is seeded.
2. **Messages:** search “Invoice”, combine sender/category/folder/date filters,
   switch status tabs, and move between messages without losing search context.
3. **Message 1:** Draft reply is reachable immediately on desktop and phone.
   Open the composer, generate an editable draft, change it and Save to Drafts.
   Quoted history and technical details start collapsed.
4. **Corrections:** choose another category; clear/re-enable Needs reply, repeat,
   and reclassify. Human corrections persist and enter the learning dataset.
5. **Snooze:** pick a duration, then Wake now. Inspect the audit disclosure.
6. **Rules:** the invoice duplicate is identified. New rules start with one
   condition; Add condition reveals another. Test an unsaved “Invoice” rule:
   the existing invoice rule wins first, even if your draft's conditions match.
7. **Flows:** edit Lunch drafts, change the fixed draft text without saving,
   choose a lunch email under Test this draft, and Run preview. The generated
   report uses the unsaved text, with no mailbox or automation changes.
8. **Settings:** five sections; deep links such as `#ai-perms` and `#ai-search`
   still work. Advanced endpoint settings are disclosed. Saves remain scoped.
9. **Learning:** source/label agreement/next actions are explicit. The category
   learner is watching; live-routing prerequisites are shown before promotion.
10. **Plugins:** Invoice finder → Use in Assistant opens an editable request.
    Remove the context chip before sending to detach the previous page.
11. **Reply status:** two seeded reimbursement threads exercise the sent-reply
    state machine — one clears to **Answered** (mock AI confirms the reply is
    sufficient), the other stays **Partial reply** (holding acknowledgement).
    "Check sent replies" queues a worker pass; marking Needs reply reopens.

## Demo behavior

- 120 synthetic messages plus two reply conversations in a mock `Sent Items`
  folder; no real mail. Mock IMAP accepts filing/Undo/draft actions. Mock AI and
  TEI are in-process implementations reused from the suite.
- AI responses are deterministic fixtures, so this is a UI/workflow demo, not a
  model-quality evaluation. Plugin requests still obey the configured grants.
- Rules/flows begin in dry-run; category auto-filing starts off. Explicit manual
  moves operate on the mock mailbox. Send/Trash retain their usual defaults.
- The database persists through container recreation. The mock mailbox is
  rebuilt at stored message locations on boot; newly appended mock drafts remain
  in memory until indexed. Endpoint ports change on restart and are refreshed.
- Generated viewer/automation reports expire after 30 minutes or an app restart.

## Stop / reset

```bash
# Stop; keep demo data
docker compose -p mail-triage-ux -f docker-compose.demo.yml down

# Reset this synthetic sandbox only; next start recreates all fixtures
docker compose -p mail-triage-ux -f docker-compose.demo.yml down --volumes
docker compose -p mail-triage-ux -f docker-compose.demo.yml up -d
```

## Validation

```bash
.venv/bin/python tests/mock_e2e.py --all
.venv/bin/python tests/ux_e2e.py
node --check static/ux.js
git diff --check
```

The workbench regressions are also invoked by the full mock suite's T64 section.
Browser evidence and final check counts are recorded in `qa/ux-demo.md`.
