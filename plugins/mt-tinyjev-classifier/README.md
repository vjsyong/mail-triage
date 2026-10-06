# mt-tinyjev-classifier

Local **TinyJev-0.6B** classifier. The sidecar in [`/tinyjev`](../../tinyjev)
owns the model lifecycle — it downloads the weights from HuggingFace on first
boot (progress on `GET /status`), loads them, and answers one-forward-pass
`Choice` questions. This plugin is the bridge:

- **classifier kind** — when native heuristics abstain and the plugin is opted
  in on its Plugins page (bound to a heuristic row for the confidence gate),
  the host feeds the featurized message here; the plugin asks the sidecar for
  a category and returns `{label, confidence}`. It abstains quietly while the
  sidecar is downloading/loading or unreachable, so the LLM path stays in
  charge until TinyJev is actually ready.
- **tools** — `tinyjev_status` (lifecycle report), `tinyjev_classify`
  (test-drive a snippet), `tinyjev_load` (kick a (re)download + load).

Everything is local: no LLM permission, only `net.http` to the sidecar host.
Run the sidecar with `docker compose -f tinyjev/docker-compose.yml up -d --build`,
then set the service URL in the plugin's Settings (default
`http://tinyjev:8099`). Design notes: `docs/tinyjev-classifier.md`.
