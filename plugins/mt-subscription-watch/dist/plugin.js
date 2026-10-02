// mt-subscription-watch - track subscriptions and upcoming renewals from mail.
//
// Read-only: scans the local index for receipts/renewal notices, asks the LLM
// for structured subscription facts, and keeps a ledger in plugin KV so it can
// spot price changes and renewal dates across runs.
globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "mt-subscription-watch ready"); },

  execute: function (ctx, call) {
    if (call.tool !== "watch_subscriptions") {
      return { ok: false, summary: "unknown tool",
               error: { code: "invalid_args", message: "unknown tool " + call.tool } };
    }
    var out = runWatch(ctx, call.args || {});
    return { ok: true, summary: out.summary, data: out.data, card: out.card };
  },

  onSchedule: function (ctx, input) {
    var out = runWatch(ctx, {});
    if (out.data.renewals.length || out.data.price_changes.length) {
      try { ctx.action.propose(out.card); } catch (e) {
        ctx.log("warn", "subscription propose failed: " + e);
      }
    }
    return { ok: true, summary: out.summary, data: { scheduled: true } };
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

function nowSec() {
  try { return Math.floor(Date.now() / 1000); } catch (e) { return 0; }
}

function whenLabel(days) {
  if (days === null || days === undefined) return "no date";
  if (days < 0) return "overdue";
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  return "in " + days + " days";
}

function parseJsonArray(text) {
  var t = String(text || "").trim();
  try {
    var v = JSON.parse(t);
    if (Array.isArray(v)) return v;
    if (v && typeof v === "object" && Array.isArray(v.subscriptions)) return v.subscriptions;
    if (v && typeof v === "object" && Array.isArray(v.items)) return v.items;
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

function normAmount(s) {
  var n = String(s === null || s === undefined ? "" : s).replace(/[^0-9.]/g, "");
  if (!n) return "";
  var f = parseFloat(n);
  return isNaN(f) ? "" : String(f);
}

function scoreHit(h) {
  var blob = (String(h.subject || "") + " " + String(h.snippet || "")).toLowerCase();
  var s = 0;
  var strong = ["renew", "subscription", "subscribed", "trial", "expires", "expiring",
                "membership", "billing", "annual plan", "your plan", "auto-renew"];
  for (var i = 0; i < strong.length; i++) {
    if (blob.indexOf(strong[i]) >= 0) s += 2;
  }
  var weak = ["receipt", "invoice", "payment", "charged", "credited", "plan"];
  for (var j = 0; j < weak.length; j++) {
    if (blob.indexOf(weak[j]) >= 0) s += 1;
  }
  return s;
}

function collectCandidates(ctx, since, maxRead) {
  var queries = ["subscription", "renew", "receipt", "invoice", "billing", "trial",
                 "membership", "your plan", "expires"];
  var byId = {};
  for (var i = 0; i < queries.length; i++) {
    var rows = [];
    try { rows = ctx.mail.search({ query: queries[i], since_days: since, limit: 20 }) || []; }
    catch (e) { rows = []; }
    for (var r = 0; r < rows.length; r++) {
      byId[rows[r].id] = rows[r];
    }
  }
  var all = [];
  for (var k in byId) {
    if (Object.prototype.hasOwnProperty.call(byId, k)) all.push(byId[k]);
  }
  all.sort(function (a, b) { return scoreHit(b) - scoreHit(a); });

  var out = [];
  for (var m = 0; m < all.length && out.length < maxRead; m++) {
    var h = all[m];
    var full = null;
    try { full = ctx.mail.read(h.id); } catch (e2) { full = null; }
    out.push({
      id: h.id, from: String(h.from || ""), subject: String(h.subject || ""),
      date: String(h.date || ""),
      text: String((full && full.body_text) || h.snippet || "").slice(0, 600)
    });
  }
  return out;
}

function runWatch(ctx, args) {
  var since = cfgInt(ctx, args, "since_days", 120, 1, 720);
  var horizon = cfgInt(ctx, args, "horizon_days", 30, 1, 180);
  var maxRead = cfgInt(ctx, args, "max_read", 20, 5, 30);

  if (args.reset) {
    try { ctx.kv.delete("ledger"); } catch (e) {}
    return {
      summary: "Subscription ledger cleared.",
      card: { title: "Subscriptions", markdown: "The tracked-subscription ledger was cleared.",
              fields: [], actions: [{ id: "ok", label: "Dismiss", kind: "dismiss" }] },
      data: { reset: true, renewals: [], price_changes: [], trials: [], subs_count: 0,
              horizon_days: horizon }
    };
  }

  var candidates = collectCandidates(ctx, since, maxRead);
  var extracted = [];
  if (candidates.length) {
    var lines = [];
    var total = 0;
    for (var i = 0; i < candidates.length; i++) {
      var c = candidates[i];
      var chunk = "[ref:" + (i + 1) + "] from=" + c.from.slice(0, 80) +
                  " subject=" + c.subject.slice(0, 120) + "\n" + c.text;
      if (total + chunk.length > 15000) break;
      total += chunk.length;
      lines.push(chunk);
    }
    var system = "Extract subscription and renewal details from these emails. Return ONLY a JSON " +
      "array of objects: {\"ref\":<number>,\"merchant\":\"<service name>\",\"amount\":\"<number or " +
      "empty>\",\"currency\":\"<ISO code or symbol or empty>\",\"cadence\":\"monthly\"|\"yearly\"|" +
      "\"weekly\"|\"unknown\",\"next_due\":\"YYYY-MM-DD\"|null,\"status\":\"active\"|\"trial\"|" +
      "\"cancelled\"|\"unknown\"}. Only include genuine recurring services or billing notices. " +
      "Never invent amounts or dates; leave amount/next_due empty/null when the email does not " +
      "state them. A renewal notice's next_due is the stated renewal/charge date.";
    var prompt = "Today is " + ymdToday() + ". Messages:\n\n" + lines.join("\n---\n");
    var r = null;
    try {
      r = ctx.llm.complete({ system: system, prompt: prompt, json: true, max_tokens: 1000 });
    } catch (e2) { r = null; }
    var raw = parseJsonArray(r && r.text);
    for (var j = 0; j < raw.length; j++) {
      var it = raw[j] || {};
      var ref = parseInt(it.ref, 10);
      if (isNaN(ref) || ref < 1 || ref > candidates.length) continue;
      var merchant = String(it.merchant || "").replace(/\s+/g, " ").trim().slice(0, 80);
      if (!merchant) continue;
      var cadence = String(it.cadence || "unknown").toLowerCase();
      if (["monthly", "yearly", "weekly", "unknown"].indexOf(cadence) < 0) cadence = "unknown";
      var status = String(it.status || "unknown").toLowerCase();
      if (["active", "trial", "cancelled", "unknown"].indexOf(status) < 0) status = "unknown";
      var due = /^\d{4}-\d{2}-\d{2}$/.test(String(it.next_due || "")) ? String(it.next_due) : null;
      extracted.push({
        merchant: merchant, amount: normAmount(it.amount),
        amount_raw: String(it.amount === null || it.amount === undefined ? "" : it.amount).slice(0, 24),
        currency: String(it.currency || "").replace(/[^A-Za-z$\u00a3\u20ac\u00a5]/g, "").slice(0, 6),
        cadence: cadence, status: status, next_due: due,
        evidence_id: candidates[ref - 1].id, subject: candidates[ref - 1].subject,
        from: candidates[ref - 1].from
      });
    }
  }

  // merge into the persisted ledger
  var ledger = null;
  try { ledger = ctx.kv.get("ledger"); } catch (e3) { ledger = null; }
  if (!ledger || typeof ledger !== "object" || !ledger.subs) ledger = { v: 1, subs: {} };
  var ts = nowSec();
  var priceChanges = [];
  for (var m = 0; m < extracted.length; m++) {
    var e = extracted[m];
    var key = e.merchant.toLowerCase();
    var cur = ledger.subs[key];
    if (!cur) {
      ledger.subs[key] = {
        merchant: e.merchant, amount: e.amount, amount_raw: e.amount_raw, currency: e.currency,
        cadence: e.cadence, status: e.status, next_due: e.next_due, first_seen: ts, last_seen: ts,
        times_seen: 1, evidence_id: e.evidence_id, changes: []
      };
    } else {
      if (e.amount && cur.amount && e.amount !== cur.amount) {
        var change = { ts: ts, from: cur.amount_raw || cur.amount, to: e.amount_raw || e.amount,
                       currency: e.currency || cur.currency };
        cur.changes = (cur.changes || []).concat([change]).slice(-12);
        priceChanges.push({ merchant: cur.merchant, from: change.from, to: change.to,
                            currency: change.currency });
      }
      cur.merchant = e.merchant;
      if (e.amount) { cur.amount = e.amount; cur.amount_raw = e.amount_raw; }
      if (e.currency) cur.currency = e.currency;
      if (e.cadence && e.cadence !== "unknown") cur.cadence = e.cadence;
      if (e.status && e.status !== "unknown") cur.status = e.status;
      if (e.next_due) cur.next_due = e.next_due;
      cur.last_seen = ts;
      cur.times_seen = (cur.times_seen || 0) + 1;
      cur.evidence_id = e.evidence_id;
    }
  }
  try { ctx.kv.set("ledger", ledger); } catch (e4) { ctx.log("warn", "ledger save failed: " + e4); }

  // derive renewals / trials / counts
  var now = todayDays();
  var subs = [];
  for (var sk in ledger.subs) {
    if (Object.prototype.hasOwnProperty.call(ledger.subs, sk)) subs.push(ledger.subs[sk]);
  }
  var renewals = [], trials = [];
  for (var s = 0; s < subs.length; s++) {
    var sub = subs[s];
    var dd = daysFromYmd(sub.next_due);
    var days = dd === null ? null : dd - now;
    sub._days = days;
    sub._when = whenLabel(days);
    if (days !== null && days <= horizon) {
      renewals.push({ merchant: sub.merchant, amount: sub.amount_raw || sub.amount,
                      currency: sub.currency, cadence: sub.cadence, due: sub.next_due,
                      when: sub._when, days: days, status: sub.status,
                      evidence_id: sub.evidence_id });
    }
    if (sub.status === "trial") {
      trials.push({ merchant: sub.merchant, due: sub.next_due, when: sub._when });
    }
  }
  renewals.sort(function (a, b) { return a.days - b.days; });

  var md = [];
  if (renewals.length) {
    md.push("**Renewals in the next " + horizon + " days**");
    for (var a = 0; a < Math.min(renewals.length, 12); a++) {
      var rn = renewals[a];
      md.push("- **" + rn.when + "** — " + rn.merchant + " · " +
              (rn.amount || "amount?") + " " + rn.currency + " / " + rn.cadence +
              (rn.due ? " (due " + rn.due + ")" : ""));
    }
  }
  if (priceChanges.length) {
    md.push("\n**Price changes noticed**");
    for (var b = 0; b < Math.min(priceChanges.length, 8); b++) {
      md.push("- " + priceChanges[b].merchant + ": " + priceChanges[b].from + " → " +
              priceChanges[b].to + " " + (priceChanges[b].currency || ""));
    }
  }
  if (trials.length) {
    md.push("\n**Trials**");
    for (var t = 0; t < Math.min(trials.length, 8); t++) {
      md.push("- " + trials[t].merchant + " — " + trials[t].when +
              (trials[t].due ? " (" + trials[t].due + ")" : ""));
    }
  }
  if (!md.length) {
    md.push(subs.length ? "No renewals within " + horizon + " days."
                        : "No subscriptions found in the selected window.");
  }

  var card = {
    title: "Subscriptions",
    markdown: md.join("\n"),
    fields: renewals.slice(0, 12).map(function (x) {
      return { label: x.merchant, value: x.when + " · " + (x.amount || "?") + " " + x.currency };
    }),
    actions: [{ id: "ok", label: "Dismiss", kind: "dismiss" }]
  };

  var summary = subs.length + " subscription(s) tracked; " + renewals.length +
    " renew within " + horizon + " day(s)";
  if (priceChanges.length) summary += ", " + priceChanges.length + " price change(s)";
  summary += ".";

  return {
    summary: summary,
    card: card,
    data: {
      horizon_days: horizon, subs_count: subs.length, considered: candidates.length,
      renewals: renewals.slice(0, 25), price_changes: priceChanges,
      trials: trials.slice(0, 25)
    }
  };
}

function ymdToday() {
  return ymdFromDays(todayDays());
}

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
