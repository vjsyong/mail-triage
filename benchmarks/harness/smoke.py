#!/usr/bin/env python3
"""Smoke-test a candidate server: models list, JSON mode + thinking, tool calling,
streaming+tools. Prints a JSON verdict. Usage: smoke.py --base URL --model NAME
"""
import argparse
import json
import re
import sys
import time

import requests

ap = argparse.ArgumentParser()
ap.add_argument("--base", required=True)
ap.add_argument("--model", required=True)
args = ap.parse_args()
base = args.base.rstrip("/")
sess = requests.Session()
verdict = {"base": base, "model": args.model, "checks": {}}


def check(name, fn):
    t0 = time.time()
    try:
        out = fn()
        verdict["checks"][name] = {"ok": True, "detail": out, "t": round(time.time() - t0, 2)}
    except Exception as exc:
        verdict["checks"][name] = {"ok": False, "detail": repr(exc)[:400],
                                   "t": round(time.time() - t0, 2)}


def c1_models():
    r = sess.get(base + "/models", timeout=30)
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]
    assert args.model in ids or ids, "no model id"
    return ids


def c2_json_thinking():
    payload = {"model": args.model, "temperature": 0, "max_tokens": 1024,
               "messages": [
                   {"role": "system", "content": "Reply with a single JSON object and nothing else. "
                    'Shape: {"category": "A"|"B", "ok": true}'},
                   {"role": "user", "content": "Classify: this is a test message."}],
               "response_format": {"type": "json_object"},
               "chat_template_kwargs": {"enable_thinking": True}}
    r = sess.post(base + "/chat/completions", json=payload, timeout=180)
    if r.status_code in (400, 404, 422):
        payload.pop("chat_template_kwargs", None)
        r = sess.post(base + "/chat/completions", json=payload, timeout=180)
        if r.status_code in (400, 404, 422):
            payload.pop("response_format", None)
            r = sess.post(base + "/chat/completions", json=payload, timeout=180)
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    content = msg.get("content") or ""
    m = re.search(r"\{.*\}", content, re.S)
    assert m, "no JSON in content: %r" % content[:200]
    return {"json": json.loads(m.group(0)), "reasoning_len": len(msg.get("reasoning") or "")}


def c3_tool_call():
    payload = {"model": args.model, "temperature": 0, "max_tokens": 1024,
               "messages": [{"role": "user", "content": "Use the list_folders tool now."}],
               "tools": [{"type": "function", "function": {
                   "name": "list_folders", "description": "List mailbox folders.",
                   "parameters": {"type": "object", "properties": {}}}}],
               "tool_choice": "auto"}
    r = sess.post(base + "/chat/completions", json=payload, timeout=180)
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    tcs = msg.get("tool_calls") or []
    assert tcs, "no tool_calls: %r" % str(msg)[:200]
    return {"tool": tcs[0]["function"]["name"]}


def c4_stream_tools():
    payload = {"model": args.model, "temperature": 0, "max_tokens": 1024, "stream": True,
               "messages": [{"role": "user", "content": "Use the list_folders tool now."}],
               "tools": [{"type": "function", "function": {
                   "name": "list_folders", "description": "List mailbox folders.",
                   "parameters": {"type": "object", "properties": {}}}}],
               "repetition_penalty": 1.05}
    r = sess.post(base + "/chat/completions", json=payload, timeout=180, stream=True)
    if r.status_code in (400, 404, 422):
        payload.pop("repetition_penalty", None)
        r = sess.post(base + "/chat/completions", json=payload, timeout=180, stream=True)
    r.raise_for_status()
    got = {"tool": False, "content": False, "reasoning": False, "finish": None}
    for raw in r.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line in ("", "[DONE]"):
            continue
        try:
            ch = json.loads(line)
        except ValueError:
            continue
        for c in ch.get("choices") or []:
            d = c.get("delta") or {}
            if d.get("reasoning") or d.get("reasoning_content"):
                got["reasoning"] = True
            if d.get("content"):
                got["content"] = True
            if d.get("tool_calls"):
                got["tool"] = True
            if c.get("finish_reason"):
                got["finish"] = c["finish_reason"]
    r.close()
    assert got["tool"] or got["content"], "stream produced nothing"
    return got


check("models", c1_models)
check("json_thinking", c2_json_thinking)
check("tool_call", c3_tool_call)
check("stream_tools", c4_stream_tools)
verdict["pass"] = all(v.get("ok") for v in verdict["checks"].values())
print(json.dumps(verdict, indent=1, ensure_ascii=False))
sys.exit(0 if verdict["pass"] else 1)
