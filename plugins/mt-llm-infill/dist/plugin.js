// mt-llm-infill - strict infill of {llm-infill}...{/llm-infill} template blocks.
//
// Used from a flow draft step:
//   {"type":"draft","mode":"plugin","plugin":"mt-llm-infill",
//    "template_id": N, "instructions": "optional extra guidance"}
//
// The template body is kept verbatim except for the standard {sender}
// {subject} {date} {my_name} placeholders and the tagged blocks, which the LLM
// fills under a "write only this block" contract. A template without blocks is
// filled literally and never reaches the model.
function cfgValues(ctx) {
  var c = ctx.config.get() || {};
  return c.values || {};
}

function subst(text, fields) {
  var out = String(text == null ? "" : text);
  out = out.split("{sender}").join(fields.sender || "");
  out = out.split("{subject}").join(fields.subject || "");
  out = out.split("{date}").join(fields.date || "");
  out = out.split("{my_name}").join(fields.my_name || "");
  return out;
}

function blockPrompts(body) {
  var out = [];
  var re = /\{llm-infill\}([\s\S]*?)\{\/llm-infill\}/gi;
  var m;
  while ((m = re.exec(body)) !== null) {
    out.push(m[1]);
  }
  return out;
}

function replaceBlocks(body, texts) {
  var i = 0;
  return body.replace(/\{llm-infill\}([\s\S]*?)\{\/llm-infill\}/gi, function () {
    var t = i < texts.length ? texts[i] : "";
    i += 1;
    return t == null ? "" : String(t);
  });
}

function cleanBlock(s) {
  var t = String(s == null ? "" : s).trim();
  if (t.slice(0, 3) === "```") {
    t = t.replace(/^```[A-Za-z0-9_-]*\s*/, "");
    t = t.replace(/\s*```\s*$/, "").trim();
  }
  if (t.length > 1 && t.charAt(0) === '"' && t.charAt(t.length - 1) === '"') {
    t = t.slice(1, -1).trim();
  }
  return t;
}

function parseBlocks(text, n) {
  var raw = String(text == null ? "" : text).trim();
  var data = null;
  try {
    data = JSON.parse(raw);
  } catch (e) {
    data = null;
  }
  var arr = null;
  if (Array.isArray(data)) {
    arr = data;
  } else if (data && Array.isArray(data.blocks)) {
    arr = data.blocks;
  }
  if (!arr) {
    var m = raw.match(/\[[\s\S]*\]/);
    if (m) {
      try {
        var a2 = JSON.parse(m[0]);
        if (Array.isArray(a2)) arr = a2;
      } catch (e2) {
        arr = null;
      }
    }
  }
  var out = [];
  for (var i = 0; i < n; i++) {
    out.push(arr && i < arr.length ? cleanBlock(arr[i]) : "");
  }
  return out;
}

function callModel(ctx, blocks, draft, input, cfg) {
  var n = blocks.length;
  var maxTokens = parseInt(cfg.max_block_tokens, 10);
  if (!(maxTokens > 0)) maxTokens = 250;
  if (maxTokens > 1024) maxTokens = 1024;
  maxTokens = Math.max(16, maxTokens * n);
  if (maxTokens > 1800) maxTokens = 1800;

  var list = [];
  for (var i = 0; i < n; i++) {
    list.push("BLOCK " + (i + 1) + ": " +
              String(blocks[i]).replace(/\s+/g, " ").trim().slice(0, 500));
  }
  var context = "Original message:\nFrom: " + (input.from || "") +
                "\nSubject: " + (input.subject || "") + "\n\n" +
                String(input.snippet || "").slice(0, 3000);
  var body = "Draft template (keep everything except the marked blocks exactly " +
             "as written):\n---\n" + draft.slice(0, 4000) + "\n---";
  var style = cfg.style ? ("Style: " + String(cfg.style).slice(0, 300) + "\n") : "";
  var extra = input.instructions
    ? ("Extra guidance: " + String(input.instructions).slice(0, 600) + "\n") : "";

  var system, prompt;
  if (n === 1) {
    system = "You fill in ONE marked block of an email reply. Write only the " +
             "replacement text for that block - no greeting, no sign-off, no " +
             "quotes, no markdown, no commentary. Match the language, tone and " +
             "formality of the surrounding draft. Never invent facts that are " +
             "not in the original message.";
    prompt = context + "\n\n" + body + "\n\n" + style + extra + list[0] +
             "\n\nWrite only the text that replaces BLOCK 1:";
  } else {
    system = "You fill in marked blocks of an email reply. Return ONLY a JSON " +
             "object {\"blocks\": [...]} with exactly " + n + " strings, one per " +
             "numbered block, in order. Each string is only the replacement text " +
             "for that block - no greeting, no sign-off, no quotes, no markdown, " +
             "no commentary. Match the language, tone and formality of the " +
             "surrounding draft. Never invent facts that are not in the original " +
             "message.";
    prompt = context + "\n\n" + body + "\n\n" + style + extra +
             "Blocks (in order):\n" + list.join("\n") +
             "\n\nReturn JSON with exactly " + n + " strings.";
  }
  var r = ctx.llm.complete({system: system, prompt: prompt,
                            max_tokens: maxTokens, json: n > 1});
  var text = (r && r.text) || "";
  return n === 1 ? [cleanBlock(text)] : parseBlocks(text, n);
}

globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "mt-llm-infill ready"); },
  draft: function (ctx, input) {
    input = input || {};
    var tpl = input.template || null;
    var body = tpl && tpl.body != null ? String(tpl.body) : "";
    if (!body.trim()) {
      ctx.log("warn", "draft step has no template - nothing to infill");
      return {text: ""};
    }
    var fields = input.fields || {};
    if (!fields.sender) fields.sender = input.from || "";
    if (!fields.subject) fields.subject = input.subject || "";
    var rendered = subst(body, fields);
    var blocks = blockPrompts(body);
    if (!blocks.length) {
      return {text: rendered};
    }
    var texts;
    try {
      texts = callModel(ctx, blocks, rendered, input, cfgValues(ctx));
    } catch (e) {
      ctx.log("warn", "infill call failed: " + String((e && e.message) || e));
      texts = [];
    }
    return {text: replaceBlocks(rendered, texts)};
  },
  execute: function () {
    return {ok: false, summary: "mt-llm-infill has no tools",
            error: {code: "invalid_args", message: "draft-provider plugin"}};
  },
  onUnload: function () {}
};
