"""Immutable run identity.

A run is identified by a content hash over everything that could change a
number in the report: corpus + case set, prompt/tool-schema snapshot, harness
and scorer revisions, model checkpoint/tokenizer/template, runtime image,
hardware, effective inference params, adaptations, retry/fallback policy, and
cache conditions.

`resolve_resume` refuses to continue a partially-complete run whose identity
differs from the requested one — the guarantee WP1 is built around.
"""
import os

from .hashing import hash_obj, sha256_file, short


REQUIRED_FIELDS = (
    "model_key", "model_revision", "tokenizer", "chat_template",
    "runtime_image", "hardware", "harness_revision", "scorer_revision",
    "corpus_sha256", "case_manifest_sha256", "prompts_sha256",
    "params", "adaptations", "retry_policy", "fallback_policy", "cache_state",
)


def build_manifest(**fields):
    missing = [f for f in REQUIRED_FIELDS if f not in fields]
    if missing:
        raise ValueError("run manifest missing required fields: %s" % ", ".join(missing))
    manifest = {k: fields[k] for k in REQUIRED_FIELDS}
    manifest["config_hash"] = hash_obj(manifest)
    manifest["run_id"] = "%s-%s" % (manifest["model_key"], short(manifest["config_hash"]))
    return manifest


def build_from_context(model_key, *, bench_dir, model_revision="unknown",
                       tokenizer="unknown", chat_template="unknown",
                       runtime_image="unknown", hardware="unknown",
                       harness_revision, scorer_revision, params,
                       adaptations=None, retry_policy=None,
                       fallback_policy=None, cache_state="unknown"):
    """Helper that hashes on-disk case/corpus/prompt artifacts."""
    def _hash(*parts):
        p = os.path.join(bench_dir, *parts)
        return sha256_file(p) if os.path.exists(p) else None

    return build_manifest(
        model_key=model_key,
        model_revision=model_revision,
        tokenizer=tokenizer,
        chat_template=chat_template,
        runtime_image=runtime_image,
        hardware=hardware,
        harness_revision=harness_revision,
        scorer_revision=scorer_revision,
        corpus_sha256=_hash("corpus", "messages.jsonl"),
        case_manifest_sha256=_hash("cases", "manifest.json"),
        prompts_sha256=_hash("harness", "prompts.json"),
        params=params,
        adaptations=adaptations or {},
        retry_policy=retry_policy or {},
        fallback_policy=fallback_policy or {},
        cache_state=cache_state,
    )


def resolve_resume(manifest, existing_manifest):
    """Return "fresh" | "resume"; raise SystemExit-style ValueError on mismatch.

    `existing_manifest` is the manifest stored with a prior partial run (or
    None).  Any identity difference forces a fresh run so two configurations
    can never share one results directory.
    """
    if not existing_manifest:
        return "fresh"
    old = existing_manifest.get("config_hash")
    new = manifest.get("config_hash")
    if old != new:
        raise ValueError(
            "config mismatch for run_id %s: existing %s != requested %s; "
            "refusing to resume — start a fresh run_id"
            % (manifest.get("run_id"), short(old), short(new)))
    return "resume"
