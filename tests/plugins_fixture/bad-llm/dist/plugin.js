globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", ctx.plugin.id + " loaded"); },
  execute: function (ctx, call) {
    return { ok: true, summary: "pong", data: call.args };
  },
  onUnload: function (ctx) {}
};
