// mt-mirror-language - drafts a reply that mirrors the incoming mail's language.
var CJK = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/;
globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "mt-mirror-language ready"); },
  draft: function (ctx, input) {
    var cfg = (ctx.config.get() || {}).values || {};
    var style = cfg.style || "brief and professional";
    var force = String(cfg.force_language || "").trim().toLowerCase();
    var text = String((input && input.snippet) || "");
    var cjk = CJK.test(String((input && input.subject) || "") + " " + text);
    var lang;
    if (force === "en") lang = "English";
    else if (force === "zh" || force === "zh-hant" || force === "zh-hk") lang = "Traditional Chinese (Hong Kong)";
    else lang = cjk ? "Traditional Chinese (Hong Kong)" : "English";
    var extra = (input && input.instructions) ? (" Extra guidance: " + input.instructions + ".") : "";
    var r = ctx.llm.complete({
      system: "You write email replies: ONE body, no subject line, no markdown. Language: " + lang +
              ". Style: " + style + "." + extra,
      prompt: "Replying to:\nFrom: " + ((input && input.from) || "") +
              "\nSubject: " + ((input && input.subject) || "") + "\n\n" + text.slice(0, 3000),
      max_tokens: 400
    });
    return { text: String((r && r.text) || "").trim() };
  },
  execute: function (ctx, call) {
    return { ok: false, summary: "mt-mirror-language has no tools",
             error: { code: "invalid_args", message: "draft-provider plugin" } };
  },
  onUnload: function (ctx) {}
};
