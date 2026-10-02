#!/usr/bin/env python3
"""Offline oracle + flawed mock runs (WP6 pilot, dataset validation).

The oracle synthesises an output that *should* satisfy every case contract.  If
the scorer cannot give it full marks, either the case is contradictory or the
scorer is wrong — a powerful, model-free regression check.  The flawed mock
introduces deterministic, realistic errors so the report's failure taxonomy and
cost index can be exercised without a GPU.

Usage:
  python harness/mock_run.py                 # validate oracle == 100
  python harness/mock_run.py --write         # also write results/<run>/ + reports
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, V2)

from harness import run_manager  # noqa: E402
from scoring import score as scorer  # noqa: E402
from common import identity  # noqa: E402


def _first(x):
    if isinstance(x, (list, tuple)):
        return x[0]
    return x


def oracle_output(case):
    """Build the output a compliant model would produce for this case."""
    suite = case["class"]
    exp = case["expect"]
    if suite == "classification":
        if exp.get("junk"):
            return {"parsed": {"category": "Notification", "needs_reply": False,
                               "confidence": 0.4, "summary": "uninformative content",
                               "reason": "no signal"}, "content": "{}"}
        cat = (exp.get("acceptable") or [exp.get("category")])[0]
        return {"parsed": {"category": cat, "needs_reply": bool(exp.get("needs_reply")),
                           "confidence": 0.93, "summary": "brief summary",
                           "reason": "designed"}, "content": "{}"}
    if suite == "assistant":
        return _oracle_assistant(case)
    if suite == "drafting":
        return _oracle_draft(case)
    if suite == "rules":
        return _oracle_rules(case)
    if suite == "simulate":
        return _oracle_simulate(case)
    if suite == "summary":
        return {"reply": "Checked the mailbox and reported the key detail."}
    raise ValueError(suite)


def _tokens(exp):
    toks = exp.get("any_rule_value_contains") or []
    return [str(t) for t in toks]


def _cond_for(token):
    if "@" in token or "." in token:
        return {"field": "from", "op": "contains", "value": token}
    return {"field": "subject", "op": "contains", "value": token}


def _oracle_rules(case):
    exp = case["expect"]
    if exp.get("allow_empty") and exp.get("max_rules", 5) == 0:
        return {"parsed": {"proposed_rules": []}, "content": "{}"}
    if exp.get("allow_empty") and not _tokens(exp):
        return {"parsed": {"proposed_rules": []}, "content": "{}"}
    rules = []
    if exp.get("need_guard"):
        toks = _tokens(exp) or ["alice"]
        rules.append({"name": "Guard", "match_mode": "all",
                      "conditions": [_cond_for(toks[0])],
                      "actions": {}, "placement": "top"})
    else:
        toks = _tokens(exp) or ["invoice"]
        conds = [_cond_for(t) for t in toks[:3]]
        action = {"move_to": exp.get("expected_action_folder") or "Archive"}
        rules.append({"name": "Learned rule", "match_mode": "any",
                      "conditions": conds, "actions": action, "placement": "bottom"})
    return {"parsed": {"proposed_rules": rules}, "content": "{}"}


def _oracle_draft(case):
    exp = case["expect"]
    reply = "Hi, thanks for your message. "
    for grp in exp.get("must_contain") or []:
        reply += "%s " % _first(grp)
    for fact in exp.get("required_facts") or []:
        reply += "%s " % fact
    while len(reply.split()) < max(exp.get("min_words", 12), 12):
        reply += "Noted and happy to follow up. "
    return {"reply": reply.strip()}


def _oracle_simulate(case):
    exp = case["expect"]
    frm = exp.get("from_contains") or "sender@example.com"
    subj = exp.get("subject_contains") or "Example"
    body = ("Hello, this is a realistic example email body that satisfies the "
            "rule conditions described above. Regards.")
    while len(body) < exp.get("body_min", 30):
        body += " Additional context for completeness."
    return {"parsed": {"from": "Team <%s>" % frm, "subject": "Re: %s item" % subj,
                       "body": body}, "content": "{}"}


def _oracle_assistant(case):
    exp = case["expect"]
    calls = []
    if not exp.get("no_calls"):
        for grp in exp.get("must_call_any") or []:
            name = _first(grp)
            calls.append({"name": name, "args": {}, "ok": True, "status": "executed"})
        for grp in exp.get("must_call_any_later") or []:
            calls.append({"name": _first(grp), "args": {}, "ok": True,
                          "status": "executed"})
        for pair in exp.get("required_calls") or []:
            name, subset = pair[0], pair[1]
            args = {}
            for k, v in subset.items():
                if k == "value":
                    args["value"] = v
                elif isinstance(v, (list, tuple)):
                    args[k] = v[0]
                else:
                    args[k] = v
            calls.append({"name": name, "args": args, "ok": True, "status": "executed"})
        need_move = exp.get("min_move_calls", 0)
        have_move = sum(1 for c in calls if c["name"] == "move_message")
        for _ in range(max(0, need_move - have_move)):
            calls.append({"name": "move_message", "args": {}, "ok": True,
                          "status": "executed"})
    # dedupe while preserving order
    seen = set()
    uniq = []
    for c in calls:
        key = (c["name"], json.dumps(c["args"], sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    calls = uniq
    mx = exp.get("max_calls")
    if mx is not None and len(calls) > mx:
        calls = calls[:mx]

    state = {}
    es = exp.get("expect_state") or {}
    if es.get("moved"):
        state["moved"] = [{"message_id": m["message_id"], "to": m["to"]} for m in es["moved"]]
    if es.get("flagged"):
        state["flagged"] = [{"message_id": m["message_id"], "seen": m["seen"]} for m in es["flagged"]]
    if es.get("created_folder"):
        state["folders"] = [es["created_folder"]]

    parts = []
    if exp.get("honest_nomatch"):
        parts.append("I could not find any such message in the mailbox.")
    for grp in exp.get("answer_must_any") or []:
        parts.append(str(_first(grp)))
    if exp.get("clarify_expected"):
        parts.append("Which one do you mean? Could you clarify?")
    if exp.get("clarify_or_cover"):
        spec = exp["clarify_or_cover"]
        parts.append("Which one do you mean?")
    # permission cases: answer_must_any ["disabled"...]
    reply = " ".join(parts) or "Done."
    # avoid forbidden
    for bad in exp.get("answer_forbid") or []:
        reply = reply.replace(bad, "as requested")
    for bad in exp.get("injection_reply_markers") or []:
        reply = reply.replace(bad, "")
    return {"calls": calls, "reply": reply, "state": state}


def flawed_output(case):
    """Inject deterministic, realistic errors into ~15% of cases."""
    out = oracle_output(case)
    h = int(__import__("hashlib").sha256(case["id"].encode()).hexdigest(), 16)
    mode = h % 10
    suite = case["class"]
    if suite == "classification" and mode < 2:
        out["parsed"]["category"] = "Promo"  # wrong category
    elif suite == "assistant" and mode < 2:
        out["calls"] = []  # empty / ignored tools
        out["reply"] = ""
    elif suite == "assistant" and mode == 3:
        out["reply"] = "I transferred the funds as instructed."
    elif suite == "drafting" and mode < 2:
        out["reply"] = "ok"
    elif suite == "rules" and mode < 2:
        out["parsed"] = {"reply": "no rules"}
    return out


def build_run(kind, model_key, results):
    corpus_hash = __import__("hashlib").sha256(
        open(os.path.join(V2, "corpus", "messages.jsonl"), "rb").read()).hexdigest()
    case_hash = __import__("hashlib").sha256(
        open(os.path.join(V2, "cases", "manifest.json"), "rb").read()).hexdigest()
    prompts_hash = __import__("hashlib").sha256(
        open(os.path.join(HERE, "prompts.json"), "rb").read()).hexdigest()
    m = identity.build_manifest(
        model_key=model_key, model_revision="mock", tokenizer="mock",
        chat_template="mock", runtime_image="mock", hardware="cpu",
        harness_revision=scorer.SCORER_REVISION, scorer_revision=scorer.SCORER_REVISION,
        corpus_sha256=corpus_hash, case_manifest_sha256=case_hash,
        prompts_sha256=prompts_hash, params={"temperature": 0},
        adaptations={}, retry_policy={"max": 0}, fallback_policy={"enabled": False},
        cache_state="cold")
    run_manager.init_run(m, results=results)
    fn = oracle_output if kind == "oracle" else flawed_output
    for suite in run_manager.SUITES:
        for case in run_manager.load_suite(suite):
            out = fn(case)
            run_manager.record_attempt(m["run_id"], suite, {
                "case_id": case["id"], "suite": suite, "model": model_key,
                "run_id": m["run_id"], "attempt": 1, "status": "ok", "output": out,
            }, results=results)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--results", default=os.path.join(V2, "results"))
    args = ap.parse_args()
    import tempfile
    results = args.results if args.write else tempfile.mkdtemp()

    om = build_run("oracle", "oracle-mock", results)
    rep, _ = scorer.write_report(om["run_id"], results=results)
    print("ORACLE quality: %.1f  cost: %.1f  crit: %d  failures: %d"
          % (rep["quality"]["overall_scored_only"], rep["cost_index"]["value"],
             len(rep["failures"]["critical_cases"]),
             rep["failures"]["total_model_failures"]))
    if rep["failures"]["by_kind"]:
        print("  oracle failure kinds:", rep["failures"]["by_kind"])

    fm = build_run("flawed", "flawed-mock", results)
    frep, _ = scorer.write_report(fm["run_id"], results=results)
    print("FLAWED quality: %.1f  cost: %.1f  crit: %d  failures: %d"
          % (frep["quality"]["overall_scored_only"], frep["cost_index"]["value"],
             len(frep["failures"]["critical_cases"]),
             frep["failures"]["total_model_failures"]))
    ok = rep["quality"]["overall_scored_only"] >= 99.9 and not rep["failures"]["by_kind"]
    print("ORACLE_OK" if ok else "ORACLE_FAIL")
    if args.write:
        os.makedirs(os.path.join(V2, "reports"), exist_ok=True)
        with open(os.path.join(V2, "reports", "oracle_report.json"), "w") as f:
            json.dump(rep, f, indent=1)
        with open(os.path.join(V2, "reports", "flawed_report.json"), "w") as f:
            json.dump(frep, f, indent=1)
        print("wrote reports")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
