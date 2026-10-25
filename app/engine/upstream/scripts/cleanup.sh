#!/usr/bin/env bash
# cleanup.sh [--all] [--dry-run]
# Cleans the ./work scratch directory between/after runs.
#
#   default : removes refine windows (rf_*.wav/json), temp cut dirs (work/cuts_*/
#             segments), and logs. KEEPS transcripts (tr_*.json) and cut plans
#             (cuts_*.json) — they are tiny and save expensive re-computation.
#   --all   : also removes transcripts and cut plans; removes work/ entirely
#             if nothing else remains.
#   --dry-run: list what would be removed.
#
# Files NOT created by this pipeline are never touched (whitelist only).
set -u
MODE=scratch
[ "${1:-}" = "--all" ] && MODE=all
DRY=0
for a in "$@"; do [ "$a" = "--dry-run" ] && DRY=1; done

mkdir -p work || exit 1
LIST=$(mktemp)
add() { printf '%s\n' "$1" >> "$LIST"; }

# scratch, always
for f in work/rf_*.wav work/rf_*.json work/*.log; do
  [ -f "$f" ] && add "$f"
done
# temp segment dirs from cut.py
find work -maxdepth 1 -type d -name 'cuts_*' | while read -r d; do add "$d"; done
# transcripts + cut plans only with --all
if [ "$MODE" = all ]; then
  for f in work/tr_*.json work/cuts_*.json work/sample*.json; do
    [ -f "$f" ] && add "$f"
  done
fi

if [ "$DRY" = 1 ]; then
  echo "Would remove:"; sed 's/^/  /' "$LIST"; wc -l < "$LIST" | xargs echo "items:"
fi
if [ "$DRY" = 0 ]; then
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    if [ -d "$p" ]; then rm -rf "$p"; else rm -f "$p"; fi
    echo "removed: $p"
  done < "$LIST"
fi
rm -f "$LIST"
# with --all, drop work/ once nothing pipeline-related remains
if [ "$MODE" = all ] && [ "$DRY" = 0 ]; then
  if [ -z "$(find work -mindepth 1 -maxdepth 1 -not -name '.*' 2>/dev/null)" ]; then
    rmdir work && echo "removed: work/"
  fi
fi