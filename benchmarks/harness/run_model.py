#!/usr/bin/env python3
"""Benchmark runner: executes frozen cases against an OpenAI-compatible endpoint.

Mirrors the app's production payload construction (see prompts.json extracted
from engine.py @ master 5374d26): classify = non-stream + json_object +
enable_thinking + strip-on-4xx; assistant = stream + tools + repetition_penalty
+ capped tool results + 8-step loop + wrap-up turn.

Usage:
  run_model.py --model qwen3.5-9b --base http://127.0.0.1:8045/v1 \
      --suite classification --out ../results/qwen3.5-9b
  run_model.py ... --suite assistant --subset screen
"""
import argparse
import json
import os
import re
import sys
import time

import requests

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

with open(os.path.join(HERE, "prompts.json")) as f:
    PROMPTS = json.load(f)

FROZEN_TODAY = "2026-09-30 (Wed)"
SEAN_EMAIL = "seanyong@ust.hk"

# ---------------------------------------------------------------- client ----


class BenchClient:
    def __init__(self, base, model, timeout=120, temperature=0.0, top_p=None,
                 thinking_mode="auto"):
        self.base = base.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.top_p = top_p
        self.thinking_mode = thinking_mode  # auto | off | falsekw
        self.sess = requests.Session()

    def _thinking_kwarg(self, thinking):
        """Returns the chat_template_kwargs value (or None = do not send).
        auto: send {enable_thinking: True} when the call requests thinking (app behavior).
        off:  never send (app behavior when llm_thinking=off).
        falsekw: always send {enable_thinking: False} — the model defaults thinking ON
                 when unset, so the adaptation must pin it off on EVERY call site
                 (classify, assistant, draft, rules, simulate, summary)."""
        if self.thinking_mode == "falsekw":
            return {"enable_thinking": False}
        if self.thinking_mode == "off":
            return None
        return {"enable_thinking": True} if thinking else None

    def chat_once(self, system, user, max_tokens=None, json_mode=True, thinking=False,
                  timeout=None):
        """Mirror engine.LLMClient._chat_once (non-stream)."""
        convo = [{"role": "user", "content": user}]
        payload = {"model": self.model, "temperature": self.temperature,
                   "messages": [{"role": "system", "content": system}] + convo}
        if self.top_p:
            payload["top_p"] = self.top_p
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        optional = []
        ctk = self._thinking_kwarg(thinking)
        if ctk is not None:
            payload["chat_template_kwargs"] = ctk
            optional.append("chat_template_kwargs")
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
            optional.append("response_format")
        t0 = time.time()
        r = None
        while True:
            r = self.sess.post(self.base + "/chat/completions", json=payload,
                               timeout=timeout or self.timeout)
            if r.status_code in (400, 404, 422) and optional:
                payload.pop(optional.pop(0), None)
                continue
            break
        wall = time.time() - t0
        r.raise_for_status()
        data = r.json()
        ch = data["choices"][0]
        msg = ch["message"]
        return {"content": msg.get("content") or "",
                "reasoning": msg.get("reasoning") or msg.get("reasoning_content") or "",
                "finish": ch.get("finish_reason"), "usage": data.get("usage") or {},
                "wall": wall}

    def chat_stream(self, system, messages, tools=None, thinking=True, max_tokens=2500):
        """Mirror engine.LLMClient._stream_once: yields events."""
        payload = {"model": self.model, "temperature": self.temperature, "max_tokens": max_tokens,
                   "stream": True, "stream_options": {"include_usage": True},
                   "repetition_penalty": 1.05,
                   "messages": [{"role": "system", "content": system}] + messages}
        if self.top_p:
            payload["top_p"] = self.top_p
        if tools:
            payload["tools"] = tools
        ctk = self._thinking_kwarg(thinking)
        if ctk is not None:
            payload["chat_template_kwargs"] = ctk
        r = None
        while True:
            r = self.sess.post(self.base + "/chat/completions", json=payload,
                               timeout=self.timeout, stream=True)
            if r.status_code in (400, 404, 422):
                if "chat_template_kwargs" in payload:
                    payload.pop("chat_template_kwargs", None)
                    r.close()
                    continue
                if "repetition_penalty" in payload:
                    payload.pop("repetition_penalty", None)
                    r.close()
                    continue
            break
        try:
            if r.status_code != 200:
                raise RuntimeError("LLM HTTP %s: %s" % (r.status_code, (r.text or "")[:300]))
            tool_state = {}
            finish = None
            usage = None
            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                if line == "[DONE]":
                    break
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
                text = delta.get("reasoning") or delta.get("reasoning_content")
                if text:
                    yield {"type": "reasoning_delta", "text": text}
                if delta.get("content"):
                    yield {"type": "content_delta", "text": delta["content"]}
                for tc in delta.get("tool_calls") or []:
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
            if tool_state:
                calls = [{"id": tool_state[i]["id"] or ("call_%d" % i),
                          "name": tool_state[i]["name"],
                          "arguments": tool_state[i]["arguments"]}
                         for i in sorted(tool_state)]
                yield {"type": "tool_calls", "calls": calls}
            yield {"type": "turn_done", "finish_reason": finish, "usage": usage}
        finally:
            r.close()


def json_args(raw):
    """Mirror engine._json_args."""
    if not raw:
        return {}
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


def repetition_loop(text, tail=200):
    if len(text) < tail * 2 + 40:
        return False
    probe = text[-tail:]
    return probe in text[:-tail]


# ---------------------------------------------------------------- suites ----

from tool_sim import ToolSim  # noqa: E402

CORPUS = os.path.join(HERE, "..", "corpus")
CASES = os.path.join(HERE, "..", "cases")

MAX_STEPS = PROMPTS["assistant_loop"]["MAX_STEPS"]
MAX_CALLS_PER_TURN = PROMPTS["assistant_loop"]["MAX_CALLS_PER_TURN"]
RESULT_CHARS = PROMPTS["assistant_loop"]["RESULT_CHARS"]
TRANSCRIPT_BUDGET = PROMPTS["assistant_loop"]["TRANSCRIPT_BUDGET"]
ASSISTANT_TOOLS = PROMPTS["assistant_tools"]
PERMS = PROMPTS["permissions_text_default"]


def load_cases(name):
    with open(os.path.join(CASES, name + ".jsonl")) as f:
        return [json.loads(l) for l in f if l.strip()]


def assistant_system(sim, page_path=None):
    system = PROMPTS["assistant_system_template"] % {
        "user": SEAN_EMAIL, "today": FROZEN_TODAY,
        "max_calls": MAX_CALLS_PER_TURN, "permissions": PERMS}
    ctx = ("CURRENT STATE\n"
           "Rules (top to bottom, first match wins):\n%s\n\n"
           "Flows (multi-step automations; run after rules, first matching flow wins):\n%s\n\n"
           "LLM classifier categories: Action, Notification, Newsletter, Receipt, Personal, Promo\n"
           "Category \u2192 folder map: Notification=Notifications, Newsletter=Newsletters, Receipt=Receipts, Promo=Promotions\n"
           "Watched folders: INBOX | check interval: 90s\n"
           "Indexed messages: %d | assistant permissions: %s"
           % (sim.rules_text(), sim.flows_text(), len(sim.messages), PERMS))
    system += "\n\n" + ctx
    if page_path:
        system += "\n\nCURRENT PAGE: %s" % page_path
    return system


def run_classification(client, case, model_key, out_f):
    t0 = time.time()
    thinking = True
    res = client.chat_once(PROMPTS["classify_system"], case["user"],
                           max_tokens=4096, json_mode=True, thinking=thinking, timeout=120)
    content = res["content"]
    m = re.search(r"\{.*\}", content, re.S)
    retried = False
    if not m and res.get("finish") == "length":
        retried = True
        res2 = client.chat_once(PROMPTS["classify_system"], case["user"],
                                max_tokens=8192, json_mode=True, thinking=thinking, timeout=180)
        res = res2
        content = res["content"]
        m = re.search(r"\{.*\}", content, re.S)
    parsed, perr = None, None
    if m:
        try:
            parsed = json.loads(m.group(0))
        except ValueError as exc:
            perr = repr(exc)
    else:
        perr = "no JSON"
    rec = {"id": case["id"], "suite": "classification", "sub": case["sub"],
           "model": model_key, "content": content[:4000],
           "reasoning_len": len(res.get("reasoning") or ""),
           "parsed": parsed, "parse_error": perr, "retried": retried,
           "finish": res.get("finish"), "usage": res.get("usage"),
           "wall_s": round(time.time() - t0, 2), "ttft_s": round(res.get("wall", 0), 2)}
    out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out_f.flush()
    return rec


def run_assistant(client, case, model_key, out_f):
    sim = ToolSim(CORPUS)
    system = assistant_system(sim, case.get("page_path"))
    convo = [{"role": "user", "content": case["user"]}]
    t0 = time.time()
    ttft = None
    steps = 0
    tools_mode = True
    reply = ""
    all_calls = []
    loop_stopped = False
    finish_reasons = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    error = None
    reasoning_all = []
    trace = []
    try:
        while True:
            steps += 1
            use_tools = ASSISTANT_TOOLS if (tools_mode and steps <= MAX_STEPS) else None
            calls = []
            turn_reasoning, turn_content, turn_probe = [], [], []
            probe_checked = 0
            turn_start = time.time()
            gen = client.chat_stream(system, convo, tools=use_tools, thinking=True,
                                     max_tokens=2500)
            finish = None
            for ev in gen:
                if ttft is None and ev["type"] in ("reasoning_delta", "content_delta"):
                    ttft = time.time() - t0
                if ev["type"] == "reasoning_delta":
                    turn_reasoning.append(ev["text"])
                    turn_probe.append(ev["text"])
                elif ev["type"] == "content_delta":
                    turn_content.append(ev["text"])
                    turn_probe.append(ev["text"])
                elif ev["type"] == "tool_calls":
                    calls = ev["calls"]
                elif ev["type"] == "turn_done":
                    finish = ev.get("finish_reason")
                    u = ev.get("usage") or {}
                    for k in usage_total:
                        usage_total[k] += int(u.get(k) or 0)
                    finish_reasons.append(finish)
                if ev["type"] in ("reasoning_delta", "content_delta"):
                    joined = "".join(turn_probe)
                    if len(joined) - probe_checked >= 64:
                        probe_checked = len(joined)
                        if repetition_loop(joined):
                            loop_stopped = True
                            break
            reasoning_all.append("".join(turn_reasoning))
            if loop_stopped:
                break
            if not calls or use_tools is None:
                reply = "".join(turn_content).strip()
                break
            convo.append({"role": "assistant",
                          "content": "".join(turn_content) or None,
                          "reasoning": "".join(turn_reasoning) or None,
                          "tool_calls": [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"],
                                                       "arguments": c["arguments"]}}
                                         for c in calls]})
            for i, c in enumerate(calls):
                if i >= MAX_CALLS_PER_TURN:
                    res = {"ok": False, "summary": "skipped: too many tool calls in one step",
                           "result": {"error": "per-step tool call limit reached"}}
                else:
                    args = json_args(c["arguments"])
                    res = sim.call(c["name"], args)
                    all_calls.append({"name": c["name"], "args": args,
                                      "ok": bool(res.get("ok")),
                                      "summary": (res.get("summary") or "")[:400],
                                      "step": steps})
                trace.append({"step": steps, "name": c["name"],
                              "summary": (res.get("summary") or "")[:200]})
                payload = json.dumps(res.get("result", {}), ensure_ascii=False)
                convo.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"],
                              "content": truncate(payload, RESULT_CHARS)})
    except Exception as exc:
        error = repr(exc)
    rec = {"id": case["id"], "suite": "assistant", "sub": case["sub"], "model": model_key,
           "steps": steps, "calls": all_calls, "reply": reply[:6000],
           "reasoning_len": sum(len(r) for r in reasoning_all),
           "loop_stopped": loop_stopped, "finish_reasons": finish_reasons,
           "usage": usage_total, "error": error, "trace": trace[:40],
           "wall_s": round(time.time() - t0, 2),
           "ttft_s": round(ttft, 2) if ttft is not None else None}
    out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out_f.flush()
    return rec


def run_drafting(client, case, model_key, corpus_msgs, out_f):
    m = corpus_msgs[case["msg_id"]]
    guidance = ""
    if case.get("template_text"):
        guidance += ("Use this reply template as guidance for structure and tone:\n---\n%s\n---\n"
                     % case["template_text"])
    if case.get("instructions"):
        guidance += ("Follow these instructions for the reply (they win over the "
                     "template's wording where they conflict):\n%s\n" % case["instructions"][:1000])
    user = ("%sOriginal message:\nFrom: %s\nSubject: %s\nDate: %s\n\n%s"
            % (guidance, m["from"], m["subject"], m["date"], m["body"][:6000]))
    t0 = time.time()
    res = client.chat_once(PROMPTS["draft_system"], user, json_mode=False, thinking=False,
                           timeout=180)
    rec = {"id": case["id"], "suite": "drafting", "sub": case["sub"], "model": model_key,
           "reply": (res["content"] or "").strip()[:6000], "finish": res.get("finish"),
           "usage": res.get("usage"), "wall_s": round(time.time() - t0, 2),
           "error": None}
    out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out_f.flush()
    return rec


def run_rules(client, case, model_key, out_f):
    lines = []
    for i, t in enumerate(case["tagged"], 1):
        lines.append("%d. tag=%s | from=%s | subject=%s"
                     % (i, t.get("tag"), (t.get("from") or "")[:70], (t.get("subject") or "")[:90]))
    context = ("EXISTING RULES (do not duplicate):\n%s\n\n"
               "EXISTING FLOWS (multi-step; do not duplicate):\n%s\n\n"
               "CATEGORIES: %s\n\n"
               "TAGGED EXAMPLES (the user's manual labels):\n%s"
               % (case["existing_rules"], case["existing_flows"], case["categories"],
                  "\n".join(lines)))
    t0 = time.time()
    res = client.chat_once(PROMPTS["learn_system"] + "\n\n" + context,
                           "Propose rules matching my tagging.", json_mode=True,
                           thinking=False, timeout=180)
    content = res["content"]
    m = re.search(r"\{.*\}", content or "", re.S)
    parsed, perr = None, None
    if m:
        try:
            parsed = json.loads(m.group(0))
        except ValueError as exc:
            perr = repr(exc)
    else:
        perr = "no JSON"
    rec = {"id": case["id"], "suite": "rules", "sub": case["sub"], "model": model_key,
           "content": (content or "")[:8000], "parsed": parsed, "parse_error": perr,
           "finish": res.get("finish"), "usage": res.get("usage"),
           "wall_s": round(time.time() - t0, 2), "error": None}
    out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out_f.flush()
    return rec


def run_simulate(client, case, model_key, out_f):
    t0 = time.time()
    res = client.chat_once(PROMPTS["example_draft_system"], case["rule_text"] +
                           "\n\nWrite the example email now.", json_mode=True,
                           thinking=False, timeout=120)
    content = res["content"]
    m = re.search(r"\{.*\}", content or "", re.S)
    parsed, perr = None, None
    if m:
        try:
            parsed = json.loads(m.group(0))
        except ValueError as exc:
            perr = repr(exc)
    else:
        perr = "no JSON"
    rec = {"id": case["id"], "suite": "simulate", "sub": case["sub"], "model": model_key,
           "content": (content or "")[:4000], "parsed": parsed, "parse_error": perr,
           "usage": res.get("usage"), "wall_s": round(time.time() - t0, 2), "error": None}
    out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out_f.flush()
    return rec


def run_summary(client, case, model_key, out_f):
    t0 = time.time()
    res = client.chat_once(PROMPTS["summarize_thoughts_system"], case["reasoning"][:6000],
                           json_mode=False, max_tokens=60, thinking=False, timeout=60)
    rec = {"id": case["id"], "suite": "summary", "model": model_key,
           "reply": (res["content"] or "").strip()[:400],
           "usage": res.get("usage"), "wall_s": round(time.time() - t0, 2), "error": None}
    out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out_f.flush()
    return rec


SCREEN_IDS = {
    "classification": {"cls_adv_290", "cls_adv_291", "cls_adv_292", "cls_adv_293",
                       "cls_adv_294", "cls_adv_295", "cls_adv_296", "cls_adv_298",
                       "cls_adv_299", "cls_junk_empty", "cls_junk_mash", "cls_junk_b64",
                       "cls_junk_mojibake", "cls_junk_ctrl", "cls_junk_emoji",
                       "cls_junk_subj", "cls_junk_lorem", "cls_long_statement",
                       "cls_long_minutes", "cls_long_report", "cls_ambig_131",
                       "cls_ambig_298", "cls_normal_101", "cls_normal_130", "cls_ml_297"},
    "assistant": {"asst_i1_phishing", "asst_i2_lab_reply", "asst_i3_admin_claim",
                  "asst_i4_fake_tool", "asst_i5_mandy_inject", "asst_i6_set_needsreply",
                  "asst_h1_parking", "asst_h2_renovation", "asst_h3_tax_auditor",
                  "asst_h4_wedding", "asst_h5_msg9999", "asst_k1_fake_tool",
                  "asst_k2_bad_id", "asst_k3_send_off", "asst_u1_thanks",
                  "asst_x1_invoice_ambiguous", "asst_g1_move_northwind"},
    "drafting": {"draft_d8_injection_draft", "draft_d9_phishing", "draft_d3_talk_confirm"},
    "rules": {"rules_lg3_guard_never_move", "rules_lg5_no_duplicate"},
    "simulate": {"sim_s1"},
    "summary": set(),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--model-name", default=None)
    ap.add_argument("--suite", required=True,
                    choices=["classification", "assistant", "drafting", "rules",
                             "simulate", "summary"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--subset", default="full", choices=["full", "screen", "ids"])
    ap.add_argument("--ids", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--top-p", type=float, default=None)
    ap.add_argument("--thinking-mode", default="auto", choices=["auto", "off", "falsekw"])
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    client = BenchClient(args.base, args.model_name or args.model, timeout=240,
                         temperature=args.temperature, top_p=args.top_p,
                         thinking_mode=args.thinking_mode)

    corpus_msgs = {}
    with open(os.path.join(CORPUS, "messages.jsonl")) as f:
        for line in f:
            m = json.loads(line)
            corpus_msgs[m["id"]] = m

    cases = load_cases(args.suite)
    if args.subset == "screen":
        cases = [c for c in cases if c["id"] in SCREEN_IDS.get(args.suite, set())]
    elif args.subset == "ids":
        want = set(x for x in args.ids.split(",") if x)
        cases = [c for c in cases if c["id"] in want]
    if args.limit:
        cases = cases[:args.limit]

    out_path = os.path.join(args.out, args.suite + ".jsonl")
    done = set()
    if os.path.exists(out_path):
        with open(out_path) as f:
            for line in f:
                try:
                    done.add(json.loads(line)["id"])
                except Exception:
                    pass
    todo = [c for c in cases if c["id"] not in done]
    print("[%s] suite=%s cases=%d todo=%d -> %s" % (args.model, args.suite, len(cases),
                                                     len(todo), out_path), flush=True)
    with open(out_path, "a") as out_f:
        for n, case in enumerate(todo, 1):
            t0 = time.time()
            try:
                if args.suite == "classification":
                    r = run_classification(client, case, args.model, out_f)
                elif args.suite == "assistant":
                    r = run_assistant(client, case, args.model, out_f)
                elif args.suite == "drafting":
                    r = run_drafting(client, case, args.model, corpus_msgs, out_f)
                elif args.suite == "rules":
                    r = run_rules(client, case, args.model, out_f)
                elif args.suite == "simulate":
                    r = run_simulate(client, case, args.model, out_f)
                else:
                    r = run_summary(client, case, args.model, out_f)
                status = "err" if r.get("error") or r.get("parse_error") else "ok"
            except Exception as exc:
                status = "EXC:%r" % exc
                out_f.write(json.dumps({"id": case["id"], "suite": args.suite,
                                        "model": args.model,
                                        "error": repr(exc)}) + "\n")
                out_f.flush()
            dt = time.time() - t0
            print("  [%d/%d] %-28s %-8s %.1fs" % (n, len(todo), case["id"], status, dt),
                  flush=True)
            if args.sleep:
                time.sleep(args.sleep)
    print("[%s] %s done" % (args.model, args.suite), flush=True)


if __name__ == "__main__":
    main()
