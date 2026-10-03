"""Deterministic byte-stable pseudo-random stream for dataset authoring.

``random`` is intentionally avoided: its algorithm is an implementation detail,
while run identity (FR7) and repeated-build equality (FR2/FR3) require identical
output across interpreters and process restarts. This stream derives every draw
from ``sha256(seed | salt | counter)`` so determinism depends only on hashlib.
"""
import hashlib


class Stream:
    """A counter-mode deterministic RNG.

    The same ``(seed, salt)`` and the same sequence of calls always yield the
    same values; there is no global state shared between streams.
    """

    __slots__ = ("_seed", "_counter")

    def __init__(self, seed, salt=""):
        self._seed = ("%s\x1f%s" % (seed, salt)).encode("utf-8")
        self._counter = 0

    def _block(self):
        digest = hashlib.sha256(
            self._seed + b"\x1f" + str(self._counter).encode("ascii")).digest()
        self._counter += 1
        return digest

    def randint(self, n):
        """A deterministic integer in ``[0, n)``."""
        if n <= 0:
            raise ValueError("randint requires n > 0")
        return int.from_bytes(self._block()[:8], "big") % n

    def pick(self, seq):
        seq = list(seq)
        if not seq:
            raise ValueError("cannot pick from an empty sequence")
        return seq[self.randint(len(seq))]

    def shuffled(self, seq):
        """A deterministic Fisher-Yates permutation (new list)."""
        items = list(seq)
        for i in range(len(items) - 1, 0, -1):
            j = self.randint(i + 1)
            items[i], items[j] = items[j], items[i]
        return items


def stream(seed, salt):
    return Stream(seed, salt)
