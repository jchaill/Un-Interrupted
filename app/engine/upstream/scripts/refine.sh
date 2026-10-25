#!/usr/bin/env bash
# refine.sh <input.mp3> <center_sec> <halfwidth_sec> <tag>
# Re-transcribes a small fresh window around a time position for precise
# word-level boundaries (full-episode transcripts smear word edges into
# adjacent jingle music). Writes ./work/rf_<tag>.json — the tag MUST be unique
# per window; stale files are removed first (-y + rm) and errors are NOT
# swallowed (stale-json bugs caused a false QA pass once).
set -euo pipefail
IN=$1; C=$2; HW=$3; TAG=$4
MODEL=${WHISPER_MODEL:-}
if [[ -z "$MODEL" ]]; then
  MODEL=$(ls ggml-large*turbo*.bin 2>/dev/null | head -1 || true)
fi
if [[ -z "$MODEL" || ! -f "$MODEL" ]]; then
  echo "ERROR: whisper model not found (see transcribe.sh)" >&2; exit 1
fi
S=$(python3 -c "print(max(0,$C-$HW))")
E=$(python3 -c "print($C+$HW)")
# Un-Interupted patch: honor the caller's language instead of hard-coding fr.
# QA windows must be transcribed in the episode language (e.g. en) or English
# sponsor signatures can be missed. Defaults to fr for upstream compatibility.
LANGCODE=${WHISPER_LANG:-fr}
mkdir -p work
rm -f "work/rf_$TAG.wav" "work/rf_$TAG.json"
ffmpeg -hide_banner -loglevel error -y -ss "$S" -t "$(python3 -c "print($E-$S)")" -i "$IN" -ac 1 -ar 16000 "work/rf_$TAG.wav"
whisper-cli -m "$MODEL" -f "work/rf_$TAG.wav" -l "$LANGCODE" -sow -oj -of "work/rf_$TAG" >/dev/null 2>&1
python3 - "$TAG" "$S" <<'PYEOF'
import json, sys
tag, S = sys.argv[1], float(sys.argv[2])
d = json.load(open(f"work/rf_{tag}.json"))
for s in d["transcription"]:
    t0 = s["offsets"]["from"]/1000.0 + S
    t1 = s["offsets"]["to"]/1000.0 + S
    print(f"[{t0:9.2f} -> {t1:9.2f}] {s['text']}")
PYEOF