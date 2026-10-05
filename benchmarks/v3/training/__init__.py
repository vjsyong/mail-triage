"""mail SFT training slice (configurable taxonomy + bounded native tools).

This package is a **training-only** addition to benchmark v3.  It does not alter
the frozen v3.0 artifacts, adapters, runner or production code; it consumes the
public development material through ``benchmarks.v3.build`` and the frozen
sandbox, and produces an explicit ``mail-sft-0.1`` training artifact.

Modules:

* ``taxonomies``  -- name-independent taxonomy resolution and public projection
* ``tools_native``-- the bounded native tool surface (AST-extracted schemas)
* ``trace``       -- exact per-turn trace persistence
* ``messages``    -- canonical SFT messages and per-turn masks
* ``minicpm``     -- the released MiniCPM5 native template reference renderer
* ``verify``      -- factual/scope/state verification gates
* ``export``      -- fail-closed, domain-separated export
* ``samples``     -- authored example construction
"""
from .messages import TRAIN_SCHEMA_VERSION
from .schema import (GENERATION_DOMAINS, TASK_TYPES, gold_answer,
                     validate_sft_example)

__all__ = [
    "TRAIN_SCHEMA_VERSION", "TASK_TYPES", "GENERATION_DOMAINS",
    "validate_sft_example", "gold_answer",
]
