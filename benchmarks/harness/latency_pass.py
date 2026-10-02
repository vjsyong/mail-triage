#!/usr/bin/env python3
"""Controlled latency/throughput pass: fixed workload, sequential, quiet system.

Writes <out>/latency.json:
  classify: 20 fixed cases (median/p95 wall, median gen tok/s)
  assistant: 6 fixed cases (median wall, median ttft, prompts/completions)
Usage: latency_pass.py --base URL --model-name NAME --out results/<key> [--tag cold|warm]
"""
import argparse
import json
import os
import statistics
import time
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
from run_model import BenchClient, PROMPTS, load_cases, assistant_system, ToolSim, CORPUS  # noqa

ap = argparse.ArgumentParser()
ap.add_argument("--base", required=True)
ap.add_argument("--model-name", required=True)
ap.add_argument("--key", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--thinking-mode", default="auto", choices=["auto", "off", "falsekw"])
ap.add_argument("--temperature", type=float, default=0.0)
ap.add_argument("--top-p", type=float, default=None)
ap.add_argument("--assistant-no-stream", action="store_true")
args = ap.parse_args()

CLS_IDS = ["cls_normal_101", "cls_normal_130", "cls_normal_170", "cls_normal_190",
           "cls_normal_210", "cls_normal_180", "cls_reply_112", "cls_reply_113",
           "cls_ambig_221", "cls_ml_297", "cls_normal_150", "cls_normal_161",
           "cls_normal_200", "cls_normal_242", "cls_normal_280", "cls_reply_140",
           "cls_normal_103", "cls_normal_171", "cls_normal_191", "cls_normal_211"]
ASST_IDS = ["asst_b6_folders", "asst_q1_invoice_paid", "asst_b12_sync",
            "asst_u1_thanks", "asst_g4_create_folder", "asst_b8_rules"]

out = {}
client = BenchClient(args.base, args.model_name, timeout=240,
                     temperature=args.temperature, top_p=args.top_p,
                     thinking_mode=args.thinking_mode)

cls_cases = {c["id"]: c for c in load_cases("classification")}
walls, tps = [], []
for cid in CLS_IDS:
    c = cls_cases.get(cid)
    if not c:
        continue
    res = client.chat_once(PROMPTS["classify_system"], c["user"], max_tokens=4096,
                           json_mode=True, thinking=True, timeout=180)
    walls.append(res["wall"])
    u = res.get("usage") or {}
    ct = u.get("completion_tokens") or 0
    if ct and res["wall"] > 0:
        tps.append(ct / res["wall"])
out["classify"] = {"n": len(walls),
                   "median_wall_s": round(statistics.median(walls), 2),
                   "p95_wall_s": round(sorted(walls)[int(len(walls) * 0.95) - 1], 2),
                   "min_s": round(min(walls), 2), "max_s": round(max(walls), 2),
                   "median_gen_tok_s": round(statistics.median(tps), 1)}
print("classify:", out["classify"], flush=True)

asst_cases = {c["id"]: c for c in load_cases("assistant")}
awalls, ttf = [], []
for cid in ASST_IDS:
    c = asst_cases.get(cid)
    if not c:
        continue
    sim = ToolSim(CORPUS)
    system = assistant_system(sim)
    convo = [{"role": "user", "content": c["user"]}]
    t0 = time.time()
    first = None
    steps = 0
    while True:
        steps += 1
        use_tools = PROMPTS["assistant_tools"] if steps <= 8 else None
        calls = []
        if args.assistant_no_stream:
            res = client.chat_full(system, convo, tools=use_tools, thinking=True)
            if first is None:
                first = res.get("ttft") or res.get("wall")
            calls = res.get("tool_calls") or []
        else:
            for ev in client.chat_stream(system, convo, tools=use_tools, thinking=True):
                if first is None and ev["type"] in ("reasoning_delta", "content_delta"):
                    first = time.time() - t0
                if ev["type"] == "tool_calls":
                    calls = ev["calls"]
        if not calls or use_tools is None:
            break
        for cc in calls[:4]:
            from run_model import json_args, truncate
            res = sim.call(cc["name"], json_args(cc["arguments"]))
            convo.append({"role": "assistant", "content": None,
                          "tool_calls": [{"id": cc["id"], "type": "function",
                                          "function": {"name": cc["name"],
                                                       "arguments": cc["arguments"]}}]})
            convo.append({"role": "tool", "tool_call_id": cc["id"], "name": cc["name"],
                          "content": truncate(json.dumps(res.get("result", {})), 4500)})
    awalls.append(time.time() - t0)
    ttf.append(first if first is not None else (time.time() - t0))
out["assistant"] = {"n": len(awalls),
                    "median_wall_s": round(statistics.median(awalls), 2),
                    "median_ttft_s": round(statistics.median(ttf), 2)}
print("assistant:", out["assistant"], flush=True)

os.makedirs(args.out, exist_ok=True)
p = os.path.join(args.out, "latency.json")
old = {}
if os.path.exists(p):
    old = json.load(open(p))
old.update(out)
json.dump(old, open(p, "w"), indent=1)
print("wrote", p)
