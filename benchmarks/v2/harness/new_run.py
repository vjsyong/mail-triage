#!/usr/bin/env python3
"""Create a v2 run: build the immutable manifest and seed the run directory.

Usage:
  python harness/new_run.py --model-key qwen9b-v2 --split acceptance \
      [--runtime-image img@sha256:...] [--hardware "RTX 3090"] \
      [--model-revision ...] [--tokenizer ...] [--chat-template ...] \
      [--thinking-mode auto] [--results DIR]
Prints the run_id (use it for runner.py --run-id and score.py --run).
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, V2)

from common import identity  # noqa: E402
from harness import run_manager, runner  # noqa: E402
from scoring import score as scorer  # noqa: E402

DEFAULT_RESULTS = os.path.join(V2, "results")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-key", required=True)
    ap.add_argument("--model-revision", default="unknown")
    ap.add_argument("--tokenizer", default="unknown")
    ap.add_argument("--chat-template", default="unknown")
    ap.add_argument("--runtime-image", default="unknown")
    ap.add_argument("--hardware", default="unknown")
    ap.add_argument("--thinking-mode", default="auto")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--top-p", type=float, default=None)
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--max-model-len", type=int, default=32768,
                    help="served context window; recorded in the run identity")
    ap.add_argument("--assistant-no-stream", action="store_true")
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=1,
                    help="batch concurrency recorded in the run identity; must match runner.py --concurrency")
    ap.add_argument("--fallback", default="none")
    ap.add_argument("--cache-state", default="cold")
    ap.add_argument("--results", default=DEFAULT_RESULTS)
    args = ap.parse_args()

    params = {"temperature": args.temperature, "top_p": args.top_p,
              "max_tokens": args.max_tokens, "batch_concurrency": args.concurrency,
              "max_model_len": args.max_model_len}
    manifest = identity.build_from_context(
        args.model_key, bench_dir=V2,
        model_revision=args.model_revision, tokenizer=args.tokenizer,
        chat_template=args.chat_template, runtime_image=args.runtime_image,
        hardware=args.hardware, harness_revision=runner.HARNESS_REVISION,
        scorer_revision=scorer.SCORER_REVISION,
        params=params,
        adaptations={"thinking_mode": args.thinking_mode,
                     "assistant_no_stream": args.assistant_no_stream},
        retry_policy={"max": args.retries},
        fallback_policy={"mode": args.fallback},
        cache_state=args.cache_state)
    d, mode = run_manager.init_run(manifest, results=args.results)
    print("run_id:", manifest["run_id"])
    print("mode:", mode)
    print("dir:", d)
    print("config_hash:", manifest["config_hash"])
    print(json.dumps({"run_id": manifest["run_id"], "dir": d, "mode": mode,
                      "config_hash": manifest["config_hash"]}))


if __name__ == "__main__":
    main()
