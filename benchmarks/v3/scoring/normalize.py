"""Explicit, integrity-preserving normalization of v3 dataset/run bundles.

The scoring subpackage accepts the shared bundle shape described in the
subpackage README.  Normalization is deliberately *explicit and flexible about
shapes it documents* while **never swallowing malformed integrity**: duplicate
ids, broken case/gold links, duplicate attempts and attempts for unscoped cases
raise :class:`ScoringError` instead of being silently dropped.

Soft coherence problems (a request hash that does not match the rendered input,
a policy revision that disagrees with the manifest) are returned by
:func:`validate_scope` so the report can mark the run ineligible rather than
producing a green score.
"""
from ..common.hashing import hash_obj
from .errors import ScoringError

SCHEMA_VERSION = "v3.0"

# Tasks recognised by the case schema.
TRIAGE_TASKS = ("decision", "full_response")
WORKFLOW_TASK = "workflow"

# Profile names (mirrors contracts.BENCHMARK_PROFILES without importing the
# runtime just for a tuple; a mismatch would be a spec revision anyway).
PROFILES = ("native", "policy_conditioned", "full_context", "workflow")


def _is_nonempty_str(value):
    return isinstance(value, str) and value.strip() != ""


def _as_mapping(value, label):
    if not isinstance(value, dict):
        raise ScoringError("%s must be a mapping" % label)
    return value


# --------------------------------------------------------------- declarations

def declared_fields(gold, task):
    """Which answer fields a gold record actually declares (and so must be
    observable or explicitly unobservable)."""
    answer = gold.get("answer") or {}
    fields = []
    if task == WORKFLOW_TASK:
        if any(answer.get(k) not in (None, [], {}) for k in
               ("expected_state", "assertions", "required_outcomes",
                "forbidden_outcomes", "supporting_evidence")):
            fields.append("workflow")
        return tuple(fields)
    if answer.get("category") is not None or answer.get("acceptable_categories"):
        fields.append("category")
    if answer.get("needs_reply") is not None:
        fields.append("needs_reply")
    return tuple(fields)


def observable_level(gold, field):
    """The documented observability level for a field, or ``None`` when the
    metadata is absent (which is a lint/qualification failure, not an excuse to
    auto-exclude)."""
    obs = gold.get("observable")
    if not isinstance(obs, dict):
        return None
    return obs.get(field)


# ---------------------------------------------------------------- dataset

def normalize_dataset(dataset):
    """Validate structure and return an indexed, canonical view of a dataset."""
    dataset = _as_mapping(dataset, "dataset")
    version = dataset.get("schema_version")
    if version is not None and version != SCHEMA_VERSION:
        raise ScoringError("dataset schema_version must be %r, got %r"
                           % (SCHEMA_VERSION, version))

    raw_cases = dataset.get("cases")
    if not isinstance(raw_cases, list):
        raise ScoringError("dataset.cases must be a list")
    case_by_id = {}
    cases = []
    for case in raw_cases:
        _as_mapping(case, "case")
        cid = case.get("case_id")
        if not _is_nonempty_str(cid):
            raise ScoringError("each case needs a non-empty case_id")
        if cid in case_by_id:
            raise ScoringError("duplicate case_id %r" % cid)
        if not _is_nonempty_str(case.get("gold_id")):
            raise ScoringError("case %r needs a non-empty gold_id" % cid)
        case_by_id[cid] = case
        cases.append(case)

    raw_gold = dataset.get("gold") or []
    if not isinstance(raw_gold, list):
        raise ScoringError("dataset.gold must be a list")
    gold_by_id = {}
    gold_by_case = {}
    for gold in raw_gold:
        _as_mapping(gold, "gold record")
        gid = gold.get("gold_id")
        gcase = gold.get("case_id")
        if not _is_nonempty_str(gid):
            raise ScoringError("each gold record needs a non-empty gold_id")
        if not _is_nonempty_str(gcase):
            raise ScoringError("gold %r needs a non-empty case_id" % gid)
        if gid in gold_by_id:
            raise ScoringError("duplicate gold_id %r" % gid)
        if gcase in gold_by_case:
            raise ScoringError("multiple gold records for case %r" % gcase)
        gold_by_id[gid] = gold
        gold_by_case[gcase] = gold

    for cid, case in case_by_id.items():
        gid = case["gold_id"]
        gold = gold_by_id.get(gid)
        if gold is None:
            raise ScoringError("case %r references missing gold %r" % (cid, gid))
        if gold.get("case_id") != cid:
            raise ScoringError("gold %r is linked to case %r, not %r"
                               % (gid, gold.get("case_id"), cid))

    policies = dataset.get("policies") or []
    if not isinstance(policies, list):
        raise ScoringError("dataset.policies must be a list")
    policies_by_id = {}
    for policy in policies:
        _as_mapping(policy, "policy card")
        pid = policy.get("policy_id")
        if _is_nonempty_str(pid):
            policies_by_id[pid] = policy

    lineage = dataset.get("lineage") or []
    if not isinstance(lineage, list):
        raise ScoringError("dataset.lineage must be a list")
    lineage_by_id = {}
    for group in lineage:
        _as_mapping(group, "lineage group")
        lid = group.get("lineage_id")
        if not _is_nonempty_str(lid):
            raise ScoringError("each lineage group needs a lineage_id")
        if lid in lineage_by_id:
            raise ScoringError("duplicate lineage_id %r" % lid)
        lineage_by_id[lid] = group

    metadata = dataset.get("metadata")
    if metadata is not None:
        _as_mapping(metadata, "dataset.metadata")
    return {
        "schema_version": version or SCHEMA_VERSION,
        "dataset_id": dataset.get("dataset_id"),
        "cases": cases,
        "case_by_id": case_by_id,
        "gold_by_id": gold_by_id,
        "gold_by_case": gold_by_case,
        "policies_by_id": policies_by_id,
        "lineage_by_id": lineage_by_id,
        "metadata": metadata or {},
        "raw": dataset,
    }


def case_task(case):
    return case.get("task") or "decision"


def case_profile(case):
    return case.get("input_profile") or "native"


def case_split(case):
    return case.get("split") or "development"


def case_lineage(case):
    return case.get("lineage_id") or case.get("case_id")


def case_relation(case):
    relation = case.get("relation")
    return relation if isinstance(relation, dict) else {}


# ---------------------------------------------------------------- run

def normalize_run(run):
    """Validate structure and return an indexed view of a run bundle."""
    run = _as_mapping(run, "run")
    manifest = _as_mapping(run.get("manifest"), "run.manifest")
    run_id = manifest.get("run_id")
    if not _is_nonempty_str(run_id):
        raise ScoringError("run.manifest needs a non-empty run_id")
    raw_attempts = run.get("attempts")
    if not isinstance(raw_attempts, list):
        raise ScoringError("run.attempts must be a list")

    attempts_by_case = {}
    seen = set()
    for attempt in raw_attempts:
        _as_mapping(attempt, "attempt")
        cid = attempt.get("case_id")
        number = attempt.get("attempt")
        if not _is_nonempty_str(cid):
            raise ScoringError("each attempt needs a non-empty case_id")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ScoringError("attempt %r needs a positive integer attempt number"
                               % cid)
        key = (cid, number)
        if key in seen:
            raise ScoringError("duplicate attempt %d for case %r" % (number, cid))
        seen.add(key)
        if attempt.get("run_id") != run_id:
            raise ScoringError("attempt %d/%r run_id %r does not match manifest %r"
                               % (number, cid, attempt.get("run_id"), run_id))
        attempts_by_case.setdefault(cid, []).append(attempt)
    for cid in attempts_by_case:
        attempts_by_case[cid].sort(key=lambda a: a["attempt"])

    metadata = run.get("metadata")
    if metadata is not None:
        _as_mapping(metadata, "run.metadata")
    return {
        "run_id": run_id,
        "manifest": manifest,
        "attempts_by_case": attempts_by_case,
        "metadata": metadata or {},
        "raw": run,
    }


def final_attempt(attempts):
    """The attempt that represents the case outcome (highest attempt number)."""
    return attempts[-1] if attempts else None


def attempt_status(attempt):
    return (attempt or {}).get("status") or "missing"


def attempt_parsed(attempt):
    """The parsed decision payload (never the raw string)."""
    output = (attempt or {}).get("output")
    if not isinstance(output, dict):
        return {}
    parsed = output.get("parsed")
    return parsed if isinstance(parsed, dict) else {}


def attempt_provenance(attempt):
    prov = (attempt or {}).get("field_provenance")
    return prov if isinstance(prov, dict) else {}


def attempt_field(attempt, field):
    """The produced/derived value of ``field`` or ``None``.

    A field whose provenance is ``missing`` (or absent) yields ``None`` even if
    some stray value sits in the parsed payload -- provenance is the contract.
    """
    prov = attempt_provenance(attempt)
    if prov.get(field) not in ("produced", "derived"):
        return None
    return attempt_parsed(attempt).get(field)


# ------------------------------------------------------------ scope/hash

def request_hash(case):
    """Canonical hash of a case's rendered request (the runner records this)."""
    rendered = case.get("rendered_input")
    if not isinstance(rendered, dict):
        return None
    return hash_obj({
        "profile": rendered.get("profile") or case_profile(case),
        "system": rendered.get("system", ""),
        "user": rendered.get("user", ""),
    })


def validate_scope(ds, run):
    """Return soft coherence problems ([] when the run matches its manifest)."""
    problems = []
    manifest = run["manifest"]
    if ds.get("dataset_id") and manifest.get("dataset_id") \
            and ds["dataset_id"] != manifest["dataset_id"]:
        problems.append("dataset_id %r does not match manifest %r"
                        % (ds["dataset_id"], manifest["dataset_id"]))

    requested = manifest.get("requested_case_ids") or []
    if not isinstance(requested, list):
        requested = []
    requested_set = set(requested)
    for cid in requested:
        if cid not in ds["case_by_id"]:
            problems.append("manifest requests unknown case %r" % cid)
    for cid in run["attempts_by_case"]:
        if cid not in ds["case_by_id"]:
            problems.append("attempt for case %r absent from dataset" % cid)
        elif cid not in requested_set:
            problems.append("attempt for unscoped case %r" % cid)

    requested_profiles = set(manifest.get("requested_profiles") or [])
    manifest_policy_revision = manifest.get("policy_revision")
    for cid, attempts in run["attempts_by_case"].items():
        case = ds["case_by_id"].get(cid)
        if case is None:
            continue
        for attempt in attempts:
            profile = attempt.get("profile") or case_profile(case)
            if requested_profiles and profile not in requested_profiles:
                problems.append("attempt %s/%d profile %r not in requested profiles"
                                % (cid, attempt["attempt"], profile))
            expected = request_hash(case)
            stored = attempt.get("request_sha256")
            if expected is not None and stored:
                if stored != expected:
                    problems.append(
                        "attempt %s/%d request_sha256 does not match rendered input"
                        % (cid, attempt["attempt"]))
        policy = ds["policies_by_id"].get(case.get("policy_id"))
        if policy and manifest_policy_revision \
                and policy.get("revision") != manifest_policy_revision:
            problems.append("case %r policy revision %r does not match manifest %r"
                            % (cid, policy.get("revision"),
                               manifest_policy_revision))
    return problems


def lint_dataset(ds):
    """Documented dataset lint problems ([] when clean).

    Observable gold missing its documented observability level is a real lint
    failure: it is surfaced here and disqualifies the run instead of letting the
    field be silently dropped from the denominator.
    """
    problems = []
    for case in ds["cases"]:
        cid = case["case_id"]
        gold = ds["gold_by_case"][cid]
        task = case_task(case)
        for field in declared_fields(gold, task):
            if observable_level(gold, field) is None:
                problems.append("gold %s: missing observable metadata for %r"
                                % (gold.get("gold_id"), field))
        if gold.get("human_seal") and gold.get("review_status") != "sealed":
            problems.append("gold %s: human_seal on a non-sealed record"
                            % gold.get("gold_id"))
        if gold.get("source") == "real_mail" and gold.get("human_seal") \
                and not gold.get("authorized"):
            problems.append("gold %s: sealed real-mail gold is not authorized"
                            % gold.get("gold_id"))
        if task == WORKFLOW_TASK and case_profile(case) != "workflow":
            problems.append("case %s: workflow task must use the workflow profile"
                            % cid)
    return problems
