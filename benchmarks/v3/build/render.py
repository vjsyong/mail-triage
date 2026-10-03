"""Profile rendering through the frozen contracts (WP2 / FR1, FR3).

Triage rendering goes through ``benchmarks.v3.contracts``; nothing here
re-implements the production prompt. The workflow profile is rendered
explicitly (the foundation ships no workflow renderer) and is never given
native-parser credit.
"""
from .. import contracts
from . import recipes


def render_triage(profile, msg, policy, full_body=None):
    """Render one triage request for ``profile``.

    ``native`` and ``full_context`` carry no extra card; ``policy_conditioned``
    carries the trusted policy card explicitly so results can never be pooled
    with native triage.
    """
    cats = recipes.policy_category_names(policy) or list(contracts.DEFAULT_CATEGORIES)
    owner = policy.get("owner", "")
    if profile == contracts.NATIVE_PROFILE:
        req = contracts.build_native_request(msg, cats, owner)
    elif profile == contracts.POLICY_PROFILE:
        req = contracts.build_policy_request(msg, policy, cats, owner)
    elif profile == contracts.FULL_CONTEXT_PROFILE:
        req = contracts.build_full_context_request(msg, full_body, cats, owner)
    else:
        raise ValueError("render_triage does not handle profile %r" % profile)
    rendered = {"profile": req["profile"], "system": req["system"],
                "user": req["user"], "owner": req.get("owner", ""),
                "categories": list(req.get("categories") or []),
                "params": dict(req.get("params") or {})}
    if profile == contracts.POLICY_PROFILE:
        rendered["policy_id"] = policy["policy_id"]
        rendered["policy"] = policy
    return rendered


def render_workflow(task, trusted_system, owner="", tools=None):
    """Render the explicit workflow profile: trusted system + user task.

    The sandbox fixture (messages/folders/drafts/rules/permissions) rides on the
    case as ``mailbox``; it is not part of the prompt string.  The exposed
    ``tools`` are recorded alongside the rendered request so the semantic request
    hash covers everything the model can actually see.
    """
    rendered = {"profile": contracts.WORKFLOW_PROFILE,
                "system": trusted_system,
                "user": task,
                "owner": owner,
                "categories": [],
                "params": {}}
    if tools is not None:
        rendered["tools"] = list(tools)
    return rendered
