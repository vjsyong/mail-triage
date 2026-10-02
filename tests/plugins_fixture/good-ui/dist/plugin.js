/* Fixture composed controller: returns safe trees, and scenario trees that the
   server must reject. Also probes the read-only host capability subset. */
globalThis.__mt_plugin = {
  onLoad: function (ctx) {},
  execute: function (ctx, call) {
    return { ok: true, summary: "ok", data: { note: (call.args || {}).note || "hi" } };
  },
  uiOpen: function (ctx, input) { return { tree: base(), state: {} }; },
  uiDispatch: function (ctx, input) {
    var ev = (input && input.event) || {};
    var v = String(ev.value || "");
    if (ev.kind === "go" && v === "general") {
      var B = globalThis.MTUIB;
      return { tree: B.stack([
        B.tabs([{ label: "One", children: [B.text("tab one")] },
                { label: "Two", children: [B.text("tab two")] }], "One"),
        B.menu([B.menuItem("Do it", { event: "go" })]),
        B.dialog("Confirm", [B.text("body"), B.input({ name: "n", value: "x", event: "go" })], { open: true }),
        B.state("empty", { message: "nothing here" }),
        B.list([B.listItem([B.badge("ok")], { event: "go", selected: true })])
      ]) };
    }
    if (ev.kind === "go" && v === "boom") {
      throw new Error("kaboom");
    }
    if (ev.kind === "go" && v === "bad_type") {
      return { tree: { type: "RawHTML", props: { value: "<img src=x onerror=alert(1)>" } } };
    }
    if (ev.kind === "go" && v === "bad_prop") {
      return { tree: { type: "Text", props: { value: "x", href: "https://evil.example" } } };
    }
    if (ev.kind === "go" && v === "bad_handler") {
      return { tree: { type: "Button", props: { label: "x", onclick: "alert(1)" }, event: "go" } };
    }
    if (ev.kind === "go" && v === "bad_proto") {
      var t = JSON.parse('{"type":"Text","props":{"value":"hi"},"__proto__":{"polluted":true}}');
      return { tree: t };
    }
    if (ev.kind === "go" && v === "big" ) {
      var B2 = globalThis.MTUIB, kids = [];
      for (var i = 0; i < 500; i++) kids.push(B2.text("n" + i));
      return { tree: B2.stack(kids) };
    }
    if (ev.kind === "go" && v === "effects") {
      var out = [];
      function probe(name, fn) {
        try { fn(); out.push(name + ":allowed"); }
        catch (e) { out.push(name + ":" + (e && e.code || "unknown")); }
      }
      probe("http", function () { ctx.http.fetch("http://127.0.0.1:1/x"); });
      probe("llm", function () { ctx.llm.complete({ prompt: "hi" }); });
      probe("propose", function () { ctx.action.propose({ title: "x" }); });
      probe("kvset", function () { ctx.kv.set("k", "v"); });
      probe("kvget", function () { ctx.kv.get("k"); });
      probe("mail", function () { ctx.mail.search({ limit: 1 }); });
      return { tree: base(out.join(", ")) };
    }
    if (ev.kind === "go" && v === "nav") {
      return { tree: base("navigated"), state: {}, url: { q: "hello", message: "3" } };
    }
    return { tree: base() };
  },
  uiClose: function (ctx, input) { return "ok"; },
  onUnload: function (ctx) {},
  uiStatus: "fixture"
};

function base(msg) {
  var B = globalThis.MTUIB;
  return B.stack([
    B.text(msg || "Fixture page", { variant: "title" }),
    B.button("Run", { event: "go", variant: "primary" }),
    B.button("Refresh", { event: "go" })
  ]);
}
