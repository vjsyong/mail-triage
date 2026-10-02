#!/usr/bin/env python3
"""Build the comparison report (markdown + JSON) across all evaluated models.

Usage: compare.py --keys gemma-4-26b-a4b-baseline,qwen3.5-4b,... [--out ../reports/comparison.md]
"""
import argparse
import json
import os
import subprocess

HERE = os.path.dirname(__file__)
BENCH = os.path.join(HERE, "..")
RESULTS = os.path.join(BENCH, "results")

META = {
    "gemma-4-26b-a4b-baseline": {"label": "Gemma 4 26B-A4B (baseline)", "params": "25.2B MoE / 3.8B active",
                                 "quant": "AWQ-4bit + int8 KV", "dir": None, "gpu": 0,
                                 "boot_s": 397, "vram_mib": 23144},
    "qwen9b": {"label": "Qwen3.5-9B", "params": "9B dense", "quant": "bf16",
               "dir": "/home/xrim/models/qwen3.5-9b", "gpu": 1},
    "qwen4b": {"label": "Qwen3.5-4B", "params": "4B dense", "quant": "bf16",
               "dir": "/home/xrim/models/qwen3.5-4b", "gpu": 1},
    "gemma4e4b": {"label": "Gemma 4 E4B", "params": "4.5B eff (8B incl emb)", "quant": "bf16",
                  "dir": "/home/xrim/models/gemma-4-e4b-it", "gpu": 1},
    "granite3b": {"label": "Granite 4.2 3B", "params": "3B dense", "quant": "bf16",
                  "dir": "/home/xrim/models/granite-4.2-3b", "gpu": 1},
    "lfm8b": {"label": "LFM2.5-8B-A1B", "params": "8.3B MoE / 1.5B active", "quant": "bf16",
              "dir": "/home/xrim/models/lfm2.5-8b-a1b", "gpu": 1},
    "ling3": {"label": "Ling-3.0-tiny", "params": "7.9B total MoE (128 experts, top-8)", "quant": "bf16",
              "dir": "/home/xrim/models/ling-3.0-tiny", "gpu": 1},
}


def dir_size(path):
    if not path or not os.path.exists(path):
        return None
    try:
        out = subprocess.run(["du", "-sb", path], capture_output=True, text=True, timeout=60)
        return int(out.stdout.split()[0])
    except Exception:
        return None


def resource_stats(files):
    peak, last, n = None, None, 0
    for f in files:
        if not os.path.exists(f):
            continue
        if f.endswith(".json"):
            try:
                r = json.load(open(f))
                if r.get("gpu0_mem_mib"):
                    return {"peak_vram_mib": r["gpu0_mem_mib"], "last_vram_mib": r["gpu0_mem_mib"],
                            "samples": "measured"}
            except Exception:
                pass
            continue
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "gpu_mem_mib" in r:
                peak = max(peak or 0, r["gpu_mem_mib"])
                last = r["gpu_mem_mib"]
                n += 1
    return {"peak_vram_mib": peak, "last_vram_mib": last, "samples": n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", required=True)
    ap.add_argument("--out", default=os.path.join(BENCH, "reports", "comparison.md"))
    args = ap.parse_args()
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]

    rows = []
    for key in keys:
        rdir = os.path.join(RESULTS, key)
        s = {}
        sp = os.path.join(rdir, "summary.json")
        if os.path.exists(sp):
            s = json.load(open(sp))
        lat = {}
        lp = os.path.join(rdir, "latency.json")
        if os.path.exists(lp):
            lat = json.load(open(lp))
        srv = {}
        svp = os.path.join(rdir, "server.json")
        if os.path.exists(svp):
            srv = json.load(open(svp))
        res = resource_stats([os.path.join(rdir, "resources_full.jsonl"),
                              os.path.join(rdir, "resources_screen.jsonl"),
                              os.path.join(rdir, "resources.jsonl"),
                              os.path.join(rdir, "resources_measured.json")])
        meta = META.get(key, {})
        rows.append({"key": key, "meta": meta, "summary": s, "latency": lat,
                     "server": srv, "resources": res,
                     "file_bytes": dir_size(meta.get("dir"))})

    out_json = os.path.join(BENCH, "reports", "comparison_data.json")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(rows, open(out_json, "w"), indent=1)

    def g(d, *path, default="-"):
        cur = d
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur

    lines = ["# Model comparison (auto-generated)",
             "",
             "| Model | Params | Quant | Overall raw | Sev-adj | Critical cases | Classify raw | Assistant raw | "
             "Classify med lat | Asst med TTFT | Peak VRAM | File size (bf16) | Boot |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        s = r["summary"]
        ov = g(s, "overall", "raw_score")
        sev = g(s, "overall", "sev_adjusted_score")
        crit = len(g(s, "overall", "critical_cases", default=[]))
        cls = g(s, "suites", "classification", "raw_score")
        asst = g(s, "suites", "assistant", "raw_score")
        clat = g(r["latency"], "classify", "median_wall_s")
        attft = g(r["latency"], "assistant", "median_ttft_s")
        vram = r["resources"].get("peak_vram_mib") or r["meta"].get("vram_mib")
        vram_s = ("%.1f GB" % (vram / 1024)) if vram else "-"
        fs = r["file_bytes"]
        fs_s = ("%.1f GB" % (fs / 1e9)) if fs else "-"
        boot = g(r["server"], "boot_s")
        if boot == "-":
            boot = r["meta"].get("boot_s", "-")
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["meta"].get("label", r["key"]), r["meta"].get("params", "-"),
            r["meta"].get("quant", "-"), ov, sev, crit, cls, asst,
            ("%ss" % clat) if clat != "-" else "-", ("%ss" % attft) if attft != "-" else "-",
            vram_s, fs_s, ("%ss" % boot) if boot != "-" else "-"))
    open(args.out, "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print("\nwrote", args.out, "and", out_json)


if __name__ == "__main__":
    main()
