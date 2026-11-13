# Un-Interupted

A private, self-hosted podcast proxy for macOS. It subscribes to official RSS
feeds, downloads episodes, removes ads with a local Whisper-based pipeline, and
publishes one clean, private RSS feed per show for a podcast client that
fetches feeds on-device (Downcast confirmed, Apple Podcasts supported).

No Docker and no phone-side setup. The audio never leaves your Mac except when
your podcast app streams it over the LAN. Every detection job runs an AI pass
through Ollama; with a cloud model, transcript text is sent to Ollama. Use a
local Ollama model if that is not acceptable.

**Status:** working MVP, end-to-end pipeline verified on an M1 Mac mini. See
[PLAN.md](PLAN.md) for the design, decisions, and remaining work.

---

## What you get

- Add a feed by URL, or import an OPML subscription list.
- Automatic polling and downloading of new episodes.
- Ad removal using the vendored `ad-remover` engine:
  whisper.cpp transcription → repeat/sponsor detection → proposed cuts →
  review → ffmpeg cut → seam QA → audio verification.
- A review screen with audio players, transcript excerpts, editable cut ranges,
  and warnings.
- One private feed per podcast, with local artwork and HTTP Range streaming.
- Per-show settings: language, ad labels, retention, poll interval,
  auto-process, auto-publish.
- **AI ad detection on every episode** through Ollama (local or cloud), so
  rotating or newly sold sponsor reads do not depend on a fixed regex list.
- Browse older episodes and batch-select them for download/processing.
- Copy feed URL / QR code straight into Downcast or Apple Podcasts.

---

## Requirements

- macOS (Apple Silicon recommended; Metal makes Whisper fast).
- [Homebrew](https://brew.sh).
- ~1 GB free for the Whisper model, plus space for episodes.
- Network access on first run (Homebrew formulas + model download).
- Python 3.12 (the bootstrap script installs it via Homebrew if missing).

The app installs `ffmpeg` and `whisper.cpp` itself on first run. The Whisper
model (`ggml-large-v3-turbo-q5_0.bin`, ~574 MB) is downloaded into the app's
support folder; it is not bundled.

If `brew install` complains about the Xcode license on a fresh Mac, run this
once and retry:

```bash
sudo xcodebuild -license accept
```

---

## Quick start

1. Open Terminal in this folder.
2. Bootstrap the Python environment (first time only):

   ```bash
   ./scripts/bootstrap.sh
   ```

3. Start the app:

   ```bash
   ./scripts/start.sh
   ```

   Or double-click **`Un-Interupted.command`** in Finder. (If Gatekeeper blocks
   it, right-click → Open.) The launcher starts the server in the background,
   waits for it to answer, and opens <http://localhost:8090>. You can close the
   Terminal window afterwards; the server keeps running.

   The first run may spend a few minutes at **“Installing Python dependencies”**.
   Leave that window open until it says **Running**.

   To stop it later, double-click **`Stop Un-Interupted.command`** or run
   `./scripts/stop.sh`.

4. Open <http://localhost:8090> if the browser did not open automatically.

5. First-run setup:
   - choose a storage folder (default `~/Podcasts/Un-Interupted`),
   - confirm the LAN base URL (the address your phone will use),
   - set the admin password.
   The app then installs the toolchain and downloads the model. This can take a
   few minutes; the page updates live.

6. Add a podcast or import your OPML, choose a backfill (default: latest 3), and
   wait for the first episode to reach **Awaiting review**.

7. Open the episode review, check the proposed cuts, and approve.

8. Copy the private feed URL (or scan the QR code) into **Downcast** (confirmed
   working) or **Apple Podcasts**. Overcast and other server-side crawlers cannot
   reach a LAN-only feed.

That's it. New episodes will poll automatically; by default they stop at review
until you approve them.

---

## The daily workflow

```
new episode
   ↓ poll + download
   ↓ transcribe (Whisper, Metal)
   ↓ detect ad cuts
AWAITING REVIEW  ← you listen / check excerpts / edit ranges
   ↓ approve
cut → QA → verify → published
   ↓
private feed updates; Downcast sees the clean episode
```

- **Review first** is the default. Nothing is published until you approve it.
- **No ads detected:** the review button becomes **Approve & publish as-is**.
  The original audio is copied unchanged into the clean feed; there is nothing
  to cut or QA.
- **Auto-publish** is opt-in per podcast. When enabled, every episode is
  approved and queued for publishing without waiting in review — even when
  confidence is low, review warnings are present, or the show has fewer than 3
  published episodes. QA still runs on cuts. A no-ad episode is published as-is.
  This is deliberately availability-first: a bad cut can be fixed later, while
  a stuck episode blocks the feed.
- **Skip** keeps the episode out of the clean feed (the original stays until
  retention deletes it).
- **Re-detect** re-runs detection on the existing transcript (fast).
- **Re-cut** uses the approved ranges without re-transcribing.

### Older episodes

Open a podcast → **All episodes**. Tick the checkboxes for older episodes and
click **Download and process selected**, or use **Process** on a single row. The
app resumes each episode from whatever stage is already on disk (download,
transcribe, detect, cut, QA, publish). The header checkbox selects every
eligible episode on the current page.

### Full retry and delete

Every episode in the table is selectable (rows with a queued or running job are
not). Two more batch actions sit next to **Download and process selected**:

- **Full retry (download → process → publish)** deletes the episode's original
  download, transcript, cuts, review and any published clean audio, then runs the
  whole pipeline again from a fresh download. Use it after a crash, a reboot, a
  corrupted download, or any failure you do not want to resume from. The
  **Reprocess** button on a published row does the same for one episode.
- **Delete selected** removes the episodes from disk: originals, `.part` files,
  pre-converted audio, transcripts, cuts, reviews, cleaned files, job logs and
  any published clean audio, plus their stale entries in `work/index.json`. A
  deleted published episode disappears from the private feed. The row is kept as
  a tombstone so the next poll does not re-download it; it is hidden from every
  list but stays visible under the **Deleted** tab, where **Restore** brings it
  back (then use **Download and process** to rebuild it).

### Folder podcasts

**Add a podcast → From a folder** turns an existing folder of audio files into a
podcast of its own, with the same private feed, ad detection and review flow.

- **Browse instead of typing.** **Browse…** opens an in-app folder browser (the
  app runs on the machine that holds the files, so it lists real folders with
  breadcrumbs, shortcuts for Home/Desktop/Downloads/iCloud/volumes and an audio
  file count per folder). The same button appears next to the source path in the
  podcast settings, which is the usual way to re-point a folder after an external
  drive remounts under a different name.
- **Nothing is copied, moved or deleted.** The app reads your files where they
  are, so the folder stays the single copy you manage. The **download** stage for
  these episodes only checks that the file is still there.
- Each audio file becomes one episode. The title comes from the file name (a
  leading `YYYY-MM-DD` is stripped into the publication date; otherwise the file's
  modification date is used). Sub-folders are scanned too, six levels deep.
- The folder is rescanned on a per-podcast interval, next to the source path in
  the podcast settings. **Scan folder now** runs it immediately. A file only
  enters once: identity is the path relative to the folder, so moving or renaming
  a file adds a new episode and leaves the old one alone.
- Deleting an episode removes only its derived files (transcript, cuts, review,
  clean audio) — never the source. Restoring it re-scans from the original path.
- Changing the source path re-points existing episodes whose relative path still
  exists under the new folder, which is the usual case after moving the folder or
  remounting a drive at a different mount point.
- `cover.jpg`, `folder.jpg` or `artwork.png` in the folder is used as the podcast
  artwork.

Files are read in place, so keep the folder mounted while episodes are processed
or replayed. A missing file fails its episode with a clear message instead of
silently downloading or deleting anything.

---

## AI ad detection (Ollama)

The heuristic detector is the baseline. Every detection job also runs a
semantic pass that reads the timestamped transcript and proposes ad ranges by
their *shape* (host-read, produced spot, promo code, free trial, CTA), not only
by a fixed sponsor list. This runs on every episode because sponsors rotate and
new reads appear without a regex update.

1. Install the Ollama app (or `brew install ollama`) and make sure it is
   running. For cloud models, sign in once:

   ```bash
   ollama signin
   ```

2. In **Settings → AI ad detection**, click **Check LLM install**. The card
   reports whether the Ollama binary and daemon are available, then lists the
   models Ollama knows about. Pick the model from the list and save. There is no
   enable toggle: the pass runs after every heuristic detection.

3. If the list is empty, Ollama is running but no model has been made available
   yet. Run one once in Terminal, then click **Check LLM install** again:

   ```bash
   ollama run llama3.2                  # a local model
   ollama run deepseek-v4.1-flash:cloud # a cloud model after ollama signin
   ```

4. Open an episode review. The screen shows an `LLM 91%` badge and the brand
   next to each proposed range. **Re-detect** re-runs both passes on an existing
   transcript.

Defaults:

- If Ollama is not installed, the card says so and points to the Ollama app or
  `brew install ollama`. If the daemon is unreachable, start the app or run
  `ollama serve`.
- `http://127.0.0.1:11434` uses the signed-in Ollama daemon; the model name keeps
  the `:cloud` suffix.
- `https://ollama.com` calls the hosted API directly. Set an
  `UNINTERUPTED_LLM_API_KEY`/`OLLAMA_API_KEY` or paste a key in Settings; the
  `:cloud` suffix is removed automatically.
- LLM candidates below the confidence threshold, shorter than 3 s, outside the
  transcript, overlapping the show re-entry regex, or removing more than 25% of
  the episode are rejected.
- LLM proposals are merged with regex/repeat cuts. An **LLM-only** cut always
  gets a review warning; with auto-publish enabled it is still published, and
  QA still runs.
- If Ollama is unreachable, the job still finishes with the heuristic cuts and
  the review shows the LLM error.

> **Privacy:** cloud inference sends transcript text to Ollama on every
> detection job. Ollama states that cloud prompts/responses are not used for
> training and are not stored as content. Configure a local model instead if
> that trade-off is not acceptable.

---

## Retention

Per podcast you can choose:

`Keep forever` · `0 days` · `7` · `14` · `30` (default) · `60` · `90` · `180` ·
`365`

Retention applies **only to downloaded originals**, and starts when the clean
episode is published. Transcripts and cut plans are kept (they are small and
save an expensive re-transcription). Published clean files are never deleted
automatically.

A daily cleanup job runs in the worker. You can also trigger it by restarting
the app; it runs at most once per 24 h.

---

## Storage

App state:

```
~/Library/Application Support/Un-Interupted/
  config.json
  db.sqlite3
  models/ggml-large-v3-turbo-q5_0.bin
  cache/artwork/
  logs/
```

Library (default):

```
~/Podcasts/Un-Interupted/podcasts/<slug>/
  episodes/     ← clean files; this is what the private feed serves
  originals/    ← downloads; admin-only, retention applies
  work/         ← transcripts, cuts, previews, labels, per-job logs
```

You can change the library root in **Settings → Library**. The app queues a
copy job, verifies the copy, switches the config, and optionally deletes the old
`podcasts/` folder. The destination must be empty (or a previous interrupted
copy with the app's marker).

---

## Private feed URLs

Each podcast gets:

```
http://<lan-ip>:8090/feed/<slug>/<token>.xml
```

The token is secret. Audio URLs inside the feed use the same token. The admin UI
and OPML export are password-protected; the feed and media endpoints are
token-protected.

- `GET /opml.xml` (admin only) exports every private feed URL.
- The QR code on the podcast page encodes the feed URL.
- Set **Base URL** to the address your phone can reach. If your Mac's LAN IP
  changes, update it in Settings or give the Mac a DHCP reservation.

---

## Autostart (optional)

Install a per-user launchd job that starts the app at login:

```bash
./scripts/install-launchd.sh
```

Logs go to `~/Library/Logs/Un-Interupted/` (see the script output). Remove it
with:

```bash
./scripts/uninstall-launchd.sh
```

The app is a normal foreground process otherwise; Ctrl-C stops it.

---

## Configuration

Most settings live in the web UI. Environment variables are for development or
advanced setups:

| Variable | Purpose |
|---|---|
| `UNINTERUPTED_HOME` | Override the app-support folder (config/DB/logs) |
| `UNINTERUPTED_PORT` | Web port (default `8090`) |
| `UNINTERUPTED_HOST` | Bind address (default `0.0.0.0`) |
| `UNINTERUPTED_BASE_URL` | Override the generated feed base URL |
| `UNINTERUPTED_MODEL` / `WHISPER_MODEL` | Use a specific Whisper model file |
| `UNINTERUPTED_NO_WORKER=1` | Run the web UI without the worker (dev) |
| `UNINTERUPTED_LOG_LEVEL` | Uvicorn log level (default `info`) |
| `UNINTERUPTED_LLM_MODEL` | Override the Ollama model (default `deepseek-v4.1-flash:cloud`) |
| `UNINTERUPTED_LLM_BASE_URL` | Override the Ollama endpoint |
| `UNINTERUPTED_LLM_API_KEY` / `OLLAMA_API_KEY` | API key for direct `ollama.com` access |

`config.json` stores the stable session secret, password hash, storage root,
base URL, port, and defaults. It is written atomically with mode `0600`.

---

## Security notes

- Single admin user; password hashed with PBKDF2-SHA256 (600k iterations).
- Feed/media/artwork require per-podcast tokens.
- Originals and work files are never exposed by the public routes.
- Designed for a trusted LAN. The session cookie is not `Secure` because the
  default is plain HTTP.
- If you expose it beyond the LAN, put it behind Tailscale or an HTTPS reverse
  proxy, set the base URL accordingly, and add CSRF protection first.
- No telemetry. Outbound requests are feed/artwork downloads, the first-run
  model/toolchain install, and Ollama calls on every detection job.

---

## Development

```bash
./scripts/bootstrap.sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Run web and worker separately while developing:

```bash
UNINTERUPTED_NO_WORKER=1 .venv/bin/python -m app
.venv/bin/python -m app.worker
```

(Only one worker can hold the lock at a time.)

### Ad engine

The upstream scripts are vendored at `app/engine/upstream/scripts/`, with
version/upstream notes in `app/engine/upstream/VERSION` and `UPSTREAM.md`.
Only `app/engine/adapter.py` knows their file names and cwd contract; an engine
failure is surfaced per episode and never crashes the app. See
[`app/engine/upstream/UPSTREAM.md`](app/engine/upstream/UPSTREAM.md) for the
upgrade procedure.

### Logs

- Worker: `~/Library/Application Support/Un-Interupted/logs/worker.log`
- Toolchain: `.../logs/setup.log`
- Per-job: `<library>/podcasts/<slug>/work/logs/` and the job page in the UI
- Web server: the terminal / launchd log

---

## Troubleshooting

### Podcast client compatibility

This app generates LAN-only `http://192.168.x.x:8090/...` feed and media URLs.
Use a client that fetches the RSS feed on the device:

- **Downcast** — confirmed working with this app.
- **Apple Podcasts** — use **Library → Edit → Add a Show by URL**.
- **Overcast, Pocket Casts, Castro** — their servers fetch the feed, so they
  cannot reach a private LAN address. Overcast needs the RSS feed exposed on a
  public HTTPS URL first.

This is a client architecture limitation, not a Local Network permission issue.

See [docs/troubleshooting.md](docs/troubleshooting.md) for:

- double-click / background server issues
- first-run dependency or model download problems
- Xcode license / Homebrew failures
- model download interruptions
- "worker not running"
- episodes stuck in a status
- Downcast refreshing / audio seeking
- why Overcast cannot add a LAN-only feed
- storage relocation issues
- resetting the app safely

---

## Known limitations

- LAN only; no built-in HTTPS, Tailscale, or remote auth.
- Overcast/Pocket Casts/Castro need a publicly reachable feed because they crawl
  RSS server-side; Downcast and Apple Podcasts fetch it on-device.
- Public RSS feeds only (no Patreon/private feed auth).
- Folder podcasts read files in place, so the folder must stay available while
  an episode is processed; there is no copy to fall back on.
- Ad detection still needs per-show calibration; ambiguous cuts are flagged.
- Non-MP3 audio uses a re-encode fallback (implemented, not yet exercised on a
  real feed).
- No push notifications; the dashboard and review queue are the signal.
- The first clean-machine model download is the one path not yet exercised
  end-to-end in this repo.
