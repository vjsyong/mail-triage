globalThis.__mt_plugin = {
  onLoad: function (ctx) {},
  execute: function (ctx, call) {
    return { ok: true, summary: "pong", data: { note: (call.args || {}).note || "hi" } };
  },
  onUnload: function (ctx) {}
};
