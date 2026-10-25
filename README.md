# Un-Interupted

A private, self-hosted podcast proxy for macOS. It subscribes to official RSS
feeds, downloads episodes, removes ads with a local Whisper-based pipeline, and
publishes one clean, private RSS feed per show for Overcast (or any podcast
client).

No Docker and no phone-side setup. The audio never leaves your Mac except when
your podcast app streams it over the LAN. Every detection job runs an AI pass
through Ollama; with a cloud model, transcript text is sent to Ollama. Use a
local Ollama model if that is not acceptable.

**Status:** working MVP, end-to-end pipeline verified on an M1 Mac mini. See
[PLAN.md](PLAN.md) for the design, decisions, and remaining work.

---

## What you get

- Add a feed by URL, or import your Overcast OPML.
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
- Browse older episodes and flag individual ones for processing.
- Copy feed URL / QR code straight into Overcast.

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

8. Copy the private feed URL (or scan the QR code) into Overcast.

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
private feed updates; Overcast sees the clean episode
```

- **Review first** is the default. Nothing is published until you approve it.
- **Auto-publish** is opt-in per podcast and only fires when the show has at
  least 3 published episodes, the review has no warnings, confidence is ≥ 0.80,
  and QA passes.
- **Skip** keeps the episode out of the clean feed (the original stays until
  retention deletes it).
- **Re-detect** re-runs detection on the existing transcript (fast).
- **Re-cut** uses the approved ranges without re-transcribing.

### Older episodes

Open a podcast → **All episodes** → **Process** on any older episode. The app
resumes from whatever stage is already on disk (download, transcribe, detect,
cut, QA, publish).

---

## AI ad detection (Ollama)

The heuristic detector is the baseline. Every detection job also runs a
semantic pass that reads the timestamped transcript and proposes ad ranges by
their *shape* (host-read, produced spot, promo code, free trial, CTA), not only
by a fixed sponsor list. This runs on every episode because sponsors rotate and
new reads appear without a regex update.

1. Make sure Ollama is signed in to your Ollama account. The app uses the same
   local daemon as Pi by default:

   ```bash
   ollama signin
   ollama run deepseek-v4.1-flash:cloud "Reply with JSON only: {\"ok\":true}"
   ```

2. In **Settings → AI ad detection**, keep `deepseek-v4.1-flash:cloud` and
   click **Test Ollama connection**. There is no enable toggle: the pass runs
   after every heuristic detection.

3. Open an episode review. The screen shows an `LLM 91%` badge and the brand
   next to each proposed range. **Re-detect** re-runs both passes on an existing
   transcript.

Defaults:

- `http://127.0.0.1:11434` uses the signed-in Ollama daemon; the model name keeps
  the `:cloud` suffix.
- `https://ollama.com` calls the hosted API directly. Set an
  `UNINTERUPTED_LLM_API_KEY`/`OLLAMA_API_KEY` or paste a key in Settings; the
  `:cloud` suffix is removed automatically.
- LLM candidates below the confidence threshold, shorter than 3 s, outside the
  transcript, overlapping the show re-entry regex, or removing more than 25% of
  the episode are rejected.
- LLM proposals are merged with regex/repeat cuts. An **LLM-only** cut always
  gets a review warning and cannot auto-publish.
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

See [docs/troubleshooting.md](docs/troubleshooting.md) for:

- double-click / background server issues
- first-run dependency or model download problems
- Xcode license / Homebrew failures
- model download interruptions
- "worker not running"
- episodes stuck in a status
- Overcast not refreshing or not seeking
- storage relocation issues
- resetting the app safely

---

## Known limitations

- LAN only; no built-in HTTPS, Tailscale, or remote auth.
- Public RSS feeds only (no Patreon/private feed auth).
- Ad detection still needs per-show calibration; ambiguous cuts are flagged.
- Non-MP3 audio uses a re-encode fallback (implemented, not yet exercised on a
  real feed).
- No push notifications; the dashboard and review queue are the signal.
- The first clean-machine model download is the one path not yet exercised
  end-to-end in this repo.
