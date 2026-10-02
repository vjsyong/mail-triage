#!/usr/bin/env bash
# Candidate evaluation pipeline (run after downloading + adapting decision):
#   1) serve + smoke           2) unmodified probe      3) full suite (with mode)
#   4) latency pass
#   usage: pipeline.sh <key> <mode>
#     key:  qwen9b qwen4b gemma4e4b granite3b lfm8b
#     mode: auto | falsekw | off     (passed to run_model --thinking-mode)
set -eu
KEY="${1:?usage: pipeline.sh <key> <mode>}"
MODE="${2:?usage: pipeline.sh <key> <mode> (auto|falsekw|off)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
P=~/.local/venvs/mtbench/bin/python
OUT="$HERE/../results/$KEY"
mkdir -p "$OUT"

PROBE_C="cls_normal_101,cls_reply_112,cls_normal_130,cls_normal_190,cls_ambig_131,cls_adv_290,cls_long_minutes,cls_junk_mash"
PROBE_A="asst_b6_folders,asst_q1_invoice_paid,asst_g2_flag_ci"

echo "######## [$KEY] 1) serve + smoke"
bash "$HERE/serve.sh" "$KEY"
T0=$(date +%s)
for i in $(seq 1 240); do
  curl -sf http://127.0.0.1:8045/v1/models >/dev/null 2>&1 && break
  sleep 5
done
T1=$(date +%s); BOOT=$((T1 - T0))
echo "[$KEY] server up after ~${BOOT}s"
curl -s http://127.0.0.1:8045/v1/models > "$OUT/models.json" || true
MODEL_NAME=$($P -c "import json;print(json.load(open('$OUT/models.json'))['data'][0]['id'])")
$P "$HERE/smoke.py" --base http://127.0.0.1:8045/v1 --model "$MODEL_NAME" > "$OUT/smoke.json" 2>&1 || true

echo "######## [$KEY] 2) unmodified probe (app payload verbatim)"
$P "$HERE/run_model.py" --model "$KEY" --base http://127.0.0.1:8045/v1 --model-name "$MODEL_NAME" \
   --suite classification --subset ids --ids "$PROBE_C" --out "$OUT/unmodified_probe" || true
$P "$HERE/run_model.py" --model "$KEY" --base http://127.0.0.1:8045/v1 --model-name "$MODEL_NAME" \
   --suite assistant --subset ids --ids "$PROBE_A" --out "$OUT/unmodified_probe" || true

echo "######## [$KEY] 3) FULL suite (thinking-mode=$MODE)"
(nohup $P "$HERE/resources.py" --gpu 1 --container bench-server \
   --out "$OUT/resources_full.jsonl" --interval 3 --duration 14400 >/dev/null 2>&1 &)
for s in classification drafting rules simulate summary assistant; do
  echo "######## [$KEY] full $s $(date -u +%H:%M:%S)"
  $P "$HERE/run_model.py" --model "$KEY" --base http://127.0.0.1:8045/v1 --model-name "$MODEL_NAME" \
     --suite "$s" --out "$OUT" --thinking-mode "$MODE"
done

echo "######## [$KEY] 4) latency pass"
$P "$HERE/latency_pass.py" --base http://127.0.0.1:8045/v1 --model-name "$MODEL_NAME" \
   --key "$KEY" --out "$OUT" || true

$P - "$OUT" "$KEY" "$MODEL_NAME" "$BOOT" "$MODE" <<'EOF'
import json, sys
out, key, model, boot, mode = sys.argv[1:6]
path = out + "/server.json"
try:
    d = json.load(open(path))
except Exception:
    d = {}
d.update({"key": key, "model": model, "boot_s": int(boot), "thinking_mode": mode})
json.dump(d, open(path, "w"), indent=1)
print("server.json updated:", d)
EOF
echo "[$KEY] PIPELINE_DONE $(date -u)"
