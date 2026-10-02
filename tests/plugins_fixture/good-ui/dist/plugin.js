globalThis.__mt_plugin = {
  onLoad: function (ctx) {},
  execute: function (ctx, call) {
    return { ok: true, summary: "ok", data: { note: (call.args || {}).note || "hi" } };
  },
  onUnload: function (ctx) {}
};
