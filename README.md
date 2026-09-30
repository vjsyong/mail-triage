# Mail Triage

Smart email management for the `seanyong@ust.hk` mailbox, running as a container on
gpu-vm1 and reading mail through the **email-oauth2-proxy** (which handles the OAuth
side). Most mail is sorted by simple rules; everything else gets classified by an LLM,
and it can draft replies from your templates straight into your Drafts folder.

Tailnet UI:  https://gpu-vm1.bigscale-snapper.ts.net:8097/

```
                    +-------------------+        plain IMAP       +----------------+
  inbox mail  --->  |  email-oauth2-    |  <------------------   |  mail-triage   |
  (O365 / HKUST)    |  proxy (:1993)    |                        |  container     |
                    |  holds the OAuth  |    OAuth 2.0 / TLS     |  :8097 UI      |
                    |  tokens           |  ===================>  |  worker loop   |
                    +-------------------+       to Microsoft     +--------+-------+
                                                                          |
                              +-------------------------------------------+---------+
                              |                                                     |
                    +---------v----------+                              +-----------v---------+
                    |  gemma-4-26b-a4b   |   fallback (only if the      |  DeepSeek API      |
                    |  local, GPU 0      |   local endpoint is down)    |  (deepseek-chat)   |
                    |  ~/mail-triage/    | ---------------------------> |                    |
                    |  gemma  (:8040)    |                              |                    |
                    +--------------------+                              +--------------------+
```

## What it does

- **Polls your inbox** every 90 s (configurable) through the proxy and records every
  new message in a local SQLite database.
- **Quick filter rules** (Rules page): match on from / to / subject / body snippet
  (contains, equals, regex; ALL or ANY), then move to a folder, mark read, and/or
  flag. First matching rule wins, top to bottom. Folders are created if missing.
- **Rule assistant** (Assistant page): describe the sorting you want in plain English;
  a local LLM (which sees your current rules and recent senders) proposes structured
  rules; one click adds them as live or disabled. No form-filling.
- **LLM escalation** (Settings page): anything no rule matched gets classified into
  your categories (Action, Notification, Newsletter, Receipt, Personal, Promo by
  default). Auto-filing by category starts off; the LLM suggests until you enable it.
- **Reply templates + LLM drafting** (Messages page): pick a message, choose a
  template (or none), hit "Draft with LLM". Review, copy, or "Save to Drafts" --
  the draft lands in your Drafts folder to send from your normal client.

## The local model (Gemma 4 26B-A4B)

The default LLM is a **local Gemma 4 26B-A4B** (AWQ-4bit) served by vLLM on **GPU 0**
for this app, independent of the club-3090 model-pool (that stays stopped; nothing
was borrowed from it at runtime — the stack in `gemma/` is a self-contained copy).

```
gemma/                         # the model server (own compose project, one GPU)
  docker-compose.yml            #   container: mail-triage-gemma, port 8040
  patches/vllm-pr40391-v0.22.0/ #   vendored int8-KV overlay for vLLM v0.22.0
  chat_template.jinja           #   Gemma canonical chat template
  cache/                        #   warm torch/triton compile caches
```

- Start / stop / logs (from `~/mail-triage/gemma`):
  `docker compose up -d` · `docker compose stop` · `docker compose logs -f`
- It auto-starts on boot (`restart: unless-stopped`). Boot takes ~3-5 min (16 GB of
  weights load into the 3090); the healthcheck allows it 5 min to come up.
- **Fallback:** if the local endpoint is unreachable, the app automatically falls back
  to DeepSeek (`deepseek-chat`) and notes it on the Log page. Switch back happens by
  itself once the local server is up again. To make the app 100% local with no cloud
  fallback, delete the `LLM_FALLBACK_*` lines from `.env` and restart.
- To point at something else entirely: edit `LLM_BASE_URL` / `LLM_MODEL` in `.env`
  and `docker restart mail-triage` (any OpenAI-compatible endpoint works).

## Safety model

- It **never deletes mail**. Worst case it files something into a folder.
- Rules act live by default; uncheck "Apply rule actions for real" in Settings to
  go dry-run. New rules from the assistant can be added disabled first if you want.
- LLM auto-filing starts **off**; there is a per-hour cap (40) on LLM calls.
- The UI is reachable only on your tailnet (Tailscale Serve, no Funnel).

## Management

All commands from `/home/xrim/mail-triage`:

```bash
docker compose ps                     # app container status
docker logs -f mail-triage            # app live logs
docker compose up -d --build          # rebuild + start the app after code changes
docker restart mail-triage            # simple app restart
docker exec mail-triage python app.py --check   # read-only IMAP health check
.venv/bin/python tests/mock_e2e.py    # 58-check E2E suite (mock IMAP + mock LLM)

cd gemma                              # the model server
docker compose ps && docker compose logs -f
docker compose stop                   # free GPU 0
docker compose up -d                  # bring it back
```

- **Settings** (interval, toggles, categories, folder map, drafts folder) live in the
  UI under Settings and are stored in SQLite.
- **Secrets/connection** live in `.env` (chmod 600).
- **Data**: `data/triage.db` (messages, rules, templates, assistant chat, events).

## If something breaks

1. Worker errors show in the **Log** page and as a red banner on the Dashboard.
   Parked messages (3 failed classifications) can be retried with the
   "Retry parked" button.
2. "Connect/login failed" errors usually mean the OAuth token died. Fix it in the
   proxy UI: https://gpu-vm1.bigscale-snapper.ts.net:8095/ (Authorise -> login ->
   paste the URL back). The token watchdog will also ping you on Telegram.
3. LLM failures: check the model server (`cd gemma && docker compose ps`); use the
   "Test LLM endpoint" button on the Settings page. If the GPU wedges after a
   driver-level fault (Xid "GPU requires reset"), recover it per card with:
   `sudo bash -c 'echo 0000:0X:00.0 > /sys/bus/pci/drivers/nvidia/unbind; echo 1 > /sys/bus/pci/devices/0000:0X:00.0/reset; echo 0000:0X:00.0 > /sys/bus/pci/drivers/nvidia/bind'`
   (`nvidia-smi -r` is not supported on GeForce cards; the sysfs FLR reset above works).
4. `docker exec mail-triage python app.py --check` prints a read-only status JSON.

## Layout

```
app.py            Flask UI + routes + worker start        Dockerfile
engine.py         IMAP client, rules, LLM, assistant      docker-compose.yml
store.py          SQLite schema + queries                 .env (secrets, 600)
config.py         env config                              tests/mock_e2e.py
gemma/            local Gemma model server (own compose)  README.md (this file)
```

Times in the UI are HKT.
