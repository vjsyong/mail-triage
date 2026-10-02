#!/usr/bin/env bash
# Serve one candidate model on GPU 1 (RTX 3090) with vLLM v0.22.0 = the
# production image/version, so runtime effects are controlled.
#
#   serve.sh <key>          # stop previous bench server, start this one
#   serve.sh stop           # stop the bench server
#   serve.sh status
#
# Candidates: qwen9b qwen4b gemma4e4b granite3b lfm8b
set -eu
MODELS=/home/xrim/models
NAME=bench-server
PORT=8045
IMAGE=vllm/vllm-openai:v0.22.0

stop() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }

common_args() {
  echo -n "--served-model-name $1 --max-model-len 16384 --gpu-memory-utilization 0.92 \
--enable-auto-tool-choice --trust-remote-code --enable-prefix-caching --enable-chunked-prefill"
}

case "${1:-}" in
  stop) stop; echo "stopped"; exit 0 ;;
  status) docker ps -a --filter name=$NAME --format '{{.Names}} {{.Status}}'; exit 0 ;;
  qwen9b)
    DIR=$MODELS/qwen3.5-9b; MNAME=qwen3.5-9b
    EXTRA="--tool-call-parser qwen3_xml --reasoning-parser qwen3 --limit-mm-per-prompt {\"image\":0} --gpu-memory-utilization 0.90 --enforce-eager";;
  qwen4b)
    DIR=$MODELS/qwen3.5-4b; MNAME=qwen3.5-4b
    EXTRA="--tool-call-parser qwen3_xml --reasoning-parser qwen3 --limit-mm-per-prompt {\"image\":0}";;
  gemma4e4b)
    DIR=$MODELS/gemma-4-e4b-it; MNAME=gemma-4-e4b
    EXTRA="--tool-call-parser gemma4 --reasoning-parser gemma4 --limit-mm-per-prompt {\"image\":0,\"audio\":0}";;
  granite3b)
    DIR=$MODELS/granite-4.2-3b; MNAME=granite-4.2-3b
    EXTRA="--tool-call-parser granite4 --reasoning-parser granite";;
  lfm8b)
    DIR=$MODELS/lfm2.5-8b-a1b; MNAME=lfm2.5-8b-a1b
    EXTRA="--tool-call-parser lfm2";;
  *) echo "usage: serve.sh <qwen9b|qwen4b|gemma4e4b|granite3b|lfm8b|stop|status>"; exit 1 ;;
esac

stop
mkdir -p /home/xrim/models/.benchcache/vllm
# shellcheck disable=SC2086
docker run -d --name "$NAME" \
  --device nvidia.com/gpu=1 \
  -p 127.0.0.1:${PORT}:8000 \
  -v $MODELS:/models:ro \
  -v /home/xrim/models/.benchcache/vllm:/root/.cache/vllm \
  -e HF_HUB_OFFLINE=1 \
  -e VLLM_NO_USAGE_STATS=1 \
  -e VLLM_WORKER_MULTIPROC_METHOD=spawn \
  --shm-size 16g --ipc host \
  $IMAGE "/models/$(basename "$DIR")" \
  $(common_args "$MNAME") $EXTRA
echo "started $MNAME from $DIR (port $PORT)"
