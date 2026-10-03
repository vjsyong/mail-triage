"""Lineage grouping, near-duplicate detection and connected-component partitions.

Lineage clusters, not rows, are the split/bootstrap unit. A component is formed
from *real* edges -- shared lineage/scenario/source messages, parent relations,
and detected near-duplicate content -- never from a shared persona, policy or
generic template (those are coverage axes; unioning them would collapse the
dataset).
"""
import hashlib
import re

# MinHash / LSH parameters. 24 permutations in 6 bands of 4 rows is enough to
# find cross-root duplicates and near-duplicates without O(n^2) all-pairs work.
NUM_HASHES = 24
ROWS = 4
DEFAULT_NEAR_DUPE = 0.90
_TOKEN = re.compile(r"[a-z0-9]+")

_MASK = (1 << 64) - 1


def _splitmix(seed):
    z = (seed + 0x9E3779B97F4A7C15) & _MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
    return (z ^ (z >> 31)) & _MASK


# Deterministic universal-hash permutations: one stable shingle hash is
# computed per shingle and cheaply re-mixed per permutation. This keeps the
# detection exact and run-stable (no ``hash()``) while making a full 12k-case
# lint practical.
_PERMS = tuple((_splitmix(2 * k + 1) | 1, _splitmix(2 * k + 2))
               for k in range(NUM_HASHES))


def normalize_text(text):
    return " ".join(_TOKEN.findall((text or "").lower()))


def content_signature(text):
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def case_content(case):
    """The model-facing text of a case (never gold/annotations)."""
    rendered = case.get("rendered_input") or {}
    return rendered.get("user") or ""


def _shingles(tokens, k=4):
    if len(tokens) < k:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i:i + k]) for i in range(len(tokens) - k + 1)}


def _shingle_hash(shingle):
    return int.from_bytes(
        hashlib.blake2b(repr(shingle).encode("utf-8"), digest_size=8).digest(), "big")


def minhash(text, num_hashes=NUM_HASHES):
    tokens = _TOKEN.findall((text or "").lower())
    shingles = _shingles(tokens)
    if not shingles:
        return None
    base = [_shingle_hash(s) for s in shingles]
    perms = _PERMS[:num_hashes]
    return tuple(min(((h * a + b) & _MASK) for h in base) for a, b in perms)


def _estimated_similarity(sig_a, sig_b):
    if not sig_a or not sig_b:
        return 0.0
    return sum(1 for a, b in zip(sig_a, sig_b) if a == b) / float(len(sig_a))


def near_duplicate_pairs(items, threshold=DEFAULT_NEAR_DUPE):
    """Find genuinely similar case pairs.

    ``items`` is ``[(case_id, text), ...]``. Pairs are proposed by LSH band
    buckets and confirmed by MinHash similarity, so a leak is caught from content
    rather than from a claimed id.
    """
    sigs = []
    buckets = {}
    for case_id, text in items:
        sig = minhash(text)
        sigs.append((case_id, sig))
        if sig is None:
            continue
        for band in range(0, NUM_HASHES, ROWS):
            key = (band, sig[band:band + ROWS])
            buckets.setdefault(key, []).append(case_id)
    by_id = dict(sigs)
    candidates = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        members = sorted(set(members))
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                candidates.add((members[i], members[j]))
    pairs = []
    for a, b in candidates:
        if _estimated_similarity(by_id.get(a), by_id.get(b)) >= threshold:
            pairs.append((a, b))
    return pairs


class _DSU:
    def __init__(self, nodes):
        self.parent = {n: n for n in nodes}

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def connected_components(nodes, edges):
    """Return ``{root: [members]}`` for an undirected edge list."""
    dsu = _DSU(nodes)
    for a, b in edges:
        if a in dsu.parent and b in dsu.parent:
            dsu.union(a, b)
    groups = {}
    for n in nodes:
        groups.setdefault(dsu.find(n), []).append(n)
    return groups


def case_edges(cases, threshold=DEFAULT_NEAR_DUPE, source_ids=None):
    """Return ``(edges, near_dupe_pairs)`` for a case list.

    Edges are same lineage, same scenario, shared source messages, parent
    relation and detected near-duplicate content. Persona/family/policy/template
    are deliberately absent.

    ``source_ids`` optionally maps ``case_id -> [message_id, ...]`` so cases
    that reuse the same underlying source/thread message are connected even when
    their scenario records differ.
    """
    edges = set()
    by_lineage = {}
    by_scenario = {}
    for c in cases:
        by_lineage.setdefault(c.get("lineage_id"), []).append(c["case_id"])
        by_scenario.setdefault(c.get("scenario_id"), []).append(c["case_id"])
    for members in list(by_lineage.values()) + list(by_scenario.values()):
        for i in range(1, len(members)):
            edges.add(tuple(sorted((members[0], members[i]))))
    if source_ids:
        by_source = {}
        for case_id, ids in source_ids.items():
            for mid in ids or []:
                by_source.setdefault(mid, []).append(case_id)
        for members in by_source.values():
            members = sorted(set(members))
            for i in range(1, len(members)):
                edges.add(tuple(sorted((members[0], members[i]))))
    ids = {c["case_id"] for c in cases}
    for c in cases:
        parent = (c.get("relation") or {}).get("parent_case_id")
        if parent and parent in ids:
            edges.add(tuple(sorted((c["case_id"], parent))))
    # Near-duplicate detection is over *triage source content*. Workflow tasks
    # deliberately share recipe templates (a coverage axis), and a generic
    # template is not a lineage edge. Clip variants pad the body with shared
    # boilerplate to push evidence past the native boundary; they are derived,
    # never an independent source, so they are excluded from the content check.
    triage = [c for c in cases
              if (c.get("rendered_input") or {}).get("profile") != "workflow"
              and (c.get("relation") or {}).get("relation_type") != "clip_variant"]
    items = [(c["case_id"], case_content(c)) for c in triage]
    near = near_duplicate_pairs(items, threshold=threshold)
    for a, b in near:
        edges.add(tuple(sorted((a, b))))
    return sorted(edges), sorted(near)
