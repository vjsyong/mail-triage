/* Mail Desk - sandboxed backend (QuickJS). Read-only over the local index.
 *
 * The browser page never touches mail directly: it asks the host to run these
 * two tools under the `mailbox.read` grant, through the normal sandbox path
 * (args validated, timeout enforced, every call audited).
 */
globalThis.__mt_plugin = {
  onLoad: function (ctx) {
    ctx.log("info", "mail-desk " + ctx.plugin.version + " loaded");
  },

  execute: function (ctx, call) {
    var args = call.args || {};
    try {
      if (call.tool === "search_messages") {
        var q = String(args.query == null ? "" : args.query).slice(0, 200);
        var limit = parseInt(args.limit, 10);
        if (isNaN(limit) || limit < 1) limit = 20;
        if (limit > 50) limit = 50;
        var hits = ctx.mail.search({ query: q, limit: limit });
        return {
          ok: true,
          summary: hits.length + " message(s) from the local index",
          data: { messages: hits, index_only: true, query: q }
        };
      }
      if (call.tool === "read_message") {
        var id = parseInt(args.id, 10);
        if (isNaN(id) || id < 1) {
          return { ok: false, summary: "message id is required",
                   error: { code: "invalid_args", message: "id must be a positive integer" } };
        }
        var m = ctx.mail.read(id);
        return {
          ok: true,
          summary: "Read " + String(m.subject || "").slice(0, 80),
          data: { message: m, index_only: true }
        };
      }
    } catch (e) {
      var code = (e && e.code) || "internal";
      var msg = (e && e.message) || "host call failed";
      return { ok: false, summary: msg, error: { code: code, message: msg } };
    }
    return { ok: false, summary: "unknown tool",
             error: { code: "invalid_args", message: "unknown tool" } };
  },

  onUnload: function (ctx) {}
};
