#!/usr/bin/env bash
# transcribe.sh <input.mp3|m4a|flac|ogg> [tag]
# Transcribes a full episode with whisper.cpp into ./work/tr_<tag>.json
# Language: $WHISPER_LANG (default: auto, works across languages; set "fr" or
# "en" when you know it).
# Formats: whisper-cli reads mp3/flac/ogg/wav directly; anything else (m4a,
# wma, webm...) is pre-converted to 16 kHz mono wav via ffmpeg.
# Model resolution: $WHISPER_MODEL, else ggml-large*turbo*.bin in cwd, else fail with hint.
set -euo pipefail
IN=$1
TAG=${2:-$(basename "${IN%.*}")}
LANGCODE=${WHISPER_LANG:-auto}
MODEL=${WHISPER_MODEL:-}

if [[ -z "$MODEL" ]]; then
  MODEL=$(ls ggml-large*turbo*.bin 2>/dev/null | head -1 || true)
fi
if [[ -z "$MODEL" || ! -f "$MODEL" ]]; then
  echo "ERROR: whisper model not found. Set WHISPER_MODEL=/path/to/ggml-large-v3-turbo-q5_0.bin" >&2
  echo "Download: huggingface-cli download ggerganov/whisper.cpp ggml-large-v3-turbo-q5_0.bin --local-dir ." >&2
  exit 1
fi

mkdir -p work
rm -f "work/tr_$TAG.json"
EXT="${IN##*.}"; EXT=$(echo "$EXT" | tr '[:upper:]' '[:lower:]')
SRC="$IN"
case "$EXT" in
  mp3|flac|ogg|wav) ;;
  *) SRC="work/pre_$TAG.wav"
     echo "=== pre-converting $IN -> $SRC"
     ffmpeg -hide_banner -loglevel error -y -i "$IN" -ac 1 -ar 16000 "$SRC" ;;
esac
echo "=== transcribing $SRC (lang=$LANGCODE) with $MODEL $(date +%H:%M:%S)"
whisper-cli -m "$MODEL" -f "$SRC" -l "$LANGCODE" -sow -oj -of "work/tr_$TAG" 2>&1 | tail -2

# register run in work/index.json: tag -> source file (+ duration), so that
# recap.py can pair originals, outputs, transcripts and cut plans later
python3 - "$TAG" "$IN" <<'PY'
import json, os, subprocess, sys
tag, inp = sys.argv[1], sys.argv[2]
idx = {}
p = "work/index.json"
if os.path.exists(p): idx = json.load(open(p))
d = None
r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                    "-of","csv=p=0", inp], capture_output=True, text=True)
try: d = float(r.stdout.strip())
except ValueError: pass
idx[tag] = {"mp3": os.path.basename(inp), "duration": d}
json.dump(idx, open(p, "w"), ensure_ascii=False, indent=1)
PY