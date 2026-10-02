// mt-promo-fastpath - mirror a trained promo heuristic inside the sandbox.
// The host feeds {kind, model, feats} (the same model the native heuristic
// holds); predictDecisionList / predictNaiveBayes below are 1:1 ports of the
// native heuristics.py implementations, so predictions (and confidence) match.
function predictDecisionList(model, feats) {
  var toks = (feats && feats.tokens) || {};
  var conds = (model && model.conditions) || [];
  var fldMap = { d: "domain", s: "sender", t: "subject", b: "body" };
  for (var i = 0; i < conds.length; i++) {
    var c = conds[i];
    if (!c || !c.token) continue;
    if (!Object.prototype.hasOwnProperty.call(toks, c.token)) continue;
    var parts = String(c.token).split(":");
    var fld = fldMap[parts[0]] || "?";
    var val = parts.length > 1 ? parts.slice(1).join(":") : "";
    var detail = fld + " contains '" + val + "' (" + Math.round(100 * (c.precision || 0)) +
                 "% over " + (c.support || 0) + " mail" + ((c.support || 0) === 1 ? "" : "s") + ")";
    return { label: c.label, confidence: c.prob || 0, detail: detail };
  }
  return null;
}

function predictNaiveBayes(model, feats) {
  var toks = (feats && feats.tokens) || {};
  var classes = (model && model.classes) || {};
  var scores = {};
  var best = null;
  var key;
  for (key in classes) {
    if (!Object.prototype.hasOwnProperty.call(classes, key)) continue;
    var cls = classes[key] || {};
    var s = cls.log_prior || 0;
    var lik = cls.log_lik || {};
    for (var tk in toks) {
      if (!Object.prototype.hasOwnProperty.call(toks, tk)) continue;
      if (Object.prototype.hasOwnProperty.call(lik, tk)) s += toks[tk] * lik[tk];
    }
    scores[key] = s;
    if (best === null || s > scores[best]) best = key;
  }
  if (best === null) return null;
  var mx = scores[best];
  var z = 0;
  var probs = {};
  for (key in scores) {
    if (!Object.prototype.hasOwnProperty.call(scores, key)) continue;
    probs[key] = Math.exp(scores[key] - mx);
    z += probs[key];
  }
  z = z || 1.0;
  return { label: best, confidence: probs[best] / z, detail: null };
}

globalThis.__mt_plugin = {
  onLoad: function (ctx) { ctx.log("info", "mt-promo-fastpath ready"); },
  classify: function (ctx, input) {
    var kind = (input && input.kind) || "decision_list";
    var model = (input && input.model) || {};
    var feats = (input && input.feats) || {};
    var out = (kind === "naive_bayes")
      ? predictNaiveBayes(model, feats)
      : predictDecisionList(model, feats);
    if (!out) return { label: "", confidence: 0, detail: "abstained" };
    return out;
  },
  execute: function (ctx, call) {
    return { ok: false, summary: "mt-promo-fastpath has no tools",
             error: { code: "invalid_args", message: "no tools; it is a classifier" } };
  },
  onUnload: function (ctx) {}
};
