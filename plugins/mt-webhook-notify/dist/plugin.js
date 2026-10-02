// mt-webhook-notify - fire a JSON webhook on kernel events.
globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "mt-webhook-notify ready"); },
  onEvent: function (ctx, event) {
    var cfg = (ctx.config.get() || {}).values || {};
    var url = String(cfg.url || "").trim();
    if (!url) return { sent: false, reason: "no url configured" };
    var want = cfg.events || ["mail.filed"];
    if (want.indexOf(event.type) === -1) return { sent: false, reason: "event filtered" };
    var body = JSON.stringify({ source: "mail-triage", plugin: ctx.plugin.id,
                                type: event.type, ts: event.ts, data: event.payload || {} });
    try {
      var res = ctx.http.fetch(url, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: body });
      if (!(res.status >= 200 && res.status < 300)) {
        ctx.log("warn", "webhook returned " + res.status);
        return { sent: false, reason: "status " + res.status };
      }
      return { sent: true, status: res.status };
    } catch (e) {
      ctx.log("warn", "webhook failed: " + (e && e.message ? e.message : String(e)));
      return { sent: false, reason: "error" };
    }
  },
  execute: function (ctx, call) {
    return { ok: false, summary: "mt-webhook-notify has no tools",
             error: { code: "invalid_args", message: "integration plugin" } };
  },
  onUnload: function (ctx) {}
};
