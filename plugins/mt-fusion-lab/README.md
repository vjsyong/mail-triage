# mt-fusion-lab

A/B lab for classifier verdicts, built on the plugin SDK's trusted-page tier.

- **Fusion Lab** page (System → Fusion Lab): pick an indexed email, see the
  primary LLM, the fallback LLM, and the TinyJev+MiniCPM fusion classify it
  side by side with timing and the stored verdict.
- Two hidden, read-only tools back the page: `list_messages`, `compare_message`.

The fusion half is served by the sidecar in `/fusion` (TinyJev-0.6B for the
category + a MiniCPM5-2B *direct needs_reply* prompt with thinking off). The
primary/fallback halves use whatever endpoints are configured under
Settings → AI Settings, via the host LLM capability (endpoint selector
`primary`/`fallback`). Nothing is written, moved or sent.

Setup:

1. Start the sidecar: `docker compose -f fusion/docker-compose.yml up -d --build`
   and point `FUSION_LLM_BASE_URL` at your OpenAI-compatible 2B server.
2. Enable the plugin and grant `mailbox.read`, `llm.complete`, `net.http` on
   the Plugins page; set the Fusion service URL if not `http://fusion:8098`.
3. Open the browser view once (trusted views require explicit approval) and
   approve it in the plugin detail page.

See `docs/fusion-lab.md` for the measured numbers and caveats.
