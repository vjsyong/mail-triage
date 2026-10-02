// mt-priority-first - re-ranks semantic search: needs-reply and tagged mail first.
globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "mt-priority-first ready"); },
  rank: function (ctx, input) {
    var cands = (input && input.candidates) || [];
    var scored = cands.map(function (c, i) {
      var s = 0;
      if (c.needs_reply) s += 2;
      if (c.tags && c.tags.length) s += 1.5;
      s -= i * 0.001; // stable: keep the incoming order as tie-break
      return { id: c.id, s: s };
    });
    scored.sort(function (a, b) { return b.s - a.s; });
    return { ids: scored.map(function (x) { return x.id; }) };
  },
  execute: function (ctx, call) {
    return { ok: false, summary: "mt-priority-first has no tools",
             error: { code: "invalid_args", message: "retriever plugin" } };
  },
  onUnload: function (ctx) {}
};
