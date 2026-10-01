#!/usr/bin/env bash
# mail-triage UI loop — screenshot capture.
# Usage: bash qa/capture_shots.sh [tag]   (default tag: <date>-<HHMM>)
# Writes qa/shots/<tag>/mobile/*.png (393x852@2x) and qa/shots/<tag>/desktop/*.png (1440x900).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${1:-$(date +%Y%m%d-%H%M)}"
OUT="$ROOT/qa/shots/$TAG"
BASE="http://127.0.0.1:8097"

BIN="$HOME/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"
[ -x "$BIN" ] || BIN="$(find "$HOME/.cache/ms-playwright" -maxdepth 3 -name chrome -type f 2>/dev/null | sort -r | head -1)"
if [ ! -x "$BIN" ]; then echo "no chromium binary found"; exit 1; fi

# newest message id for the viewer page
MID="$(curl -s --max-time 5 "$BASE/messages" | grep -oE '/messages/[0-9]+' | head -1 | grep -oE '[0-9]+')"
[ -n "${MID:-}" ] || MID=1

mkdir -p "$OUT/mobile" "$OUT/desktop"

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
  "$BIN" --headless --no-sandbox --disable-gpu --hide-scrollbars \
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
