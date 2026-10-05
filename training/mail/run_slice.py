#!/usr/bin/env python3
"""One-command reproducible slice pipeline with real exit-status evidence (M5).

Stages (each writes ``<run>/<stage>.exit`` with the captured process exit code and
full stdout/stderr logs): briefs -> teacher -> dialogues -> dev teacher/dev
dialogues -> build-slice -> LoRA train -> base/adapter eval.  The receipt binds
the run to the exact clean code HEAD/tree, the environment, every command, and the
per-stage start/end/exit.  Heavy artifacts stay outside Git.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def _git(args):
    try:
        return subprocess.check_output(["git"] + args, cwd=ROOT,
                                       stderr=subprocess.DEVNULL).decode().strip()
    except Exception:  # noqa: BLE001
        return None


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _entry(ok, name, command, log, started, ended, exit_code):
    return {"stage": name, "command": command, "log": log, "started": started,
            "ended": ended, "exit": exit_code, "ok": exit_code == 0}


def run_stage(name, command, env, run_dir):
    log = os.path.join(run_dir, "%s.log" % name)
    started = now()
    t0 = time.time()
    with open(log, "w", encoding="utf-8") as f:
        proc = subprocess.run(command, env=env, cwd=ROOT, stdout=f,
                              stderr=subprocess.STDOUT)
    code = proc.returncode
    with open(os.path.join(run_dir, "%s.exit" % name), "w", encoding="utf-8") as f:
        f.write("%d\n" % code)
    return _entry(code == 0, name, command, log, started, now(), code)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--cpu-python", default=os.path.join(ROOT, ".venv", "bin", "python"))
    ap.add_argument("--gpu-python", default="/home/xrim/jev-sweep/venv-cuda/bin/python")
    ap.add_argument("--train-env", default=("/home/xrim/datasets/benchmark-v3/"
                                            "mail-sft-slice/train-env"))
    ap.add_argument("--gpu-uuid", default="GPU-37e9773f-42ef-4ea1-dbe1-5567b8690f72")
    ap.add_argument("--model-dir", default="/home/xrim/datasets/benchmark-v3/"
                    "mail-sft-slice/models/MiniCPM5-2B")
    ap.add_argument("--train-seeds", default="11,22,33")
    ap.add_argument("--dev-seeds", default="211,223")
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--sample-dir", default=None)
    args = ap.parse_args(argv)

    os.makedirs(args.run_dir, exist_ok=True)
    mail = os.path.join(ROOT, "training", "mail")
    gpu_env = dict(os.environ, CUDA_VISIBLE_DEVICES=args.gpu_uuid,
                   PYTHONPATH=args.train_env)
    cpu_env = dict(os.environ)

    entries = []

    def gpu(script, extra):
        return [args.gpu_python, os.path.join(mail, script)] + extra

    def cpu(extra):
        return [args.cpu_python] + extra

    run = args.run_dir
    entries.append(run_stage("briefs", cpu([
        os.path.join(mail, "briefs.py"), "--out", os.path.join(run, "briefs.json"),
        "--seeds", args.train_seeds]), cpu_env, run))
    if not entries[-1]["ok"]:
        return _finish(args, entries)
    entries.append(run_stage("teacher", gpu("generate_teacher.py", [
        "--briefs", os.path.join(run, "briefs.json"),
        "--out", os.path.join(run, "sources.json")]), gpu_env, run))
    if not entries[-1]["ok"]:
        return _finish(args, entries)
    entries.append(run_stage("dialogues", gpu("rollout_dialogue.py", [
        "--sources", os.path.join(run, "sources.json"),
        "--out", os.path.join(run, "dialogues.json")]), gpu_env, run))
    entries.append(run_stage("briefs_dev", cpu([
        os.path.join(mail, "briefs.py"), "--out",
        os.path.join(run, "briefs_dev.json"), "--seeds", args.dev_seeds]),
        cpu_env, run))
    entries.append(run_stage("teacher_dev", gpu("generate_teacher.py", [
        "--briefs", os.path.join(run, "briefs_dev.json"),
        "--out", os.path.join(run, "sources_dev.json"),
        "--role", "development", "--domain", "development"]), gpu_env, run))
    entries.append(run_stage("dialogues_dev", gpu("rollout_dialogue.py", [
        "--sources", os.path.join(run, "sources_dev.json"),
        "--out", os.path.join(run, "dialogues_dev.json")]), gpu_env, run))
    entries.append(run_stage("build_slice", cpu([
        "-m", "benchmarks.v3.training.cli", "build-slice",
        "--out", os.path.join(run, "slice"),
        "--sources", os.path.join(run, "sources.json"),
        "--dialogues", os.path.join(run, "dialogues.json"),
        "--dev-sources", os.path.join(run, "sources_dev.json"),
        "--dev-dialogues", os.path.join(run, "dialogues_dev.json"),
        "--sample-dir", args.sample_dir or os.path.join(run, "slice_samples")]),
        cpu_env, run))
    if not entries[-1]["ok"]:
        return _finish(args, entries)
    entries.append(run_stage("train", gpu("train_lora.py", [
        "--examples", os.path.join(run, "slice", "training.jsonl"),
        "--out", os.path.join(run, "train"), "--steps", str(args.steps),
        "--model-dir", args.model_dir]), gpu_env, run))
    entries.append(run_stage("evaluate", gpu("evaluate.py", [
        "--dev-sources", os.path.join(run, "sources_dev.json"),
        "--adapter", os.path.join(run, "train", "adapter"),
        "--out", os.path.join(run, "eval"), "--model-dir", args.model_dir]),
        gpu_env, run))
    return _finish(args, entries)


def _finish(args, entries):
    receipt = {
        "kind": "mail-sft-slice-run",
        "head": _git(["rev-parse", "HEAD"]),
        "tree": _git(["rev-parse", "HEAD^{tree}"]),
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "worktree_clean": _git(["status", "--porcelain"]) == "",
        "run_dir": args.run_dir,
        "env": {"gpu_uuid": args.gpu_uuid, "train_env": args.train_env,
                "gpu_python": args.gpu_python, "model_dir": args.model_dir,
                "train_seeds": args.train_seeds, "dev_seeds": args.dev_seeds,
                "steps": args.steps},
        "stages": entries,
        "all_exit_zero": all(e["exit"] == 0 for e in entries),
    }
    with open(os.path.join(args.run_dir, "run_receipt.json"), "w",
              encoding="utf-8") as f:
        json.dump(receipt, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    print(json.dumps({k: receipt[k] for k in ("head", "tree", "all_exit_zero")}
                     | {"stages": [(e["stage"], e["exit"]) for e in entries]},
                     indent=2))
    return 0 if receipt["all_exit_zero"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
