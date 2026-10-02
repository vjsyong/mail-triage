#!/usr/bin/env bash
# Serve a GGUF model (e.g. Q4_K_M) with llama.cpp's OpenAI-compatible server on
# GPU 1.  This is the reference engine for GGUF quants; vLLM's GGUF path is
# limited, so use this for K-quants / imatrix files.
#
#   serve_llamacpp.sh <path-to.gguf> [alias] [host-port] [ctx-total] [slots]
#   serve_llamacpp.sh stop
#
# Per-request context = ctx-total / slots.  E.g. the AgentMercury Q4_K_M run
# used: 262144 / 8 = 32768 tokens per request, matching the vLLM runs.
#
# Notes:
# - `--jinja` is required for tool calling (uses the GGUF's chat template).
# - `--flash-attn on` matches current llama.cpp flag syntax.
# - The container binds 0.0.0.0 so the app can reach it over the Tailscale IP.
set -eu
NAME=llamacpp-server
IMAGE=ghcr.io/ggml-org/llama.cpp:server-cuda

if [ "${1:-}" = "stop" ]; then docker rm -f "$NAME" >/dev/null 2>&1 || true; echo stopped; exit 0; fi

MODEL=${1:?usage: serve_llamacpp.sh <gguf> [alias] [port] [ctx_total] [slots]}
ALIAS=${2:-$(basename "$MODEL" .gguf)}
PORT=${3:-8042}
CTX=${4:-262144}
PARALLEL=${5:-8}

if [ ! -f "$MODEL" ]; then echo "no such file: $MODEL" >&2; exit 1; fi

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  --device nvidia.com/gpu=1 \
  -p "${PORT}:8000" \
  -v "$(dirname "$(readlink -f "$MODEL")")":/models:ro \
  --shm-size 16g --ipc host \
  "$IMAGE" \
  -m "/models/$(basename "$MODEL")" \
  --alias "$ALIAS" --host 0.0.0.0 --port 8000 \
  -ngl 99 -c "$CTX" --jinja --flash-attn on --parallel "$PARALLEL"
echo "serving $ALIAS on 0.0.0.0:${PORT} (ctx $CTX / $PARALLEL slots = $((CTX / PARALLEL)) per request)"
