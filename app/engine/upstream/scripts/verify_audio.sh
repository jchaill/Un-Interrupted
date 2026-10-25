#!/usr/bin/env bash
# verify_audio.sh <out.mp3> [expected_duration_s]
# Checks that a produced audio file is valid and playable:
#   1. container/stream sane (codec, sample rate, channels, bit_rate)
#   2. full decode scan with ffmpeg (-v error) -> any decode error fails
#   3. tail seek+decode (last 5 s) -> catches truncated/corrupt endings
#   4. duration matches expected within 0.5 s (if provided)
#   5. ID3 title tag preserved (warn only)
#   6. file size sanity
set -u
F=$1
EXPECTED=${2:-}   # optional expected duration in seconds
FAIL=0
note() { printf '%s\n' "$*"; }
bad()  { printf 'FAIL: %s\n' "$*"; FAIL=1; }

[ -f "$F" ] || { bad "file not found: $F"; exit 1; }

SIZE=$(stat -f %z "$F" 2>/dev/null || stat -c %s "$F")
[ "$SIZE" -gt 100000 ] || bad "file suspiciously small: $SIZE bytes"

# 1. stream info
INFO=$(ffprobe -v error -select_streams a:0 -show_entries \
  stream=codec_name,sample_rate,channels,bit_rate -of csv=p=0 "$F")
note "stream: $INFO"
case "$INFO" in
  mp3,*) : ;;
  *) bad "unexpected codec/stream: '$INFO' (expected mp3,...)" ;;
esac
BR=${INFO##*,}
case "$BR" in ''|0|*[!0-9]*) bad "bit_rate unreadable/zero: '$BR'" ;;
  [0-9]*) [ "$BR" -lt 32000 ] && bad "bit_rate $BR absurdly low" ;;
esac

# 2. full decode scan
ERRS=$(ffmpeg -v error -i "$F" -f null - 2>&1 | head -5)
if [ -n "$ERRS" ]; then bad "decode errors:\n$ERRS"; else note "decode: clean (full file)"; fi

# 3. tail seek+decode
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$F")
TAIL_START=$(python3 -c "print(max(0,$DUR-5))")
TERRS=$(ffmpeg -v error -ss "$TAIL_START" -i "$F" -f null - 2>&1 | head -3)
if [ -n "$TERRS" ]; then bad "tail seek/decode errors:\n$TERRS"; else note "tail seek+decode: clean"; fi

# 4. expected duration
if [ -n "${EXPECTED:-}" ]; then
  DELTA=$(python3 -c "print(abs($DUR-$EXPECTED))")
  if python3 -c "exit(0 if $DELTA <= 0.5 else 1)"; then
    note "duration: $DUR s (expected $EXPECTED, delta ${DELTA}s) OK"
  else
    bad "duration $DUR differs from expected $EXPECTED by ${DELTA}s"
  fi
fi

# 5. tags
TITLE=$(ffprobe -v error -show_entries format_tags=title -of csv=p=0 "$F")
[ -n "$TITLE" ] && note "id3 title: $TITLE" || note "WARN: no title tag (check -map_metadata)"

# 6. verdict
if [ "$FAIL" -eq 0 ]; then note "VERIFY PASS: $F"; else note "VERIFY FAILED: $F"; fi
# append QC line for recap.py
mkdir -p work
printf '%s|%s|%s|%s\n' "$([ "$FAIL" -eq 0 ] && echo PASS || echo FAIL)" "$(basename "$F")" "$DUR" "${BR:-na}" >> work/verify.log
exit $FAIL