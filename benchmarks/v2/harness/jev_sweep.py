#!/usr/bin/env python3
"""Sweep the v2 classification suite through Jev-family typed-decision models.

One protocol for every backend: pose the production classification task as two
typed questions over the case's rendered email —

  category     : choice over the six production labels, each with a description
  needs_reply  : noul / boolean "does this email need a reply?"

— then synthesize the production JSON shape the v2 classification scorer
requires (``summary``/``reason`` are adapter artifacts; the schema gate demands
non-empty strings, decision models never generate text).

Attempts are written through ``harness.run_manager`` so the official
``scoring/score.py`` and ``harness/report.py`` consume them unchanged.  Run one
backend per process:

  python benchmarks/v2/harness/jev_sweep.py --backend tinyjev \
      --results /path/to/v2/results --split all

Backend notes (inference path verified against each project's own docs):
  tinyjev   AnkitAI/TinyJev-0.6B  pip tinyjev, one forward pass, System One payload
  laya      convaiinnovations/laya-typed-decisions  pip laya Router
  kev       jaredpalmer/kev-0.8b  kev repo server on /v1/systemone
  gliner    fastino/GLiNER2.5-Decide  pip gliner2 classify_text
  nanojev   C-Tianyu/NanoJev unified-games-v1  upstream DecisionPredictor (CUDA)
  nanojev_rag  sdmlai/nano-jev  pip nano-jev (RAG-only model, off-label here)
"""
import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, V2)

from common.validation import load_schema, validate_or_raise  # noqa: E402
from harness import run_manager  # noqa: E402

CATEGORIES = ["Action", "Notification", "Newsletter", "Receipt", "Personal", "Promo"]
DESC = {
    "Action": "Needs the owner to do something: reply, decide, submit, pay, or act",
    "Notification": "Automatic status updates and notices that need no action",
    "Newsletter": "Recurring editorial or digest content the owner subscribed to",
    "Receipt": "Order confirmations, invoices, and payment receipts",
    "Personal": "Mail from friends, family, or personal contacts",
    "Promo": "Marketing, offers, and sales promotions",
}

CATEGORY_Q = {
    "type": "choice",
    "instructions": "Which category does this email belong to?",
    "criteria": dict(DESC),
}
NEEDS_REPLY_Q = {
    "type": "noul",
    "instructions": "Does this email need a reply from the account owner?",
}
# System One-style / kev / laya payload; nanojev swaps noul for boolean itself.
QUESTIONS = {"category": CATEGORY_Q, "needs_reply": NEEDS_REPLY_Q}

SUMMARY = "typed decision over provided options"


def _conf(p):
    p = float(p)
    return max(p, 1.0 - p)


def _result(category, p_category, p_needs_reply, raw):
    return {"category": category, "p_category": float(p_category),
            "p_needs_reply": float(p_needs_reply), "raw": raw}


# ------------------------------------------------------------------ backends

class Backend:
    """Common lifecycle: load once, predict per case, release on exit."""

    def close(self):
        pass


class TinyJevBackend(Backend):
    key = "tinyjev-06b"
    revision = "AnkitAI/TinyJev-0.6B (HF snapshot), tinyjev 0.1.3"
    runtime = "tinyjev[torch] one-forward-pass pointer head"

    def __init__(self, args):
        self.args = args
        self.agent = None

    def load(self):
        import tinyjev
        self.agent = tinyjev.load("TinyJev-0.6B", device=self.args.device)

    def predict(self, case):
        out = self.agent.predict({"state": case["user"], "questions": QUESTIONS})
        ans = out["states"][0]["answers"]
        cat, nr = ans["category"], ans["needs_reply"]
        return _result(cat["choice"], max(cat["probabilities"].values()),
                       nr["p_true"], ans)


class LayaBackend(Backend):
    key = "laya-typed"
    revision = "convaiinnovations/laya-typed-decisions (bundle subfolder)"
    runtime = "pip laya Router(model='typed-decisions')"

    def __init__(self, args):
        self.args = args
        self.router = None

    def load(self):
        from laya import Router
        self.router = Router(device=self.args.device, max_loaded=1)

    def predict(self, case):
        res = self.router.predict(case["user"], QUESTIONS, model="typed-decisions")
        ans = res["answers"]
        cat, nr = ans["category"], ans["needs_reply"]
        return _result(cat["choice"], max(cat["probabilities"].values()),
                       nr["noul"], ans)


class KevBackend(Backend):
    key = "kev-08b"
    revision = "jaredpalmer/kev-0.8b rev 9a45d25e + Qwen/Qwen3.5-0.8B-Base"
    runtime = "kev repo server (CPU fp32), /v1/systemone"

    def __init__(self, args):
        self.args = args
        self.proc = None
        self.url = None

    def _free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def load(self):
        import requests
        self.requests = requests
        port = self.args.port or self._free_port()
        env = dict(os.environ)
        env.setdefault("KEV_FUSED", "0")
        log = open(os.path.join(self.args.log_dir, "kev-server.log"), "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "kev.serve", "--run", "jaredpalmer/kev-0.8b",
             "--port", str(port)],
            stdout=log, stderr=log, env=env)
        self.url = "http://127.0.0.1:%d/v1/systemone" % port
        deadline = time.time() + 1800
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("kev server exited early (see kev-server.log)")
            try:
                r = requests.post(self.url, json={
                    "model": "kev-latest", "state": "ready check",
                    "questions": {"q": {"type": "noul", "instructions": "Ready?"}}},
                    timeout=10)
                if r.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(3)
        raise RuntimeError("kev server did not become ready in 1800s")

    def predict(self, case):
        body = {"model": "kev-latest", "state": case["user"], "questions": QUESTIONS}
        r = self.requests.post(self.url, json=body, timeout=300)
        if r.status_code != 200:
            raise RuntimeError("kev HTTP %s: %s" % (r.status_code, r.text[:300]))
        ans = r.json()["answers"]
        cat, nr = ans["category"], ans["needs_reply"]
        return _result(cat["choice"], max(cat["probabilities"].values()),
                       nr["noul"], ans)

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except Exception:
                self.proc.kill()


class GlinerBackend(Backend):
    key = "gliner25-decide"
    revision = "fastino/GLiNER2.5-Decide (DeBERTa-v3-large encoder, 340M)"
    runtime = "pip gliner2 AutoExtractor.classify_text"

    def __init__(self, args):
        self.args = args
        self.model = None

    def load(self):
        from gliner2 import AutoExtractor
        self.model = AutoExtractor.from_pretrained("fastino/GLiNER2.5-Decide")

    @staticmethod
    def _task_value(out, task, positive=None):
        """Normalise one classification head to (label, confidence-or-None)."""
        v = out.get(task)
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, dict):
            label = v.get("choice") or v.get("label") or v.get("text")
            conf = v.get("confidence")
            if label is None and positive is not None and positive in v:
                label = positive
            return label, (float(conf) if isinstance(conf, (int, float)) else None)
        return v, None

    def predict(self, case):
        tasks = {
            "category": dict(DESC),
            "needs_reply": {"labels": ["yes", "no"],
                            "prompt": "Does this email need a reply from the account owner?"},
        }
        out = self.model.classify_text(case["user"], tasks, include_confidence=True)
        cat, cat_conf = self._task_value(out, "category")
        nr, nr_conf = self._task_value(out, "needs_reply", positive="yes")
        if isinstance(nr, str):
            p_nr = (nr_conf if nr_conf is not None else 0.5) if nr.lower() == "yes" \
                else 1.0 - (nr_conf if nr_conf is not None else 0.5)
        else:
            p_nr = 0.5
        return _result(cat, cat_conf if cat_conf is not None else 0.5,
                       p_nr, out)


class NanoJevBackend(Backend):
    key = "nanojev-06b"
    revision = "C-Tianyu/NanoJev unified-games-v1 (Qwen3-0.6B + decision heads)"
    runtime = "upstream DecisionPredictor, CUDA bf16"
    checkpoint = os.path.expanduser("~/jev-models/NanoJev/checkpoints/NanoJev-unified")

    def __init__(self, args):
        self.args = args
        self.engine = None

    def _download(self):
        if os.path.isdir(self.checkpoint) and os.listdir(self.checkpoint):
            return
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id="C-Tianyu/NanoJev", revision="unified-games-v1",
                          local_dir=self.checkpoint,
                          allow_patterns=["best.safetensors", "config.json",
                                          "tokenizer/*", "backbone_config/*"])

    def load(self):
        sys.path.insert(0, os.path.expanduser("~/jev-models/NanoJev/scripts"))
        self._download()
        from predict_toy_decisions import DecisionPredictor
        self.engine = DecisionPredictor(
            self.checkpoint, device_name=self.args.device,
            disable_native_triton=True, precision=self.args.precision)

    def predict(self, case):
        questions = {
            "category": {"type": "choice",
                         "instructions": CATEGORY_Q["instructions"],
                         "criteria": dict(DESC)},
            "needs_reply": {"type": "boolean",
                            "instructions": NEEDS_REPLY_Q["instructions"]},
        }
        payload = {"states": [{"id": case["id"], "state": case["user"],
                               "questions": questions}]}
        out = self.engine.predict(payload)
        ans = out["states"][0]["answers"]
        cat, nr = ans["category"], ans["needs_reply"]
        return _result(cat["choice"], max(cat["probabilities"].values()),
                       nr["p_true"], ans)


class NanoJevRagBackend(Backend):
    key = "nano-jev-rag"
    version = None            # v1.0, 33.4M (the row in the sweep list)
    revision = "sdmlai/nano-jev v1.0 (MiniLM-L12-H384, 33.4M)"
    runtime = "pip nano-jev, decider.decide (custom options; RAG-only model)"

    def __init__(self, args):
        self.args = args
        self.decider = None

    def load(self):
        import nanojev
        self.decider = nanojev.load(self.version, device=self.args.device) \
            if self.version else nanojev.load(device=self.args.device)

    def predict(self, case):
        cat_probs = self.decider.decide(
            "Which of these categories does this email belong to?", CATEGORIES,
            case["user"])
        nr_probs = self.decider.decide(
            "Does this email need a reply from the account owner?", ["yes", "no"],
            case["user"])
        cat = max(cat_probs, key=cat_probs.get)
        raw = {"category": cat_probs, "needs_reply": nr_probs}
        return _result(cat, cat_probs[cat], nr_probs.get("yes", 0.5), raw)


class NanoJevRagV01Backend(NanoJevRagBackend):
    key = "nano-jev-rag01"
    version = "v0.1"
    revision = "sdmlai/nano-jev v0.1 (MS MARCO MiniLM-L6, 22.7M)"
    runtime = "pip nano-jev, decider.decide (custom options; RAG-only model)"


BACKENDS = {
    "tinyjev": TinyJevBackend,
    "laya": LayaBackend,
    "kev": KevBackend,
    "gliner": GlinerBackend,
    "nanojev": NanoJevBackend,
    "nanojev_rag": NanoJevRagBackend,
    "nanojev_rag01": NanoJevRagV01Backend,
}


# ------------------------------------------------------------------ runner

def make_attempt(backend, case, run_id, status="ok", error=None, wall=None):
    parsed = None
    if status == "ok":
        r = backend.last
        conf = round(min(r["p_category"], _conf(r["p_needs_reply"])), 4)
        parsed = {
            "category": r["category"],
            "needs_reply": r["p_needs_reply"] >= 0.5,
            "confidence": max(0.0, min(1.0, conf)),
            "summary": SUMMARY,
            "reason": "%s: argmax of calibrated option probabilities" % backend.key,
        }
        out = {"parsed": parsed, "content": json.dumps(parsed),
               "finish": "stop", backend.key: {
                   "category_probability": r["p_category"],
                   "needs_reply_p_true": r["p_needs_reply"],
                   "summary_reason_synthesized": True,
                   "raw": r["raw"]}}
    else:
        out = None
    a = {
        "case_id": case["id"], "suite": "classification", "model": backend.key,
        "run_id": run_id, "attempt": 1, "status": status,
        "wall_s": round(wall, 3) if wall is not None else None,
        "ttft_s": None, "first_visible_s": None, "first_tool_call_s": None,
        "completion_s": round(wall, 3) if wall is not None else None,
        "decode_s": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
        "error": error, "output": out,
    }
    return a


def make_manifest(run_id, backend, split, hardware):
    with open(os.path.join(V2, "cases", "manifest.json"), "rb") as f:
        case_sha = hashlib.sha256(f.read()).hexdigest()
    cfg = hashlib.sha256(json.dumps(
        {"run_id": run_id, "backend": backend.key, "split": split},
        sort_keys=True).encode()).hexdigest()
    return {
        "model_key": backend.key,
        "model_revision": backend.revision,
        "tokenizer": "as shipped with the checkpoint",
        "chat_template": "typed-decision questions (choice/noul/boolean)",
        "runtime_image": backend.runtime,
        "hardware": hardware,
        "harness_revision": "v2.4", "scorer_revision": "v2.0",
        "corpus_sha256": None, "case_manifest_sha256": case_sha,
        "prompts_sha256": None,
        "params": {"temperature": None, "max_tokens": None},
        "adaptations": {"interface": "typed questions over the production prompt",
                        "questions": ["category choice (6 labels + descriptions)",
                                      "needs_reply noul/boolean"],
                        "summary_reason": "synthesized schema strings (adapter artifact)",
                        "confidence": "min(category top probability, needs_reply margin)",
                        "split": split},
        "retry_policy": {"max": 0}, "fallback_policy": {"mode": "none"},
        "cache_state": "hot", "config_hash": cfg, "run_id": run_id,
    }


def load_cases(split, limit=None, offset=0):
    cases = run_manager.load_suite("classification")
    if split != "all":
        cases = [c for c in cases if c.get("split") == split]
    if offset:
        cases = cases[offset:]
    if limit:
        cases = cases[:limit]
    return cases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True, choices=sorted(BACKENDS))
    ap.add_argument("--split", default="all", choices=["all", "dev", "acceptance"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--results", default=os.path.join(V2, "results"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--precision", default="bf16", choices=["fp32", "bf16"])
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--log-dir", default=os.path.join(V2, "results"))
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()

    try:
        import torch
        torch.set_num_threads(args.threads)
    except Exception:
        pass

    run_id = args.run_id or ("jev-%s-v2" % args.backend.replace("_", "-"))
    results = args.results
    os.makedirs(args.log_dir, exist_ok=True)
    run_dir = os.path.join(results, run_id)
    os.makedirs(run_dir, exist_ok=True)
    backend = BACKENDS[args.backend](args)
    hardware = ("host CPU, %d torch threads" % args.threads if args.device == "cpu"
                else "RTX 3090, %s" % args.precision)
    manifest = make_manifest(run_id, backend, args.split, hardware)
    validate_or_raise(manifest, load_schema("run_manifest.schema.json"), "run manifest")
    with open(os.path.join(run_dir, "run_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    open(os.path.join(run_dir, "attempts.jsonl"), "w").close()

    cases = load_cases(args.split, args.limit or None, args.offset)
    print("[%s] %d cases, loading..." % (run_id, len(cases)), flush=True)
    t0 = time.time()
    backend.load()
    print("[%s] loaded in %.1fs" % (run_id, time.time() - t0), flush=True)

    t0 = time.time()
    errors = 0
    try:
        for i, case in enumerate(cases, 1):
            t = time.perf_counter()
            try:
                backend.last = backend.predict(case)
                a = make_attempt(backend, case, run_id, wall=time.perf_counter() - t)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                a = make_attempt(backend, case, run_id, status="error",
                                 error=repr(exc), wall=time.perf_counter() - t)
            run_manager.record_attempt(run_id, "classification", a, results=results)
            if i % 20 == 0 or i == len(cases):
                print("[%s] %d/%d wall=%.1fs errors=%d" % (
                    run_id, i, len(cases), time.time() - t0, errors), flush=True)
    finally:
        backend.close()
    print("[%s] DONE %.1fs errors=%d" % (run_id, time.time() - t0, errors), flush=True)


if __name__ == "__main__":
    main()
