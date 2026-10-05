"""Benchmark v3 adapters (WP4).

Public surface:

``Adapter`` / ``make_result`` / ``provenance_for``
``FakeAdapter``            offline scripted mock (never a real baseline)
``OpenAICompatAdapter``    production native params, greedy parse + retry
``TinyJevAdapter``         typed decision-only head (lazy ``tinyjev`` import)
``FusionAdapter``          composed decision + prose, labelled components
``systemone:<key>``        five decision-only v2 sweep heads (lazy imports)

``get_adapter(name, **kwargs)`` builds a registered adapter by id; a missing
optional dependency (e.g. TinyJev) surfaces as an explicit error, never a silent
capability claim.
"""

from .base import (
    Adapter,
    AdapterError,
    UnsupportedCapability,
    make_result,
    provenance_for,
    STATUS_OK,
    STATUS_ERROR,
    STATUS_TIMEOUT,
    STATUS_MISSING,
    STATUS_SKIPPED,
    FAIL_MODEL,
    FAIL_INFRASTRUCTURE,
    FAIL_BUDGET,
    TASK_CAPABILITIES,
)
from .fake import FakeAdapter
from .generative import OpenAICompatAdapter
from .tinyjev import TinyJevAdapter
from .fusion import FusionAdapter
from .systemone import (
    ADAPTER_CLASSES as SYSTEMONE_ADAPTERS,
    SystemOneGlinerAdapter,
    SystemOneLayaAdapter,
    SystemOneKevAdapter,
    SystemOneNanoJevAdapter,
    SystemOneNanoJevRagAdapter,
    build_systemone_adapter,
)

ADAPTERS = {
    "offline-fake": FakeAdapter,
    "generative-openai": OpenAICompatAdapter,
    "tinyjev-decision": TinyJevAdapter,
    "fusion": FusionAdapter,
    "systemone:gliner": SystemOneGlinerAdapter,
    "systemone:laya": SystemOneLayaAdapter,
    "systemone:kev": SystemOneKevAdapter,
    "systemone:nanojev": SystemOneNanoJevAdapter,
    "systemone:nanojev-rag": SystemOneNanoJevRagAdapter,
}


def get_adapter(name, **kwargs):
    """Build a registered adapter by id (raises ``AdapterError`` if unknown)."""
    cls = ADAPTERS.get(name)
    if cls is None:
        raise AdapterError("unknown adapter %r (expected one of %s)"
                           % (name, ", ".join(sorted(ADAPTERS))))
    return cls(**kwargs)


def list_adapters():
    return sorted(ADAPTERS)


__all__ = [
    "Adapter", "AdapterError", "UnsupportedCapability", "make_result",
    "provenance_for", "FakeAdapter", "OpenAICompatAdapter", "TinyJevAdapter",
    "FusionAdapter", "SystemOneGlinerAdapter", "SystemOneLayaAdapter",
    "SystemOneKevAdapter", "SystemOneNanoJevAdapter",
    "SystemOneNanoJevRagAdapter", "build_systemone_adapter",
    "SYSTEMONE_ADAPTERS", "get_adapter", "list_adapters", "ADAPTERS",
    "STATUS_OK", "STATUS_ERROR", "STATUS_TIMEOUT", "STATUS_MISSING",
    "STATUS_SKIPPED", "FAIL_MODEL", "FAIL_INFRASTRUCTURE", "FAIL_BUDGET",
    "TASK_CAPABILITIES",
]
