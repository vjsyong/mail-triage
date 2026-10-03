"""Dataset lint: the executable integrity gate for a v3 bundle (WP2/FR2/FR3).

``validate_dataset`` returns a list of problems ([] when clean). It recomputes
its own partitions from content and references rather than trusting declared
ids, so an intentionally leaked clean/attack pair, a duplicated source, a
broken reference or a cross-split near-duplicate is caught even when the
lineage records lie.
"""
from collections import Counter

from .. import contracts, schema
from . import catalog, lineage as lineage_mod

SCHEMA_VERSION = "v3.0"

_BUNDLE_KEYS = ("schema_version", "dataset_id", "cases", "gold", "scenarios",
                "policies", "lineage", "provenance", "metadata")
_TRIAGE_PROFILES = set(contracts.TRIAGE_PROFILES)
_PRIVATE_SPLITS = {"calibration", "private_test", "private_shift", "real_holdout"}
_MAILBOX_KEYS = ("messages", "folders", "drafts", "rules", "permissions")


def _index(records, key):
    out = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        out.setdefault(rec.get(key), rec)
    return out


def _duplicates(records, key, label, errs):
    counts = Counter(r.get(key) for r in records if isinstance(r, dict))
    for value, n in sorted(counts.items(), key=lambda kv: str(kv[0])):
        if n > 1:
            errs.append("%s: duplicate %s %r (%d records)" % (label, key, value, n))


def validate_dataset(bundle):
    """Return a list of integrity problems for a bundle ([] when clean)."""
    errs = []
    if not isinstance(bundle, dict):
        return ["bundle must be a mapping"]
    for key in _BUNDLE_KEYS:
        if key not in bundle:
            errs.append("bundle missing %r" % key)
    if errs:
        return errs
    if bundle.get("schema_version") != SCHEMA_VERSION:
        errs.append("bundle.schema_version must be %r" % SCHEMA_VERSION)
    if not (isinstance(bundle.get("dataset_id"), str) and bundle["dataset_id"].strip()):
        errs.append("bundle.dataset_id must be a non-empty string")

    cases, golds = bundle["cases"], bundle["gold"]
    scenarios, policies = bundle["scenarios"], bundle["policies"]
    lineages, provenance = bundle["lineage"], bundle["provenance"]
    for label, records in (("case", cases), ("gold", golds),
                           ("scenario", scenarios), ("policy", policies),
                           ("lineage", lineages), ("provenance", provenance)):
        if not isinstance(records, list):
            errs.append("%s records must be a list" % label)

    # -- schema validation -------------------------------------------------
    for kind, records, key in (("case", cases, "case_id"), ("gold", golds, "gold_id"),
                               ("scenario", scenarios, "scenario_id"),
                               ("policy", policies, "policy_id"),
                               ("lineage", lineages, "lineage_id"),
                               ("provenance", provenance, "provenance_id")):
        for rec in records:
            if not isinstance(rec, dict):
                errs.append("%s record is not a mapping" % kind)
                continue
            for problem in schema.validate_artifact(kind, rec):
                errs.append("%s %s: %s" % (kind, rec.get(key), problem))

    for kind, records, key in (("case", cases, "case_id"), ("gold", golds, "gold_id"),
                               ("scenario", scenarios, "scenario_id"),
                               ("policy", policies, "policy_id"),
                               ("lineage", lineages, "lineage_id"),
                               ("provenance", provenance, "provenance_id")):
        _duplicates(records, key, kind, errs)

    cases_by_id = _index(cases, "case_id")
    golds_by_id = _index(golds, "gold_id")
    scenarios_by_id = _index(scenarios, "scenario_id")
    lineages_by_id = _index(lineages, "lineage_id")
    policies_by_id = _index(policies, "policy_id")
    prov_by_id = _index(provenance, "provenance_id")

    scenario_message_ids = {}
    for scn in scenarios:
        for m in scn.get("messages") or []:
            scenario_message_ids.setdefault(m.get("message_id"), scn.get("scenario_id"))

    # -- reference resolution ---------------------------------------------
    for case in cases:
        cid = case.get("case_id")
        if case.get("scenario_id") not in scenarios_by_id:
            errs.append("case %s references unknown scenario %r"
                        % (cid, case.get("scenario_id")))
        if case.get("lineage_id") not in lineages_by_id:
            errs.append("case %s references unknown lineage %r"
                        % (cid, case.get("lineage_id")))
        if case.get("policy_id") not in policies_by_id:
            errs.append("case %s references unknown policy %r"
                        % (cid, case.get("policy_id")))
        gold = golds_by_id.get(case.get("gold_id"))
        if gold is None:
            errs.append("case %s references unknown gold %r"
                        % (cid, case.get("gold_id")))
        elif gold.get("case_id") != cid:
            errs.append("case %s gold %s belongs to case %r"
                        % (cid, case.get("gold_id"), gold.get("case_id")))
    for gold in golds:
        if gold.get("case_id") not in cases_by_id:
            errs.append("gold %s references unknown case %r"
                        % (gold.get("gold_id"), gold.get("case_id")))
        if gold.get("source") == "real_mail" and not gold.get("authorized"):
            errs.append("gold %s is real_mail but not authorized" % gold.get("gold_id"))
    for scn in scenarios:
        if scn.get("source_provenance") not in prov_by_id:
            errs.append("scenario %s references unknown provenance %r"
                        % (scn.get("scenario_id"), scn.get("source_provenance")))
        if scn.get("recipient", {}).get("policy_id") not in policies_by_id:
            errs.append("scenario %s references unknown recipient policy %r"
                        % (scn.get("scenario_id"),
                           scn.get("recipient", {}).get("policy_id")))
    for lin in lineages:
        root = lin.get("root_id")
        if root not in scenarios_by_id:
            errs.append("lineage %s references unknown root scenario %r"
                        % (lin.get("lineage_id"), root))
        for member in lin.get("members") or []:
            if member not in cases_by_id:
                errs.append("lineage %s references unknown member %r"
                            % (lin.get("lineage_id"), member))
        known = {m.get("message_id")
                 for m in (scenarios_by_id.get(root, {}).get("messages") or [])}
        for mid in lin.get("source_message_ids") or []:
            if mid not in known:
                errs.append("lineage %s source message %r not in scenario %r"
                            % (lin.get("lineage_id"), mid, root))

    # -- provenance reuse gate (fail closed) ------------------------------
    for scn in scenarios:
        prov = prov_by_id.get(scn.get("source_provenance"))
        if not prov:
            continue
        try:
            catalog.assert_reusable(prov)
        except ValueError as exc:
            errs.append("scenario %s: %s" % (scn.get("scenario_id"), exc))

    # -- rendered-input contract consistency ------------------------------
    for case in cases:
        cid = case.get("case_id")
        rendered = case.get("rendered_input") or {}
        profile = case.get("input_profile")
        if rendered.get("profile") != profile:
            errs.append("case %s rendered profile %r != input_profile %r"
                        % (cid, rendered.get("profile"), profile))
        if not rendered.get("system") or not rendered.get("user"):
            errs.append("case %s rendered_input needs non-empty system and user" % cid)
        task = case.get("task")
        if task == "workflow" or profile == contracts.WORKFLOW_PROFILE:
            if task != "workflow" or profile != contracts.WORKFLOW_PROFILE:
                errs.append("case %s workflow task/profile mismatch" % cid)
            mailbox = case.get("mailbox")
            if not isinstance(mailbox, dict):
                errs.append("case %s workflow needs a mailbox fixture" % cid)
            else:
                for key in _MAILBOX_KEYS:
                    if key not in mailbox:
                        errs.append("case %s mailbox missing %r" % (cid, key))
                if not isinstance(mailbox.get("permissions"), dict):
                    errs.append("case %s mailbox.permissions must be a mapping" % cid)
                for m in mailbox.get("messages") or []:
                    for field in ("id", "from_addr", "to_addr", "subject",
                                  "body", "folder"):
                        if field not in m:
                            errs.append("case %s mailbox message missing %r"
                                        % (cid, field))
        else:
            if profile not in _TRIAGE_PROFILES:
                errs.append("case %s triage task has profile %r" % (cid, profile))
            if task not in ("decision", "full_response"):
                errs.append("case %s unexpected triage task %r" % (cid, task))
        if profile == contracts.POLICY_PROFILE:
            card = rendered.get("policy")
            if not isinstance(card, dict):
                errs.append("case %s policy_conditioned lacks its trusted policy card"
                            % cid)
            elif rendered.get("policy_id") != case.get("policy_id"):
                errs.append("case %s policy card id %r != policy_id %r"
                            % (cid, rendered.get("policy_id"), case.get("policy_id")))
        elif "policy" in rendered:
            errs.append("case %s profile %s must not carry a policy card"
                        % (cid, profile))

    # -- gold isolation ----------------------------------------------------
    for case in cases:
        gold = golds_by_id.get(case.get("gold_id"))
        if not gold:
            continue
        for leak in contracts.find_gold_leakage(case.get("rendered_input"), gold):
            errs.append("case %s gold leaked into rendered input: %s"
                        % (case.get("case_id"), leak))
        if case.get("mailbox"):
            for leak in contracts.find_gold_leakage(case["mailbox"], gold):
                errs.append("case %s gold leaked into mailbox fixture: %s"
                            % (case.get("case_id"), leak))

    # -- split / lineage consistency --------------------------------------
    case_split = {c.get("case_id"): c.get("split") for c in cases}
    for lin in lineages:
        part = lin.get("partition")
        for member in lin.get("members") or []:
            if case_split.get(member) != part:
                errs.append("lineage %s partition %r != member %s split %r"
                            % (lin.get("lineage_id"), part, member,
                               case_split.get(member)))
    for case in cases:
        parent = (case.get("relation") or {}).get("parent_case_id")
        if parent:
            if parent not in cases_by_id:
                errs.append("case %s references unknown parent %r"
                            % (case.get("case_id"), parent))
            elif case_split.get(parent) != case.get("split"):
                errs.append("case %s parent %s is in a different split"
                            % (case.get("case_id"), parent))

    # -- recomputed connected components (content + refs, not declared ids) --
    source_ids = {}
    for case in cases:
        scn = scenarios_by_id.get(case.get("scenario_id"))
        if scn:
            source_ids[case.get("case_id")] = [m.get("message_id")
                                               for m in scn.get("messages") or []]
    edges, near_pairs = lineage_mod.case_edges(cases, source_ids=source_ids)
    groups = lineage_mod.connected_components([c["case_id"] for c in cases], edges)
    for members in groups.values():
        splits = sorted({case_split.get(m) for m in members if case_split.get(m)})
        if len(splits) > 1:
            errs.append("connected lineage/source/near-duplicate component spans "
                        "splits %s: %s" % (splits, ", ".join(sorted(members))))
    for a, b in near_pairs:
        if case_split.get(a) != case_split.get(b):
            errs.append("near-duplicate cases %s and %s sit in different splits"
                        % (a, b))

    # -- private / public export boundary ---------------------------------
    metadata = bundle.get("metadata") or {}
    private_cases = [c for c in cases if c.get("split") in _PRIVATE_SPLITS]
    private_present = bool(private_cases)
    if metadata.get("visibility") == "public" and private_present:
        errs.append("public bundle contains private/calibration records")
    if bool(metadata.get("contains_private")) != private_present:
        errs.append("metadata.contains_private=%r but private records present=%r"
                    % (metadata.get("contains_private"), private_present))
    if metadata.get("review_status") not in schema.REVIEW_STATUS:
        errs.append("metadata.review_status must be one of %s" % (schema.REVIEW_STATUS,))
    if metadata.get("review_status") == "draft":
        for gold in golds:
            if gold.get("review_status") != "draft" or gold.get("human_seal"):
                errs.append("draft bundle carries a reviewed/sealed gold %r"
                            % gold.get("gold_id"))
    return errs
