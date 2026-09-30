"""Deterministic classifier plugins ("heuristics") for mail-triage.

The triage pipeline runs enabled heuristics BEFORE the LLM: a confident
verdict is applied as-is, so recurring categories stop depending on the
non-deterministic model and can no longer be steered by text inside emails
(prompt injection). The LLM assistant trains, retrains and retires these
itself through the registry here.

Every heuristic is a JSON-serializable artifact in the `heuristics` table:
kind + model + stats. Adding a new algorithm means calling `register_kind()`:
nothing else in the app needs to change.

Shipped kinds:
  * decision_list - learned ordered conditions (interpretable, rules-like),
                    gated by precision/support over the labelled examples
  * naive_bayes   - multinomial NB over domain/sender/subject/body tokens

Everything is pure stdlib, deterministic, and fast (training is milliseconds).
"""
import json
import math
import re
import time

import store

# ------------------------------------------------------------------ features

_TOKEN_RE = re.compile(r"[a-z0-9@._\-]{3,}")
_FIELD_LABEL = {"d": "domain", "s": "sender", "t": "subject", "b": "body"}
STOPWORDS = frozenset("""
the and for you your our its with from this that are was were will would can could should
have has had not but all any our out his her their they them then than there here what when
who how why was one two via per new now get got let may might must shall very just also more
most some such only over under about into onto off don't won't can't didn't doesn't isn't
please thanks thank regards best hello dear hey hi ok okay
charset content type transfer encoding plain html base64 quoted printable mime version
boundary name filename octet stream padding attachment inline multipart alternative related
""".split())


def featurize(msg):
    """msg: a messages row (dict). Returns token counts plus raw fields."""
    frm = (msg.get("from_addr") or "").lower().strip()
    dom = frm.split("@")[-1] if "@" in frm else frm
    subj = (msg.get("subject") or "").lower()
    body = _clean_body_for_features((msg.get("snippet") or "").lower())
    toks = {}
    if dom:
        toks["d:" + dom] = toks.get("d:" + dom, 0) + 1
    if frm:
        toks["s:" + frm] = toks.get("s:" + frm, 0) + 2
    for w in _TOKEN_RE.findall(subj)[:40]:
        if w not in STOPWORDS:
            toks["t:" + w] = toks.get("t:" + w, 0) + 2
    for w in _TOKEN_RE.findall(body)[:400]:
        if w not in STOPWORDS:
            toks["b:" + w] = toks.get("b:" + w, 0) + 1
    return {"tokens": toks, "from": frm, "domain": dom, "subject": subj, "body": body}


def _label_token_counts(examples):
    per, totals, doc_n = {}, {}, {}
    for label, feats in examples:
        seen = set(feats["tokens"])
        doc_n[label] = doc_n.get(label, 0) + 1
        for t in seen:
            per.setdefault(label, {})
            per[label][t] = per[label].get(t, 0) + 1
            totals[t] = totals.get(t, 0) + 1
    return per, totals, doc_n


# ------------------------------------------------------------------ kinds

def train_naive_bayes(examples, params):
    max_vocab = int(params.get("max_vocab") or 4000)
    min_df = int(params.get("min_df") or 1)
    per, totals, doc_n = _label_token_counts(examples)
    vocab = {t: n for t, n in totals.items() if n >= min_df}
    vocab = dict(sorted(vocab.items(), key=lambda kv: -kv[1])[:max_vocab])
    total_docs = sum(doc_n.values()) or 1
    classes = {}
    for label, n in doc_n.items():
        counts = {t: per[label].get(t, 0) for t in vocab}
        denom = sum(counts.values()) + len(vocab)  # Laplace smoothing
        classes[label] = {
            "log_prior": math.log(n / total_docs),
            "log_lik": {t: math.log((c + 1) / denom) for t, c in counts.items()},
        }
    model = {"classes": classes, "vocab": len(vocab)}
    stats = {"samples": total_docs, "labels": doc_n, "vocab": len(vocab)}
    return model, stats


def predict_naive_bayes(model, feats):
    scores = {}
    for label, cls in (model.get("classes") or {}).items():
        s = cls.get("log_prior", 0.0)
        lik = cls.get("log_lik") or {}
        for t, c in feats["tokens"].items():
            lp = lik.get(t)
            if lp is not None:
                s += c * lp
        scores[label] = s
    if not scores:
        return None
    best = max(scores, key=scores.get)
    mx = scores[best]
    exp = {l: math.exp(s - mx) for l, s in scores.items()}
    z = sum(exp.values()) or 1.0
    return best, exp[best] / z, None


def train_decision_list(examples, params):
    min_prec = float(params.get("min_precision") or 0.9)
    min_sup = int(params.get("min_support") or 3)
    per, totals, doc_n = _label_token_counts(examples)
    cands = []
    for label, tokens in per.items():
        for t, c in tokens.items():
            tot = totals.get(t) or 0
            if c < min_sup or tot <= 0:
                continue
            # shrunk precision: small samples must exceed the bar by more
            prob = (c + 0.5) / (tot + 1.0)
            if prob < min_prec:
                continue
            cands.append({"token": t, "label": label, "prob": round(prob, 4),
                          "precision": round(c / tot, 4), "support": c, "seen": tot})
    cands.sort(key=lambda c: (-c["prob"], -c["support"]))
    model = {"conditions": cands[:40]}
    stats = {"samples": sum(doc_n.values()), "labels": doc_n, "conditions": len(model["conditions"])}
    return model, stats


def predict_decision_list(model, feats):
    toks = feats["tokens"]
    for c in model.get("conditions") or []:
        if c.get("token") in toks:
            fld = _FIELD_LABEL.get((c.get("token") or ":").split(":", 1)[0], "?")
            val = (c.get("token") or ":").split(":", 1)[-1]
            detail = "%s contains %r (%d%% over %d mail%s)" % (
                fld, val, round(100 * float(c.get("precision") or 0)),
                c.get("support") or 0, "s" if (c.get("support") or 0) != 1 else "")
            return c.get("label"), float(c.get("prob") or 0), detail
    return None


def describe_naive_bayes(model, limit=6):
    classes = list((model.get("classes") or {}).keys())
    return "naive Bayes, vocab %s, classes: %s" % (model.get("vocab"), ", ".join(classes))


def describe_decision_list(model, limit=6):
    out = []
    for c in (model.get("conditions") or [])[:limit]:
        fld = _FIELD_LABEL.get((c.get("token") or ":").split(":", 1)[0], "?")
        val = (c.get("token") or ":").split(":", 1)[-1]
        out.append("%s contains %r → %s (%d%%, n=%d)" % (
            fld, val, c.get("label"), round(100 * float(c.get("precision") or 0)), c.get("support") or 0))
    return "; ".join(out) or "(no conditions met the precision bar)"


KINDS = {}


def register_kind(name, train_fn, predict_fn, describe_fn, blurb=""):
    """Plug a new classifier algorithm into the registry."""
    KINDS[name] = {"train": train_fn, "predict": predict_fn, "describe": describe_fn, "blurb": blurb}


def kind_names():
    return sorted(KINDS)


def train(kind, examples, params=None):
    if kind not in KINDS:
        raise RuntimeError("unknown heuristic kind %r (available: %s)" % (kind, ", ".join(kind_names())))
    return KINDS[kind]["train"](examples, params or {})


def predict(kind, model, feats):
    fn = (KINDS.get(kind) or {}).get("predict")
    return fn(model, feats) if fn else None


def describe(kind, model, limit=6):
    fn = (KINDS.get(kind) or {}).get("describe")
    return fn(model, limit) if fn else ""


register_kind("decision_list", train_decision_list, predict_decision_list,
              describe_decision_list,
              "learned ordered conditions (sender/domain/subject/body) with precision + support")
register_kind("naive_bayes", train_naive_bayes, predict_naive_bayes,
              describe_naive_bayes,
              "multinomial Naive Bayes over sender/domain/subject/body tokens")


# ------------------------------------------------------------------ training data

MIN_EXAMPLES = 5
OTHER_LABEL = "__other__"
_MIME_SCRUB = re.compile(
    r"(?i)\b(content[- ]type|content[- ]transfer[- ]encoding|charset|utf-8|us-ascii|"
    r"7bit|8bit|quoted-printable|base64|multipart/\w+|text/plain|text/html|"
    r"iso-\d+-\d+|windows-\d+)\b[:;.]?")


def _clean_body_for_features(body):
    """Salvage decoded text from MIME-junk snippets (legacy rows) before featurizing."""
    try:
        import engine  # lazy: engine imports this module at load time
        if engine.looks_like_mime_junk(body):
            body = engine.readable_body(body, limit=2000)
    except Exception:
        pass
    return _MIME_SCRUB.sub(" ", body)


def labels_from_tags(category, limit=500):
    cat = (category or "").strip().lower()
    return [t for t in store.tagged_examples(limit)
            if (t.get("user_tag") or "").strip().lower() == cat]


def labels_from_classified(category, limit=800):
    return [r for r in store.messages(limit=limit)
            if (r.get("llm_category") or "") == category
            and r.get("status") in ("classified", "llm-moved")]


def negatives_from_classified(category, limit=800):
    return [r for r in store.messages(limit=limit)
            if (r.get("llm_category") or "") and r.get("llm_category") != category
            and r.get("status") in ("classified", "llm-moved")]


def build_examples(category, source="tags", limit=500):
    """(label, feats) pairs: the category's positives plus negative examples.

    Negatives keep precision numbers honest - a single-class training set makes
    every token look 100% precise. When the user's own labels are thin, other
    classified mail fills the negative pool (marked in stats)."""
    if source == "tags":
        rows = labels_from_tags(category, limit)
        others = [t for t in store.tagged_examples(limit)
                  if (t.get("user_tag") or "").strip().lower() != (category or "").strip().lower()]
        neg_rows = others[:max(20, 3 * len(rows))]
    else:
        rows = labels_from_classified(category, limit)
        neg_rows = negatives_from_classified(category, limit)[:max(20, 3 * len(rows))]
    examples = [(category, featurize(r)) for r in rows]
    examples += [(OTHER_LABEL, featurize(r)) for r in neg_rows]
    return examples, len(rows)


def train_heuristic(kind, category, source="tags", params=None, limit=500,
                    min_confidence=0.8, name="", created_by="assistant"):
    examples, n = build_examples(category, source, limit)
    if n < MIN_EXAMPLES:
        raise RuntimeError("only %d labelled example(s) for category %r - need >= %d "
                           "(tag mail on the Messages page, or use source='classified')"
                           % (n, category, MIN_EXAMPLES))
    model, stats = train(kind, examples, params or {})
    stats = dict(stats or {})
    n_neg = sum(1 for label, _f in examples if label == OTHER_LABEL)
    stats.update({"source": source, "trained_label_count": n, "negatives": n_neg,
                  "trained_at": int(time.time()), "params": params or {},
                  "weak_labels": source != "tags"})
    return model, stats


def evaluate_heuristic(heuristic, limit=500):
    """In-sample evaluation against the current label source.

    Positives: predicted the category. Negatives: predicted anything else."""
    try:
        stats = json.loads(heuristic.get("stats") or "{}")
    except (TypeError, ValueError):
        stats = {}
    source = stats.get("source") or "tags"
    category = heuristic.get("category") or ""
    try:
        model = json.loads(heuristic.get("model") or "{}")
    except (TypeError, ValueError):
        model = {}
    examples, n = build_examples(category, source, limit)
    right, wrong, misses = 0, 0, []
    false_positives = 0
    for label, feats in examples:
        out = predict(heuristic.get("kind") or "", model, feats)
        pred = out[0] if out else None
        if label == OTHER_LABEL:
            ok = pred != category
            if not ok:
                false_positives += 1
        else:
            ok = pred == category
            if not ok and len(misses) < 3:
                misses.append({"subject": (feats.get("subject") or "")[:70],
                               "expected": label, "predicted": pred or "(abstain)"})
        if ok:
            right += 1
        else:
            wrong += 1
    total = right + wrong
    return {"total": total, "correct": right, "wrong": wrong,
            "positives": n, "negatives": total - n, "false_positives": false_positives,
            "accuracy": round(right / total, 4) if total else None,
            "note": "in-sample over the current %s labels (+ negative examples)" % source,
            "mistakes": misses}


# ------------------------------------------------------------------ pipeline

DEFAULT_MIN_CONFIDENCE = 0.8


def classify(msg):
    """Run the enabled heuristics. Returns a verdict dict, or None (= let the LLM decide)."""
    feats = featurize(msg)
    best = None
    for h in store.list_heuristics(enabled_only=True):
        try:
            model = json.loads(h.get("model") or "{}")
        except (TypeError, ValueError):
            continue
        out = predict(h.get("kind") or "", model, feats)
        if not out:
            continue
        label, prob, detail = out
        if str(label).startswith("__"):
            continue  # explicit "not this category" from the negative set: abstain
        if prob is None or prob < float(h.get("min_confidence") or DEFAULT_MIN_CONFIDENCE):
            continue
        if not best or prob > best["confidence"]:
            best = {"category": str(label), "confidence": float(prob),
                    "heuristic_id": h["id"], "heuristic_name": h.get("name") or ("heuristic %s" % h["id"]),
                    "kind": h.get("kind") or "", "detail": detail or ""}
    if best:
        where = (" - %s" % best["detail"]) if best["detail"] else ""
        best["reason"] = "heuristic %r (%s)%s" % (best["heuristic_name"], best["kind"], where)
    return best


AUTO_REFINE_MIN_NEW = 5


def auto_refine():
    """Retrain tag-sourced heuristics when enough new labels arrived.

    Returns [(id, name, new_labels)] for everyone that got refit."""
    done = []
    for h in store.list_heuristics(enabled_only=True):
        try:
            stats = json.loads(h.get("stats") or "{}")
        except (TypeError, ValueError):
            stats = {}
        if stats.get("source") != "tags":
            continue
        current = len(labels_from_tags(h.get("category") or "", 2000))
        before = int(stats.get("trained_label_count") or 0)
        if current - before < AUTO_REFINE_MIN_NEW:
            continue
        try:
            model, new_stats = train_heuristic(
                h.get("kind") or "", h.get("category") or "", source="tags",
                params=stats.get("params") or {},
                min_confidence=float(h.get("min_confidence") or DEFAULT_MIN_CONFIDENCE),
                created_by="auto-refine")
            new_stats["refined_from"] = before
            store.update_heuristic(h["id"], model=json.dumps(model), stats=json.dumps(new_stats))
            done.append((h["id"], h.get("name"), current - before))
        except Exception:
            continue
    return done


# ------------------------------------------------------------------ views

def view(h):
    """UI/assistant-friendly dict for a heuristics row."""
    try:
        stats = json.loads(h.get("stats") or "{}")
    except (TypeError, ValueError):
        stats = {}
    try:
        model = json.loads(h.get("model") or "{}")
    except (TypeError, ValueError):
        model = {}
    return {"id": h["id"], "name": h.get("name") or ("heuristic %s" % h["id"]),
            "kind": h.get("kind") or "", "category": h.get("category") or "",
            "enabled": bool(h.get("enabled")), "min_confidence": h.get("min_confidence"),
            "samples": stats.get("samples"), "label_source": stats.get("source"),
            "weak_labels": bool(stats.get("weak_labels")), "trained_at": stats.get("trained_at"),
            "description": describe(h.get("kind") or "", model),
            "created_by": h.get("created_by") or ""}
