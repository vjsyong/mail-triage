#!/usr/bin/env bash
# Full candidate pipeline: serve -> wait -> smoke -> screen subset.
#   run_candidate.sh <key>        (qwen9b qwen4b gemma4e4b granite3b lfm8b)
# Screen results land in ../results/<key>/screen/ ; boot time + smoke recorded
# in ../results/<key>/server.json
set -eu
KEY="${1:?usage: run_candidate.sh <key>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
P=~/.local/venvs/mtbench/bin/python
OUT="$HERE/../results/$KEY"
mkdir -p "$OUT"

bash "$HERE/serve.sh" "$KEY"
echo "[$KEY] waiting for server on :8045 ..."
T0=$(date +%s)
for i in $(seq 1 240); do
  if curl -sf http://127.0.0.1:8045/v1/models >/dev/null 2>&1; then break; fi
  sleep 5
done
T1=$(date +%s)
BOOT=$((T1 - T0))
echo "[$KEY] server up after ~${BOOT}s"
curl -s http://127.0.0.1:8045/v1/models > "$OUT/models.json" || true

MODEL_NAME=$($P - <<EOF
import json
d = json.load(open("$OUT/models.json"))
print(d["data"][0]["id"])
EOF
)

$P "$HERE/smoke.py" --base http://127.0.0.1:8045/v1 --model "$MODEL_NAME" \
  > "$OUT/smoke.json" 2>&1 && SMOKE=pass || SMOKE=fail
echo "[$KEY] smoke: $SMOKE (boot ${BOOT}s)"
$P - <<EOF
import json
boot = $BOOT
smoke = json.load(open("$OUT/smoke.json"))
json.dump({"key": "$KEY", "model": "$MODEL_NAME", "boot_s": boot, "smoke": smoke},
          open("$OUT/server.json", "w"), indent=1)
EOF
if [ "$SMOKE" != "pass" ]; then
  echo "[$KEY] SMOKE FAILED - server left up for inspection"; exit 2
fi

# resource sampler for GPU 1 while screening
(nohup $P "$HERE/resources.py" --gpu 1 --container bench-server \
   --out "$OUT/resources_screen.jsonl" --interval 3 --duration 5400 >/dev/null 2>&1 &)

for s in classification assistant drafting rules simulate summary; do
  echo "######## [$KEY] screen suite $s"
  $P "$HERE/run_model.py" --model "$KEY" --base http://127.0.0.1:8045/v1 \
    --model-name "$MODEL_NAME" --suite "$s" --subset screen --out "$OUT/screen"
done
echo "[$KEY] SCREEN_DONE"
