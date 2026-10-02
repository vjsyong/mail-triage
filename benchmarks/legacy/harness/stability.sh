#!/usr/bin/env bash
# Phase-19 stability: repeat the hard/adversarial subset 3x for a model.
#   stability.sh <key> <mode> <which: clsfy|asst|both>
set -eu
KEY="${1:?usage: stability.sh <key> <mode> [clsfy|asst|both]}"
MODE="${2:?}"
WHICH="${3:-both}"
HERE="$(cd "$(dirname "$0")" && pwd)"
P=~/.local/venvs/mtbench/bin/python
OUT="$HERE/../results/$KEY/stability"
MODEL_NAME=$($P -c "import json;print(json.load(open('$HERE/../results/$KEY/models.json'))['data'][0]['id'])")

if [ "$MODE" = "sampling" ]; then
  EXTRA="--thinking-mode auto --temperature 0.7 --top-p 0.8"
else
  EXTRA="--thinking-mode $MODE"
fi

HARD_C="cls_adv_290,cls_adv_291,cls_adv_293,cls_adv_294,cls_adv_296,cls_adv_299,cls_junk_ctrl,cls_junk_b64"
HARD_A="asst_i1_phishing,asst_i3_admin_claim,asst_i5_mandy_inject,asst_h2_renovation,asst_h3_tax_auditor,asst_x1_invoice_ambiguous"

for r in 1 2 3; do
  echo "######## [$KEY] stability run $r $(date -u +%H:%M:%S)"
  ROUT="$OUT/r$r"
  mkdir -p "$ROUT"
  if [ "$WHICH" != "asst" ]; then
    # shellcheck disable=SC2086
    $P "$HERE/run_model.py" --model "$KEY-r$r" --base http://127.0.0.1:8045/v1 \
       --model-name "$MODEL_NAME" --suite classification --subset ids --ids "$HARD_C" \
       --out "$ROUT" $EXTRA
  fi
  if [ "$WHICH" != "clsfy" ]; then
    # shellcheck disable=SC2086
    $P "$HERE/run_model.py" --model "$KEY-r$r" --base http://127.0.0.1:8045/v1 \
       --model-name "$MODEL_NAME" --suite assistant --subset ids --ids "$HARD_A" \
       --out "$ROUT" $EXTRA
  fi
done
echo "[$KEY] STABILITY_DONE $(date -u)"
