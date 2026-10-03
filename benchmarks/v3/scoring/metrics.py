"""Pure decision metrics for v3 scoring (WP5).

Everything here operates on plain ``(gold, pred)`` pairs so the same function
can be recomputed inside a bootstrap resample.  Missing/abstained predictions
are **never silently dropped**: a missing category prediction is a miss for the
gold class (it lowers that class's recall) and a missing reply prediction is
treated as "no reply" (so a missed reply becomes a false negative).  That is the
fixed-denominator rule that stops abstention from raising the headline score.
"""


def accuracy(pairs):
    """Fraction of pairs whose prediction equals the single gold label.

    ``pairs`` is a list of ``(gold, pred)`` with non-``None`` gold.  A
    ``None``/missing prediction is simply wrong.
    """
    if not pairs:
        return None
    return sum(1 for gold, pred in pairs if pred is not None and pred == gold) \
        / float(len(pairs))


def confusion(pairs):
    """``{gold_label: {pred_label_or_'__missing__': n}}``."""
    out = {}
    for gold, pred in pairs:
        p = pred if pred is not None else "__missing__"
        out.setdefault(gold, {})
        out[gold][p] = out[gold].get(p, 0) + 1
    return out


def per_category(pairs):
    """Per-label precision/recall/F1/support for the union of gold and
    actually-predicted labels.  Predicted-only labels are included (with zero
    support) so a spurious class is penalised rather than ignored."""
    gold_labels = [g for g, _ in pairs if g is not None]
    pred_labels = [p for _, p in pairs if p is not None]
    labels = sorted(set(gold_labels) | set(pred_labels))
    out = {}
    for label in labels:
        tp = sum(1 for g, p in pairs if g == label and p == label)
        fp = sum(1 for g, p in pairs if g != label and p == label)
        fn = sum(1 for g, p in pairs if g == label and p != label)
        prec = tp / float(tp + fp) if (tp + fp) else 0.0
        rec = tp / float(tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        out[label] = {"support": tp + fn, "precision": prec, "recall": rec,
                      "f1": f1}
    return out


def macro_f1(pairs):
    """Macro-averaged F1 over the union of gold and predicted labels.

    Recomputed from the resampled pairs inside the bootstrap -- never the mean
    of per-row correctness (that would be micro accuracy, not F1).
    """
    cats = per_category(pairs)
    if not cats:
        return None
    return sum(v["f1"] for v in cats.values()) / float(len(cats))


def binary_metrics(pairs):
    """Precision/recall/F1/missed-reply for a binary decision.

    ``pairs`` is a list of ``(gold_bool, pred_bool_or_None)``.  A missing
    prediction is ``False`` ("no reply"), so it can only hurt recall on a
    positive gold and is never removed from the denominator.
    """
    tp = fp = fn = tn = 0
    for gold, pred in pairs:
        actual = bool(pred)
        if gold and actual:
            tp += 1
        elif gold and not actual:
            fn += 1
        elif not gold and actual:
            fp += 1
        else:
            tn += 1
    total = tp + fp + fn + tn
    precision = tp / float(tp + fp) if (tp + fp) else 0.0
    recall = tp / float(tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    missed = fn / float(tp + fn) if (tp + fn) else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "denominator": total,
            "positives": tp + fn, "negatives": tn + fp,
            "precision": precision, "recall": recall, "f1": f1,
            "missed_reply_rate": missed}


def acceptable_accuracy(items):
    """Acceptable-set accuracy: ``items`` is ``(pred, acceptable_labels)``.

    Every item stays in the denominator; a missing prediction is wrong.  This is
    reported *separately* from single-label macro-F1.
    """
    if not items:
        return None
    correct = sum(1 for pred, acceptable in items
                  if pred is not None and pred in (acceptable or []))
    return correct / float(len(items))
