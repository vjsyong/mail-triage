/* Fusion Lab backend (QuickJS sandbox; synchronous by contract).
 *
 * list_messages    - recent indexed mail for the picker (read-only)
 * compare_message  - verdicts from the primary LLM, the configured fallback
 *                    LLM, and the TinyJev+MiniCPM fusion sidecar.
 *
 * The fusion service reproduces the measured system: TinyJev-0.6B category
 * (description-enriched Choice) + MiniCPM5-2B direct needs_reply prompt with
 * thinking off. See /fusion and docs/fusion-lab.md.
 */
var DEFAULT_CATS = ["Action", "Notification", "Newsletter", "Receipt", "Personal", "Promo"];
var CAT_DESC = {
  "Action": "Needs the owner to do something: reply, decide, submit, pay, or act",
  "Notification": "Automatic status updates and notices that need no action",
  "Newsletter": "Recurring editorial or digest content the owner subscribed to",
  "Receipt": "Order confirmations, invoices, and payment receipts",
  "Personal": "Mail from friends, family, or personal contacts",
  "Promo": "Marketing, offers, and sales promotions"
};

function _clip(s, n) {
  s = String(s == null ? "" : s);
  return s.length > n ? s.slice(0, n) : s;
}

function _cfg(ctx) {
  try {
    var c = ctx.config.get() || {};
    return c.values || c || {};
  } catch (e) {
    return {};
  }
}

function _cats(ctx) {
  var cfg = _cfg(ctx);
  var raw = String(cfg.categories || "");
  var out = [];
  var parts = raw.split(",");
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i].replace(/^\s+|\s+$/g, "");
    if (p) out.push(p);
  }
  return out.length ? out : DEFAULT_CATS.slice();
}

function _stripFences(text) {
  var t = String(text == null ? "" : text).replace(/^\s+|\s+$/g, "");
  if (t.indexOf("```") === 0) {
    var nl = t.indexOf("\n");
    if (nl >= 0) t = t.slice(nl + 1);
    var end = t.lastIndexOf("```");
    if (end >= 0) t = t.slice(0, end);
  }
  return t.replace(/^\s+|\s+$/g, "");
}

function _json(text) {
  var t = _stripFences(text);
  try { return JSON.parse(t); } catch (e) { /* fall through */ }
  var a = t.indexOf("{"), b = t.lastIndexOf("}");
  if (a >= 0 && b > a) {
    try { return JSON.parse(t.slice(a, b + 1)); } catch (e2) { /* fall through */ }
  }
  return null;
}

function _state(m) {
  return "From: " + (m.from || "") + "\nTo: " + (m.to || "") +
         "\nSubject: " + (m.subject || "") + "\nDate: " + (m.date || "") +
         "\n\n" + _clip(m.body_text || m.snippet || "", 1500);
}

function _classifyPrompt(cats) {
  return "You triage incoming email for the user. Reply with a single JSON " +
         "object and nothing else. Shape: {\"category\": one of [" + cats.join(", ") +
         "], \"needs_reply\": true|false, \"confidence\": 0.0-1.0, " +
         "\"summary\": \"one short sentence saying what the email is\", " +
         "\"reason\": \"why that category, max 15 words\"}";
}

function _ask(ctx, system, state, endpoint) {
  var t0 = Date.now();
  try {
    var out = ctx.llm.complete({ system: system, prompt: state, json: true,
                                 max_tokens: 400, endpoint: endpoint, thinking: true });
    return { ok: true, verdict: _json(out.text), raw: _clip(out.text, 500),
             ms: Date.now() - t0, error: null };
  } catch (e) {
    return { ok: false, verdict: null, raw: "", ms: Date.now() - t0,
             error: String((e && e.message) || e) };
  }
}

function _fusion(ctx, state, cats) {
  var cfg = _cfg(ctx);
  var base = String(cfg.fusion_url || "http://fusion:8098").replace(/\/+$/, "");
  var t0 = Date.now();
  var res;
  try {
    res = ctx.http.fetch(base + "/classify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: state, categories: cats }),
      timeout_ms: 30000
    });
  } catch (e) {
    return { ok: false, verdict: null, ms: Date.now() - t0,
             error: String((e && e.message) || e) };
  }
  if (res.status !== 200) {
    return { ok: false, verdict: null, ms: Date.now() - t0,
             error: "fusion HTTP " + res.status };
  }
  var parsed = _json(res.body);
  if (!parsed) {
    return { ok: false, verdict: null, ms: Date.now() - t0,
             error: "fusion returned non-JSON" };
  }
  return { ok: true, verdict: parsed, ms: Date.now() - t0,
           error: parsed.error || null };
}

function _list(ctx, args) {
  var limit = parseInt(args.limit, 10) || 30;
  if (limit > 40) limit = 40;
  if (limit < 1) limit = 1;
  var rows = ctx.mail.search({ query: String(args.query || ""), limit: limit });
  var out = [];
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    out.push({ id: r.id, subject: _clip(r.subject, 90), from: _clip(r.from, 60),
               date: r.date, category: r.category, needs_reply: r.needs_reply });
  }
  return { ok: true, summary: out.length + " message(s)", data: { messages: out } };
}

function _compare(ctx, args) {
  var mid = parseInt(args.message_id, 10);
  if (!mid) {
    return { ok: false, summary: "message_id is required",
             error: { code: "invalid_args", message: "message_id is required" } };
  }
  var m;
  try {
    m = ctx.mail.read(mid);
  } catch (e) {
    return { ok: false, summary: "message not found",
             error: { code: "invalid_args", message: String((e && e.message) || e) } };
  }
  var cats = _cats(ctx);
  var state = _state(m);
  var system = _classifyPrompt(cats);
  var primary = _ask(ctx, system, state, "primary");
  var fallback = _ask(ctx, system, state, "fallback");
  var fusion = _fusion(ctx, state, cats);
  var answered = (primary.ok ? 1 : 0) + (fallback.ok ? 1 : 0) + (fusion.ok ? 1 : 0);
  return {
    ok: true,
    summary: "Compared message #" + mid + " (" + answered + "/3 classifiers answered)",
    data: {
      message: { id: m.id, subject: m.subject, from: m.from, to: m.to, date: m.date,
                 category: m.category, needs_reply: m.needs_reply, tags: m.tags || [] },
      categories: cats,
      state_excerpt: _clip(state, 300),
      primary: primary, fallback: fallback, fusion: fusion
    }
  };
}

globalThis.__mt_plugin = {
  onLoad: function (ctx) {
    ctx.log("info", ctx.plugin.id + " " + ctx.plugin.version + " loaded");
  },
  execute: function (ctx, call) {
    call = call || {};
    var args = call.args || {};
    if (call.tool === "list_messages") return _list(ctx, args);
    if (call.tool === "compare_message") return _compare(ctx, args);
    return { ok: false, summary: "unknown tool: " + String(call.tool) };
  },
  onUnload: function (ctx) {}
};
