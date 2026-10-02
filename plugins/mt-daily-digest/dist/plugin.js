// mt-daily-digest - a catch-me-up summary built from the local index + the LLM.
globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "mt-daily-digest ready"); },
  execute: function (ctx, call) {
    if (call.tool !== "daily_digest") {
      return { ok: false, summary: "unknown tool",
               error: { code: "invalid_args", message: "unknown tool " + call.tool } };
    }
    var days = parseInt(call.args.since_days || 1, 10) || 1;
    var maxRead = Math.min(parseInt(call.args.max_read || 10, 10) || 10, 20);
    var hits = ctx.mail.search({ since_days: days, limit: 100 });
    var byCat = {};
    var needReply = [];
    for (var i = 0; i < hits.length; i++) {
      var c = hits[i].category || "(unclassified)";
      byCat[c] = (byCat[c] || 0) + 1;
      if (hits[i].needs_reply) needReply.push(hits[i]);
    }
    var lines = [];
    var n = Math.min(hits.length, maxRead);
    for (var k = 0; k < n; k++) {
      lines.push("- " + (hits[k].from || "?") + ": " + (hits[k].subject || "(no subject)") +
                 " — " + String(hits[k].snippet || "").slice(0, 120));
    }
    var digest = "";
    if (lines.length) {
      var r = ctx.llm.complete({
        system: "Summarize this email batch for the user in 3-4 short lines: themes, anything urgent, no fluff. Plain text.",
        prompt: lines.join("\n"), max_tokens: 300
      });
      digest = String((r && r.text) || "").trim();
    }
    var fields = [];
    for (var j = 0; j < Math.min(needReply.length, 8); j++) {
      fields.push({ label: needReply[j].subject || "(no subject)", value: "needs a reply" });
    }
    for (var cat in byCat) {
      if (Object.prototype.hasOwnProperty.call(byCat, cat)) {
        fields.push({ label: cat, value: String(byCat[cat]) + " message(s)" });
      }
    }
    return { ok: true,
             summary: hits.length + " message(s) in the last " + days + " day(s); " +
                      needReply.length + " need a reply.",
             data: { since_days: days, total: hits.length,
                     needs_reply: needReply.slice(0, 10).map(function (m) {
                       return { id: m.id, subject: m.subject, from: m.from }; }),
                     categories: byCat, digest: digest },
             card: { title: "Daily digest (" + days + "d)",
                     markdown: digest || "(nothing new)",
                     fields: fields.slice(0, 12),
                     actions: [{ id: "ok", label: "Dismiss", kind: "dismiss" }] } };
  },
  onUnload: function (ctx) {}
};
