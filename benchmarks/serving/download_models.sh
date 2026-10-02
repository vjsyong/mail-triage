#!/bin/bash
# Download candidate models to /home/xrim/models (flat per-model dirs, matching
# the existing gemma-4-26b-a4b-awq-4bit layout the serving stack mounts).
# Research date: 2026-10-02. Runner: bench model-eval.
set -u
HF=/home/xrim/.local/venvs/mtbench/bin/hf
cd /home/xrim/models

dl() {
  local repo="$1" dir="$2"
  echo "=== $(date -u +%H:%M:%S) START $repo -> $dir ==="
  if "$HF" download "$repo" --local-dir "/home/xrim/models/$dir"; then
    echo "=== $(date -u +%H:%M:%S) DONE  $repo ($dir) $(du -sh /home/xrim/models/$dir | cut -f1) ==="
  else
    echo "=== $(date -u +%H:%M:%S) FAIL  $repo ==="
  fi
}

dl "Qwen/Qwen3.5-4B"          "qwen3.5-4b"
dl "Qwen/Qwen3.5-9B"          "qwen3.5-9b"
dl "ibm-granite/granite-4.2-3b" "granite-4.2-3b"
dl "google/gemma-4-E4B-it"    "gemma-4-e4b-it"
dl "LiquidAI/LFM2.5-8B-A1B"   "lfm2.5-8b-a1b"
echo "=== ALL_DONE $(date -u) ==="
