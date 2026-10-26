# Un-Interupted — Product & Build Plan

> Status: **working MVP implemented** (Phases 0–2 and most of Phase 3).
> The full pipeline has been run end-to-end on this Mac: local RSS feed →
> download → Whisper transcription → cut detection → review → approval →
> cut → QA → published feed → HTTP Range playback.
>
> Remaining before calling it v1: first-run model download on a clean machine,
> a couple of real feeds subscribed in Downcast, and per-show calibration.

---

## 1. What it does

Un-Interupted is a private, self-hosted podcast proxy for macOS:

1. Subscribes to official podcast RSS feeds.
2. Polls them and downloads new episodes.
3. Runs the vendored **ad-remover** pipeline (whisper.cpp + repeat/sponsor
   detection + ffmpeg cut + QA) to produce an ad-free copy.
4. Holds the clean copy for review before publishing (by default).
5. Publishes **one private RSS feed per podcast**, plus audio served with HTTP
   Range support, for on-device podcast clients such as Downcast and Apple
   Podcasts. Server-side crawlers (Overcast, Pocket Casts, Castro) need a public
   feed URL.
6. Provides a local web UI to add feeds, import OPML, configure each show,
   browse older episodes, flag them for processing, review cuts, and copy the
   private feed URL.

The phone only talks to this Mac. Original feeds are never exposed.

---

## 2. Locked decisions

| Topic | Decision |
|---|---|
| Host | Native macOS (Mac mini M1, 16 GB), no Docker |
| Whisper | Native `whisper-cli` from Homebrew (Metal); model downloaded on first run, not bundled |
| Python | 3.12 virtualenv, created by `scripts/bootstrap.sh` |
| Toolchain | First-run UI installs `ffmpeg` + `whisper.cpp` via Homebrew and downloads `ggml-large-v3-turbo-q5_0.bin` (~574 MB) |
| Network | LAN only for now; remote access is out of scope |
| Auth | One admin password + per-podcast feed/media tokens; no multi-user |
| Feed shape | One private feed per podcast; **no combined feed**; admin-only OPML export |
| Review | Download → transcribe → detect → **AWAITING REVIEW**; publish only after approval by default |
| Auto-publish | Opt-in per podcast, gated on history/confidence/warnings |
| Backfill | Default **latest 3**; UI offers 0/3/5/10/all; older episodes can be selected individually or in batches |
| Retention | Per-podcast dropdown: 0/7/14/30/60/90/180/365 days or keep forever; **originals only** (clean files are never auto-deleted) |
| Metadata | Preserve original title, description, link, comments, pubDate, episode/season, artwork; clean title gets ` (no ads)` |
| Artwork | Cached locally and served from the app |
| Audio | Preserve source format when possible; MP3 uses the upstream stream-copy cutter, other containers use an ffmpeg re-encode fallback |
| Language | Auto-detect from the feed, editable per show (`auto`/`fr`/`en`/…); Whisper and QA share it |
| Ads | Default policy: remove clear sponsor/network reads and repeated blocks; flag ambiguous cuts for review |
| LLM | Ollama semantic pass on every detect job (local or cloud); suggestions only, deterministic cutter/QA stay authoritative |
| Port | `8090` by default; auto-detected LAN IP; both editable at setup/settings |
| Storage | GUI-selected root; default `~/Podcasts/Un-Interupted`; relocatable from Settings |
| Spelling | Folder/repo stays `Un-Interupted` |

### Existing data

The old MP3s in `RemoveAdds/` are **not** imported. The app starts from the RSS
feeds in `~/Documents/Overcast Podcast Subscriptions.opml`. The old model may be
reused manually by setting `UNINTERUPTED_MODEL`, but a fresh install downloads
its own copy.

---

## 3. Architecture

```
┌────────────────────┐        ┌──────────────────────────┐
│ Browser (HTMX)     │        │ Downcast / on-device app │
└─────────┬──────────┘        └─────────────┬────────────┘
          │ admin UI                         │ RSS + Range audio
          ▼                                  ▼
┌──────────────────────────────────────────────────────────┐
│ app.web — FastAPI                                        │
│ setup · auth · feeds · review · queue · RSS · OPML · media│
└──────────────┬──────────────────────────────┬────────────┘
               │ SQLite WAL                   │ files
               ▼                              ▼
┌────────────────────────────┐      ┌──────────────────────┐
│ app.worker (thread)        │      │ storage/podcasts/... │
│ scheduler · job queue      │─────▶│ episodes/ originals/ │
│ pipeline stages            │      │ work/ logs/          │
└──────────────┬─────────────┘      └──────────────────────┘
               │ subprocesses
               ▼
   app/engine/upstream/scripts
   (vendored ad-remover: transcribe, detect, cut, QA)
```

### Runtime layout

- **One process** (`python -m app`) hosts the web UI and one worker thread.
  `UNINTERUPTED_NO_WORKER=1` is available for development.
- The worker holds an exclusive `flock` (`worker.lock`) so a second instance
  cannot double-process jobs, and writes `worker.json` every 10 s so long
  transcriptions do not look like a dead worker.
- SQLite in WAL mode is the queue and the source of truth. Job claiming uses
  `BEGIN IMMEDIATE`; one heavy job runs at a time.
- The ad-removal engine is isolated behind `app/engine/adapter.py`. Only that
  module knows the upstream script names and cwd rules. An engine failure is a
  per-episode failure, never an app crash.

### Engine contract and upgrades

- Scripts live in `app/engine/upstream/scripts/`; version/upstream notes in
  `app/engine/upstream/VERSION` and `UPSTREAM.md`.
- Every job runs with `cwd = podcasts/<slug>/` so the upstream scripts can use
  their expected relative `work/` paths. Scripts are invoked by absolute path.
- To upgrade: replace the vendored scripts, keep the one documented local patch
  (`refine.sh` honors `WHISPER_LANG`), bump `VERSION`, and run one review
  episode before trusting auto-publish. No other app module changes.

---

## 4. Storage layout

App state lives in `~/Library/Application Support/Un-Interupted/`:

```
config.json          # secret_key, password hash, storage root, base URL, port
db.sqlite3           # podcasts, episodes, jobs, settings
models/              # ggml-large-v3-turbo-q5_0.bin (downloaded on first run)
cache/artwork/       # local artwork cache
logs/                # setup.log, worker.log, job logs for non-podcast jobs
setup_state.json     # first-run toolchain progress
worker.json / .lock  # heartbeat and single-worker guard
```

Library files live under the selected storage root:

```
podcasts/
  <slug>/
    episodes/        # clean files, the only audio served publicly
    originals/       # downloaded sources, admin-only, retention applies
    work/            # transcripts, cuts, clean previews, labels, per-job logs
```

Filenames are ASCII-safe and stable:
`YYYY-MM-DD - episode-slug - <guid8> (no ads).mp3`. The real title remains in
the DB and in the RSS item; the filename is not the title source for downloaded
episodes.

---

## 5. Data model (SQLite, schema v1)

- `podcasts`: slug, feed URL, metadata, artwork, `labels_json`, `ad_show_stop`,
  `ad_lang`, auto-process/publish, backfill/poll/retention, `feed_token`,
  conditional-GET validators, last error.
- `episodes`: original GUID, metadata, original URL/path, clean filename/guid/
  duration/size, status, confidence, proposed/approved cuts, review JSON,
  LLM provenance JSON, engine version, error, feed `published_at`,
  `clean_published_at`.
- `jobs`: type, state, podcast/episode, payload, priority, attempts,
  `run_after`, log path, progress.
- `settings`: key/value (`last_cleanup_at`, `default_retention_days`).

Episode statuses:

`discovered → queued → downloading → downloaded → transcribing → detecting →
awaiting_review → approved → cutting → qa → ready_publish → publishing →
published`, plus `qa_failed`, `failed`, `skipped`.

`clean_published_at` is the retention clock; the original feed `published_at`
is kept for RSS ordering and display.

---

## 6. Processing pipeline

1. **Poll** — conditional GET; upsert all entries by GUID; auto-select new ones
   only if the podcast has auto-process enabled.
2. **Download** — resumable HTTP Range, `.part` + atomic rename, free-space
   guard, never overwrite an existing original.
3. **Transcribe** — `transcribe.sh` with the app model and podcast language.
4. **Detect** — `detect_cuts.py` with per-podcast labels, show-stop, ad language;
   always merge Ollama segment-ID proposals; build a review object with cut
   excerpts, labels, repeats, LLM provenance, warnings, and a confidence score.
5. **Review** — default stop. The UI lets the user edit/delete/add ranges,
   approve, skip, re-detect, or re-cut.
6. **Cut** — upstream `cut.py` for MP3 (stream copy); generic ffmpeg
   `atrim`+`concat` re-encode for other containers. Duration sanity check.
7. **QA** — `qa_join.py` seam checks + `verify_audio.sh` + Python full-decode
   verification. QA failure marks `qa_failed`; it never reaches the feed.
8. **Publish** — atomic move to `episodes/`, set clean GUID/size/duration/
   `clean_published_at`, enqueue retention cleanup.
9. **Cleanup** — daily; deletes originals older than the per-podcast retention
   window. Clean files are never deleted automatically.

### Auto-publish gate

Auto-publish is off by default. When enabled for a podcast, an episode with cuts
can skip review only if:

- the podcast already has at least 3 published episodes,
- the review has no warnings,
- confidence is at least 0.80,
- and QA passes.

A no-ad episode is approved and published as-is immediately, because there is
nothing to cut or QA. The exception is a failed LLM pass: “no ads” is not
trusted then, so the episode stays in review.

Otherwise it stays in `awaiting_review`.

### 6.1 LLM-assisted detection (implemented, always on)

The deterministic detector remains the baseline and the final cutter/QA stays
unchanged. Every detect job sends transcript *segments* (never audio) to an
Ollama chat model and asks for JSON ranges by segment ID. It does not receive
timestamps and never edits audio itself.

- Default model: `deepseek-v4.1-flash:cloud` through the signed-in local Ollama
  daemon (`http://127.0.0.1:11434`). A direct `https://ollama.com` endpoint plus
  API key is also supported.
- Settings **Check LLM install** reports the Ollama binary/daemon status and
  lists models from `/api/tags`; if none exist it explains the missing
  `ollama run <model>` step.
- Chunked transcript with four-segment overlap; each chunk returns
  `start_seg`/`end_seg`, `kind`, `brand`, `confidence`, `evidence`.
- Guardrails: confidence threshold (default 0.72), minimum 3 s, in-range IDs,
  show-stop regex, 25% removal cap, contiguous-range normalisation, overlap
  de-duplication.
- Merge policy: union with regex/repeat cuts, with provenance in
  `review_json`. LLM-only cuts are warned and blocked from auto-publish until
  they have been reviewed and corroborated.
- Failure policy: Ollama errors are logged; the job still reaches review with
  heuristic cuts. The LLM result is stored in `episodes.llm_json` for audit.
- Sponsor memory / cross-episode repeats / few-shot feedback are still future
  work; this first version intentionally does not promote brands automatically.

---

## 7. Web UI

- **Dashboard** — cards per podcast (artwork, status counts, last poll, error),
  queue depth, worker/toolchain health.
- **Add podcast** — URL preview, backfill 0/3/5/10/all, auto-process.
- **Import OPML** — upload or paste; select feeds and backfill.
- **Podcast detail** — copy feed URL, QR code, per-show settings (title,
  description, author, language, category, labels JSON, show-stop, ad language,
  poll interval, retention, auto-process/auto-publish), library sizes, and a
  filterable/paginated episode table with individual **Process** buttons and
  checkbox multi-select for batch download/processing.
- **Review** — original audio player, proposed clean preview, cut rows with
  transcript excerpts/warnings, editable start/end, approve/skip/re-detect/
  re-cut.
- **Queue / job** — job list, states, attempts, per-job log tail, retry/cancel.
- **Settings** — base URL, port, default retention, admin password, storage root
  relocation (queued copy job with marker/overlap guards).
- **First run** — setup, toolchain install with live progress, then dashboard.

No frontend build step: Jinja2 + HTMX + small vanilla JS.

---

## 8. RSS and media

- RSS 2.0 with iTunes, Atom self-link, and `content:encoded`.
- Channel: original title + ` (no ads)`, description, language, author,
  category, explicit, local artwork.
- Item: original title/description/link/comments/pubDate, iTunes episode/season,
  duration, author, explicit, artwork.
- Stable GUID: `uninterupted:<slug>:<original-guid>:clean` (or filename hash for
  hand-dropped files).
- One private feed per podcast: `/feed/<slug>/<token>.xml`.
- Media: `/media/<slug>/<token>/<filename>` with `Accept-Ranges`, `206`,
  `Content-Range`, ETag, Last-Modified, If-None-Match, If-Range, HEAD, 416.
- Local artwork: `/artwork/<slug>/<token>.<ext>`, with SVG placeholder fallback.
- Admin-only OPML: `/opml.xml` containing all private feed URLs.
- The feed scans `episodes/`; a hand-dropped audio file is published even if it
  has no DB row, using the filename as the title.

---

## 9. Toolchain and first run

`scripts/bootstrap.sh`:

1. Finds/installs Homebrew `python@3.12`.
2. Creates `.venv`, installs `requirements.txt`.

`scripts/run.sh` / `Un-Interupted.command` start `python -m app`.

On first run the setup page asks for storage root, base URL, and admin password.
`app/toolchain.py` then:

- checks `ffmpeg`, `ffprobe`, `whisper-cli`, the model, and the vendored scripts;
- installs missing Homebrew formulas (`ffmpeg`, `whisper.cpp`);
- downloads the model from Hugging Face with resume and progress;
- writes `setup_state.json` and `logs/setup.log`;
- marks `toolchain_ready` when everything is present.

The model is **not** in the repo. On a clean Mac the first run needs network
access and roughly 1 GB of free space.

### macOS notes

- Homebrew must be installed.
- If `brew install` fails on a fresh machine, accept the Xcode license once:
  `sudo xcodebuild -license accept`.
- Gatekeeper may warn on the double-click `.command`; right-click → Open.
- Optional autostart: `scripts/install-launchd.sh` (label `com.uninterupted.app`),
  removed with `scripts/uninstall-launchd.sh`.

---

## 10. Security and privacy

- Admin password hashed with PBKDF2-SHA256 (600k iterations), signed HttpOnly
  session cookie (30 days).
- Feed/media/artwork require the per-podcast token; comparison is constant-time.
- Originals and work artifacts are admin-only and never appear in feeds.
- LAN HTTP only; `secure=False` on the cookie. Put Tailscale/Caddy in front if
  exposing beyond the LAN, and set the base URL to the reachable address.
- No CSRF tokens yet (single-user LAN app). Add them before any public exposure.
- The app does not phone home. Outbound calls are feed/artwork/model downloads
  and Ollama requests on every detect job. Cloud inference sends transcript
  text; configure a local Ollama model if that is not acceptable.

---

## 11. Testing

`tests/` (40 tests, no ffmpeg/whisper required):

- slug/filename/duration/safe-join helpers
- RSS/OPML parsing (including real-world fixture shape)
- review confidence/warnings/excerpts, including LLM provenance and LLM-only caps
- LLM JSON parsing, segment-ID mapping, guardrails, merging, ratio cap
- feed XML validity, stable GUID/enclosure/duration, hand-dropped files, OPML
- setup/login redirects, admin pages, podcast/review rendering
- media Range/HEAD/ETag/304 and token rejection.

Run with:

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

A real end-to-end run was performed with a local feed + a 92 s episode:
download → transcribe (40 s) → detect → review (confidence 0.5, warning) →
approve → cut → QA pass → publish → RSS item → Range 206.

---

## 12. Remaining work / known limitations

- **Clean-machine first run**: the model download and Homebrew path have been
  verified only with the model already present via `UNINTERUPTED_MODEL`. Do one
  fresh install before calling it shipped.
- **Real subscriptions**: import the four OPML feeds and calibrate labels per
  show (especially Les Grosses Têtes and Conan).
- **Podcast client acceptance**: subscribe in Downcast, refresh, stream, seek,
  background play, and check new-episode pickup.
- **Ad policy**: defaults are conservative; per-show labels still need tuning.
- **LLM calibration**: run on 10–20 labelled episodes before trusting LLM-only
  cuts; cross-episode repeat detection and sponsor memory are not built yet.
- **Non-MP3**: generic re-encode path is implemented but not exercised on a real
  m4a/aac feed yet.
- **Notifications**: dashboard only.
- **Private/paid feeds**: public RSS only.
- **CSRF / multi-user / remote auth**: intentionally out of v1 scope.
- **`labels_json` UI**: raw JSON editor; a friendlier regex tester can come
  later.
- **Artwork metadata**: item-level artwork is not preserved separately; channel
  artwork is used for items.

---

## 13. Next steps

1. Fresh-machine install test (delete app support, run `scripts/run.sh`, let the
   toolchain installer download the model).
2. Import the four Overcast feeds with backfill 3.
3. Calibrate `labels_json` / `ad_lang` per show; process one episode each.
4. Subscribe in Downcast, verify seek/stream/download.
5. Tune confidence/auto-publish after a few approved episodes.
