"""Explicit training-artifact schema (separate from frozen v3.0 artifacts).

The training slice introduces its own schema id (``mail-sft-0.1``) and does not
mutate the frozen ``v3.0`` artifact schemas or their default semantics.  A
``TRAIN_SCHEMA_VERSION`` change is a training-only revision.
"""
from __future__ import annotations

from .messages import TRAIN_SCHEMA_VERSION

TASK_TYPES = ("decision", "workflow")
GENERATION_DOMAINS = ("training", "development", "evaluation")

PUBLIC_METADATA_KEYS = (
    "review_status", "human_seal", "test_qualified", "generation_domain",
    "seed", "teacher", "provenance",
)


def gold_answer(gold):
    """The authored answer block of a gold record (never model-fabricated)."""
    if not isinstance(gold, dict):
        return {}
    answer = gold.get("answer")
    return answer if isinstance(answer, dict) else {}


def validate_sft_example(example):
    """Structural problems with a training example ([] when valid)."""
    problems = []
    if not isinstance(example, dict):
        return ["example must be a mapping"]
    if example.get("schema_version") != TRAIN_SCHEMA_VERSION:
        problems.append("schema_version must be %r" % TRAIN_SCHEMA_VERSION)
    if example.get("task") not in TASK_TYPES:
        problems.append("task must be one of %s" % (TASK_TYPES,))
    if example.get("generation_domain") not in GENERATION_DOMAINS:
        problems.append("generation_domain must be one of %s"
                        % (GENERATION_DOMAINS,))
    if not example.get("example_id"):
        problems.append("example_id is required")
    messages = example.get("messages")
    if not isinstance(messages, list) or not messages:
        problems.append("messages must be a non-empty list")
    if not example.get("identities"):
        problems.append("identities are required")
    meta = example.get("metadata") or {}
    if meta.get("test_qualified"):
        problems.append("test_qualified must not be asserted")
    return problems
