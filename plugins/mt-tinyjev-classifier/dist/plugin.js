// mt-tinyjev-classifier - bridge the classification pipeline to the TinyJev
// sidecar (see /tinyjev). The sidecar owns the model lifecycle (download from
// HuggingFace -> load -> warm up); this bundle never runs the model itself:
//
//   classify(ctx, input)  - pipeline path: the host feeds {kind, model, feats}
//                           when native heuristics abstain; we build a state
//                           string, ask the sidecar for a Choice over the
//                           category list, and return {label, confidence}.
//                           Abstain (empty label) while the sidecar is
//                           downloading/loading or on any error.
//   tinyjev_status        - lifecycle report (phase, download progress)
//   tinyjev_classify      - test-drive one snippet through the sidecar
//   tinyjev_load          - kick a (re)download+load; no-op when ready
//
// The sidecar's /classify response matches the fusion sidecar's shape, so
// tinyjev_url may point at either service.
var DEFAULT_CATS = ["Action", "Notification", "Newsletter", "Receipt", "Personal", "Promo"];

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

function _int(v, dflt) {
  var n = parseInt(v, 10);
  return isNaN(n) || n <= 0 ? dflt : Math.min(n, 30000);
}

function _base(ctx) {
  return String(_cfg(ctx).tinyjev_url || "http://tinyjev:8099").replace(/\/+$/, "");
}

function _cats(ctx, model) {
  // categories: bound-heuristic model overrides > plugin config > defaults
  var m = (model && model.categories) || null;
  if (m && m.length) {
    var mc = [];
    for (var i = 0; i < m.length; i++) {
      var s = String(m[i] || "").replace(/^\s+|\s+$/g, "");
      if (s) mc.push(s);
    }
    if (mc.length) return mc;
  }
  var raw = String(_cfg(ctx).categories || "");
  var out = [];
  var parts = raw.split(",");
  for (var j = 0; j < parts.length; j++) {
    var p = parts[j].replace(/^\s+|\s+$/g, "");
    if (p) out.push(p);
  }
  return out.length ? out : DEFAULT_CATS.slice();
}

function _json(text) {
  try { return JSON.parse(String(text == null ? "" : text)); } catch (e) { return null; }
}

function _estr(e) {
  return String((e && e.message) || e).slice(0, 120);
}

function _abstain(detail) {
  return { label: "", confidence: 0, detail: detail };
}

function _state(from, subject, body, maxChars) {
  return "From: " + (from || "") + "\nSubject: " + (subject || "") +
         "\n\n" + _clip(body, maxChars);
}

function _post(ctx, path, payload, timeoutMs) {
  return ctx.http.fetch(_base(ctx) + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    timeout_ms: timeoutMs
  });
}

function _get(ctx, path) {
  return ctx.http.fetch(_base(ctx) + path, { method: "GET", timeout_ms: 5000 });
}

function _topProbs(probs, n) {
  var pairs = [];
  for (var k in probs) {
    if (Object.prototype.hasOwnProperty.call(probs, k)) pairs.push([k, Number(probs[k]) || 0]);
  }
  pairs.sort(function (a, b) { return b[1] - a[1]; });
  var out = [];
  for (var i = 0; i < pairs.length && i < n; i++) {
    out.push(pairs[i][0] + " " + Math.round(100 * pairs[i][1]) + "%");
  }
  return out.join(", ");
}

// --------------------------------------------------------------- classify path

function classify(ctx, input) {
  var feats = (input && input.feats) || {};
  var from = String(feats.from || "");
  var subject = String(feats.subject || "");
  var body = String(feats.body || "");
  if (!subject && !body) return _abstain("no text to classify");
  var maxChars = _int(_cfg(ctx).state_chars, 1500);
  var timeoutMs = _int(_cfg(ctx).classify_timeout_ms, 8000);
  var cats = _cats(ctx, input && input.model);
  var state = _state(from, subject, body, maxChars);
  var t0 = Date.now();
  var res;
  try {
    res = _post(ctx, "/classify", { state: state, categories: cats }, timeoutMs);
  } catch (e) {
    return _abstain("tinyjev unreachable: " + _estr(e));
  }
  if (res.status === 503) {
    var pj = _json(res.body) || {};
    return _abstain("tinyjev not ready (phase " + (pj.phase || "?") + ")");
  }
  if (res.status !== 200) return _abstain("tinyjev HTTP " + res.status);
  var v = _json(res.body);
  if (!v || !v.category) return _abstain("tinyjev returned no category");
  var label = String(v.category);
  var known = false;
  for (var i = 0; i < cats.length; i++) {
    if (cats[i] === label) { known = true; break; }
  }
  if (!known) return _abstain("off-enum category '" + label + "'");
  var conf = Number(v.category_confidence);
  if (isNaN(conf)) conf = 0;
  conf = Math.max(0, Math.min(1, conf));
  var parts = ["tinyjev " + ((v.components && v.components.tinyjev_ms != null)
                            ? v.components.tinyjev_ms + "ms" : (Date.now() - t0) + "ms")];
  var probs = v.category_probabilities;
  if (probs && typeof probs === "object") {
    var tops = _topProbs(probs, 2);
    if (tops) parts.push(tops);
  }
  if (v.needs_reply !== undefined && v.needs_reply !== null) {
    parts.push("nr: " + (v.needs_reply ? "yes" : "no") +
               (v.needs_reply_confidence != null ? " " + v.needs_reply_confidence : ""));
  }
  return { label: label, confidence: conf, detail: parts.join("; ") };
}

// ---------------------------------------------------------------------- tools

function toolStatus(ctx) {
  var res;
  try {
    res = _get(ctx, "/status");
  } catch (e) {
    return { ok: true, summary: "TinyJev sidecar unreachable at " + _base(ctx),
             data: { phase: "unreachable", error: _estr(e), base: _base(ctx) } };
  }
  var s = _json(res.body);
  if (res.status !== 200 || !s) {
    return { ok: true, summary: "TinyJev sidecar answered HTTP " + res.status,
             data: { phase: "error", status: res.status, base: _base(ctx) } };
  }
  var phase = String(s.phase || "?");
  var summary = "TinyJev " + phase;
  if (phase === "ready") {
    summary = "TinyJev ready (" + (s.model || "?") + ", " + (s.device || "?") +
              ", " + (s.calls || 0) + " calls)";
  } else if (phase === "download") {
    summary = "TinyJev downloading model" +
              (s.progress != null ? " (" + Math.round(100 * s.progress) + "%)" : "") +
              (s.cached_bytes ? ", " + Math.round(s.cached_bytes / 1048576) + " MB cached" : "");
  } else if (phase === "load") {
    summary = "TinyJev loading model" + (s.detail ? " - " + s.detail : "");
  } else if (phase === "error") {
    summary = "TinyJev error: " + _clip(s.error || "unknown", 140);
  }
  return { ok: true, summary: summary, data: s };
}

function toolClassify(ctx, args) {
  var text = String((args && args.text) || "");
  if (!text.replace(/^\s+|\s+$/g, "")) {
    return { ok: false, summary: "text is required",
             error: { code: "invalid_args", message: "text is required" } };
  }
  var maxChars = _int(_cfg(ctx).state_chars, 1500);
  var timeoutMs = _int(_cfg(ctx).classify_timeout_ms, 8000);
  var res;
  try {
    res = _post(ctx, "/classify", {
      state: _state(args.from, args.subject, text, maxChars),
      categories: _cats(ctx, null)
    }, timeoutMs);
  } catch (e) {
    return { ok: true, summary: "TinyJev sidecar unreachable: " + _estr(e),
             data: { ok: false, error: _estr(e) } };
  }
  if (res.status === 503) {
    var pj = _json(res.body) || {};
    return { ok: true, summary: "TinyJev not ready (phase " + (pj.phase || "?") + ")",
             data: { ok: false, phase: pj.phase || "?", progress: pj.progress } };
  }
  var v = _json(res.body);
  if (res.status !== 200 || !v || !v.category) {
    return { ok: true, summary: "TinyJev classify failed (HTTP " + res.status + ")",
             data: { ok: false, status: res.status } };
  }
  return { ok: true,
           summary: String(v.category) + " (" + Math.round(100 * (v.category_confidence || 0)) + "%)",
           data: v };
}

function toolLoad(ctx) {
  var res;
  try {
    res = ctx.http.fetch(_base(ctx) + "/load", { method: "POST", timeout_ms: 5000, body: "{}",
                                                 headers: { "Content-Type": "application/json" } });
  } catch (e) {
    return { ok: true, summary: "TinyJev sidecar unreachable at " + _base(ctx),
             data: { ok: false, error: _estr(e) } };
  }
  var v = _json(res.body) || {};
  if (res.status !== 200 && res.status !== 202) {
    return { ok: true, summary: "TinyJev load refused (HTTP " + res.status + ")",
             data: { ok: false, status: res.status } };
  }
  return { ok: true,
           summary: v.already ? "TinyJev already " + (v.phase || "ready")
                              : "TinyJev download/load started (phase " + (v.phase || "?") + ")",
           data: v };
}

globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "mt-tinyjev-classifier ready"); },
  classify: classify,
  execute: function (ctx, call) {
    var tool = (call && call.tool) || "";
    if (tool === "tinyjev_status") return toolStatus(ctx);
    if (tool === "tinyjev_classify") return toolClassify(ctx, (call && call.args) || {});
    if (tool === "tinyjev_load") return toolLoad(ctx);
    return { ok: false, summary: "unknown tool",
             error: { code: "invalid_args", message: "unknown tool '" + tool + "'" } };
  },
  onUnload: function (ctx) {}
};
