#!/usr/bin/env python3
"""Poll GPU + container resource usage during a benchmark run.

Writes JSONL samples: t, gpu_mem_mib, gpu_util_pct, cpu_pct, ram_mib.
Usage:
  python3 resources.py --gpu 0 --container mail-triage-gemma \
      --out ../results/<model>/resources.jsonl --interval 2 [--duration 3600]
"""
import argparse
import json
import subprocess
import time

ap = argparse.ArgumentParser()
ap.add_argument("--gpu", type=int, required=True)
ap.add_argument("--container", default="")
ap.add_argument("--out", required=True)
ap.add_argument("--interval", type=float, default=2.0)
ap.add_argument("--duration", type=float, default=0)
args = ap.parse_args()

t_end = time.time() + args.duration if args.duration else None
with open(args.out, "a") as f:
    while True:
        rec = {"t": round(time.time(), 2)}
        try:
            out = subprocess.run(
                ["nvidia-smi", "--id=%d" % args.gpu,
                 "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10).stdout.strip()
            mem, util = [x.strip() for x in out.split(",")[:2]]
            rec["gpu_mem_mib"] = int(float(mem))
            rec["gpu_util_pct"] = int(float(util))
        except Exception as exc:
            rec["gpu_err"] = repr(exc)[:80]
        if args.container:
            try:
                out = subprocess.run(
                    ["docker", "stats", "--no-stream", "--format",
                     "{{.CPUPerc}} {{.MemUsage}}", args.container],
                    capture_output=True, text=True, timeout=15).stdout.strip()
                cpu, mem = out.split(" ", 1)
                rec["cpu_pct"] = cpu
                rec["ram"] = mem.split("/")[0].strip()
            except Exception as exc:
                rec["container_err"] = repr(exc)[:80]
        f.write(json.dumps(rec) + "\n")
        f.flush()
        if t_end and time.time() > t_end:
            break
        time.sleep(args.interval)
