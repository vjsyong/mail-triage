// mt-invoice-finder - find invoices in the local index, extract amount + due date.
globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "mt-invoice-finder ready"); },
  execute: function (ctx, call) {
    if (call.tool !== "find_invoices") {
      return { ok: false, summary: "unknown tool",
               error: { code: "invalid_args", message: "unknown tool " + call.tool } };
    }
    var since = parseInt(call.args.since_days || 90, 10) || 90;
    var minAmount = call.args.min_amount;
    var hits = ctx.mail.search({ query: "invoice", limit: 12 });
    var rows = [];
    var scanned = 0;
    for (var i = 0; i < hits.length && scanned < 6; i++) {
      var full = ctx.mail.read(hits[i].id);
      scanned += 1;
      var out = ctx.llm.complete({
        system: "Extract amount and due date as JSON: {amount, due}. Use null when absent.",
        prompt: full.subject + "\n\n" + String(full.body_text || "").slice(0, 2000),
        json: true, max_tokens: 120
      });
      var j = null;
      try { j = JSON.parse(out.text); } catch (e) { j = null; }
      if (!j || !j.amount) continue;
      var amt = parseFloat(String(j.amount).replace(/[^0-9.]/g, ""));
      if (minAmount && !(amt >= minAmount)) continue;
      rows.push({ subject: full.subject, amount: String(j.amount), due: j.due ? String(j.due) : "-" });
    }
    var fields = [];
    for (var r = 0; r < rows.length; r++) {
      fields.push({ label: rows[r].subject, value: rows[r].amount + " due " + rows[r].due });
    }
    var card = fields.length ? { title: "Invoices found", fields: fields,
                                 actions: [{ id: "ok", label: "Dismiss", kind: "dismiss" }] } : null;
    return { ok: true,
             summary: "Found " + rows.length + " invoice(s); scanned " + hits.length +
                      " candidate(s) from the last ~" + since + " days.",
             data: { invoices: rows, considered: hits.length },
             card: card };
  },
  onUnload: function (ctx) {}
};
