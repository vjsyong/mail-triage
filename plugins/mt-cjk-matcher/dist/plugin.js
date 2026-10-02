// mt-cjk-matcher - a rule/flow condition the native ops cannot express:
// "does this mail contain CJK text?" (Han, Kana, Hangul ranges).
var CJK = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]/;
globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "mt-cjk-matcher ready"); },
  match: function (ctx, input) {
    var f = (input && input.fields) || {};
    var hay = [f.subject, f.snippet, f.body, f.from, (input && input.text) || ""]
                .filter(Boolean).join(" ");
    var m = CJK.test(String(hay));
    return { match: m, detail: m ? "contains CJK text" : "no CJK text" };
  },
  execute: function (ctx, call) {
    return { ok: false, summary: "mt-cjk-matcher has no tools",
             error: { code: "invalid_args", message: "matcher plugin" } };
  },
  onUnload: function (ctx) {}
};
