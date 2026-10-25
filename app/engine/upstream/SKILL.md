---
name: ad-remover
description: Remove ad breaks / sponsor spots from podcast or radio episodes in any language (French, English, …), e.g. "produce the episode without the ads" / "enlève les pubs". Also use when asked to find where ads are in an episode, or to make repeated "sans pubs"/"no ads" batches. Works by transcribing with whisper.cpp (auto language), locating the repeated sponsor spot and/or sponsor-language cues in the transcript, refining cut points with silence-dip detection, cutting with ffmpeg, verifying the result and producing a removal recap. Requires ffmpeg + whisper-cli (brew whisper-cpp) + a Whisper turbo ggml model in the working directory (or $WHISPER_MODEL).
---

# Remove ads from podcast episodes (any language)

Turn an episode with inserted ad breaks into a `"<base> (no ads).mp3"` version
(standard suffix `(no ads)` in every language; legacy `(sans pubs)` files still pair in the recap)
without touching the original. Pipeline: **calibrate → transcribe → locate ad
blocks → refine boundaries → cut → QA → verify → recap → cleanup**.
Scripts live in `scripts/`; run them from the directory that contains the
episodes (scratch goes to `./work/`).

Ads are found through **three signals, combined**: (1) *the repeated spot* —
podcasts insert the same sponsor recording 3–6 times per episode and often the
same ad across episodes (`scripts/find_repeats.py` finds those blocks whatever
the language); (2) *sponsor language* in the transcript ("brought to you by",
"présenté par", promo codes, URLs) (`scripts/analyze.py`); (3) *structure* —
untranscribed gaps where jingles sit. Detection without review is NOT trusted;
each episode's cut plan is reviewed before cutting.

## Requirements

- `ffmpeg`/`ffprobe`, `whisper-cli` (brew: `whisper-cpp`)
- A Whisper turbo model (e.g. `ggml-large-v3-turbo-q5_0.bin`, 574 MB) in the
  working directory or `$WHISPER_MODEL` (download hint at the bottom).
- Budget: ~8 min transcription per 100 min episode on an M4 (GPU); the rest is
  seconds.

## Hard rules

1. **Never overwrite originals.** Cut stream-copy results into a new file. Do
   re-cuts only after proving an existing output is wrong.
2. **Never hand-type accented filenames.** Address files via shell globs on
   ASCII substrings: `ls *"22 septembre"*` — a visually identical curly
   apostrophe typed literally will not match the disk name. Beware globs that
   match both the original and the `(sans pubs)` copy (disambiguate with the
   `.mp3` / `(sans pubs).mp3` ending). Restore original names at the end with
   a Python script built from unicode escapes if they were renamed.
3. **Every seam gets verified** by transcribing *fresh* windows with unique
   tags (a stale refine-tag file once caused a false QA pass).
4. **Review every episode's cut plan.** The scripts automate boundaries, not
   judgment. Decisions like "is the self-promo after each break an ad?" are
   made by reading the transcript and, when ambiguous, asking the user.

## Step 0 — inventory

`ls`, `ffprobe -show_entries format=duration` per file, `-show_entries
format_tags` (ID3 tags — `cut.py` preserves title/album/etc), `-show_chapters`
(usually none). Check the language of the conversation with the user; set
`WHISPER_LANG` (e.g. `fr`, `en`) or leave `auto`. Start long transcriptions
sequentially in a background `nohup` loop early; analyze transcripts as they
land.

## Step 1 — transcribe

`WHISPER_LANG=<lang> scripts/transcribe.sh <episode> <tag>` →
`work/tr_<tag>.json`. This also appends the episode to `work/index.json`
(tag → file, duration) which `recap.py` uses for pairing. mp3/flac/ogg/wav go
straight to whisper-cli; other containers (m4a…) are pre-converted by ffmpeg.

## Step 2 — calibrate THIS podcast (once, on one episode)

1. Read the transcript around suspicious spots: `scripts/analyze.py
   work/tr_<tag>.json` (sponsor-language hits + gaps) and `scripts/view.py
   work/tr_<tag>.json <from> <to>` to inspect regions.
2. **Run `scripts/find_repeats.py work/tr_<tag>.json`** — repeated blocks are
   almost always the inserted ad spot (same script read 3–6 times). Review the
   groups: distinct break regions; songs/hallucinations/catchphrases may
   appear — discard them by reading the text. If you have several episodes of
   the same podcast, pass two transcripts: ads are often shared, show content
   never is.
3. Probe untranscribed gaps ≥ 6 s (`scripts/refine.sh <mp3> <center> <width>
   <unique-tag>`): repeated junk ("C'est parti !" ×20, "Sous-titrage …") is
   whisper hallucinating on music/jingles — look WIDER, because a garbled or
   fully-skipped ad may hide there (this once hid an entire break; the
   fresh window revealed its real opener).
4. Decide the keep-list WITH the user if unclear: host self-promos repeated
   after each break, game/prize jingles, "subscribe to the show" plugs — ads,
   self-promo, or show flow? Sponsor mentions woven into conversation
   ("integrations") are usually kept and flagged.
5. Write the calibration file **`work/labels.json`**:
   `[["<regex>", "<Ad title>"], …]`. One regex per ad campaign covering its
   opener to closer (opener anchors the block start — omit it and cuts land
   mid-ad voice). Example for a worked RTL "Les Grosses Têtes" run:
   `[["(?i)wayfair|big ?news|every style|avenues at northfield|come see",
   "Wayfair «Every style, every home»"],
    ["(?i)sncf|voyageurs|bienvenue dans ce train|ouigo|eurostar|liberté plus",
   "SNCF Voyageurs «Jours trains incroyables»"]]`.
   Everything downstream (detect, QA, recap) reads this one file. Optionally
   set `AD_SHOW_STOP` (a regex for show re-entry lines / sung idents that must
   never be absorbed) and `AD_LANG=fr` to also flag English text inside French
   episodes.

## Step 3 — detect blocks, refine boundaries, review

`scripts/detect_cuts.py <in.mp3> work/tr_<tag>.json work/cuts_<tag>.json`

Prints blocks (labels.json seeds merged ≤ 25 s apart; interior text belongs to
the ad), refined cut points, leftover candidates and gaps ≥ 6 s. Read the
report; hand-fix the JSON where the transcript lied. Boundary logic
(already in the script):

- Start when speech is glued to the ad (gap ≤ 3 s): only trust a HARD RMS dip
  (≤ −60 dB) near the seam, else `sig_start + 0.6`.
- Start across a music gap (> 3 s): earliest hard dip inside
  `[prev_end + 0.5, sig_start − 0.5]` (never beyond the ad's transcribed
  voice), else decreasing thresholds, else a fixed small offset into the gap.
- End: hard/deep dip between ad tail and the next segment, capped at
  `next_seg_start + 0.9`.
- `near_eof` blocks cut to EOF. Whisper smears word edges into adjacent music
  by up to ~1 s — for suspicious boundaries re-transcribe a ±6–9 s fresh
  window with `scripts/refine.sh` and compare; `scripts/dip.py <mp3> <time>`
  lists real RMS dips by hand (`−104 dB` levels are the podcast's edit seams).

If a break's opener was garbled/skipped by whisper, repeat-signatures anchor
too late: probe the preceding gap and extend the range manually (curated
ranges can be fed as `--blocks <file.json>`, plain `[[start,end],…]`).

## Step 4 — cut

`scripts/cut.py <in.mp3> "<base> (no ads).mp3" work/cuts_<tag>.json`

Stream copy per keep-range + concat + ID3 tags preserved (mp3 path verified
frame-accurately). Other containers skip the mp3-only muxer flags; if a
container misbehaves in concat, re-encode instead: `CUT_REENCODE="aac 128k"
scripts/cut.py …`. Expect duration delta ≤ ±0.2 s vs `input − Σ cuts`;
otherwise the concat silently failed — fix and re-run.

## Step 5 — QA the seams (content)

`scripts/qa_join.py "<base> (no ads).mp3" work/cuts_<tag>.json`

Re-transcribes fresh windows around every seam, head and tail, and FAILS on
any leak of the labels.json signatures. Eyeball the windows for clipped show
words (a clipped sentence = bug; a few seconds of kept jingle music = style).

## Step 5b — verify the file (integrity / playability)

`scripts/verify_audio.sh "<base> (no ads).mp3" <expected_duration>`

Stream sanity (codec/rate/channels/sane bitrate), full error-scanning decode,
seek+decode of the last 5 s (truncated-ending detector), duration vs expected
±0.5 s, ID3 title preserved. Non-zero exit on failure — re-cut from the
original before debugging further. PASS/FAIL is logged to `work/verify.log`.

## Step 6 — recap

`scripts/recap.py`

End-of-run report (printed + `work/recap.txt`): per episode — original
length, output length, total ads removed, sponsor title(s) with break counts
and ranges, QC status; plus totals and a NEEDS-ATTENTION list. Pairs files
via `work/index.json`; sponsor names come from `work/labels.json` (add new
campaigns there when discovered). Run BEFORE `cleanup --all`.

## Step 7 — cleanup

`scripts/cleanup.sh` (`--dry-run` to preview; `--all` also removes
transcripts + cut plans + removes an empty `work/`). Default keeps the tiny
transcripts (`work/tr_*.json`) and cut plans (`work/cuts_*.json`) — they save
the ~8 min/episode transcription when re-analyzing. `cut.py` always cleans
its own temp dir, even on failure. The whitelist only matches
pipeline-generated names; never wipe a directory you have not reviewed.

## Limitations (state them to the user when relevant)

- Ads inserted **dynamically with different reads per break**, or live host
  reads woven into the conversation, do NOT repeat cleanly — they surface via
  sponsor-language cues and gaps, and still need human review.
- A **single-occurrence** ad (no repeats anywhere) relies entirely on sponsor
  language + structure — lower confidence, review carefully.
- Boundary quality depends on transcripts; music-heavy break openers whisper
  may skip entirely — the gap probe is the safety net.

## Download a missing model

`huggingface-cli download ggerganov/whisper.cpp ggml-large-v3-turbo-q5_0.bin --local-dir .`