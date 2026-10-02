// mt-commitments - extract commitments and deadlines from recent mail.
//
// Read-only: searches the local index, reads snippets, and asks the LLM for a
// structured list with source quotes. Never mutates mail. A daily schedule can
// surface a card via ctx.action.propose().
globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "mt-commitments ready"); },

  execute: function (ctx, call) {
    if (call.tool !== "extract_commitments") {
      return { ok: false, summary: "unknown tool",
               error: { code: "invalid_args", message: "unknown tool " + call.tool } };
    }
    var out = runExtraction(ctx, call.args || {}, false);
    return { ok: true, summary: out.summary, data: out.data, card: out.card };
  },

  // Daily scheduled run: propose a card only when there is something to say.
  onSchedule: function (ctx, input) {
    var out = runExtraction(ctx, {}, true);
    if (out.items.length) {
      try { ctx.action.propose(out.card); } catch (e) {
        ctx.log("warn", "commitments propose failed: " + e);
      }
      return { ok: true, summary: out.summary, data: { scheduled: true } };
    }
    return { ok: true, summary: "no commitments found", data: { scheduled: true } };
  },

  onUnload: function (ctx) {}
};

// ---------------------------------------------------------------- helpers

function cfgInt(ctx, args, key, fallback, lo, hi) {
  var v = args[key];
  if (v === undefined || v === null || v === "") {
    try {
      var c = ctx.config.get() || {};
      v = (c.values || {})[key];
    } catch (e) { v = undefined; }
  }
  var n = parseInt(v, 10);
  if (isNaN(n)) n = fallback;
  if (lo !== undefined && n < lo) n = lo;
  if (hi !== undefined && n > hi) n = hi;
  return n;
}

function cfgStr(ctx, key, fallback) {
  try {
    var c = ctx.config.get() || {};
    var v = (c.values || {})[key];
    if (v === undefined || v === null) return fallback;
    return String(v);
  } catch (e) { return fallback; }
}

// Days since 1970-01-01 for a YYYY-MM-DD string (UTC, proleptic Gregorian
// arithmetic - no Date parsing, so no timezone surprises in QuickJS).
function daysFromYmd(s) {
  var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || "").trim());
  if (!m) return null;
  var y = parseInt(m[1], 10), mo = parseInt(m[2], 10), d = parseInt(m[3], 10);
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;
  y -= mo <= 2 ? 1 : 0;
  var era = Math.floor((y >= 0 ? y : y - 399) / 400);
  var yoe = y - era * 400;
  var doy = Math.floor((153 * (mo + (mo > 2 ? -3 : 9)) + 2) / 5) + d - 1;
  var doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468;
}

function todayDays() {
  try { return Math.floor(Date.now() / 86400000); } catch (e) { return 0; }
}

// Inverse of daysFromYmd: days-since-epoch -> YYYY-MM-DD (UTC arithmetic).
function ymdFromDays(z) {
  z += 719468;
  var era = Math.floor((z >= 0 ? z : z - 146096) / 146097);
  var doe = z - era * 146097;
  var yoe = Math.floor((doe - Math.floor(doe / 1460) + Math.floor(doe / 36524) -
                        Math.floor(doe / 146096)) / 365);
  var y = yoe + era * 400;
  var doy = doe - (365 * yoe + Math.floor(yoe / 4) - Math.floor(yoe / 100));
  var mp = Math.floor((5 * doy + 2) / 153);
  var d = doy - Math.floor((153 * mp + 2) / 5) + 1;
  var mo = mp + (mp < 10 ? 3 : -9);
  y += (mo <= 2 ? 1 : 0);
  function p(n) { return (n < 10 ? "0" : "") + n; }
  return y + "-" + p(mo) + "-" + p(d);
}

function whenLabel(days) {
  if (days === null || days === undefined) return "no date";
  if (days < 0) return "overdue";
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days <= 7) return "in " + days + " days";
  return "in " + days + " days";
}

function parseJsonArray(text) {
  var t = String(text || "").trim();
  try {
    var v = JSON.parse(t);
    if (Array.isArray(v)) return v;
    if (v && typeof v === "object" && Array.isArray(v.items)) return v.items;
    if (v && typeof v === "object" && Array.isArray(v.commitments)) return v.commitments;
  } catch (e) {}
  var a = t.indexOf("["), b = t.lastIndexOf("]");
  if (a >= 0 && b > a) {
    try {
      var w = JSON.parse(t.slice(a, b + 1));
      if (Array.isArray(w)) return w;
    } catch (e2) {}
  }
  return [];
}

function scoreHit(h) {
  var s = 0;
  if (h.needs_reply) s += 3;
  var cat = String(h.category || "").toLowerCase();
  if (cat === "action" || cat === "request" || cat === "personal") s += 2;
  var blob = (String(h.subject || "") + " " + String(h.snippet || "")).toLowerCase();
  var kws = ["due", "deadline", "by ", "before", "confirm", "rsvp", "expires",
             "please", "remind", "no later than", "respond", "reply by", "action required"];
  for (var i = 0; i < kws.length; i++) {
    if (blob.indexOf(kws[i]) >= 0) s += 1;
  }
  return s;
}

function findSentHits(ctx, cfgFolder) {
  var names = [];
  if (cfgFolder) names.push(cfgFolder);
  names.push("Sent", "Sent Items", "Sent Messages", "[Gmail]/Sent Mail", "[Gmail]/Sent");
  for (var i = 0; i < names.length; i++) {
    var name = names[i];
    if (!name) continue;
    try {
      var rows = ctx.mail.search({ folder: name, limit: 6 });
      if (rows && rows.length) return { folder: name, rows: rows };
    } catch (e) {}
  }
  return { folder: "", rows: [] };
}

function buildCandidates(ctx, args, scheduled) {
  var since = cfgInt(ctx, args, "since_days", 14, 1, 365);
  var maxMessages = cfgInt(ctx, args, "max_messages", 20, 5, 30);
  var hits = [];
  try { hits = ctx.mail.search({ since_days: since, limit: 50 }) || []; } catch (e) { hits = []; }

  var sent = { folder: "", rows: [] };
  try {
    if (cfgStr(ctx, "sent_folder", "") !== "__off__") {
      sent = findSentHits(ctx, cfgStr(ctx, "sent_folder", ""));
    }
  } catch (e2) { sent = { folder: "", rows: [] }; }

  var sentN = Math.min(sent.rows.length, 6);
  var ranked = hits.slice(0);
  ranked.sort(function (a, b) { return scoreHit(b) - scoreHit(a); });
  var inboxN = Math.max(0, maxMessages - sentN);

  var chosen = [];
  var seen = {};
  for (var i = 0; i < ranked.length && chosen.length < inboxN; i++) {
    chosen.push({ hit: ranked[i], sent: false });
    seen[ranked[i].id] = true;
  }
  for (var j = 0; j < sent.rows.length && j < sentN; j++) {
    if (seen[sent.rows[j].id]) continue;
    chosen.push({ hit: sent.rows[j], sent: true });
    seen[sent.rows[j].id] = true;
  }

  var candidates = [];
  for (var k = 0; k < chosen.length; k++) {
    var h = chosen[k].hit;
    var full = null;
    try { full = ctx.mail.read(h.id); } catch (e3) { full = null; }
    var text = String((full && full.body_text) || h.snippet || "").slice(0, 600);
    candidates.push({
      id: h.id, from: String(h.from || ""), subject: String(h.subject || ""),
      sent: !!chosen[k].sent, needs_reply: !!h.needs_reply,
      category: h.category || null, text: text
    });
  }
  return { since: since, candidates: candidates, sent_folder: sent.folder,
           sent_indexed: sent.rows.length > 0 };
}

function runExtraction(ctx, args, scheduled) {
  var horizon = cfgInt(ctx, args, "horizon_days", 14, 1, 120);
  var built = buildCandidates(ctx, args, scheduled);
  var candidates = built.candidates;

  var lines = [];
  var total = 0;
  for (var i = 0; i < candidates.length; i++) {
    var c = candidates[i];
    var head = "[ref:" + (i + 1) + "] sent=" + (c.sent ? 1 : 0) +
               " from=" + c.from.slice(0, 80) + " subject=" + c.subject.slice(0, 120);
    var chunk = head + "\n" + c.text;
    if (total + chunk.length > 15000) break;
    total += chunk.length;
    lines.push(chunk);
  }

  var items = [];
  if (lines.length) {
    var system = "Extract commitments and deadlines from the user's email. Return ONLY a JSON " +
      "array of objects: {\"ref\":<number>,\"kind\":\"owe\"|\"deadline\"|\"promise\"|\"announcement\"," +
      "\"action\":\"<what must happen>\",\"due\":\"YYYY-MM-DD\"|null,\"confidence\":0.0-1.0," +
      "\"quote\":\"<short verbatim source phrase>\"}. " +
      "'owe' = the sender asks the user to do something; 'deadline' = a date by which something " +
      "must happen; 'promise' = evidence the USER committed to do something (sent=1 lines are the " +
      "user's own outgoing mail); 'announcement' = an informational date with no action for the " +
      "user. Only include items with a concrete action or a concrete date. Never invent dates or " +
      "amounts; use null/empty when absent. Keep quotes under 140 characters.";
    var prompt = "Today is " + ymdFromDays(todayDays()) +
      ". Resolve relative dates (today, tomorrow, Friday, next week) against that. Messages:\n\n" +
      lines.join("\n---\n");
    var r = null;
    try {
      r = ctx.llm.complete({ system: system, prompt: prompt, json: true, max_tokens: 900 });
    } catch (e) { r = null; }
    var raw = parseJsonArray(r && r.text);

    for (var j = 0; j < raw.length; j++) {
      var it = raw[j] || {};
      var ref = parseInt(it.ref, 10);
      if (isNaN(ref) || ref < 1 || ref > candidates.length) continue;
      var src = candidates[ref - 1];
      var due = /^\d{4}-\d{2}-\d{2}$/.test(String(it.due || "")) ? String(it.due) : null;
      var action = String(it.action || "").replace(/\s+/g, " ").trim().slice(0, 200);
      if (!action && !due) continue;
      var kind = String(it.kind || "owe").toLowerCase();
      if (["owe", "deadline", "promise", "announcement"].indexOf(kind) < 0) kind = "owe";
      var conf = parseFloat(it.confidence);
      if (isNaN(conf)) conf = 0.6;
      if (conf < 0) conf = 0;
      if (conf > 1) conf = 1;
      items.push({
        kind: kind, action: action, due: due, confidence: conf,
        quote: String(it.quote || "").replace(/\s+/g, " ").trim().slice(0, 140),
        source_id: src.id, source_subject: src.subject, source_from: src.from,
        sent: src.sent
      });
    }
  }

  // order by due date (dated first, ascending), then undated by confidence
  var now = todayDays();
  for (var m = 0; m < items.length; m++) {
    var dd = daysFromYmd(items[m].due);
    items[m].days = dd === null ? null : dd - now;
    items[m].when = whenLabel(items[m].days);
    items[m].soon = (items[m].days !== null && items[m].days <= horizon);
    items[m].overdue = (items[m].days !== null && items[m].days < 0);
  }
  items.sort(function (a, b) {
    var ad = a.days === null ? 999999 : a.days;
    var bd = b.days === null ? 999999 : b.days;
    if (ad !== bd) return ad - bd;
    return b.confidence - a.confidence;
  });

  var dueSoon = 0, overdue = 0, promises = 0;
  for (var q = 0; q < items.length; q++) {
    if (items[q].soon) dueSoon += 1;
    if (items[q].overdue) overdue += 1;
    if (items[q].kind === "promise") promises += 1;
  }

  var md = [];
  var shown = Math.min(items.length, 12);
  for (var z = 0; z < shown; z++) {
    var it2 = items[z];
    var label = (it2.kind === "promise") ? "may have promised" : it2.kind;
    var line = "- **" + it2.when + "** — " + (it2.action || "(no action given)") +
               (it2.due ? " (due " + it2.due + ")" : "") + " · _" + label + "_";
    line += "\n  " + it2.source_subject.slice(0, 90) + " — " + it2.source_from.slice(0, 60);
    if (it2.quote) line += "\n  \u201c" + it2.quote + "\u201d";
    md.push(line);
  }
  if (!items.length) md.push("No commitments or deadlines found in the selected window.");

  var card = {
    title: "Commitments (last " + built.since + "d)",
    markdown: md.join("\n"),
    fields: [],
    actions: [{ id: "ok", label: "Dismiss", kind: "dismiss" }]
  };
  for (var f = 0; f < Math.min(items.length, 10); f++) {
    card.fields.push({ label: items[f].when,
                       value: (items[f].action || items[f].due || "").slice(0, 120) });
  }
  if (!built.sent_indexed) {
    card.markdown += "\n\n_Sent mail is not in the index, so \"may have promised\" items come " +
      "from received context only. Add your Sent folder in Settings → watch folders for better " +
      "results._";
  }

  var summary = items.length + " commitment(s); " + dueSoon +
    " due within " + horizon + " day(s)";
  if (overdue) summary += ", " + overdue + " overdue";
  if (promises) summary += ", " + promises + " possible promise(s)";
  summary += ".";

  return {
    summary: summary,
    items: items,
    card: card,
    data: {
      since_days: built.since, horizon_days: horizon,
      considered: candidates.length, sent_indexed: built.sent_indexed,
      counts: { total: items.length, due_soon: dueSoon, overdue: overdue, promises: promises },
      items: items.slice(0, 25).map(function (x) {
        return { kind: x.kind, action: x.action, due: x.due, when: x.when,
                 confidence: x.confidence, quote: x.quote, source_id: x.source_id,
                 subject: x.source_subject, from: x.source_from };
      })
    }
  };
}
