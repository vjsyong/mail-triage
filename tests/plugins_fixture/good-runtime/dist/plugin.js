globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "runtime fixture loaded"); },
  execute: function (ctx, call) {
    var t = call.tool;
    if (t === "echo") return { ok: true, summary: "pong", data: { echo: call.args.note || "" } };
    if (t === "kv_set") { ctx.kv.set("last", call.args.v); return { ok: true, summary: "saved" }; }
    if (t === "kv_get") return { ok: true, summary: "value=" + String(ctx.kv.get("last")) };
    if (t === "slow") { var t0 = Date.now(); while (Date.now() - t0 < 8000) {} return { ok: true, summary: "finished" }; }
    if (t === "hostile") {
      try { ctx.mail.search({ limit: 1 }); return { ok: true, summary: "search allowed" }; }
      catch (e) { return { ok: true, summary: "caught " + (e.code || "?") + ": " + e.message }; }
    }
    if (t === "count") {
      var r = ctx.mail.search({ limit: 50 });
      return { ok: true, summary: "rows=" + r.length, data: { n: r.length } };
    }
    if (t === "ask") {
      var r2 = ctx.llm.complete({ prompt: "Reply with the single word OK.", max_tokens: 16 });
      return { ok: true, summary: "llm:" + String(r2.text).slice(0, 60) };
    }
    if (t === "boom") throw new Error("kaboom");
    if (t === "memhog") { var a = []; for (;;) { a.push(new Array(200000).fill(1)); } }
    if (t === "card") return { ok: true, summary: "card ready", card: { title: "Demo card",
      fields: [{ label: "A", value: "1" }], actions: [{ id: "ok", label: "Dismiss", kind: "dismiss" }] } };
    return { ok: false, summary: "unknown tool", error: { code: "invalid_args", message: "unknown tool" } };
  },
  onUnload: function (ctx) {}
};
