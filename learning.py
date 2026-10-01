"""Learning loop for mail-triage: compile repeated LLM reasoning into cheap
specialists (design + audit: docs/mail-intelligence/design.md).

Lifecycle (every generated rule/model moves through these, never skipped):
    DISCOVER -> PROPOSE -> VALIDATE -> SHADOW -> PROMOTE -> MONITOR -> RETIRE
This module implements the VALIDATE..MONITOR half for the first task
(`needs_reply`), and the recording primitives (decisions / decision_evidence /
observations / labels) the whole loop is built on.

Ground rules (hard):
  * The LLM never writes executable code. Models are JSON (this file's kinds are
    pure stdlib, deterministic, interpretable: weights + bias + scaler).
  * Nothing here influences live behavior unless `learning_route_mode=enforce`
    AND the specialist is >= shadow; the default mode is `shadow`, which only
    RECORDS what routing would have done.
  * LLM annotations are WEAK labels. Provenance decides training weight
    (LABEL_SOURCES); a prediction is never treated as new ground truth.

Where things run (see engine.classify_and_store -> learning.observe_classification):
    new email -> features -> specialists (shadow) -> confidence router (logged)
              -> the real pipeline's decision is recorded alongside, so
                 agreement / escalation metrics come from evidence, not memory.
"""
import json
import math
import re
import sys
import time

import config
import heuristics
import store

FEATURE_SCHEMA_VERSION = 1

# ---------------------------------------------------------------- feature layer
# A controlled, versioned schema. Specialists declare the features they consume;
# adding a feature bumps FEATURE_SCHEMA_VERSION and old models keep working
# (they store their own feature list + scaler).

_RE_MONEY = re.compile(r"(hkd|usd|eur|gbp|\$|€|¥|\d{1,3}(,\d{3})+|\d+\.\d{2}\b)", re.I)
_RE_DATE = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)(day)?\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b|\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b", re.I)
_RE_REQUEST = re.compile(r"\b(please|could you|can you|would you|kindly|confirm|review|approve|sign|reply|let me know|send me|share|schedule|book|arrange|check|update me|help)\b", re.I)
_RE_DEADLINE = re.compile(r"\b(deadline|due|by (mon|tue|wed|thu|fri|sat|sun)|tomorrow|today|eod|end of day|asap|urgent|expires|closes|last chance)\b", re.I)
_RE_AUTOMATED = re.compile(r"(no.?reply|do ?not ?reply|noreply|automated|notification|mailer-daemon|donotreply|robot)", re.I)
_RE_NEWSLETTER = re.compile(r"(unsubscribe|newsletter|digest|view in browser|this email was sent to|weekly roundup|email preferences)", re.I)
_RE_URL = re.compile(r"https?://|www\.", re.I)


def _ts_of(msg):
    for k in ("sort_ts", "date_ts", "processed_at"):
        try:
            v = int(msg.get(k) or 0)
        except (TypeError, ValueError):
            v = 0
        if v:
            return v
    return 0


_CONTACT_CACHE = {"loaded_at": 0, "stats": {}}


def _contact_table():
    """Per-sender aggregates, loaded once per process (cheap GROUP BY)."""
    now = time.time()
    if now - _CONTACT_CACHE["loaded_at"] < 900 and _CONTACT_CACHE["stats"]:
        return _CONTACT_CACHE["stats"]
    stats = {}
    try:
        with store.db() as conn:
            for r in conn.execute(
                    "SELECT lower(from_addr) AS f, COUNT(*) AS n, "
                    "SUM(CASE WHEN llm_needs_reply=1 THEN 1 ELSE 0 END) AS nr "
                    "FROM messages WHERE coalesce(from_addr,'')!='' GROUP BY f"):
                stats[r["f"]] = {"n": int(r["n"] or 0), "nr": int(r["nr"] or 0)}
    except Exception:
        stats = {}
    _CONTACT_CACHE["stats"] = stats
    _CONTACT_CACHE["loaded_at"] = now
    return stats


def contact_features(from_addr):
    frm = (from_addr or "").lower().strip()
    st = _contact_table().get(frm) or {"n": 0, "nr": 0}
    n = st["n"]
    dom = frm.split("@")[-1] if "@" in frm else ""
    my_dom = (config.IMAP_USER.split("@")[-1] if "@" in (config.IMAP_USER or "") else "").lower()
    return {
        "c_sender_known": 1.0 if n else 0.0,
        "c_sender_count_log": math.log1p(n),
        "c_sender_reply_rate": (st["nr"] / n) if n else 0.0,
        "c_internal": 1.0 if (dom and my_dom and dom == my_dom) else 0.0,
    }


def extract_features(msg, contact=None):
    """Deterministic feature dict for a messages row. No I/O except the cached
    contact table. Returns exactly the keys in FEATURE_ORDER."""
    from_addr = (msg.get("from_addr") or "").lower().strip()
    if contact is None:
        contact = contact_features(from_addr)
    subject = msg.get("subject") or ""
    snippet = (msg.get("snippet") or "")[:4000]
    text = "%s\n%s" % (subject, snippet)
    to_addr = (msg.get("to_addr") or "").lower()
    me = (config.IMAP_USER or "").lower()
    ts = _ts_of(msg)
    hour, weekday, age_days = 12.0, 3.0, 0.0
    if ts:
        lt = time.gmtime(ts)
        hour, weekday = float(lt.tm_hour), float(lt.tm_wday)
        age_days = min(3650.0, max(0.0, (time.time() - ts) / 86400.0))
    f = {
        "f_body_len": float(len(snippet)),
        "f_subject_len": float(len(subject)),
        "f_question_marks": float(min(5, text.count("?"))),
        "f_exclaim_marks": float(min(5, text.count("!"))),
        "f_caps_words": float(min(5, sum(1 for w in re.findall(r"\b[A-Z]{3,}\b", text)))),
        "f_n_recipients": float(min(20, len([x for x in to_addr.split(",") if x.strip()]))),
        "f_direct_recipient": 1.0 if (me and me in to_addr) or ("ust.hk" in to_addr) else 0.0,
        "f_contains_money": 1.0 if _RE_MONEY.search(text) else 0.0,
        "f_has_dates": 1.0 if _RE_DATE.search(text) else 0.0,
        "f_contains_request": 1.0 if _RE_REQUEST.search(text) else 0.0,
        "f_deadline_lang": 1.0 if _RE_DEADLINE.search(text) else 0.0,
        "f_automated_hint": 1.0 if _RE_AUTOMATED.search("%s %s" % (from_addr, text)) else 0.0,
        "f_newsletter_hint": 1.0 if _RE_NEWSLETTER.search(text) else 0.0,
        "f_reply_marker": 1.0 if re.match(r"\s*(re|fwd?)\s*:", subject, re.I) else 0.0,
        "f_has_url": 1.0 if _RE_URL.search(text) else 0.0,
        "t_hour": hour,
        "t_weekday": weekday,
        "t_age_days": age_days,
    }
    f.update(contact)
    return {k: float(f.get(k, 0.0)) for k in FEATURE_ORDER}


FEATURE_ORDER = [
    "f_body_len", "f_subject_len", "f_question_marks", "f_exclaim_marks",
    "f_caps_words", "f_n_recipients", "f_direct_recipient", "f_contains_money",
    "f_has_dates", "f_contains_request", "f_deadline_lang", "f_automated_hint",
    "f_newsletter_hint", "f_reply_marker", "f_has_url",
    "c_sender_known", "c_sender_count_log", "c_sender_reply_rate", "c_internal",
    "t_hour", "t_weekday", "t_age_days",
]

FEATURE_BLURB = {
    "f_body_len": "body length (chars)",
    "f_subject_len": "subject length",
    "f_question_marks": "question marks",
    "f_exclaim_marks": "exclamation marks",
    "f_caps_words": "ALL-CAPS words",
    "f_n_recipients": "recipient count",
    "f_direct_recipient": "addressed to the user",
    "f_contains_money": "mentions amounts/currency",
    "f_has_dates": "mentions dates",
    "f_contains_request": "contains a request",
    "f_deadline_lang": "deadline language",
    "f_automated_hint": "automated sender indicators",
    "f_newsletter_hint": "newsletter indicators",
    "f_reply_marker": "Re:/Fwd: subject marker",
    "f_has_url": "contains links",
    "c_sender_known": "sender seen before",
    "c_sender_count_log": "sender history (log count)",
    "c_sender_reply_rate": "sender's historical needs-reply rate",
    "c_internal": "internal sender (same domain)",
    "t_hour": "arrival hour",
    "t_weekday": "arrival weekday",
    "t_age_days": "age at decision time (days)",
}

# ---------------------------------------------------------------- label sources
# Ordering = training weight. A prediction is NOT ground truth; the LLM's own
# annotations are weak labels, and even strong sources are down-weighted when
# the sample is old (w_age below).
LABEL_SOURCES = {
    "explicit_user_correction": 4.0,
    "explicit_user_label": 3.0,
    "deterministic_event": 2.0,
    "high_precision_rule": 2.0,
    "established_model": 1.5,
    "llm_annotation": 1.0,
    "inferred_behavior": 0.7,
}


def label_weight(source):
    return float(LABEL_SOURCES.get(source, 1.0))


# ---------------------------------------------------------------- model kinds
KINDS = {}


def register_kind(name, train_fn, predict_fn, describe_fn, blurb=""):
    KINDS[name] = {"train": train_fn, "predict": predict_fn, "describe": describe_fn, "blurb": blurb}


def kind_names():
    return sorted(KINDS)


def train(kind, examples, params=None):
    if kind not in KINDS:
        raise RuntimeError("unknown kind %r (have: %s)" % (kind, ", ".join(kind_names())))
    return KINDS[kind]["train"](examples, params or {})


def predict(kind, model, feats):
    fn = (KINDS.get(kind) or {}).get("predict")
    return fn(model, feats) if fn else None


def describe(kind, model, limit=8):
    fn = (KINDS.get(kind) or {}).get("describe")
    return fn(model, limit) if fn else ""


def _sigmoid(z):
    if z < -30:
        return 0.0
    if z > 30:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def _standardize_fit(X):
    n = len(X) or 1
    means = [sum(row[j] for row in X) / n for j in range(len(X[0]))]
    stds = []
    for j in range(len(X[0])):
        var = sum((row[j] - means[j]) ** 2 for row in X) / n
        stds.append(math.sqrt(var) or 1.0)
    return means, stds


def _row_x(model, feats):
    return [float(feats.get(name, 0.0)) for name in model["features"]]


def _scaled(model, x):
    return [(x[j] - model["mean"][j]) / model["std"][j] for j in range(len(x))]


def _fit_binary(xs, ys, ws, params, dim):
    """Full-batch gradient descent on pre-scaled rows -> (w, b, iters, log_loss)."""
    iters = int(params.get("iters") or 120)
    lr0 = float(params.get("lr") or 0.25)
    l2 = float(params.get("l2") or 0.001)
    n = len(xs)
    w = [0.0] * dim
    b = 0.0
    wsum = sum(ws) or 1.0
    prev_loss = None
    used = 0
    for it in range(iters):
        grad = [0.0] * dim
        gb = 0.0
        loss = 0.0
        for i in range(n):
            x = xs[i]
            z = b
            for j in range(dim):
                z += w[j] * x[j]
            p = _sigmoid(z)
            err = (p - ys[i]) * ws[i]
            for j in range(dim):
                grad[j] += err * x[j]
            gb += err
            if ys[i] > 0.5:
                loss -= ws[i] * math.log(max(p, 1e-9))
            else:
                loss -= ws[i] * math.log(max(1.0 - p, 1e-9))
        loss = loss / wsum + l2 * sum(v * v for v in w)
        lr = lr0 / (1.0 + 0.02 * it)
        for j in range(dim):
            w[j] -= lr * (grad[j] / wsum + l2 * w[j])
        b -= lr * (gb / wsum)
        used = it + 1
        if prev_loss is not None and abs(prev_loss - loss) < 1e-5:
            break
        prev_loss = loss
    return w, b, used, prev_loss


def train_logreg(examples, params):
    """examples: [(y 0/1, weight, feats-dict)]. Deterministic L2 logistic
    regression; model = JSON weights + bias + scaler (no pickle, auditable)."""
    feats_list = params.get("features") or FEATURE_ORDER
    X, Y, W = [], [], []
    for y, w, feats in examples:
        X.append([float(feats.get(n, 0.0)) for n in feats_list])
        Y.append(1.0 if y else 0.0)
        W.append(max(0.0, float(w)))
    if not X:
        raise RuntimeError("no training examples")
    mean, std = _standardize_fit(X)
    Xs = [[(row[j] - mean[j]) / std[j] for j in range(len(row))] for row in X]
    w, b, used_iters, prev_loss = _fit_binary(Xs, Y, W, params, len(feats_list))
    model = {"kind": "logreg", "features": list(feats_list), "weights": [round(v, 6) for v in w],
             "bias": round(b, 6), "mean": [round(v, 6) for v in mean],
             "std": [round(v, 6) for v in std], "trained_at": int(time.time()),
             "samples": len(X), "iters": used_iters, "log_loss": round(prev_loss or 0, 6)}
    stats = {"samples": len(X), "features": len(feats_list), "iters": used_iters,
             "log_loss": round(prev_loss or 0, 6), "kind": "logreg"}
    return model, stats


def predict_logreg(model, feats):
    x = _row_x(model, feats)
    xs = _scaled(model, x)
    z = float(model.get("bias") or 0.0)
    for j, name in enumerate(model["features"]):
        z += float(model["weights"][j]) * xs[j]
    p = _sigmoid(z)
    pred = p >= 0.5
    contrib = {}
    for j, name in enumerate(model["features"]):
        c = float(model["weights"][j]) * xs[j]
        if abs(c) >= 0.02:
            contrib[name] = round(c, 3)
    return {"prediction": pred, "proba": round(p, 4),
            "confidence": round(max(p, 1.0 - p), 4),
            "contributions": contrib}


def describe_logreg(model, limit=8):
    if not model or model.get("kind") != "logreg":
        return "(empty model)"
    pairs = sorted(zip(model["features"], model["weights"]), key=lambda kv: -abs(kv[1]))[:limit]
    return "; ".join("%s %+0.2f" % (n, w) for n, w in pairs)


register_kind("logreg", train_logreg, predict_logreg, describe_logreg,
              "L2 logistic regression, standardized features, JSON weights (interpretable)")


def train_logreg_ovr(examples, params):
    """One-vs-rest logistic regression for multi-class tasks. examples:
    [(label-str, weight, feats)]. One shared scaler; one JSON weight vector per
    class - the artifact stays inspectable end to end."""
    feats_list = params.get("features") or FEATURE_ORDER
    labels, X, W = [], [], []
    for lab, w, feats in examples:
        s = str(lab)
        if s not in labels:
            labels.append(s)
        X.append([float(feats.get(n, 0.0)) for n in feats_list])
        W.append(max(0.0, float(w)))
    if not X:
        raise RuntimeError("no training examples")
    labels.sort()
    mean, std = _standardize_fit(X)
    Xs = [[(row[j] - mean[j]) / std[j] for j in range(len(row))] for row in X]
    dim = len(feats_list)
    models = {}
    for lab in labels:
        ys = [1.0 if str(e[0]) == lab else 0.0 for e in examples]
        w, b, used, ll = _fit_binary(Xs, ys, W, params, dim)
        models[lab] = {"weights": [round(v, 6) for v in w], "bias": round(b, 6),
                       "iters": used, "log_loss": round(ll or 0, 6)}
    model = {"kind": "logreg_ovr", "features": list(feats_list), "classes": labels,
             "models": models, "mean": [round(v, 6) for v in mean],
             "std": [round(v, 6) for v in std], "trained_at": int(time.time()),
             "samples": len(X)}
    stats = {"samples": len(X), "classes": len(labels), "features": dim, "kind": "logreg_ovr"}
    return model, stats


def predict_logreg_ovr(model, feats):
    x = _row_x(model, feats)
    xs = [(x[j] - model["mean"][j]) / model["std"][j] for j in range(len(x))]
    best_lab, best_p = None, -1.0
    for lab in model.get("classes") or []:
        m = model["models"][lab]
        z = float(m.get("bias") or 0.0)
        for j, _name in enumerate(model["features"]):
            z += float(m["weights"][j]) * xs[j]
        p = _sigmoid(z)
        if p > best_p:
            best_lab, best_p = lab, p
    if best_lab is None:
        return None
    m = model["models"][best_lab]
    contrib = {}
    for j, name in enumerate(model["features"]):
        c = float(m["weights"][j]) * xs[j]
        if abs(c) >= 0.05:
            contrib[name] = round(c, 3)
    return {"prediction": best_lab, "proba": round(best_p, 4),
            "confidence": round(best_p, 4), "contributions": contrib}


def describe_logreg_ovr(model, limit=6):
    if not model or model.get("kind") != "logreg_ovr":
        return "(empty model)"
    return "one-vs-rest logreg over %d classes: %s" % (
        len(model.get("classes") or []), ", ".join((model.get("classes") or [])[:limit]))


register_kind("logreg_ovr", train_logreg_ovr, predict_logreg_ovr, describe_logreg_ovr,
              "one-vs-rest logistic regression over JSON weight vectors (multi-class)")

# ---------------------------------------------------------------- metrics


def binary_metrics(y_true, probs, threshold=0.5):
    tp = fp = tn = fn = 0
    for y, p in zip(y_true, probs):
        pred = p >= threshold
        if y >= 0.5 and pred:
            tp += 1
        elif y >= 0.5:
            fn += 1
        elif pred:
            fp += 1
        else:
            tn += 1
    n = tp + fp + tn + fn
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else None
    acc = (tp + tn) / n if n else None
    brier = (sum((p - y) ** 2 for y, p in zip(y_true, probs)) / n) if n else None
    return {"n": n, "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "precision": _r(prec), "recall": _r(rec), "f1": _r(f1), "accuracy": _r(acc),
            "brier": _r(brier), "threshold": threshold}


def average_precision(y_true, probs):
    """Binary average precision (area under precision-recall, step integral)."""
    if not y_true:
        return None
    pairs = sorted(zip(probs, y_true), key=lambda t: -t[0])
    pos = sum(1 for y in y_true if y >= 0.5)
    if not pos:
        return None
    tp = 0
    ap = 0.0
    prev_recall = 0.0
    for i, (p, y) in enumerate(pairs):
        if y >= 0.5:
            tp += 1
            recall = tp / pos
            prec = tp / (i + 1)
            ap += prec * (recall - prev_recall)
            prev_recall = recall
    return _r(ap)


def calibration(y_true, probs, bins=5):
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        sel = [(y, p) for y, p in zip(y_true, probs) if (p >= lo and (p < hi or i == bins - 1))]
        if not sel:
            continue
        out.append({"lo": lo, "hi": hi, "n": len(sel),
                    "avg_conf": _r(sum(p for _y, p in sel) / len(sel)),
                    "pos_rate": _r(sum(1 for y, _p in sel if y >= 0.5) / len(sel))})
    return out


def _r(v):
    return None if v is None else round(float(v), 4)


def evaluate_probs(y_true, probs):
    m = binary_metrics(y_true, probs)
    m["avg_precision"] = average_precision(y_true, probs)
    m["calibration"] = calibration(y_true, probs)
    return m


def multiclass_metrics(y_true, y_pred):
    """Accuracy + per-class precision/recall/F1 for multi-class tasks."""
    classes = sorted(set(y_true) | set(y_pred))
    acc = {c: {"tp": 0, "fp": 0, "fn": 0} for c in classes}
    correct = 0
    for y, p in zip(y_true, y_pred):
        if y == p:
            correct += 1
        for c in classes:
            if p == c and y == c:
                acc[c]["tp"] += 1
            elif p == c:
                acc[c]["fp"] += 1
            elif y == c:
                acc[c]["fn"] += 1
    per = {}
    f1s = []
    for c in classes:
        tp, fp, fn = acc[c]["tp"], acc[c]["fp"], acc[c]["fn"]
        prec = tp / (tp + fp) if (tp + fp) else None
        rec = tp / (tp + fn) if (tp + fn) else None
        f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else None
        if f1 is not None:
            f1s.append(f1)
        per[c] = {"precision": _r(prec), "recall": _r(rec), "f1": _r(f1), "n": tp + fn}
    n = len(y_true)
    return {"n": n, "accuracy": _r(correct / n if n else None),
            "macro_f1": _r(sum(f1s) / len(f1s) if f1s else None),
            "classes": per}


# ---------------------------------------------------------------- tasks
# One entry per learning task. Task config is deliberately small and explicit;
# routing thresholds are policy, evaluated in reports before being trusted.

TASKS = {
    "needs_reply": {
        "name": "needs_reply",
        "description": "Does this email need a reply from the user?",
        "weak_label_column": "llm_needs_reply",
        "kind": "logreg",
        "positive": "needs a reply",
        "min_accept": 0.97,   # router: accept specialist without LLM
        "min_verify": 0.75,   # router: moderate confidence -> LLM verify
        "min_samples": 40,
        "holdout": 0.2,
    },
    "category": {
        "name": "category",
        "description": "Which of the user's categories does this email belong to?",
        "weak_label_column": "llm_category",
        "kind": "logreg_ovr",
        "multi": True,        # one-vs-rest over the label set
        "positive": "",
        "min_accept": 0.85,   # router: accept specialist without LLM
        "min_verify": 0.55,   # router: moderate confidence -> LLM verify
        "min_samples": 30,
        "holdout": 0.2,
    },
}

TASK_TITLES = {
    "needs_reply": "Reply detector",
    "category": "Category sorter",
    "priority": "Importance ranker",
    "newsletter": "Newsletter splitter",
}

STATUS_FLOW = {
    "proposed": {"validated", "rejected"},
    "validated": {"shadow", "rejected"},
    "shadow": {"active", "degraded", "rejected", "retired"},
    "active": {"degraded", "retired"},
    "degraded": {"shadow", "active", "retired"},
    "rejected": {"retired"},
    "retired": set(),
}


def build_dataset(task, limit=6000):
    """(samples, meta) for a task. Labels: the `labels` table wins when present
    (stronger source); otherwise the LLM's stored verdict is used as a WEAK
    label (llm_annotation). Every sample carries its provenance + weight."""
    t = TASKS.get(task)
    if not t:
        raise RuntimeError("unknown task %r" % task)
    col = t["weak_label_column"]
    multi = bool(t.get("multi"))
    # raw scan on purpose: this is training data, so list filters (snooze etc.)
    # must not silently drop samples
    if multi:
        sql = ("SELECT id, from_addr, to_addr, subject, snippet, date_ts, sort_ts, processed_at, "
               "llm_category FROM messages WHERE coalesce(llm_category,'')!='' "
               "ORDER BY id DESC LIMIT ?")
        args = (int(limit),)
    else:
        sql = ("SELECT id, from_addr, to_addr, subject, snippet, date_ts, sort_ts, processed_at, "
               "llm_category, %s FROM messages "
               "WHERE coalesce(llm_category,'')!='' AND %s IN (0,1) "
               "ORDER BY id DESC LIMIT ?" % (col, col))
        args = (int(limit),)
    with store.db() as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
    labels_by_msg = {}
    try:
        for lab in store.list_labels(task=task, limit=20000):
            labels_by_msg.setdefault(lab["msg_id"], []).append(lab)
    except Exception:
        pass
    samples, meta = [], []
    for r in rows:
        raw = r["llm_category"] if multi else r[col]
        strong = labels_by_msg.get(r["id"]) or []
        if strong:
            best = max(strong, key=lambda l: label_weight(l["source"]))
            src = best["source"]
            try:
                val = json.loads(best["label"])
            except (TypeError, ValueError):
                continue
            val = str(val) if multi else (1 if val in (True, 1, "1") else 0)
            weight = label_weight(src)
        else:
            src, weight = "llm_annotation", label_weight("llm_annotation")
            val = str(raw) if multi else int(raw)
        feats = extract_features(r)
        samples.append({"msg_id": r["id"], "label": val, "weight": weight, "source": src,
                        "ts": _ts_of(r), "feats": feats})
        meta.append((src, val))
    samples.sort(key=lambda s: s["ts"] or 0)
    out_meta = {"n": len(samples),
                "by_source": {s: sum(1 for x, _v in meta if x == s)
                              for s in sorted(set(x for x, _v in meta))}}
    if multi:
        out_meta["classes"] = sorted(set(v for _s, v in meta))
    else:
        out_meta["positives"] = sum(1 for _s, v in meta if v == 1)
    return samples, out_meta


def _split(samples, holdout):
    n = len(samples)
    cut = max(1, n - max(10, int(n * holdout)))
    return samples[:cut], samples[cut:]


def _eval_chunk(t, kind, model, chunk):
    """Metrics for one split: binary -> threshold metrics, multi -> accuracy +
    per-class P/R/F1."""
    ys, preds, probs = [], [], []
    for s in chunk:
        res = predict(kind, model, s["feats"])
        if res is None:
            continue
        ys.append(s["label"])
        preds.append(res["prediction"])
        probs.append(res.get("proba"))
    if not ys:
        return {"n": 0}
    if t.get("multi"):
        return multiclass_metrics(ys, preds)
    return evaluate_probs([int(bool(y)) for y in ys], probs)


def _fit_and_eval(kind, samples, params):
    t = TASKS[params.get("task") or "needs_reply"]
    train_rows, val_rows = _split(samples, t["holdout"])
    ex = [(s["label"], s["weight"], s["feats"]) for s in train_rows]
    model, stats = train(kind, ex, params)
    out = {"train": {}, "val": {}, "stats": stats}
    for name, chunk in (("train", train_rows), ("val", val_rows)):
        out[name] = _eval_chunk(t, kind, model, chunk) if chunk else {"n": 0}
    return model, out


def train_specialist(task, kind="", name="", params=None, limit=6000, created_by="ui"):
    """Train + VALIDATE a candidate. Stored as a new version with status
    `validated` (enabled=0): it must be deployed to shadow explicitly, and can
    never jump straight to active from here."""
    t = TASKS.get(task)
    if not t:
        raise RuntimeError("unknown task %r (have: %s)" % (task, ", ".join(TASKS)))
    kind = kind or t.get("kind") or "logreg"
    if kind not in KINDS:
        raise RuntimeError("unknown kind %r (have: %s)" % (kind, ", ".join(kind_names())))
    samples, meta = build_dataset(task, limit=limit)
    if meta["n"] < t["min_samples"]:
        raise RuntimeError("only %d labelled sample(s) for %r - need >= %d "
                           "(classify more mail or record corrections first)"
                           % (meta["n"], task, t["min_samples"]))
    params = dict(params or {})
    params.setdefault("task", task)
    model, ev = _fit_and_eval(kind, samples, params)
    name = name or ("%s_%s" % (task, kind))
    version = store.next_specialist_version(name)
    labels_needed = sum(1 for s in samples if s["source"] != "llm_annotation")
    stats = {"dataset": meta, "split": {"train": ev["train"].get("n"), "val": ev["val"].get("n")},
             "weak_labels": meta["n"] - labels_needed, "confirmed_labels": labels_needed,
             "trained_at": int(time.time()), "trained_by": created_by,
             "params": {k: params[k] for k in sorted(params)}}
    sid = store.add_specialist(
        name=name, task=task, kind=kind, version=version, status="validated",
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        model=json.dumps(model), stats=json.dumps(stats), metrics=json.dumps(ev),
        min_confidence=float(t["min_accept"]), enabled=False, created_by=created_by)
    store.log_event("info", "learning: trained %s %s v%d on %d samples (val %s) - status validated"
                    % (task, kind, version, meta["n"],
                       ev["val"].get("f1") if ev["val"].get("f1") is not None else ev["val"].get("accuracy")))
    return {"specialist_id": sid, "name": name, "version": version, "task": task, "kind": kind,
            "dataset": meta, "val": ev["val"], "train": ev["train"]}


def evaluate_specialist(sid, limit=6000):
    """Re-evaluate a stored specialist against the current dataset."""
    row = store.get_specialist(sid)
    if not row:
        raise RuntimeError("specialist %s not found" % sid)
    t = TASKS.get(row["task"])
    if not t:
        raise RuntimeError("specialist task %r has no task config" % row["task"])
    samples, meta = build_dataset(row["task"], limit=limit)
    if not samples:
        return {"n": 0}
    model = json.loads(row["model"] or "{}")
    train_rows, val_rows = _split(samples, t["holdout"])
    out = {"dataset": meta}
    for name, chunk in (("train", train_rows), ("val", val_rows)):
        out[name] = _eval_chunk(t, row["kind"], model, chunk) if chunk else {"n": 0}
    return out


def transition(sid, to, reason="", by="ui"):
    """State machine transition; PROPOSED/VALIDATED candidates can never become
    ACTIVE directly - they must go through SHADOW first. Logged, always."""
    row = store.get_specialist(sid)
    if not row:
        raise RuntimeError("specialist %s not found" % sid)
    frm = row.get("status") or "proposed"
    if to not in STATUS_FLOW.get(frm, set()):
        raise RuntimeError("illegal transition %s -> %s" % (frm, to))
    enabled = 1 if to in ("shadow", "active", "degraded") else 0
    fields = {"status": to, "enabled": enabled}
    if to in ("shadow", "active", "degraded"):
        # one running version per task: anything already running is superseded
        for other in store.list_specialists(task=row["task"],
                                            statuses=("shadow", "active", "degraded")):
            if other["id"] != row["id"]:
                store.update_specialist(other["id"], status="retired", enabled=0,
                                        superseded_by=row["id"])
                store.log_event("info", "learning: specialist #%s '%s' retired (superseded by #%s)"
                                % (other["id"], other["name"], row["id"]))
    store.update_specialist(sid, **fields)
    store.log_event("info", "learning: specialist #%s '%s' v%s %s -> %s%s"
                    % (sid, row["name"], row["version"], frm, to,
                       (" (%s)" % reason) if reason else ""))
    return True


def enabled_specialists(task=None):
    rows = store.list_specialists(task=task, enabled_only=True, statuses=("shadow", "active", "degraded"))
    return rows

# ---------------------------------------------------------------- routing
# The classify call today answers TWO tasks at once: `category` + `needs_reply`.
# The router may skip that LLM call only when EVERY task it answers is covered by
# a confident cheap decision. In v1 only `needs_reply` has a specialist and
# `category` coverage comes from the heuristics - so routing is logged ("what
# WOULD happen"), and in `enforce` mode the one safe effect is filling the
# needs_reply slot on the heuristic path (which otherwise hard-codes False).

ROUTE_LLM = "llm"
ROUTE_VERIFY = "verify"
ROUTE_SPECIALISTS = "specialists"


def call_task_policy():
    """The tasks ONE classify call answers, with their routing thresholds."""
    return {t: {"min_verify": TASKS[t]["min_verify"], "min_accept": TASKS[t]["min_accept"]}
            for t in ("category", "needs_reply")}


def route_decision(task_policy, coverage):
    """coverage: {task: {"by": 'specialist'|'heuristic', "confidence": c}}."""
    missing, moderate = [], []
    for task, policy in task_policy.items():
        c = coverage.get(task)
        if not c:
            missing.append(task)
            continue
        conf = float(c.get("confidence") or 0)
        if conf < policy.get("min_verify", 0.75):
            missing.append(task)
        elif conf < policy.get("min_accept", 0.97):
            moderate.append(task)
    if missing:
        return ROUTE_LLM, missing
    if moderate:
        return ROUTE_VERIFY, moderate
    return ROUTE_SPECIALISTS, []


# ---------------------------------------------------------------- runtime hooks

def observe(msg_id, event_type, event_value="", source="ui"):
    """Record an objective observation ('user did X'). Never raises."""
    try:
        store.record_observation(msg_id, event_type, event_value, source)
    except Exception:
        pass


def observe_classification(msg, fields, res, hres, settings):
    """Shadow hook, called by engine.classify_and_store after the verdict is
    stored. Records: (a) specialist decisions (shadow=1 unless acting live),
    (b) the system's own decision for the same task, (c) the router's would-be
    route for the whole call. Never raises; never changes behavior unless
    settings say `learning_route_mode == enforce` and the specialist is active."""
    if not settings.get("learning_enabled", True):
        return None
    try:
        msg_id = int(msg["id"])
        specs = enabled_specialists()
        feats = extract_features(msg)
        mode = settings.get("learning_route_mode") or "shadow"
        best = {}
        for spec in specs:
            try:
                model = json.loads(spec["model"] or "{}")
            except (TypeError, ValueError):
                continue
            out = predict(spec["kind"], model, feats)
            if out is None:
                continue
            acting = (mode == "enforce" and spec["status"] == "active")
            did = store.record_decision(
                msg_id, spec["task"], json.dumps(out["prediction"]), out["confidence"],
                "specialist", "%s@v%d" % (spec["name"], spec["version"]),
                model_version=str(spec["version"]), feature_version=FEATURE_SCHEMA_VERSION,
                shadow=0 if acting else 1, routed=("" if acting else ROUTE_LLM))
            top = sorted(out["contributions"].items(), key=lambda kv: -abs(kv[1]))[:8]
            store.record_decision_evidence(did, [(n, json.dumps(feats.get(n)), c) for n, c in top])
            prev = best.get(spec["task"])
            if not prev or out["confidence"] > prev["out"]["confidence"]:
                best[spec["task"]] = {"spec": spec, "out": out, "decision_id": did, "acting": acting}
        # (b) the system's decision per task (provenance for agreement)
        sys_src = ("heuristic:%s %s" % (hres["heuristic_id"], hres["heuristic_name"])) if hres else "llm"
        sys_by = "heuristic" if hres else "llm"
        sys_nr = bool(res.get("needs_reply"))
        store.record_decision(msg_id, "needs_reply", json.dumps(sys_nr),
                              float(res.get("confidence") or 0), sys_by, sys_src, shadow=0)
        store.record_decision(msg_id, "category", json.dumps(str(res.get("category") or "")),
                              float(res.get("confidence") or 0), sys_by, sys_src, shadow=0)
        # (c) router intent for the whole classify call
        coverage = {}
        if hres:
            coverage["category"] = {"by": "heuristic", "confidence": float(hres.get("confidence") or 0)}
        for task in ("category", "needs_reply"):
            c = best.get(task)
            if c and (task not in coverage or c["out"]["confidence"] > coverage[task]["confidence"]):
                coverage[task] = {"by": "specialist", "confidence": c["out"]["confidence"]}
        would, detail = route_decision(call_task_policy(), coverage)
        store.record_decision(msg_id, "route", json.dumps(would),
                              min([c["confidence"] for c in coverage.values()]) if coverage else 0.0,
                              "router", "route_v1", shadow=1,
                              routed=json.dumps({"missing": detail}) if detail else "")
        # enforce: fill the needs_reply slot the heuristic path hard-codes False
        nr = best.get("needs_reply")
        if (mode == "enforce" and hres and nr and nr["acting"]
                and nr["out"]["confidence"] >= TASKS["needs_reply"]["min_accept"]):
            if sys_nr != nr["out"]["prediction"]:
                store.update_message(msg_id, llm_needs_reply=1 if nr["out"]["prediction"] else 0)
                store.log_msg_event(msg_id, "specialist",
                                    "needs_reply set by %s@v%d (%.2f) on the heuristic path"
                                    % (nr["spec"]["name"], nr["spec"]["version"],
                                       nr["out"]["confidence"]))
        return {"specialists": {t: best[t]["out"] for t in best}}
    except Exception as exc:
        try:
            store.log_event("debug", "learning: observe failed for msg %s: %r" % (msg.get("id"), exc))
        except Exception:
            pass
        return None


def system_decision(msg_id, task="needs_reply"):
    with store.db() as conn:
        row = conn.execute(
            "SELECT * FROM decisions WHERE msg_id=? AND task=? AND source_type IN ('llm','heuristic') "
            "ORDER BY id DESC LIMIT 1", (int(msg_id), task)).fetchone()
    return dict(row) if row else None


def specialist_live_stats(name=None, sid=None, window=500):
    """Shadow agreement vs the system's own decision, from stored evidence."""
    row = None
    if sid is not None:
        row = store.get_specialist(sid)
    elif name:
        nm = str(name)
        with store.db() as conn:
            if "@v" in nm:
                base, _sep, ver = nm.rpartition("@v")
                try:
                    r = conn.execute("SELECT * FROM specialists WHERE name=? AND version=?",
                                     (base, int(ver))).fetchone()
                except (TypeError, ValueError):
                    r = None
            else:
                r = conn.execute("SELECT * FROM specialists WHERE name=? "
                                 "ORDER BY version DESC LIMIT 1", (nm,)).fetchone()
        row = dict(r) if r else None
    if not row:
        return {"n": 0}
    task = row["task"]
    name = "%s@v%d" % (row["name"], row["version"])
    decs = store.list_decisions(task=task, source_type="specialist",
                                source_id=name, limit=window)
    n = agree = 0
    for d in decs:
        sysd = system_decision(d["msg_id"], task)
        if not sysd:
            continue
        n += 1
        try:
            agree += 1 if json.loads(d["predicted_value"]) == json.loads(sysd["predicted_value"]) else 0
        except (TypeError, ValueError):
            pass
    return {"n": n, "agree": agree, "agreement": round(agree / n, 4) if n else None,
            "window": window, "last_at": (decs[0]["ts"] if decs else None)}


def routing_stats(window=500):
    decs = store.list_decisions(task="route", source_type="router", limit=window)
    counts = {ROUTE_LLM: 0, ROUTE_VERIFY: 0, ROUTE_SPECIALISTS: 0}
    for d in decs:
        try:
            counts[json.loads(d["predicted_value"])] = counts.get(json.loads(d["predicted_value"]), 0) + 1
        except (TypeError, ValueError):
            counts[ROUTE_LLM] = counts.get(ROUTE_LLM, 0) + 1
    n = len(decs)
    return {"window": window, "n": n, "counts": counts,
            "escalation_rate": round(counts[ROUTE_LLM] / n, 4) if n else None,
            "would_skip_rate": round(counts[ROUTE_SPECIALISTS] / n, 4) if n else None}


def recent_disagreements(limit=15):
    """Shadow specialist vs system disagreements, across every running model."""
    out = []
    for spec in enabled_specialists():
        key = "%s@v%d" % (spec["name"], spec["version"])
        for d in store.list_decisions(task=spec["task"], source_type="specialist",
                                      source_id=key, limit=limit * 3):
            sysd = system_decision(d["msg_id"], spec["task"])
            if not sysd:
                continue
            try:
                a, b = json.loads(d["predicted_value"]), json.loads(sysd["predicted_value"])
            except (TypeError, ValueError):
                continue
            if a != b:
                row = store.get_message(d["msg_id"]) or {}
                out.append({"msg_id": d["msg_id"], "task": spec["task"],
                            "specialist": a, "specialist_conf": d["confidence"],
                            "system": b, "system_source": sysd["source_id"], "ts": d["ts"],
                            "source_id": d["source_id"],
                            "subject": (row.get("subject") or "")[:90],
                            "from_addr": (row.get("from_addr") or "")[:70]})
            if len(out) >= limit * 2:
                break
    out.sort(key=lambda x: -(x["ts"] or 0))
    return out[:limit]


def reconcile(limit=5000):
    """Materialize labels from signals already in the app: user tags are
    explicit labels; dataset relabels (classified_by='user') are explicit
    corrections. Idempotent - rows are INSERT OR IGNORE."""
    tagged = corrected = 0
    for r in store.tagged_examples(limit):
        tag = (r.get("user_tag") or "").strip().lower()
        by = (r.get("user_tag_by") or "").strip()
        # only tags the USER set count as explicit labels: assistant tags are
        # quarantined by tagged_examples, and flow-applied tags currently carry
        # no provenance marker (audit gap) - they must not be trusted here
        if by == "user" and tag and store.record_label(r["id"], "category", tag, 1.0,
                                                       "explicit_user_label", "user_tag"):
            tagged += 1
    with store.db() as conn:
        corr = [dict(r) for r in conn.execute(
            "SELECT id, llm_category FROM messages WHERE classified_by='user' "
            "AND coalesce(llm_category,'')!='' ORDER BY id DESC LIMIT ?", (int(limit),))]
    for r in corr:
        if store.record_label(r["id"], "category", json.dumps(r["llm_category"]), 1.0,
                              "explicit_user_correction", "dataset relabel"):
            corrected += 1
    return {"tagged": tagged, "corrected": corrected}


# ---------------------------------------------------------------- reporting

def classifier_models():
    """Tag/classified-trained fast-paths (the heuristics registry) presented in
    the same shape as specialists, so the page can show one unified list."""
    out = []
    try:
        rows = store.list_heuristics()
    except Exception:
        return out
    for h in rows:
        try:
            stats = json.loads(h.get("stats") or "{}")
        except (TypeError, ValueError):
            stats = {}
        acc = None
        try:
            acc = (heuristics.evaluate_heuristic(h, limit=600) or {}).get("accuracy")
        except Exception:
            acc = None
        out.append({
            "id": h["id"],
            "name": h.get("name") or ("classifier %s" % h["id"]),
            "job": h.get("category") or "",
            "kind": h.get("kind") or "",
            "status": "live" if h.get("enabled") else "paused",
            "samples": stats.get("samples"),
            "labels": stats.get("trained_label_count"),
            "weak": bool(stats.get("weak_labels")),
            "source": stats.get("source") or "",
            "trained_at": stats.get("trained_at"),
            "accuracy": acc,
        })
    out.sort(key=lambda c: (c["status"] != "live", c["id"]))
    return out


def proposals():
    """Data-driven 'what could be trained next' for the Learning page: what has
    labels, what it would buy, and what is blocked on missing signal. Nothing
    here trains anything - this is the discovery layer's first honest step."""
    specs = store.list_specialists()

    def state(t):
        rows = [x for x in specs if x["task"] == t]
        if any(x["status"] in ("shadow", "active", "degraded") for x in rows):
            return "watching"
        return "ready"

    with store.db() as conn:
        n_cat = conn.execute("SELECT COUNT(*) FROM messages "
                             "WHERE coalesce(llm_category,'')!=''").fetchone()[0]
        n_news = conn.execute("SELECT COUNT(*) FROM messages "
                              "WHERE llm_category='Newsletter'").fetchone()[0]
        n_fdbk = (conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                  + conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0])
    out = []
    if state("category") != "watching":
        out.append({"task": "category", "title": TASK_TITLES.get("category", "category"),
                    "status": "ready", "trainable": n_cat >= TASKS["category"]["min_samples"],
                    "evidence": "%s labelled emails · 6 categories" % "{:,}".format(n_cat),
                    "why": "the other half of the AI's job — with the reply detector, "
                           "confident emails could skip the AI entirely."})
    if state("newsletter") != "watching":
        out.append({"task": "newsletter", "title": TASK_TITLES.get("newsletter", "newsletter"),
                    "status": "ready" if n_news >= 100 else "blocked", "trainable": False,
                    "evidence": "%s newsletter examples" % "{:,}".format(n_news),
                    "why": "low gain — the existing fast-path rules already catch most "
                           "newsletters. Train only if those start slipping."})
    out.append({"task": "priority", "title": TASK_TITLES.get("priority", "priority"),
                "status": "blocked", "trainable": False,
                "evidence": "%s corrections so far" % "{:,}".format(n_fdbk),
                "why": ("no signal yet — tag, reclassify or undo the mail that matters; "
                        "your corrections make this trainable.") if n_fdbk < 150 else
                       "signal collected — the ranker itself is the next milestone."})
    return out


def status_report():
    specs = store.list_specialists()
    for s in specs:
        try:
            s["metrics_parsed"] = json.loads(s.get("metrics") or "{}")
        except (TypeError, ValueError):
            s["metrics_parsed"] = {}
        try:
            s["stats_parsed"] = json.loads(s.get("stats") or "{}")
        except (TypeError, ValueError):
            s["stats_parsed"] = {}
        s["live"] = specialist_live_stats(s.get("name"), s["id"]) if s.get("enabled") else {"n": 0}
    label_sources = {}
    try:
        with store.db() as conn:
            for r in conn.execute("SELECT source, COUNT(*) n FROM labels GROUP BY source"):
                label_sources[r["source"]] = r["n"]
    except Exception:
        pass
    current = None
    srank = {"active": 0, "shadow": 1, "degraded": 2}
    trank = {"needs_reply": 0, "category": 1}
    running = [x for x in specs if x["status"] in srank]
    running.sort(key=lambda x: (srank[x["status"]], trank.get(x["task"], 9), x["id"]))
    if running:
        current = running[0]
    elif specs:
        current = specs[0]
    classifiers = classifier_models()
    return {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "route_mode": store.get_setting("learning_route_mode", "shadow"),
        "enabled": bool(store.get_setting("learning_enabled", True)),
        "specialists": specs,
        "current": current,
        "classifiers": classifiers,
        "counts": {
            "classifiers_live": sum(1 for c in classifiers if c["status"] == "live"),
            "learners_running": len(running),
        },
        "proposals": proposals(),
        "routing": routing_stats(),
        "library": {
            "decisions": store.count_decisions(),
            "observations": store.count_observations(),
            "labels": store.count_labels(),
            "labels_by_source": label_sources,
        },
        "disagreements": recent_disagreements(),
        "firewall": "shadow specialists never influence live behavior (route_mode=shadow)",
    }


# ---------------------------------------------------------------- CLI

def _cli(argv):
    store.init_db()  # idempotent; the CLI may be the first thing to touch a DB copy
    if not argv or argv[0] == "report":
        rep = status_report()
        if "--json" in argv:
            print(json.dumps(rep, indent=1))
            return 0
        print("learning loop - feature schema v%d, route mode=%s, enabled=%s"
              % (rep["feature_schema_version"], rep["route_mode"], rep["enabled"]))
        print("library: %d decisions, %d observations, %d labels" % (
            rep["library"]["decisions"], rep["library"]["observations"], rep["library"]["labels"]))
        r = rep["routing"]
        print("routing (last %d): escalation=%.1f%% would-skip=%.1f%% %s"
              % (r["window"], 100 * (r["escalation_rate"] or 0), 100 * (r["would_skip_rate"] or 0),
                 r["counts"]))
        for s in rep["specialists"]:
            m = s["metrics_parsed"].get("val") or {}
            if m.get("classes"):
                score = "acc=%s macroF1=%s" % (m.get("accuracy"), m.get("macro_f1"))
            else:
                score = "P=%s R=%s F1=%s" % (m.get("precision"), m.get("recall"), m.get("f1"))
            print("#%s %-24s %s v%s [%s] val: %s n=%s live: %s"
                  % (s["id"], s["name"], s["task"], s["version"], s["status"],
                     score, m.get("n"), s["live"]))
        return 0
    if argv[0] == "train":
        task = argv[1] if len(argv) > 1 else "needs_reply"
        res = train_specialist(task, created_by="cli")
        v = res["val"]
        if v.get("classes"):
            score = "accuracy=%s macro_f1=%s" % (v.get("accuracy"), v.get("macro_f1"))
        else:
            score = "precision=%s recall=%s f1=%s" % (v.get("precision"), v.get("recall"), v.get("f1"))
        print("trained %s v%d (#%d): val %s n=%s classes=%s"
              % (res["name"], res["version"], res["specialist_id"], score, v.get("n"),
                 len(v.get("classes") or []) or ""))
        print("status=validated - deploy to shadow with: learning.py deploy %d" % res["specialist_id"])
        return 0
    if argv[0] == "deploy":
        sid = int(argv[1])
        transition(sid, "shadow", reason="deployed by CLI", by="cli")
        print("specialist #%d is now SHADOW (recording only)" % sid)
        return 0
    if argv[0] == "promote":
        sid = int(argv[1])
        transition(sid, "active", reason="promoted by CLI", by="cli")
        print("specialist #%d is now ACTIVE" % sid)
        return 0
    if argv[0] == "reconcile":
        print(json.dumps(reconcile(), indent=1))
        return 0
    print("usage: python learning.py [report [--json] | train [task] | deploy <id> | promote <id> | reconcile]")
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
