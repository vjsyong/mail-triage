#!/usr/bin/env bash
# Full-suite run for a candidate that passed screening (server already up).
#   run_full.sh <key>
set -eu
KEY="${1:?usage: run_full.sh <key>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
P=~/.local/venvs/mtbench/bin/python
OUT="$HERE/../results/$KEY"

MODEL_NAME=$($P - <<EOF
import json
print(json.load(open("$OUT/models.json"))["data"][0]["id"])
EOF
)

(nohup $P "$HERE/resources.py" --gpu 1 --container bench-server \
   --out "$OUT/resources_full.jsonl" --interval 3 --duration 14400 >/dev/null 2>&1 &)

for s in classification drafting rules simulate summary assistant; do
  echo "######## [$KEY] FULL suite $s $(date -u +%H:%M:%S)"
  $P "$HERE/run_model.py" --model "$KEY" --base http://127.0.0.1:8045/v1 \
    --model-name "$MODEL_NAME" --suite "$s" --out "$OUT"
done
echo "[$KEY] FULL_DONE $(date -u)"
