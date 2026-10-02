/* Mail Desk - sandboxed composed UI controller + read-only backend.
 *
 * No browser code runs: the host calls uiOpen / uiDispatch / uiClose
 * synchronously, validates the returned component tree, owns the state and
 * renders it. Controller execution runs with a read-only host capability subset
 * (mail read/search, config, kv read, log) - net/llm/action/kv writes are denied.
 */
globalThis.__mt_plugin = {
  onLoad: function (ctx) {
    ctx.log("info", "mail-desk " + ctx.plugin.version + " loaded (composed)");
  },

  // ---- tool surface (kept declared; not advertised to the assistant)
  execute: function (ctx, call) {
    var args = call.args || {};
    try {
      if (call.tool === "search_messages") {
        return { ok: true, summary: "search", data: { messages: search(ctx, args.query, args.limit) } };
      }
      if (call.tool === "read_message") {
        return { ok: true, summary: "read", data: { message: read(ctx, args.id) } };
      }
    } catch (e) {
      return { ok: false, summary: (e && e.message) || "failed",
               error: { code: (e && e.code) || "internal", message: (e && e.message) || "failed" } };
    }
    return { ok: false, summary: "unknown tool",
             error: { code: "invalid_args", message: "unknown tool" } };
  },

  // ---- composed controller -------------------------------------------------
  uiOpen: function (ctx, input) {
    var state = norm(input && input.state);
    return { tree: build(ctx, state), state: state, view: state.selected ? "reader" : "list" };
  },

  uiDispatch: function (ctx, input) {
    var state = norm(input && input.state);
    var event = (input && input.event) || {};
    var kind = String(event.kind || "");
    if (kind === "search") {
      state.q = String(event.value == null ? "" : event.value).slice(0, 200);
      state.selected = 0;
    } else if (kind === "select") {
      var id = parseInt(event.value, 10);
      state.selected = (isNaN(id) || id < 1) ? 0 : id;
    } else if (kind === "back") {
      state.selected = 0;
    } else if (kind === "refresh") {
      state.selected = state.selected || 0;
    }
    return { tree: build(ctx, state), state: state, view: state.selected ? "reader" : "list",
             url: { q: state.q, message: state.selected ? String(state.selected) : "" } };
  },

  uiClose: function (ctx, input) { /* nothing to release */ },

  onUnload: function (ctx) {}
};

function norm(s) {
  s = (s && typeof s === "object") ? s : {};
  var sel = parseInt(s.selected, 10);
  if (isNaN(sel)) sel = parseInt(s.message, 10);
  return { q: String(s.q || "").slice(0, 200), selected: sel || 0 };
}

function search(ctx, query, limit) {
  var q = String(query == null ? "" : query).slice(0, 200);
  var n = parseInt(limit, 10);
  if (isNaN(n) || n < 1) n = 20;
  if (n > 50) n = 50;
  return ctx.mail.search({ query: q, limit: n });
}

function read(ctx, id) {
  var mid = parseInt(id, 10);
  if (isNaN(mid) || mid < 1) throw { code: "invalid_args", message: "id must be a positive integer" };
  return ctx.mail.read(mid);
}

function build(ctx, state) {
  var B = globalThis.MTUIB;
  var q = state.q || "";
  var results = [];
  var listError = null;
  if (q) {
    try { results = search(ctx, q, 20); }
    catch (e) { listError = (e && e.message) || "Search failed."; }
  }
  var list;
  if (listError) {
    list = B.state("denied", { message: "The mailbox read permission was not granted for this plugin." });
  } else if (!q) {
    list = B.state("empty", { message: "Search the local index to begin." });
  } else if (!results.length) {
    list = B.state("empty", { message: "No messages match that search." });
  } else {
    list = B.messageList(results, { event: "select", selected: state.selected || undefined });
  }
  var reader;
  if (state.selected) {
    try {
      reader = B.stack([
        B.button("\u2190 Results", { event: "back" }),
        B.messageReader(read(ctx, state.selected))
      ], { gap: "sm" });
    } catch (e) {
      reader = B.state("error", { message: "Could not read that message.", actionLabel: "Retry", retryEvent: "refresh" });
    }
  } else {
    reader = B.state("empty", { message: "Select a message to read its plain-text body." });
  }
  return B.stack([
    B.stack([
      B.searchField({ value: q, placeholder: "Search the local index (sender, subject, text)", event: "search" }),
      B.button("Refresh", { event: "refresh" })
    ], { direction: "row", gap: "sm" }),
    B.split(list, reader)
  ], { gap: "sm" });
}
