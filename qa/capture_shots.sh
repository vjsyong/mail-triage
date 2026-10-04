#!/usr/bin/env bash
# mail-triage UI loop — screenshot capture.
# Usage: bash qa/capture_shots.sh [tag]   (default tag: <date>-<HHMM>)
# Writes qa/shots/<tag>/mobile/*.png (393x852@2x) and qa/shots/<tag>/desktop/*.png (1440x900).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${1:-$(date +%Y%m%d-%H%M)}"
OUT="$ROOT/qa/shots/$TAG"
BASE="http://127.0.0.1:8097"
SHOT_TIMEOUT=90   # per-shot hard cap: a wedged page or binary must never stall the loop

mkdir -p "$OUT/mobile" "$OUT/desktop"

# Pick the newest chromium that can actually take a screenshot. Some Chrome-for-Testing
# builds hang forever on --screenshot (browser starts, PNG never lands), so probe each
# candidate with a data: URL first and use the first one that produces a PNG.
BIN=""
CANDIDATES="$HOME/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome
$(find "$HOME/.cache/ms-playwright" -maxdepth 3 \( -name chrome -o -name chrome-headless-shell \) -type f 2>/dev/null | sort -r)"
while IFS= read -r cand; do
  [ -n "$cand" ] && [ -x "$cand" ] || continue
  rm -f "$OUT/.probe.png"
  timeout 30 "$cand" --headless --no-sandbox --disable-gpu --screenshot="$OUT/.probe.png" "data:text/html,ok" >/dev/null 2>&1
  if [ -s "$OUT/.probe.png" ]; then BIN="$cand"; break; fi
done <<< "$CANDIDATES"
rm -f "$OUT/.probe.png"
if [ -z "$BIN" ]; then echo "no working chromium binary found"; exit 1; fi
echo "using: $BIN"

# newest message id for the viewer page
MID="$(curl -s --max-time 5 "$BASE/messages" | grep -oE '/messages/[0-9]+' | head -1 | grep -oE '[0-9]+')"
[ -n "${MID:-}" ] || MID=1

PAGES="dashboard:/
messages:/messages
viewer:/messages/$MID
assistant:/assistant
rules:/rules
rule-editor:/rules/new
classifiers:/classifiers
settings:/settings
more:/more
log:/log
accounts:/accounts
templates:/templates
flows:/flows"

shot(){ # name url w h scale subdir
  rm -f "$OUT/$6/$1.png"
  timeout "$SHOT_TIMEOUT" "$BIN" --headless --no-sandbox --disable-gpu --hide-scrollbars \
    --force-device-scale-factor="$5" --window-size="$3,$4" \
    --virtual-time-budget=4000 --screenshot="$OUT/$6/$1.png" "$BASE$2" >/dev/null 2>&1
  if [ -s "$OUT/$6/$1.png" ]; then echo "ok $6/$1"; else echo "FAIL $6/$1"; fi
}

while IFS= read -r p; do
  [ -z "$p" ] && continue
  name="${p%%:*}"; url="${p#*:}"
  shot "$name" "$url" 393 852 2 mobile
done <<< "$PAGES"
while IFS= read -r p; do
  [ -z "$p" ] && continue
  name="${p%%:*}"; url="${p#*:}"
  shot "$name" "$url" 1440 900 1 desktop
done <<< "$PAGES"

echo "done: $OUT"
