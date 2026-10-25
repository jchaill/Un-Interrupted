# Ad-removal engine (vendored)

This directory is a pinned copy of the `ad-remover` skill's scripts, copied from
`~/Documents/Projects/RemoveAdds/ad-remover` on 2026-10-25.

Un-Interupted treats this as an **external engine** behind `app/engine/adapter.py`.
Nothing outside this directory depends on the engine's internal file names except
the adapter, so the engine can be upgraded independently.

## Local patch

`scripts/refine.sh` hard-coded `whisper-cli -l fr`. It now reads
`WHISPER_LANG` (falling back to `fr`). This matters for English shows: QA seam
windows must be transcribed in the episode language or English sponsor
signatures can be missed. No other scripts were modified.

## Upgrading

1. Copy a new upstream `scripts/` over `app/engine/upstream/scripts/`.
2. Re-apply the `WHISPER_LANG` patch to `refine.sh` (or check whether upstream
   has gained the same behaviour).
3. Update `VERSION`.
4. Run `python -m app.engine.selftest` (with ffmpeg/whisper installed) and the
   test suite. The app's contract is only the script names and their CLI,
   documented in `SKILL.md`; failures are surfaced per episode, never as an app
   crash.

The Whisper model is **not** bundled. It is downloaded on first run into the
app support directory (`models/`), and can be swapped without touching the app.
