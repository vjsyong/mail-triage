#!/usr/bin/env bash
# Phase B for a candidate: full suites with the chosen thinking mode + latency pass.
#   finish_candidate.sh <key> <mode>
set -eu
KEY="${1:?usage: finish_candidate.sh <key> <mode>}"
MODE="${2:?usage: finish_candidate.sh <key> <mode> (auto|falsekw|off|sampling)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
P=~/.local/venvs/mtbench/bin/python
OUT="$HERE/../results/$KEY"
MODEL_NAME=$($P -c "import json;print(json.load(open('$OUT/models.json'))['data'][0]['id'])")

if [ "$MODE" = "sampling" ]; then
  EXTRA="--thinking-mode auto --temperature 0.7 --top-p 0.8"
else
  EXTRA="--thinking-mode $MODE"
fi
if [ "${NOSTREAM:-0}" = "1" ]; then
  EXTRA="$EXTRA --assistant-no-stream"
fi

(nohup $P "$HERE/resources.py" --gpu 1 --container bench-server \
   --out "$OUT/resources_full.jsonl" --interval 3 --duration 14400 >/dev/null 2>&1 &)

for s in classification drafting rules simulate summary assistant; do
  echo "######## [$KEY] full $s $(date -u +%H:%M:%S)"
  # shellcheck disable=SC2086
  $P "$HERE/run_model.py" --model "$KEY" --base http://127.0.0.1:8045/v1 --model-name "$MODEL_NAME" \
     --suite "$s" --out "$OUT" $EXTRA
done

$P "$HERE/latency_pass.py" --base http://127.0.0.1:8045/v1 --model-name "$MODEL_NAME" \
   --key "$KEY" --out "$OUT" $EXTRA || true

$P - "$OUT" "$MODE" <<'EOF'
import json, sys
out, mode = sys.argv[1:3]
path = out + "/server.json"
d = {}
try:
    d = json.load(open(path))
except Exception:
    pass
d["thinking_mode"] = mode
json.dump(d, open(path, "w"), indent=1)
print("server.json:", d)
EOF
echo "[$KEY] FINISH_DONE $(date -u)"
