# Deployment

Notes for running Mail Triage for real, beyond the Quick start. The app is a
single container with no external services of its own: Flask UI, the mailbox
worker, the search indexer and the embedded OAuth proxy all live inside it.
State lives on disk under `data/` (SQLite) and `ragmodels/` (CPU models).

## Ports

| port | what | exposure |
|---|---|---|
| 8097 | the UI | **loopback by default** (`127.0.0.1:8097:8097`). Reach it remotely through a reverse proxy or VPN - never publish it raw. |
| 41810-41819 | OAuth callback ports (used by some account modes) | loopback; map externally only if your callback setup needs it (see below). |
| 1993 | the embedded email-oauth2-proxy's plain-IMAP listener | internal to the container (`127.0.0.1` inside); nothing to expose. |

The app has no built-in auth. Treat the port like a shell: give it TLS and an
auth layer, or keep it VPN-only.

## TLS and remote access

Two approaches that work well:

**Reverse proxy.** Terminate TLS and require authentication before forwarding to
the loopback port. TLS alone does not restrict access to the mailbox UI. Configure
authentication in your proxy or identity gateway; Mail Triage has no login layer.

For OAuth callbacks that must land on a public hostname (some providers require
an `https://` redirect URI that is not localhost), publish the callback port(s)
alongside and register the matching URL - the Accounts page shows the exact
redirect URI it expects and reports token status.

**VPN / overlay network** (Tailscale, WireGuard): leave the compose ports on
loopback and use the VPN's forwarding facility or an authenticated tunnel to
reach it. Joining a VPN alone does not expose a loopback-bound port. This
keeps the app off the public internet; OAuth can complete via the
paste-back box when the provider's page is loaded from the same device.

## Backups

Everything that matters is in two places:

- `data/triage.db` - messages, rules, flows, settings, learning tables, chat.
- `data/emailproxy/` - generated proxy config + encrypted OAuth token cache.

`ragmodels/` is a downloaded cache (re-derivable), and the search index inside
the database rebuilds with `--reindex`. A simple backup is:

```bash
docker compose stop mail-triage          # or: sqlite3 .backup while running
tar czf mail-triage-$(date +%F).tgz data/
docker compose start mail-triage
```

Never delete `data/`: the mail is still on the server, but rules, learning
state and tokens are not.

## Upgrades

```bash
git pull
docker compose build
docker compose create --force-recreate mail-triage && docker start mail-triage
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8097/healthz   # wait for 200
```

Schema migrations run automatically at boot. There is no source mount: code
edits always require a rebuild, and the image bakes the app.

## Resource notes

- **CPU/RAM**: the app is light compared with inference. The measured CPU
  embedder used about 1.9 GB RAM after load; bulk backfills can use much more.
  CPU count, thread settings, and corpus size affect indexing and query latency.
- **Disk**: reserve several GB for `ragmodels/` (the evaluated 0.6B fp32 ONNX
  embedding model alone is about 2.4 GB), plus the database and index. A local LLM
  server and its weights are separate. See [RAG evaluation](rag-lite-report.md).
- **GPU (optional)**: only for serving an LLM or the legacy GPU RAG backend
  (`embed/`). The app itself needs no GPU.
- **LLM concurrency**: classification is batched by the worker; a local vLLM
  server with `--max-num-seqs` well below its default is plenty for one mailbox.

## Monitoring

- `docker exec mail-triage python app.py --check` - read-only health JSON
  (IMAP connectivity, folders, unseen count).
- `docker exec mail-triage python app.py --doctor` - hardware + endpoints +
  setup advice.
- The dashboard system card and the Log page surface worker errors, LLM
  failures and proxy restarts; the container's `HEALTHCHECK` pings `/healthz`.
- `docker logs -f mail-triage` for raw output.

## Security posture

- Mail content stays on the machine. The remote endpoints you explicitly
  configure (an LLM API, a webhook plugin) see what you point at them at - the
  footer of the Settings page and the plugin pages say which.
- Rules and flows act live by default (dry-run toggle in Settings); LLM
  auto-filing starts off; guard rules can pin mail in place.
- The app does not permanently delete mail. The optional Trash capability moves
  messages to the server's Trash folder; the provider's own retention policy still
  applies. Sending and Trash are disabled by default. Actions are logged, but
  sending mail and external plugin effects are not reversible through Undo.

## Running without Docker

For development, create a Python 3.12 venv and install `requirements.txt`, then
run `DATA_DIR=/path/to/dev-data UI_HOST=127.0.0.1 python app.py`. Use a separate
development data directory. Docker is the documented deployment path and bundles
the OAuth proxy. Python dependencies have version ranges, so builds are not
fully locked; the mock suites validate the installed environment.
