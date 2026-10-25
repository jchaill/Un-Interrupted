# Troubleshooting

## Launcher / “localhost:8090 is not available”

The double-click launcher now starts the server **in the background** and waits
until `/health` answers. If it reports success, you can close the Terminal
window.

If it fails, it prints the last lines of the log. The full log is:

```bash
tail -80 "$HOME/Library/Logs/Un-Interupted.log"
```

Check whether the server is actually listening:

```bash
curl -s http://127.0.0.1:8090/health
lsof -nP -iTCP:8090 -sTCP:LISTEN
```

If nothing is listening, start it from Terminal so errors stay visible:

```bash
./scripts/start.sh
```

Stop it with:

```bash
./scripts/stop.sh
```

or double-click **`Stop Un-Interupted.command`**.

### It looked stuck at “Installing Python dependencies”

That is the one-time bootstrap: Homebrew Python 3.12 + pip packages. It can take
a few minutes on a slow connection. Leave the window open. If it fails, the
script now prints the error; check the network/proxy and retry.

### “Port 8090 is used by PID … which does not look like Un-Interupted”

Something else owns the port. Find it with `lsof -nP -iTCP:8090` and either stop
it or choose another port via Settings / `UNINTERUPTED_PORT`.

### It keeps restarting after `scripts/stop.sh`

You installed the launchd autostart agent (`scripts/install-launchd.sh`), which
uses `KeepAlive`. Remove it first:

```bash
./scripts/uninstall-launchd.sh
```

## First run / toolchain

### `brew install` fails with an Xcode license error

Run once, then retry:

```bash
sudo xcodebuild -license accept
```

### Toolchain install seems stuck

Open **Setup → Toolchain** and check the live log, or:

```bash
tail -f "$HOME/Library/Application Support/Un-Interupted/logs/setup.log"
```

Common causes:

- **No Homebrew** — install from <https://brew.sh>.
- **No network / DNS block** — the model comes from `huggingface.co`; the
  download resumes from `.part` if interrupted.
- **Not enough disk** — the installer refuses to start the model download with
  less than ~1.2 GB free.
- **The app was killed mid-install** — restart the app; the interrupted state is
  detected and the install can be retried from the toolchain page.

### Recheck after installing manually

If you install `ffmpeg` or `whisper.cpp` yourself:

```bash
brew install ffmpeg whisper.cpp
```

then press **Recheck** on the toolchain page. The expected binary is
`whisper-cli`.

### Use an existing model

To avoid the 574 MB download:

```bash
export UNINTERUPTED_MODEL=/path/to/ggml-large-v3-turbo-q5_0.bin
./scripts/run.sh
```

`WHISPER_MODEL` works too. The app only requires a model larger than 100 MB.

---

## Worker

### “Worker not running”

The worker is a thread inside the app. Check the top bar and:

```bash
tail -50 "$HOME/Library/Application Support/Un-Interupted/logs/worker.log"
```

If a second copy of the app is running, only one can hold `worker.lock`. Quit
the extra copy. If the worker died, restart the app.

During a long transcription the heartbeat is refreshed every 10 s, so
“worker offline” should not appear just because Whisper is busy.

### A job is stuck in `running`

Stop the app, then start it again. On startup the worker re-queues jobs that
were left `running` by a previous process. If the job keeps failing, open the
job page and read the log; use **Retry** after fixing the cause.

### Cancel / retry

The queue page only cancels jobs that are still `queued`; a running Whisper job
cannot be interrupted safely from the UI. Let it finish or stop the app.

---

## Episodes

### Status is `queued` but nothing happens

- Is the worker alive? (top bar / `worker.log`)
- Is a different heavy job running? Jobs run one at a time.
- Is the podcast's `auto_process` off? New episodes then stay `discovered`
  until you press **Process**.
- Is disk space below `min_free_gb` (default 5 GB)? Downloads are refused.

### Status is `failed` or `qa_failed`

Open the episode review page and the job log. Common causes:

- **Source URL expired / 403** — re-poll the feed; podcast hosts rotate URLs.
- **QA duration mismatch** — the cut removed more/less than expected. Review the
  ranges and re-cut.
- **Whisper model missing** — recheck the toolchain.
- **Transcript empty** — often a music-only or very short file; skip it.
- **Seam still has ad audio** — QA deliberately fails rather than publishing a
  bad file. Adjust the cut end and re-cut.

After fixing, **Re-detect** (fast, uses the existing transcript) or **Process**
(resumes from the earliest missing stage).

### The feed has no episodes

- The feed only lists files in `podcasts/<slug>/episodes/`.
- An episode appears only after `published`; `ready_publish` is not public.
- Check the episode's clean filename and `published` status.
- A manually dropped file is published too; its filename becomes the title.

### Wrong language / poor transcription

Set **Language** on the podcast page (`fr`, `en`, …). `auto` lets Whisper
decide. The detection/QA language is separate (`ad_lang`); set both when the
show is monolingual.

### Ads are missed or show content is cut

Edit `labels_json` on the podcast page. It is a JSON array of
`[regex, human label]` pairs, for example:

```json
[
  ["(?i)wayfair|every style|avenues at northfield", "Wayfair"],
  ["(?i)sncf|ouigo|tgv", "SNCF"]
]
```

Then **Re-detect** on the episode. Start with one show and a few episodes; do
not enable auto-publish until the ranges are consistently right.

### AI ad detection does not run or finds nothing

- Check **Settings → AI ad detection → Test Ollama connection** succeeds. The
  pass is always on; there is no enable toggle.
- The default endpoint `http://127.0.0.1:11434` uses the signed-in Ollama app.
  Run `ollama signin` and make sure the Ollama menu-bar app is running.
- Cloud models do not need `ollama pull`, but the model name must end in
  `:cloud` when going through the local daemon. The default is
  `deepseek-v4.1-flash:cloud`.
- For a direct `https://ollama.com` endpoint, paste an API key in Settings (or
  export `OLLAMA_API_KEY`) and drop the `:cloud` suffix; the app handles the
  suffix automatically.
- Open the job log. A failed LLM pass is non-fatal: the episode still reaches
  review with heuristic cuts and the error is shown as a warning.
- If Ollama replies but the JSON is invalid, the chunk is rejected. Check for a
  model that follows JSON instructions; `deepseek-v4.1-flash:cloud` is the
  default because it follows the segment-ID schema reliably.

> Transcripts are sent to the cloud model. Keep AI detection off if that is not
> acceptable; the local Whisper + regex/repeat pipeline still works without it.

---

## Podcast client / feed

### Overcast does not show the new episode

- Pull-to-refresh in Overcast; clients cache feeds.
- Open the private feed URL in a browser on the phone; it must return XML (not a
  login page).
- Confirm **Base URL** is the LAN address the phone can reach, not
  `127.0.0.1`.
- If the Mac's IP changed, update Settings → Base URL and refresh.

### Audio will not seek / stalls

The media route supports Range. Test from the Mac:

```bash
curl -I -H 'Range: bytes=0-99' 'http://<lan-ip>:8090/media/...'
```

Expect `206` and `Content-Range`. If you get `200`, the request may be going
through a proxy that strips Range.

### Feed URL leaked / rotate the token

Not exposed in the UI yet. Stop the app, edit the podcast's `feed_token` in
SQLite, and restart:

```bash
sqlite3 "$HOME/Library/Application Support/Un-Interupted/db.sqlite3" \
  "update podcasts set feed_token='new-secret' where slug='my-show';"
```

Then re-subscribe in Overcast with the new URL.

---

## Storage

### “The new storage folder must be empty”

Pick an empty folder, or one left by an interrupted relocation (it contains
`.relocate-in-progress`). The app refuses to mix libraries.

### Relocation failed halfway

The config is only switched after a complete copy. The old library is untouched.
Retry from Settings; the new folder with the marker is reused/cleared.

### Disk is filling up

- Lower the per-podcast retention (originals are the big files).
- Check **Library** sizes on the podcast page.
- The model is ~574 MB; transcripts are small.
- Clean files are never auto-deleted; remove old ones manually only if you are
  sure you no longer need them.

---

## Reset

To start over **without** deleting your library:

```bash
# stop the app first
mv "$HOME/Library/Application Support/Un-Interupted" \
   "$HOME/Library/Application Support/Un-Interupted.bak"
```

The next start is a fresh setup. Your library under `~/Podcasts/Un-Interupted`
remains; re-add the feeds or keep the old DB by restoring only `db.sqlite3` and
`config.json` (the secret must match existing sessions).

To reset only the admin password, there is no CLI yet; restore a backup or start
a fresh app-support folder and re-add the feeds.

---

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The test suite does not need ffmpeg, Whisper, or a model.
