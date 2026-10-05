"""Anti-TF-IDF diversity audit for a generated corpus (WP6, item 8).

The failure mode this guards against is a corpus that *looks* varied per email
but is really one template re-skinned: high cross-document n-gram overlap, a few
sentences reused everywhere, a low unique-n-gram ratio, or a tiny set of
high-document-frequency terms dominating.  The audit reports each of those and
applies a rejection threshold; the pilot packaging records the verdict.

Definitions:

* ``char_jaccard`` / ``word_jaccard`` -- distribution of pairwise Jaccard
  similarity of character 5-grams and word 3-grams across documents.
* ``unique_word_ngram_ratio`` -- share of word n-grams that occur in exactly one
  document (1.0 means every n-gram is unique to its document).
* ``shared_sentences`` -- sentences appearing in two or more documents.
* ``top_features`` -- word unigrams ranked by document frequency; ``top_share``
  is the document frequency of the most common feature divided by document count
  (a proxy for TF-IDF top-feature leakage).
"""
import math
import re
from collections import Counter, defaultdict

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[A-Za-z0-9']+")

DEFAULT_MAX_CHAR_JACCARD = 0.55
DEFAULT_MAX_WORD_JACCARD = 0.35
DEFAULT_MIN_UNIQUE_NGRAM_RATIO = 0.55
DEFAULT_MAX_TOP_FEATURE_SHARE = 0.70


def _words(text):
    return _WORD.findall((text or "").lower())


def _word_ngrams(text, n):
    words = _words(text)
    return set(tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1)))


def _char_ngrams(text, n):
    norm = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return set(norm[i:i + n] for i in range(max(0, len(norm) - n + 1)))


def _sentences(text):
    return [s.strip() for s in _SENT_SPLIT.split((text or "").strip()) if s.strip()]


def _jaccard(a, b):
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / float(union) if union else 0.0


def _distribution(values):
    if not values:
        return {"mean": 0.0, "p50": 0.0, "p90": 0.0, "max": 0.0}
    ordered = sorted(values)
    n = len(ordered)

    def pct(p):
        idx = min(n - 1, max(0, int(math.ceil(p * n)) - 1))
        return ordered[idx]

    return {"mean": round(sum(ordered) / n, 4), "p50": round(pct(0.5), 4),
            "p90": round(pct(0.9), 4), "max": round(ordered[-1], 4)}


def audit_corpus(texts, *, max_char_jaccard=DEFAULT_MAX_CHAR_JACCARD,
                 max_word_jaccard=DEFAULT_MAX_WORD_JACCARD,
                 min_unique_ngram_ratio=DEFAULT_MIN_UNIQUE_NGRAM_RATIO,
                 max_top_feature_share=DEFAULT_MAX_TOP_FEATURE_SHARE,
                 word_n=3, char_n=5, top_k=10):
    """Audit a list of rendered message texts; return a report dict."""
    docs = [t or "" for t in texts]
    n = len(docs)
    char_sets = [_char_ngrams(t, char_n) for t in docs]
    word_sets = [_word_ngrams(t, word_n) for t in docs]

    char_pairs, word_pairs = [], []
    for i in range(n):
        for j in range(i + 1, n):
            char_pairs.append(_jaccard(char_sets[i], char_sets[j]))
            word_pairs.append(_jaccard(word_sets[i], word_sets[j]))

    ngram_docs = Counter()
    for ws in word_sets:
        for gram in ws:
            ngram_docs[gram] += 1
    total = sum(ngram_docs.values())
    unique = sum(1 for c in ngram_docs.values() if c == 1)
    unique_ratio = (unique / float(total)) if total else 1.0

    sent_docs = defaultdict(set)
    for i, t in enumerate(docs):
        for s in set(_sentences(t)):
            if len(s) >= 20:
                sent_docs[s].add(i)
    shared = [{"sentence": s, "docs": len(d), "count": len(d)}
              for s, d in sent_docs.items() if len(d) >= 2]
    shared.sort(key=lambda x: (-x["docs"], -len(x["sentence"])))

    # Top-feature leakage is measured on word n-grams (not raw unigrams, which
    # are dominated by stopwords): a repeated sentence or signature makes the
    # same n-gram appear in many documents.
    term_docs = Counter()
    for gram_set in word_sets:
        for gram in gram_set:
            term_docs[gram] += 1
    top_features = [{"term": " ".join(g), "doc_freq": c, "share": round(c / float(n), 4)}
                    for g, c in term_docs.most_common(top_k)]
    top_share = (term_docs.most_common(1)[0][1] / float(n)) if (n and term_docs) else 0.0

    report = {
        "n_documents": n,
        "char_jaccard": _distribution(char_pairs),
        "word_jaccard": _distribution(word_pairs),
        "unique_word_ngram_ratio": round(unique_ratio, 4),
        "shared_sentence_count": len(shared),
        "most_shared_sentences": shared[:10],
        "top_features": top_features,
        "top_feature_share": round(top_share, 4),
        "thresholds": {
            "max_char_jaccard": max_char_jaccard,
            "max_word_jaccard": max_word_jaccard,
            "min_unique_ngram_ratio": min_unique_ngram_ratio,
            "max_top_feature_share": max_top_feature_share,
        },
    }
    reasons = []
    if report["char_jaccard"]["max"] > max_char_jaccard:
        reasons.append("char %d-gram Jaccard max %.3f exceeds %.3f"
                       % (char_n, report["char_jaccard"]["max"], max_char_jaccard))
    if report["word_jaccard"]["max"] > max_word_jaccard:
        reasons.append("word %d-gram Jaccard max %.3f exceeds %.3f"
                       % (word_n, report["word_jaccard"]["max"], max_word_jaccard))
    if report["unique_word_ngram_ratio"] < min_unique_ngram_ratio:
        reasons.append("unique word n-gram ratio %.3f below %.3f"
                       % (report["unique_word_ngram_ratio"], min_unique_ngram_ratio))
    if report["top_feature_share"] > max_top_feature_share:
        reasons.append("top feature document share %.3f exceeds %.3f"
                       % (report["top_feature_share"], max_top_feature_share))
    report["rejected"] = bool(reasons)
    report["reasons"] = reasons
    return report
