#!/usr/bin/env bash
# Phase A for a candidate: serve + wait + smoke + unmodified probe (auto mode).
#   next_candidate.sh <key>
set -eu
KEY="${1:?usage: next_candidate.sh <key>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
P=~/.local/venvs/mtbench/bin/python
OUT="$HERE/../results/$KEY"
mkdir -p "$OUT"

bash "$HERE/serve.sh" "$KEY"
T0=$(date +%s)
for i in $(seq 1 300); do
  curl -sf http://127.0.0.1:8045/v1/models >/dev/null 2>&1 && break
  sleep 5
done
T1=$(date +%s); BOOT=$((T1 - T0))
echo "[$KEY] server up after ~${BOOT}s"
curl -s http://127.0.0.1:8045/v1/models > "$OUT/models.json" || true
MODEL_NAME=$($P -c "import json;print(json.load(open('$OUT/models.json'))['data'][0]['id'])")
$P "$HERE/smoke.py" --base http://127.0.0.1:8045/v1 --model "$MODEL_NAME" > "$OUT/smoke.json" 2>&1 && SMOKE=pass || SMOKE=fail
echo "[$KEY] smoke: $SMOKE | boot ${BOOT}s"
$P - "$OUT" "$KEY" "$MODEL_NAME" "$BOOT" <<'EOF'
import json, sys
out, key, model, boot = sys.argv[1:5]
path = out + "/server.json"
d = {}
try:
    d = json.load(open(path))
except Exception:
    pass
d.update({"key": key, "model": model, "boot_s": int(boot)})
json.dump(d, open(path, "w"), indent=1)
EOF
if [ "$SMOKE" != "pass" ]; then
  echo "[$KEY] SMOKE FAILED — inspect $OUT/smoke.json"; exit 2
fi

echo "######## [$KEY] unmodified probe"
$P "$HERE/run_model.py" --model "$KEY" --base http://127.0.0.1:8045/v1 --model-name "$MODEL_NAME" \
   --suite classification --subset ids \
   --ids "cls_normal_101,cls_reply_112,cls_normal_130,cls_normal_190,cls_ambig_131,cls_adv_290,cls_long_minutes,cls_junk_mash" \
   --out "$OUT/unmodified_probe" || true
$P "$HERE/run_model.py" --model "$KEY" --base http://127.0.0.1:8045/v1 --model-name "$MODEL_NAME" \
   --suite assistant --subset ids --ids "asst_b6_folders,asst_q1_invoice_paid,asst_g2_flag_ci" \
   --out "$OUT/unmodified_probe" || true
$P "$HERE/summarize_probes.py" "$KEY" || true
echo "[$KEY] PROBE_DONE"
