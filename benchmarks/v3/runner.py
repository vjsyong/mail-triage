"""Benchmark v3 runner: immutable identity, resumable execution, resources (WP4).

``run_dataset(dataset, adapter, ...) -> run`` executes the cases a caller
requests, assembles foundation-schema attempts, and returns::

    {"manifest": <foundation run manifest>,
     "attempts": [<attempt>, ...],
     "metadata":  {...}}

Identity is exact and immutable: the manifest hashes the dataset, the requested
case subset, the rendered prompts, the adapter source/revision, model revision
or artifact, policy, scorer, calibrator, engine contract and runtime/generation
config.  A resume revalidates and recomputes both manifests, refuses a stale or
tampered hash, rejects duplicate/mismatched case attempts, and preserves the
failure denominator.  Adapters only ever see a case's declared visible input and
their sandbox -- never the dataset bundle or gold (a leakage assertion enforces
it here).

Resource evidence is measured, not asserted: an externally enforced cgroup
cpuset/memory limit is verified from the cgroup filesystem before any CPU
qualification is granted, and a shared/warm external endpoint is recorded as
unverified and ineligible.
"""
from __future__ import annotations

import copy
import json
import os
import re

from .common.hashing import hash_obj, sha256_file
from .common import identity
from .contracts import assert_no_gold_leakage
from .sandbox import Mailbox

MANIFEST_NAME = "manifest.json"
ATTEMPTS_NAME = "attempts.jsonl"
METADATA_NAME = "metadata.json"

DEFAULT_SCORER_REVISION = "v3.0-scoring"
DEFAULT_CALIBRATOR_REVISION = "none"
DEFAULT_SCHEMA = "v3.0"


class RunnerError(ValueError):
    """Raised when a run cannot be built (identity, scope or dataset problem)."""


# --------------------------------------------------------------------- dataset

def _review_status(dataset):
    meta = dataset.get("metadata") or {}
    return meta.get("review_status") or "draft"


def _check_sealed_gold(dataset):
    """A sealed dataset must not carry contradictory gold review state.

    Reuses the foundation gate so a fabricated seal (``human_seal`` without a
    reviewer, or sealed real-mail without authorization) is refused, not merely
    warned about.
    """
    from .schema import validate_artifact
    for i, gold in enumerate(dataset.get("gold") or []):
        errs = validate_artifact("gold", gold)
        if errs:
            raise RunnerError("sealed dataset has invalid gold[%d]:\n  %s"
                              % (i, "\n  ".join(errs)))


def _as_cases(dataset):
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RunnerError("dataset has no cases")
    return cases


def select_cases(dataset, requested_case_ids=None, requested_splits=None,
                 requested_profiles=None):
    """Select and validate the requested case subset.

    Defaults are the whole dataset; an explicit request that names a case,
    split or profile not present is rejected rather than silently ignored.
    """
    cases = _as_cases(dataset)
    by_id = {}
    for case in cases:
        cid = case.get("case_id")
        if not cid:
            raise RunnerError("case without case_id in dataset")
        if cid in by_id:
            raise RunnerError("duplicate case_id %r in dataset" % cid)
        by_id[cid] = case

    all_ids = sorted(by_id)
    all_splits = sorted({c.get("split") for c in cases if c.get("split")})
    all_profiles = sorted({c.get("input_profile") for c in cases
                           if c.get("input_profile")})

    ids = list(requested_case_ids) if requested_case_ids else all_ids
    splits = list(requested_splits) if requested_splits else all_splits
    profiles = list(requested_profiles) if requested_profiles else all_profiles
    for cid in ids:
        if cid not in by_id:
            raise RunnerError("requested case_id %r is not in the dataset" % cid)
    for s in splits:
        if s not in all_splits:
            raise RunnerError("requested split %r is not in the dataset" % s)
    for p in profiles:
        if p not in all_profiles:
            raise RunnerError("requested profile %r is not in the dataset" % p)

    selected = [by_id[cid] for cid in ids
                if by_id[cid].get("split") in splits
                and by_id[cid].get("input_profile") in profiles]
    if not selected:
        raise RunnerError("no cases match the requested scope")
    return selected, {"requested_case_ids": list(ids),
                      "requested_splits": list(splits),
                      "requested_profiles": list(profiles)}


def gold_for(dataset, case):
    gid = case.get("gold_id")
    for gold in dataset.get("gold") or []:
        if gold.get("gold_id") == gid:
            return gold
    return None


def select_policy(dataset, case=None, policy=None):
    if policy is not None:
        return copy.deepcopy(policy)
    pid = (case or {}).get("policy_id")
    for card in dataset.get("policies") or []:
        if card.get("policy_id") == pid:
            return copy.deepcopy(card)
    cards = dataset.get("policies") or []
    return copy.deepcopy(cards[0]) if cards else None


def policies_for(dataset, cases, explicit=None):
    """The distinct policy cards the requested cases actually resolve to."""
    if explicit is not None:
        return [copy.deepcopy(explicit)]
    seen = {}
    for case in cases:
        card = select_policy(dataset, case)
        if card is None:
            continue
        key = card.get("policy_id") or hash_obj(card)
        seen[key] = card
    if not seen:
        fallback = select_policy(dataset)
        return [fallback] if fallback else []
    return [seen[key] for key in sorted(seen)]


def dataset_sha256(dataset):
    body = {k: dataset.get(k) for k in
            ("schema_version", "dataset_id", "cases", "gold", "scenarios",
             "policies", "lineage", "provenance")}
    return hash_obj(body)


def case_manifest_sha256(cases):
    entries = [{"case_id": c.get("case_id"), "sha256": hash_obj(c)}
               for c in sorted(cases, key=lambda c: c.get("case_id") or "")]
    return hash_obj(entries)


def prompt_sha256(cases):
    entries = []
    for case in sorted(cases, key=lambda c: c.get("case_id") or ""):
        rendered = case.get("rendered_input") or {}
        entries.append({"case_id": case.get("case_id"),
                        "system": rendered.get("system"),
                        "user": rendered.get("user"),
                        "policy_id": (rendered.get("policy") or {}).get("policy_id")
                        if isinstance(rendered.get("policy"), dict)
                        else (case.get("policy_id"))})
    return hash_obj(entries)


def engine_contract_sha256():
    path = os.path.join(os.path.dirname(__file__), "contracts.py")
    if os.path.exists(path):
        return sha256_file(path)
    return hash_obj({"contracts": DEFAULT_SCHEMA})


def _scoring_defaults():
    scorer, calibrator = DEFAULT_SCORER_REVISION, DEFAULT_CALIBRATOR_REVISION
    try:  # scoring owns these; import lazily and tolerate its absence in dev
        from . import scoring  # type: ignore  # noqa: PLC0415
        scorer = getattr(scoring, "SCORER_REVISION", scorer) or scorer
        calibrator = getattr(scoring, "CALIBRATOR_REVISION", calibrator) or calibrator
    except Exception:  # noqa: BLE001 - not integrated yet
        pass
    return scorer, calibrator


# ------------------------------------------------------------------- resources

_CGROUP_PATHS = {
    "cpuset": ("/sys/fs/cgroup/cpuset.cpus.effective",
               "/sys/fs/cgroup/cpuset.cpus"),
    "memory": ("/sys/fs/cgroup/memory.max",
               "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    "cpu": ("/sys/fs/cgroup/cpu.max",),
}


def _read(path, probe):
    if probe is not None and path in probe:
        return probe[path]
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _parse_cpuset(value):
    if not value:
        return None
    value = value.strip()
    if value in ("max", "-1"):
        return None
    return value


def _parse_memory(value):
    if not value:
        return None
    value = value.strip()
    if value in ("max", "-1", ""):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_cpu_quota(value):
    if not value:
        return None
    parts = value.split()
    try:
        quota, period = int(parts[0]), int(parts[1])
    except (TypeError, ValueError, IndexError):
        return None
    if quota <= 0:
        return None
    return round(quota / period, 3)


def _parse_cpu_model(probe):
    text = _read("/proc/cpuinfo", probe) or ""
    m = re.search(r"model name\s*:\s*(.+)", text)
    return m.group(1).strip() if m else None


def _parse_rss(probe):
    if probe is not None and probe.get("process_tree_rss_bytes") is not None:
        return int(probe["process_tree_rss_bytes"])
    text = _read("/proc/self/status", probe) or ""
    m = re.search(r"VmRSS:\s*(\d+)\s*kB", text)
    return int(m.group(1)) * 1024 if m else None


def collect_resource_evidence(probe=None, endpoint_class="in_process"):
    """Measure CPU/device/thread/memory evidence and CPU-qualify honestly.

    ``probe`` maps real or pseudo paths to contents for testing.  A limit is
    "verified" only when it is actually present in the cgroup filesystem (or an
    explicit probe), never because a config file *says* 8 GiB.  A shared or warm
    external endpoint is recorded as unverified and ineligible.
    """
    cpuset = None
    for p in _CGROUP_PATHS["cpuset"]:
        cpuset = _parse_cpuset(_read(p, probe))
        if cpuset:
            break
    memory = None
    memory_source = None
    for p in _CGROUP_PATHS["memory"]:
        memory = _parse_memory(_read(p, probe))
        if memory:
            memory_source = p
            break
    quota = None
    for p in _CGROUP_PATHS["cpu"]:
        quota = _parse_cpu_quota(_read(p, probe))
        if quota:
            break
    if probe is not None and probe.get("affinity") is not None:
        affinity = list(probe["affinity"])
    else:
        try:
            affinity = sorted(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            affinity = list(range(os.cpu_count() or 0))

    notes = []
    external = endpoint_class in ("external", "external_warm", "remote")
    verified = bool(cpuset and memory and not external)
    if not cpuset:
        notes.append("no enforced cpuset limit detected")
    if not memory:
        notes.append("no enforced memory limit detected")
    if external:
        notes.append("endpoint is shared/warm external: CPU qualification "
                     "unverified and ineligible")
    elif verified:
        notes.append("externally enforced limits verified")
    else:
        notes.append("process RSS is the runner process (in-process direct model); "
                     "no externally enforced limit verified")

    record = {
        "device": "cpu",
        "cpu_model": _parse_cpu_model(probe),
        "logical_cpus": os.cpu_count(),
        "affinity_cpus": affinity,
        "threads": len(affinity) if affinity else None,
        "cpuset": {"value": cpuset, "verified": bool(cpuset)},
        "memory": {"limit_bytes": memory, "source": memory_source,
                   "verified": bool(memory)},
        "cpu_quota_cores": quota,
        "process_tree_rss_bytes": _parse_rss(probe),
        "endpoint_class": endpoint_class,
        "measurement": ("cgroup/proc" if probe is None else "probe"),
        "enforced_limits_verified": verified,
        "cpu_qualified": verified,
        "notes": notes or ["externally enforced limits verified"],
        "cold_startup": "unverified",
    }
    return record


# --------------------------------------------------------------------- attempt

def _unsupported_result(task, adapter):
    from .adapters import make_result, provenance_for, STATUS_SKIPPED
    return make_result(
        status=STATUS_SKIPPED, raw=None, parsed=None,
        error="adapter %r does not support task %r" % (adapter.adapter_id, task),
        field_provenance=provenance_for(None))


def _attempt(manifest, case, adapter, result, attempt_no):
    from .schema import validate_artifact
    attempt = {
        "schema_version": DEFAULT_SCHEMA,
        "run_id": manifest["run_id"],
        "case_id": case["case_id"],
        "attempt": int(attempt_no),
        "adapter_id": adapter.adapter_id,
        "profile": case.get("input_profile"),
        "status": result.get("status", "error"),
        "output": result.get("output") or {"raw": None, "parsed": None, "error": None},
        "field_provenance": result.get("field_provenance") or {},
        "tool_events": list(result.get("tool_events") or []),
        "timings": dict(result.get("timings") or {}),
        "resources": dict(result.get("resources") or {}),
        "request_sha256": result.get("request_sha256", ""),
        "failure_class": result.get("failure_class"),
        "capabilities_used": dict(result.get("capabilities_used") or {}),
    }
    errors = validate_artifact("attempt", attempt)
    if errors:
        raise RunnerError("adapter %r produced an invalid attempt for %s:\n  %s"
                          % (adapter.adapter_id, case["case_id"], "\n  ".join(errors)))
    return attempt


def _build_view(dataset, case, explicit_policy=None):
    rendered = copy.deepcopy(case.get("rendered_input") or {})
    profile = case.get("input_profile")
    policy = select_policy(dataset, case, explicit_policy)
    view = {
        "case_id": case["case_id"],
        "scenario_id": case.get("scenario_id"),
        "lineage_id": case.get("lineage_id"),
        "split": case.get("split"),
        "task": case.get("task"),
        "input_profile": profile,
        "rendered_input": rendered,
        "policy": policy if profile == "policy_conditioned" else None,
        "mailbox": copy.deepcopy(case.get("mailbox")),
        "tools": copy.deepcopy(case.get("tools")),
        "permissions": copy.deepcopy(case.get("permissions")),
    }
    gold = gold_for(dataset, case)
    if gold:
        model_input = {"system": rendered.get("system"), "user": rendered.get("user"),
                       "policy": view["policy"], "mailbox": view["mailbox"],
                       "tools": view["tools"]}
        assert_no_gold_leakage(model_input, gold)
    return view


def _sandbox_for(dataset, case):
    if case.get("input_profile") != "workflow" and case.get("task") != "workflow":
        return None
    permissions = case.get("permissions")
    if permissions is None:
        policy = select_policy(dataset, case)
        permissions = (policy or {}).get("permissions") if policy else None
    return Mailbox(case.get("mailbox") or {}, permissions=permissions,
                   case_id=case["case_id"])


# --------------------------------------------------------------------- manifest

def build_run_manifest(dataset, adapter, scope, policy=None, *,
                       scorer_revision=None, calibrator_revision=None,
                       generation_config=None, runtime_config=None,
                       engine_sha=None):
    """Build the hashed run manifest for a requested scope (FR7)."""
    from .adapters import AdapterError
    cases = scope["cases"]
    fp = adapter.fingerprint()
    scorer, calibrator = _scoring_defaults()
    scorer = scorer_revision or scorer
    calibrator = calibrator_revision or calibrator
    policies_used = policies_for(dataset, cases, explicit=policy)
    policy_sha = (hash_obj(policies_used) if policies_used
                  else hash_obj({"policy_id": "none"}))
    revisions = sorted({(card.get("revision") or "none") for card in policies_used})
    policy_revision = revisions[0] if len(revisions) == 1 else "|".join(revisions or ["none"])
    gen = dict(fp.get("generation_config") or {})
    gen.update(generation_config or {})
    run_cfg = dict(fp.get("runtime_config") or {})
    run_cfg.update(runtime_config or {})
    fields = {
        "dataset_id": dataset.get("dataset_id") or "dataset",
        "dataset_sha256": dataset_sha256(dataset),
        "case_manifest_sha256": case_manifest_sha256(cases),
        "prompt_revision": fp.get("prompt_revision") or adapter.revision,
        "prompt_sha256": prompt_sha256(cases),
        "model_key": fp.get("model_key") or adapter.adapter_id,
        "model_revision": fp.get("model_revision") or "",
        "model_artifact_sha256": fp.get("model_artifact_sha256") or "",
        "adapter_id": fp["adapter_id"],
        "adapter_revision": fp["adapter_revision"],
        "scorer_revision": scorer,
        "policy_revision": policy_revision,
        "policy_sha256": policy_sha,
        "engine_contract_sha256": engine_sha or engine_contract_sha256(),
        "calibrator_revision": calibrator,
        "generation_config": gen,
        "runtime_config": run_cfg,
        "requested_case_ids": scope["requested_case_ids"],
        "requested_splits": scope["requested_splits"],
        "requested_profiles": scope["requested_profiles"],
    }
    if not (fields["model_revision"] or fields["model_artifact_sha256"]):
        raise AdapterError(
            "adapter %r has neither model_revision nor model_artifact_sha256"
            % adapter.adapter_id)
    manifest = identity.build_manifest(**fields)
    manifest["capabilities"] = fp.get("capabilities")
    manifest["mock"] = fp.get("mock")
    manifest["qualifies_as_baseline"] = fp.get("qualifies_as_baseline")
    manifest["confidence_meaning"] = fp.get("confidence_meaning")
    manifest["adapter_source_sha256"] = fp.get("adapter_source_sha256")
    return manifest


# ------------------------------------------------------------------ resume/run

def _existing_run(resume):
    if resume is None:
        return None
    if isinstance(resume, dict):
        return resume
    return load_run(resume)


def _prior_attempts(existing, selected_ids):
    """Return ``{case_id: [attempt, ...]}`` with duplicate/mismatch rejection."""
    manifest = (existing or {}).get("manifest") or {}
    prior = {}
    for attempt in (existing or {}).get("attempts") or []:
        cid = attempt.get("case_id")
        prior.setdefault(cid, []).append(attempt)
    dupes = sorted(cid for cid, rows in prior.items() if len(rows) > 1)
    if dupes:
        raise RunnerError("existing run has duplicate attempts for %s" % ", ".join(dupes))
    stray = sorted(cid for cid in prior if cid not in selected_ids)
    if stray:
        raise RunnerError(
            "existing run has attempts outside the requested cases: %s"
            % ", ".join(stray))
    for cid, rows in prior.items():
        for row in rows:
            if row.get("run_id") != manifest.get("run_id"):
                raise RunnerError(
                    "existing attempt for %s has a mismatched run_id" % cid)
            if row.get("adapter_id") != manifest.get("adapter_id"):
                raise RunnerError(
                    "existing attempt for %s has a mismatched adapter_id" % cid)
    return prior


def run_dataset(dataset, adapter, *, requested_case_ids=None,
                requested_splits=None, requested_profiles=None, out_dir=None,
                resume=None, policy=None, allow_draft=False,
                scorer_revision=None, calibrator_revision=None,
                generation_config=None, runtime_config=None, max_cases=None,
                resource_probe=None, endpoint_class="in_process",
                save=True):
    """Run the requested cases and return the run bundle."""
    if not isinstance(dataset, dict):
        raise RunnerError("dataset must be a mapping")
    if dataset.get("schema_version") != DEFAULT_SCHEMA:
        raise RunnerError("dataset schema_version must be %r" % DEFAULT_SCHEMA)
    if not dataset.get("dataset_id"):
        raise RunnerError("dataset is missing dataset_id")
    status = _review_status(dataset)
    if status != "sealed" and not allow_draft:
        raise RunnerError(
            "dataset review_status=%r: pass allow_draft=True (--allow-draft) to "
            "run development data" % status)
    if status == "sealed":
        _check_sealed_gold(dataset)

    selected, scope_ids = select_cases(dataset, requested_case_ids,
                                       requested_splits, requested_profiles)
    if max_cases is not None:
        selected = selected[:max_cases]
        scope_ids["requested_case_ids"] = [c["case_id"] for c in selected]
    explicit_policy = policy
    scope = {"cases": selected, **scope_ids}

    manifest = build_run_manifest(
        dataset, adapter, scope, policy=explicit_policy,
        scorer_revision=scorer_revision, calibrator_revision=calibrator_revision,
        generation_config=generation_config, runtime_config=runtime_config)

    existing = _existing_run(resume)
    if existing is not None and not existing.get("manifest"):
        if existing.get("attempts"):
            raise RunnerError("existing run has attempts but no manifest identity")
        existing = None
    resumed_from = None
    prior = {}
    if existing is not None:
        existing_manifest = existing.get("manifest")
        identity.resolve_resume(manifest, existing_manifest)
        resumed_from = existing_manifest.get("run_id")
        prior = _prior_attempts(existing, {c["case_id"] for c in selected})

    # Carry prior attempts verbatim (preserves the failure denominator).
    attempts = []
    for cid in sorted(prior):
        attempts.extend(copy.deepcopy(prior[cid]))

    completed = set(prior)
    executed = 0
    for case in selected:
        if case["case_id"] in completed:
            continue
        executed += 1
        if not adapter.supports(case.get("task")):
            result = _unsupported_result(case.get("task"), adapter)
        else:
            view = _build_view(dataset, case, explicit_policy)
            sandbox = _sandbox_for(dataset, case)
            try:
                result = adapter.run_case(view, sandbox)
            except Exception as exc:  # noqa: BLE001 - never lose a case
                from .adapters import make_result, provenance_for
                result = make_result(
                    status="error", raw=None, parsed=None,
                    error="%s: %s" % (type(exc).__name__, exc),
                    field_provenance=provenance_for(None),
                    failure_class="infrastructure")
        attempts.append(_attempt(manifest, case, adapter, result, 1))

    resource = collect_resource_evidence(probe=resource_probe,
                                         endpoint_class=endpoint_class)
    failures = {}
    skips = []
    for attempt in attempts:
        if attempt.get("status") == "skipped":
            skips.append({"case_id": attempt["case_id"],
                          "reason": (attempt.get("output") or {}).get("error")})
        if attempt.get("status") != "ok":
            key = attempt.get("failure_class") or ("skipped"
                                                   if attempt.get("status") == "skipped"
                                                   else "unknown")
            failures[key] = failures.get(key, 0) + 1

    metadata = {
        "schema_version": DEFAULT_SCHEMA,
        "dataset_id": dataset.get("dataset_id"),
        "dataset_review_status": status,
        "adapter_id": adapter.adapter_id,
        "mock": bool(getattr(adapter, "mock", False)),
        "qualifies_as_baseline": bool(getattr(adapter, "qualifies_as_baseline", True)),
        "resumed_from": resumed_from,
        "executed_case_count": executed,
        "attempt_count": len(attempts),
        "failure_counts": failures,
        "skips": skips,
        "resource": resource,
        "cpu_qualification": {
            "cpu_qualified": resource["cpu_qualified"],
            "enforced_limits_verified": resource["enforced_limits_verified"],
            "endpoint_class": resource["endpoint_class"],
            "notes": resource["notes"],
        },
        "timing_labels": {
            "cold_startup": "unverified",
            "warm": "measured",
            "simulated_tools": "simulated",
        },
    }
    run = {"manifest": manifest, "attempts": attempts, "metadata": metadata}
    if save and out_dir:
        save_run(run, out_dir)
    return run


# ------------------------------------------------------------------- persistence

def save_run(run, out_dir):
    """Write a run to ``out_dir/<run_id>/`` as manifest + attempts + metadata."""
    run_id = run["manifest"]["run_id"]
    target = os.path.join(out_dir, run_id)
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, MANIFEST_NAME), "w") as f:
        json.dump(run["manifest"], f, indent=1, sort_keys=True)
    with open(os.path.join(target, ATTEMPTS_NAME), "w") as f:
        for attempt in run.get("attempts") or []:
            f.write(json.dumps(attempt, sort_keys=True) + "\n")
    with open(os.path.join(target, METADATA_NAME), "w") as f:
        json.dump(run.get("metadata") or {}, f, indent=1, sort_keys=True)
    return target


def _load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_run(path):
    """Load a run bundle written by :func:`save_run` (or an in-memory bundle)."""
    if isinstance(path, dict):
        return path
    if os.path.isdir(path):
        manifest_path = os.path.join(path, MANIFEST_NAME)
        if not os.path.exists(manifest_path):
            sub = sorted(d for d in os.listdir(path)
                         if os.path.isdir(os.path.join(path, d)))
            if len(sub) == 1:
                return load_run(os.path.join(path, sub[0]))
            raise RunnerError("no %s under %s" % (MANIFEST_NAME, path))
        with open(manifest_path) as f:
            manifest = json.load(f)
        attempts = _load_jsonl(os.path.join(path, ATTEMPTS_NAME))
        metadata = {}
        meta_path = os.path.join(path, METADATA_NAME)
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                metadata = json.load(f)
        return {"manifest": manifest, "attempts": attempts, "metadata": metadata}
    if os.path.isfile(path):
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and "manifest" in data:
            return data
        return {"manifest": data, "attempts": [], "metadata": {}}
    raise RunnerError("run path does not exist: %s" % path)


def run_summary(run):
    manifest = run.get("manifest") or {}
    attempts = run.get("attempts") or []
    ok = sum(1 for a in attempts if a.get("status") == "ok")
    return {
        "run_id": manifest.get("run_id"),
        "dataset_id": manifest.get("dataset_id"),
        "adapter_id": manifest.get("adapter_id"),
        "model_key": manifest.get("model_key"),
        "cases": len(attempts),
        "ok": ok,
        "failed": len(attempts) - ok,
        "config_hash": manifest.get("config_hash"),
        "mock": (run.get("metadata") or {}).get("mock"),
        "qualifies_as_baseline": (run.get("metadata") or {}).get(
            "qualifies_as_baseline"),
    }
