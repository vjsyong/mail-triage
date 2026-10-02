#!/usr/bin/env python3
"""Unified instrumented runner for benchmark v2 (WP3).

One execution path produces both correctness artifacts and timing, so the
latency pass can never drift from the quality pass (a v1 flaw).  It:

- replays the production prompt snapshot (``prompts.json``) and loop constants;
- records every emitted tool call, including per-turn overflow calls as
  ``skipped`` events, so nothing a model emitted is invisible to scoring;
- enforces the transcript budget and records when it trips;
- captures latency milestones separately: first stream event, first visible
  text, first tool call, completion, and decode time;
- writes immutable attempt records through ``run_manager``.

Network access is isolated in :class:`BenchClient`; the loop functions accept a
duck-typed client, so they can be unit-tested with a fake offline client.
"""
import json
import os
import sys
import time
import hashlib

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
CORPUS = os.path.join(V2, "corpus")
CASES = os.path.join(V2, "cases")
sys.path.insert(0, V2)

from scoring.base import extract_json  # noqa: E402

HARNESS_REVISION = "v2.0"

with open(os.path.join(HERE, "prompts.json")) as f:
    PROMPTS = json.load(f)

LOOP = PROMPTS["assistant_loop"]
MAX_STEPS = LOOP["MAX_STEPS"]
MAX_CALLS_PER_TURN = LOOP["MAX_CALLS_PER_TURN"]
RESULT_CHARS = LOOP["RESULT_CHARS"]
TRANSCRIPT_BUDGET = LOOP["TRANSCRIPT_BUDGET"]
ASSISTANT_TOOLS = PROMPTS["assistant_tools"]
PERMS = PROMPTS["permissions_text_default"]
FROZEN_TODAY = "2026-09-30 (Wed)"
SEAN_EMAIL = "seanyong@ust.hk"


# ------------------------------------------------------------------ helpers

def json_args(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except (TypeError, ValueError):
        pass
    try:
        import ast
        val = ast.literal_eval(raw)
        return val if isinstance(val, dict) else {"value": val}
    except Exception:
        return {}


def truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " \u2026[truncated]"


def assistant_system(sim, page_path=None):
    system = PROMPTS["assistant_system_template"] % {
        "user": SEAN_EMAIL, "today": FROZEN_TODAY,
        "max_calls": MAX_CALLS_PER_TURN, "permissions": PERMS}
    ctx = ("CURRENT STATE\n"
           "Rules (top to bottom, first match wins):\n%s\n\n"
           "Flows (multi-step automations; run after rules, first matching flow wins):\n%s\n\n"
           "LLM classifier categories: Action, Notification, Newsletter, Receipt, Personal, Promo\n"
           "Category \u2192 folder map: Notification=Notifications, Newsletter=Newsletters, "
           "Receipt=Receipts, Promo=Promotions\n"
           "Watched folders: INBOX | check interval: 90s\n"
           "Indexed messages: %d | assistant permissions: %s"
           % (sim.rules_text(), sim.flows_text(), len(sim.messages), PERMS))
    system += "\n\n" + ctx
    if page_path:
        system += "\n\nCURRENT PAGE: %s" % page_path
    return system


def transcript_chars(messages):
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        for tc in m.get("tool_calls") or []:
            n += len(str(tc))
    return n


# ------------------------------------------------------------------ client

class BenchClient(object):
    """OpenAI-compatible client with per-turn latency instrumentation."""

    def __init__(self, base, model, timeout=240, temperature=0.0, top_p=None,
                 thinking_mode="auto"):
        self.base = base.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.top_p = top_p
        self.thinking_mode = thinking_mode
        import requests
        self.sess = requests.Session()

    def _thinking_kwarg(self, thinking):
        if self.thinking_mode == "falsekw":
            return {"enable_thinking": False}
        if self.thinking_mode == "explicit":
            return {"enable_thinking": bool(thinking)}
        if self.thinking_mode == "off":
            return None
        return {"enable_thinking": True} if thinking else None

    def _base_payload(self, system, messages, tools, thinking, max_tokens):
        payload = {"model": self.model, "temperature": self.temperature,
                   "max_tokens": int(max_tokens),
                   "messages": [{"role": "system", "content": system}] + list(messages)}
        if self.top_p:
            payload["top_p"] = self.top_p
        if tools:
            payload["tools"] = tools
        ctk = self._thinking_kwarg(thinking)
        if ctk is not None:
            payload["chat_template_kwargs"] = ctk
        return payload

    def chat_turn(self, system, messages, tools=None, thinking=True, max_tokens=2500,
                  stream=True, json_mode=False):
        """One model turn. Returns content/calls/usage + metrics + request hash."""
        payload = self._base_payload(system, messages, tools, thinking, max_tokens)
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
            payload["repetition_penalty"] = 1.05
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        t0 = time.time()
        first_event = first_visible = first_tool = None
        content, reasoning, calls, finish, usage = [], [], [], None, {}
        if stream:
            r = self.sess.post(self.base + "/chat/completions", json=payload,
                               timeout=self.timeout, stream=True)
            if r.status_code != 200:
                raise RuntimeError("LLM HTTP %s: %s" % (r.status_code, (r.text or "")[:300]))
            tool_state = {}
            try:
                for raw in r.iter_lines():
                    if not raw:
                        continue
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line or line == "[DONE]":
                        if line == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    ch = choices[0]
                    delta = ch.get("delta") or {}
                    if delta.get("reasoning") or delta.get("reasoning_content"):
                        if first_event is None:
                            first_event = time.time() - t0
                        reasoning.append(delta.get("reasoning") or delta.get("reasoning_content"))
                    if delta.get("content"):
                        if first_event is None:
                            first_event = time.time() - t0
                        if first_visible is None:
                            first_visible = time.time() - t0
                        content.append(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        if first_event is None:
                            first_event = time.time() - t0
                        if first_tool is None:
                            first_tool = time.time() - t0
                        try:
                            idx = int(tc.get("index") or 0)
                        except (TypeError, ValueError):
                            idx = 0
                        st = tool_state.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            st["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            st["name"] += fn["name"]
                        if fn.get("arguments"):
                            st["arguments"] += fn["arguments"]
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
            finally:
                r.close()
            calls = [{"id": tool_state[i]["id"] or ("call_%d" % i),
                      "name": tool_state[i]["name"], "arguments": tool_state[i]["arguments"]}
                     for i in sorted(tool_state)]
        else:
            r = self.sess.post(self.base + "/chat/completions", json=payload,
                               timeout=self.timeout)
            if r.status_code != 200:
                raise RuntimeError("LLM HTTP %s: %s" % (r.status_code, (r.text or "")[:300]))
            data = r.json()
            ch = data["choices"][0]
            msg = ch.get("message") or {}
            content = [msg.get("content") or ""]
            reasoning = [msg.get("reasoning") or msg.get("reasoning_content") or ""]
            usage = data.get("usage") or {}
            finish = ch.get("finish_reason")
            calls = []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments")
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(raw_args or {})
                calls.append({"id": tc.get("id") or ("call_%d" % len(calls)),
                              "name": fn.get("name") or "", "arguments": raw_args})
            first_event = first_visible = first_tool = time.time() - t0
            if calls and not (content and content[0]):
                first_visible = None

        completion = time.time() - t0
        ct = int(usage.get("completion_tokens") or 0)
        pt = int(usage.get("prompt_tokens") or 0)
        metrics = {
            "wall_s": round(completion, 3),
            "first_event_s": round(first_event, 3) if first_event is not None else None,
            "first_visible_s": round(first_visible, 3) if first_visible is not None else None,
            "first_tool_call_s": round(first_tool, 3) if first_tool is not None else None,
            "completion_s": round(completion, 3),
            "decode_s": round(completion - first_event, 3) if first_event is not None else None,
            "prompt_tokens": pt, "completion_tokens": ct,
        }
        req_hash = hashlib.sha256(json.dumps(payload, sort_keys=True,
                                             ensure_ascii=False).encode()).hexdigest()
        return {"content": "".join(content) if isinstance(content, list) else (content or ""),
                "reasoning": "".join(reasoning) if isinstance(reasoning, list) else (reasoning or ""),
                "tool_calls": calls, "finish": finish, "usage": usage,
                "metrics": metrics, "request_sha256": req_hash}


def _attempt_base(case, suite, run_id, model, m=None):
    m = m or {}
    a = {"case_id": case["id"], "suite": suite, "model": model, "run_id": run_id,
         "attempt": 1, "status": "ok",
         "wall_s": m.get("wall_s"), "ttft_s": m.get("first_event_s"),
         "first_visible_s": m.get("first_visible_s"),
         "first_tool_call_s": m.get("first_tool_call_s"),
         "completion_s": m.get("completion_s"), "decode_s": m.get("decode_s"),
         "prompt_tokens": m.get("prompt_tokens", 0),
         "completion_tokens": m.get("completion_tokens", 0)}
    if m.get("request_sha256"):
        a["request_sha256"] = m["request_sha256"]
    return a


# ------------------------------------------------------------------ suites

def run_classification(client, case, run_id, model):
    res = client.chat_turn(PROMPTS["classify_system"], [{"role": "user", "content": case["user"]}],
                           tools=None, thinking=True, max_tokens=4096, stream=False, json_mode=True)
    parsed, perr = extract_json(res["content"])
    retried = False
    if parsed is None and res.get("finish") == "length":
        retried = True
        res = client.chat_turn(PROMPTS["classify_system"],
                               [{"role": "user", "content": case["user"]}],
                               thinking=True, max_tokens=8192, stream=False, json_mode=True)
        parsed, perr = extract_json(res["content"])
    a = _attempt_base(case, "classification", run_id, model, res["metrics"])
    a["request_sha256"] = res["request_sha256"]
    a["output"] = {"parsed": parsed, "content": res["content"][:4000],
                   "finish": res.get("finish")}
    a["retried"] = retried
    a["error"] = perr if parsed is None else None
    return a


def run_assistant(client, case, run_id, model, corpus_dir=CORPUS, no_stream=False):
    from harness.tool_sim_v2 import ToolSim
    sim = ToolSim(corpus_dir, case_id=case["id"])
    system = assistant_system(sim, case.get("page_path"))
    convo = list(case.get("history") or []) + [{"role": "user", "content": case["user"]}]
    all_calls = []
    reply = ""
    steps = 0
    budget_tripped = False
    metrics = {}
    error = None
    total_chars = transcript_chars(convo)
    try:
        while steps < MAX_STEPS + 1:
            steps += 1
            use_tools = ASSISTANT_TOOLS if steps <= MAX_STEPS else None
            res = client.chat_turn(system, convo, tools=use_tools, thinking=True,
                                   max_tokens=2500, stream=not no_stream)
            metrics = res["metrics"]
            calls = res.get("tool_calls") or []
            if not calls or use_tools is None:
                reply = res.get("content") or ""
                break
            convo.append({"role": "assistant", "content": res.get("content") or None,
                          "reasoning": res.get("reasoning") or None,
                          "tool_calls": [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"],
                                                       "arguments": c["arguments"]}}
                                         for c in calls]})
            for i, c in enumerate(calls):
                status = "executed" if i < MAX_CALLS_PER_TURN else "skipped"
                args = json_args(c["arguments"])
                r = sim.call(c["name"], args, status=status)
                all_calls.append({"name": c["name"], "args": args, "status": status,
                                  "ok": bool(r.get("ok")),
                                  "summary": (r.get("summary") or "")[:300]})
                payload = json.dumps(r.get("result", {}), ensure_ascii=False)
                body = truncate(payload, RESULT_CHARS)
                total_chars += len(body)
                convo.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"],
                              "content": body})
                if total_chars > TRANSCRIPT_BUDGET:
                    budget_tripped = True
                    break
            if budget_tripped:
                break
    except Exception as exc:
        error = repr(exc)
    a = _attempt_base(case, "assistant", run_id, model, metrics)
    a["output"] = {"calls": all_calls, "reply": reply[:6000], "state": sim.snapshot(),
                   "steps": steps, "budget_tripped": budget_tripped,
                   "transcript_chars": total_chars}
    a["tool_events"] = sim.events
    a["error"] = error
    if error:
        a["status"] = "error"
    return a


def run_drafting(client, case, run_id, model, corpus_msgs):
    m = corpus_msgs[case["msg_id"]]
    guidance = ""
    if case.get("template_text"):
        guidance += ("Use this reply template as guidance:\n---\n%s\n---\n" % case["template_text"])
    if case.get("instructions"):
        guidance += ("Follow these instructions (they win over the template):\n%s\n"
                     % case["instructions"][:1000])
    user = ("%sOriginal message:\nFrom: %s\nSubject: %s\nDate: %s\n\n%s"
            % (guidance, m["from"], m["subject"], m["date"], m["body"][:6000]))
    res = client.chat_turn(PROMPTS["draft_system"], [{"role": "user", "content": user}],
                           thinking=False, max_tokens=2048, stream=False, json_mode=False)
    a = _attempt_base(case, "drafting", run_id, model, res["metrics"])
    a["request_sha256"] = res["request_sha256"]
    a["output"] = {"reply": (res["content"] or "").strip()[:6000]}
    return a


def run_rules(client, case, run_id, model):
    lines = ["%d. tag=%s | from=%s | subject=%s"
             % (i, t.get("tag"), (t.get("from") or "")[:70], (t.get("subject") or "")[:90])
             for i, t in enumerate(case["tagged"], 1)]
    context = ("EXISTING RULES (do not duplicate):\n%s\n\n"
               "EXISTING FLOWS (multi-step; do not duplicate):\n%s\n\n"
               "CATEGORIES: %s\n\nTAGGED EXAMPLES:\n%s"
               % (case["existing_rules"], case["existing_flows"], case["categories"],
                  "\n".join(lines)))
    res = client.chat_turn(PROMPTS["learn_system"] + "\n\n" + context,
                           [{"role": "user", "content": "Propose rules matching my tagging."}],
                           thinking=False, max_tokens=2048, stream=False, json_mode=True)
    parsed, perr = extract_json(res["content"])
    a = _attempt_base(case, "rules", run_id, model, res["metrics"])
    a["request_sha256"] = res["request_sha256"]
    a["output"] = {"parsed": parsed, "content": (res["content"] or "")[:8000]}
    a["error"] = perr if parsed is None else None
    return a


def run_simulate(client, case, run_id, model):
    res = client.chat_turn(PROMPTS["example_draft_system"],
                           [{"role": "user", "content": case["rule_text"] + "\n\nWrite the example email now."}],
                           thinking=False, max_tokens=1024, stream=False, json_mode=True)
    parsed, perr = extract_json(res["content"])
    a = _attempt_base(case, "simulate", run_id, model, res["metrics"])
    a["request_sha256"] = res["request_sha256"]
    a["output"] = {"parsed": parsed, "content": (res["content"] or "")[:4000]}
    a["error"] = perr if parsed is None else None
    return a


def run_summary(client, case, run_id, model):
    res = client.chat_turn(PROMPTS["summarize_thoughts_system"],
                           [{"role": "user", "content": case["reasoning"][:6000]}],
                           thinking=False, max_tokens=60, stream=False, json_mode=False)
    a = _attempt_base(case, "summary", run_id, model, res["metrics"])
    a["request_sha256"] = res["request_sha256"]
    a["output"] = {"reply": (res["content"] or "").strip()[:400]}
    return a


def run_case(client, case, run_id, model, corpus_msgs=None, no_stream=False):
    suite = case["class"]
    if suite == "classification":
        return run_classification(client, case, run_id, model)
    if suite == "assistant":
        return run_assistant(client, case, run_id, model, no_stream=no_stream)
    if suite == "drafting":
        return run_drafting(client, case, run_id, model, corpus_msgs or {})
    if suite == "rules":
        return run_rules(client, case, run_id, model)
    if suite == "simulate":
        return run_simulate(client, case, run_id, model)
    if suite == "summary":
        return run_summary(client, case, run_id, model)
    raise ValueError("unknown suite %r" % suite)


def load_corpus():
    out = {}
    with open(os.path.join(CORPUS, "messages.jsonl")) as f:
        for line in f:
            m = json.loads(line)
            out[m["id"]] = m
    return out


def main():
    import argparse
    from harness import run_manager
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--suite", required=True,
                    choices=run_manager.SUITES)
    ap.add_argument("--base", required=True)
    ap.add_argument("--model-name", required=True)
    ap.add_argument("--split", default=None, choices=["dev", "acceptance"])
    ap.add_argument("--results", default=os.path.join(V2, "results"))
    ap.add_argument("--thinking-mode", default="auto")
    ap.add_argument("--assistant-no-stream", action="store_true")
    ap.add_argument("--retries", type=int, default=1)
    args = ap.parse_args()

    client = BenchClient(args.base, args.model_name, thinking_mode=args.thinking_mode)
    corpus_msgs = load_corpus()
    cases = run_manager.load_suite(args.suite)
    if args.split:
        cases = [c for c in cases if c.get("split") == args.split]
    for i, case in enumerate(cases, 1):
        attempt_no = 1
        while True:
            try:
                a = run_case(client, case, args.run_id, args.model_name,
                             corpus_msgs=corpus_msgs, no_stream=args.assistant_no_stream)
                a["attempt"] = attempt_no
                run_manager.record_attempt(args.run_id, args.suite, a, results=args.results)
                status = a["status"]
                break
            except Exception as exc:
                attempt_no += 1
                if attempt_no > args.retries:
                    run_manager.record_attempt(args.run_id, args.suite, {
                        "case_id": case["id"], "suite": args.suite, "model": args.model_name,
                        "run_id": args.run_id, "attempt": attempt_no - 1,
                        "status": "error", "error": repr(exc),
                    }, results=args.results)
                    status = "EXC"
                    break
        print("  [%d/%d] %-28s %s" % (i, len(cases), case["id"], status), flush=True)
    print("done", args.suite, flush=True)


if __name__ == "__main__":
    main()
