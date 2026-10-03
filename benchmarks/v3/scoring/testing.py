"""Small, explicit fixture builders for the WP5 scoring tests.

Kept inside the scoring package (rather than a hidden test helper) so the
documented bundle shape has one obvious constructor and the tests do not grow
ad-hoc copies.  Not imported by production scoring code.
"""
from ..common import identity

PRODUCED = object()
"""Sentinel: build a produced field with its default value."""


def case(case_id, *, lineage_id=None, task="decision", profile="native",
         split="development", gold_id=None, policy_id="default",
         scenario_id=None, relation=None, rendered_input=None):
    record = {
        "schema_version": "v3.0",
        "case_id": case_id,
        "scenario_id": scenario_id or ("scn_" + case_id),
        "lineage_id": lineage_id or ("lin_" + case_id),
        "task": task,
        "input_profile": profile,
        "policy_id": policy_id,
        "split": split,
        "gold_id": gold_id or ("gold_" + case_id),
        "rendered_input": rendered_input or {"profile": profile, "system": "s",
                                             "user": "u"},
    }
    if relation is not None:
        record["relation"] = relation
    return record


def gold(case_id, *, gold_id=None, category="Action", acceptable=None,
         needs_reply=True, observable=None, task="decision", source="synthetic",
         review_status="draft", human_seal=False, reviewer=None, authorized=False,
         hidden_evidence=None, **answer_extra):
    answer = dict(answer_extra)
    if task == "workflow":
        answer.setdefault("required_outcomes", [])
        answer.setdefault("forbidden_outcomes", [])
        answer.setdefault("supporting_evidence", [])
    else:
        if category is not None or acceptable:
            answer["category"] = category
            answer["acceptable_categories"] = list(
                acceptable if acceptable is not None
                else ([category] if category else []))
        if needs_reply is not None:
            answer["needs_reply"] = needs_reply
    if observable is None:
        fields = ("workflow",) if task == "workflow" else ("category", "needs_reply")
        observable = {field: "visible" for field in fields}
    return {
        "schema_version": "v3.0",
        "gold_id": gold_id or ("gold_" + case_id),
        "case_id": case_id,
        "review_status": review_status,
        "human_seal": human_seal,
        "reviewer": reviewer,
        "authorized": authorized,
        "source": source,
        "observable": observable,
        "hidden_evidence": list(hidden_evidence or []),
        "answer": answer,
    }


_DEFAULTS = {"category": "Action", "needs_reply": True, "confidence": 0.9,
             "summary": "a short sentence", "reason": "because"}


def attempt(case_id, run_id=None, number=1, *, profile="native",
            status="ok", category=PRODUCED, needs_reply=PRODUCED,
            confidence=None, summary=None, reason=None, tool_events=None,
            final_state=None, request_sha256=None, adapter_id="fake-native"):
    parsed = {}
    provenance = {}
    values = {"category": category, "needs_reply": needs_reply,
              "confidence": confidence, "summary": summary, "reason": reason}
    for field, value in values.items():
        if value is PRODUCED:
            parsed[field] = _DEFAULTS[field]
            provenance[field] = "produced"
        elif value is None:
            provenance[field] = "missing"
        else:
            parsed[field] = value
            provenance[field] = "produced"
    if final_state is not None:
        parsed["final_state"] = final_state
    record = {
        "schema_version": "v3.0",
        "run_id": run_id,  # None: T.run stamps the manifest run_id
        "case_id": case_id,
        "attempt": number,
        "adapter_id": adapter_id,
        "profile": profile,
        "status": status,
        "output": {"raw": "", "parsed": parsed, "error": None},
        "field_provenance": provenance,
        "tool_events": list(tool_events or []),
    }
    if request_sha256 is not None:
        record["request_sha256"] = request_sha256
    return record


def manifest(*, dataset_id="ds-test", model_key="fake-model",
             scorer_revision="3.0", requested_case_ids=("case_0001",),
             requested_profiles=("native",), requested_splits=("development",),
             mock=False, model_identity_source="pinned",
             qualifies_as_baseline=True, **overrides):
    fields = dict(
        dataset_id=dataset_id, dataset_sha256="a" * 64,
        case_manifest_sha256="b" * 64, prompt_revision="native-v3.0",
        prompt_sha256="c" * 64, model_key=model_key, model_revision="rev-1",
        model_artifact_sha256="", adapter_id="fake-native", adapter_revision="1.0",
        scorer_revision=scorer_revision, policy_revision="3.0",
        policy_sha256="d" * 64, engine_contract_sha256="e" * 64,
        calibrator_revision="none",
        generation_config={"temperature": 0}, runtime_config={"cores": 4},
        requested_case_ids=list(requested_case_ids),
        requested_splits=list(requested_splits),
        requested_profiles=list(requested_profiles))
    fields.update(overrides)
    record = identity.build_manifest(**fields)
    record["mock"] = mock
    record["model_identity_source"] = model_identity_source
    record["qualifies_as_baseline"] = qualifies_as_baseline
    return record


def dataset(cases, golds, *, policies=(), lineage=(), metadata=None,
            dataset_id="ds-test"):
    return {
        "schema_version": "v3.0",
        "dataset_id": dataset_id,
        "cases": list(cases),
        "gold": list(golds),
        "scenarios": [],
        "policies": list(policies),
        "lineage": list(lineage),
        "provenance": [],
        "metadata": metadata or {"review_status": "reviewed"},
    }


def run(manifest_record, attempts, *, metadata=None):
    stamped = []
    for record in attempts:
        record = dict(record)
        if record.get("run_id") is None:
            record["run_id"] = manifest_record["run_id"]
        stamped.append(record)
    return {
        "manifest": manifest_record,
        "attempts": stamped,
        "metadata": metadata or {},
    }
