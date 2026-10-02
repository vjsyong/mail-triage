// mt-unsubscribe - find unsubscribe links in recent mail, group by sender.
globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "mt-unsubscribe ready"); },

  execute: function (ctx, call) {
    try {
      if (call.tool === "find_unsubscribe") return findUnsubscribe(ctx, call.args || {});
      if (call.tool === "mark_unsubscribed") return markUnsubscribed(ctx, call.args || {});
      return { ok: false, summary: "unknown tool",
               error: { code: "invalid_args", message: "unknown tool " + call.tool } };
    } catch (e) {
      return { ok: false, summary: "unsubscribe scan failed: " + msg(e),
               error: { code: (e && e.code) || "internal", message: msg(e) } };
    }
  },

  onUnload: function (ctx) {}
};

var TWO_LEVEL = { "co.uk": 1, "org.uk": 1, "ac.uk": 1, "gov.uk": 1, "com.au": 1, "net.au": 1,
                  "org.au": 1, "co.nz": 1, "com.hk": 1, "org.hk": 1, "edu.hk": 1, "co.jp": 1,
                  "or.jp": 1, "ne.jp": 1, "com.sg": 1, "com.br": 1, "com.cn": 1, "co.in": 1,
                  "co.za": 1, "com.mx": 1, "com.tw": 1, "com.my": 1, "co.kr": 1 };
var URL_RE = /https?:\/\/[^\s<>"'()\[\]{}]+/gi;
var MAILTO_RE = /mailto:[^\s<>"'()\[\]{}]+/gi;
var KW_RE = /unsubscrib\w*|opt[\s_-]?out|email preferences|manage (?:your )?preferences|subscription preferences|update (?:your )?preferences/gi;
var UNSUB_URL_RE = /unsubscrib|opt-?out|preference|leave|remove/i;
var SCAN_QUERIES = ["unsubscribe", "opt out", "opt-out", "email preferences",
                    "manage preferences", "subscription preferences"];
var CARD_MAX = 12;

function msg(e) { return e && e.message ? String(e.message) : String(e); }

function baseDomain(host) {
  host = String(host || "").toLowerCase().replace(/^\.+|\.+$/g, "");
  if (!host || host.indexOf(".") < 0) return host;
  var parts = host.split(".");
  if (parts.length >= 3 && TWO_LEVEL[parts.slice(-2).join(".")]) {
    return parts.slice(-3).join(".");
  }
  return parts.slice(-2).join(".");
}

function parseFrom(from) {
  from = String(from || "").trim();
  var m = from.match(/<([^>]+)>/);
  var email = m ? m[1].trim() : from;
  var display = m ? from.slice(0, m.index).replace(/["']/g, "").trim() : "";
  if (!email || email.indexOf("@") < 0) email = "";
  return { display: display || (email ? email.split("@")[0] : ""), email: email };
}

function hostOf(url) {
  var u = String(url || "");
  if (u.indexOf("mailto:") === 0) u = u.slice(7);
  var m = u.match(/^(?:[a-z][a-z0-9+.-]*:\/\/)?(?:[^@/?#]*@)?([^/:?#]+)/i);
  return m ? m[1].toLowerCase() : "";
}

function relatedHost(host, senderDomain) {
  var h = baseDomain(host), s = baseDomain(senderDomain);
  if (!h || !s) return false;
  return h === s || h.slice(-(s.length + 1)) === "." + s
         || s.slice(-(h.length + 1)) === "." + h;
}

function candidateScore(url, kind, dist, senderDomain) {
  var score = kind === "http" ? 1 : -1;
  var lower = url.toLowerCase();
  if (UNSUB_URL_RE.test(lower)) score += 3;
  if (lower.indexOf("https://") === 0) score += 1;
  if (dist < 40) score += 2; else if (dist < 180) score += 1;
  var host = hostOf(url);
  if (host && senderDomain && relatedHost(host, senderDomain)) score += 3;
  return score;
}

function findLinks(text, senderDomain) {
  text = String(text || "");
  var found = {}, windows = [], m;
  KW_RE.lastIndex = 0;
  while ((m = KW_RE.exec(text)) !== null) {
    windows.push([Math.max(0, m.index - 160), Math.min(text.length, m.index + m[0].length + 200),
                  m.index, m.index + m[0].length]);
  }
  function consider(url, kind, dist) {
    url = url.replace(/[.,;:]+$/, "");
    if (!url) return;
    var score = candidateScore(url, kind, dist, senderDomain);
    if (!found[url] || found[url].score < score) {
      found[url] = { url: url, kind: kind, score: score };
    }
  }
  for (var w = 0; w < windows.length; w++) {
    var start = windows[w][0], seg = text.slice(windows[w][0], windows[w][1]);
    var kmid = (windows[w][2] + windows[w][3]) / 2, u;
    URL_RE.lastIndex = 0;
    while ((u = URL_RE.exec(seg)) !== null) {
      consider(u[0], "http", Math.abs(start + u.index + u[0].length / 2 - kmid));
    }
    MAILTO_RE.lastIndex = 0;
    while ((u = MAILTO_RE.exec(seg)) !== null) {
      consider(u[0], "mailto", Math.abs(start + u.index + u[0].length / 2 - kmid));
    }
  }
  URL_RE.lastIndex = 0;
  while ((m = URL_RE.exec(text)) !== null) {
    if (UNSUB_URL_RE.test(m[0])) consider(m[0], "http", 500);
  }
  var out = [];
  for (var k in found) if (Object.prototype.hasOwnProperty.call(found, k)) out.push(found[k]);
  out.sort(function (a, b) { return b.score - a.score; });
  return out;
}

function loadDone(ctx) {
  var v = null;
  try { v = ctx.kv.get("unsubscribed"); } catch (e) { v = null; }
  return (v && typeof v === "object" && !(v instanceof Array)) ? v : {};
}

function saveDone(ctx, done) {
  var keys = Object.keys(done);
  if (keys.length > 500) {
    keys.sort(function (a, b) { return (done[a].ts || 0) - (done[b].ts || 0); });
    for (var i = 0; i < keys.length - 500; i++) delete done[keys[i]];
  }
  ctx.kv.set("unsubscribed", done);
}

function clampInt(v, lo, hi, dflt) {
  var n = parseInt(v, 10);
  if (isNaN(n)) n = dflt;
  return Math.max(lo, Math.min(hi, n));
}

function shortLabel(g) {
  var label = g.display || g.domain;
  return label.length > 28 ? label.slice(0, 27) + "\u2026" : label;
}

function findUnsubscribe(ctx, args) {
  var cfg = ctx.config.get() || {};
  var vals = cfg.values || {};
  var sinceDays = clampInt(args.since_days || vals.since_days, 1, 3650, 30);
  var maxScan = clampInt(args.max_scan || vals.max_scan, 10, 200, 120);
  var includeDone = !!args.include_done;

  var refs = {}, order = [];
  for (var qi = 0; qi < SCAN_QUERIES.length && order.length < maxScan; qi++) {
    var hits = ctx.mail.search({ query: SCAN_QUERIES[qi], since_days: sinceDays, limit: 50 }) || [];
    for (var hi = 0; hi < hits.length; hi++) {
      var id = hits[hi].id;
      if (!refs[id]) { refs[id] = hits[hi]; order.push(id); }
    }
  }

  var groups = {}, scanned = 0, matched = 0;
  for (var i = 0; i < order.length && scanned < maxScan; i++) {
    var ref = refs[order[i]], full;
    try { full = ctx.mail.read(ref.id); } catch (e) { continue; }
    scanned++;
    var text = String((full && (full.body_text || full.snippet)) || ref.snippet || "");
    var pf = parseFrom((full && full.from) || ref.from || "");
    var senderDomain = pf.email ? pf.email.split("@").pop().toLowerCase() : "";
    var domain = baseDomain(senderDomain) || "(unknown)";
    var links = findLinks(text, senderDomain);
    if (!links.length) continue;
    matched++;
    var g = groups[domain];
    if (!g) {
      g = groups[domain] = { domain: domain, display: pf.display, count: 0,
                             links: {}, subjects: [], last: "" };
    }
    g.count++;
    if (pf.display && (!g.display || g.display.indexOf("@") >= 0)) g.display = pf.display;
    if (g.subjects.length < 3 && (full.subject || ref.subject)) {
      g.subjects.push(full.subject || ref.subject);
    }
    if (full.date || ref.date) g.last = full.date || ref.date;
    for (var li = 0; li < links.length; li++) {
      var key = links[li].url;
      if (!g.links[key] || g.links[key].score < links[li].score) g.links[key] = links[li];
    }
  }

  var senders = [], skipped = 0, done = loadDone(ctx);
  for (var dom in groups) {
    if (!Object.prototype.hasOwnProperty.call(groups, dom)) continue;
    if (done[dom] && !includeDone) { skipped++; continue; }
    var grp = groups[dom], best = null, mailto = "";
    for (var lk in grp.links) {
      var link = grp.links[lk];
      if (link.kind === "mailto") { if (!mailto) mailto = link.url; continue; }
      if (!best || link.score > best.score) best = link;
    }
    if (!best && !mailto) continue;
    var url = best ? best.url : "";
    senders.push({ domain: dom, sender: grp.display || dom, count: grp.count,
                   url: url, mailto: mailto, related: best ? relatedHost(hostOf(url),
                   grp.domain) : false, examples: grp.subjects, last: grp.last,
                   done: !!done[dom] });
  }
  senders.sort(function (a, b) {
    if (b.count !== a.count) return b.count - a.count;
    return a.domain < b.domain ? -1 : 1;
  });

  var total = senders.length;
  var summary = total
    ? "Found " + total + " sender(s) to unsubscribe from across " + matched
      + " message(s)." + (skipped ? " " + skipped + " already marked done." : "")
    : "No unsubscribe links found in the last " + sinceDays + " day(s).";
  var data = { senders: senders.slice(0, 50), scanned: scanned, matched: matched,
               skipped: skipped, since_days: sinceDays };
  return { ok: true, summary: summary, data: data, card: buildCard(senders, skipped) };
}

function buildCard(senders, skipped) {
  if (!senders.length) {
    return { title: "No unsubscribe links found",
             markdown: "Nothing to leave in the scanned window.",
             actions: [{ id: "ok", label: "Dismiss", kind: "dismiss" }] };
  }
  var fields = [], actions = [];
  var top = senders.slice(0, CARD_MAX);
  for (var i = 0; i < top.length; i++) {
    var g = top[i];
    fields.push({ label: shortLabel(g), value: g.count + " msg"
                  + (g.related ? "" : " \u00b7 external link") });
    if (g.url) actions.push({ id: g.domain, label: "Unsubscribe \u00b7 " + shortLabel(g),
                              kind: "link", url: g.url });
  }
  actions.push({ id: "ok", label: "Dismiss", kind: "dismiss" });
  return { title: "Unsubscribe (" + senders.length
                   + (senders.length === 1 ? " sender" : " senders") + ")",
           markdown: "Click a sender to open its unsubscribe page."
                     + (skipped ? " " + skipped + " sender(s) already marked done." : ""),
           fields: fields, actions: actions };
}

function markUnsubscribed(ctx, args) {
  var raw = String(args.domain || args.sender || "").trim().toLowerCase();
  if (raw.indexOf("@") >= 0) raw = raw.split("@").pop();
  var domain = baseDomain(raw);
  if (!domain) {
    return { ok: false, summary: "a sender domain is required",
             error: { code: "invalid_args", message: "domain is required" } };
  }
  var done = loadDone(ctx);
  if (args.undo) {
    delete done[domain];
  } else {
    done[domain] = { ts: Date.now(), url: String(args.url || "") };
  }
  saveDone(ctx, done);
  return { ok: true,
           summary: (args.undo ? "Restored " : "Marked ") + domain
                    + (args.undo ? " to future unsubscribe scans." : " as unsubscribed."),
           data: { domain: domain, undo: !!args.undo } };
}
