# clipwatch — the clipd capture client

Watches the OBS output folder, remuxes captures to faststart MP4, uploads to
clipd, and puts the URL on the clipboard.

## Requirements
- Python 3.12
- **ffmpeg on PATH.** The entire clip path depends on it; without it every
  clip fails to remux. Screenshots still work.

## OBS setup
- Settings → Output → Replay Buffer: **enabled**, ~90 s
- Encoder **NVENC H.264**, `yuv420p`, recording format **MKV**
- Settings → Output → Recording Path: the folder set as `watch_dir` in `config.toml`
- Settings → Hotkeys → **Save Replay Buffer**

MKV is deliberate: it survives a crash where an MP4 would be unplayable. The
watcher converts it with `-c copy -map 0`, so nothing is re-encoded, every
audio track is preserved, and the conversion is sub-second.

Screenshots need no extra code — any `.png`, `.jpg` or `.jpeg` landing in
`watch_dir` takes the same path, minus the remux.

## Install
```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy config.example.toml config.toml   # then edit watch_dir
setx CLIPD_TOKEN "<the server's INGEST_TOKEN>"
```

## Run
```powershell
.venv\Scripts\python -m clipwatch.main --config config.toml --games games.toml
```

`--adapter null` forces the no-op platform adapter, which is how the suite runs
on macOS.

## Build the exe
```powershell
.venv\Scripts\pyinstaller clipwatch.spec
```

## Run at login
Task Scheduler → Create Task → Trigger "At log on" → Action: `clipwatch.exe`,
Start in: the install folder.

## How it behaves

**Every clip container is remuxed**, not only MKV — an OBS set to record MP4 or
MOV would otherwise upload a file whose index sits at the end, so playback would
wait on a full download. The pass is `-c copy`, so a re-remux costs a file copy.

**A capture is only deleted locally after clipd confirms a 200.** If clipd is
unreachable the job waits in `.clipwatch/queue` with exponential backoff
(5s, 10s, 20s… capped at 5 min) and survives a reboot. On recovery it uploads
under the same `capture_uuid`, so the server stores it once.

**Nothing is dropped silently.** A sweep every 5 minutes picks up captures that
never reached the queue — written while the daemon was down, or left behind by a
failed remux. A stray MP4 in `.clipwatch/work` with no job is adopted as
`Unknown`, since its game can no longer be determined.

**Transient failures are retried, not discarded.** 5xx, connection errors, and
401/403/408/425/429 and redirects all keep the job queued — a token that has not
been set yet, or has just been rotated, must not cost a capture. Only a genuinely
malformed request (400/413/422) is treated as permanent; that capture is moved to
`.clipwatch/rejected/` and a notification says so, rather than being deleted.

**The game** is the most frequent non-ignored foreground executable over the last
90 s, resolved through `games.toml`. Failing that it falls back to the window
title, then to `Unknown`. Add your own entries to `games.toml`; `Unknown` doubles
as the re-tagging queue in the gallery.

## State directory
Everything lives under `<watch_dir>/.clipwatch/`:

| Path | Contents |
|---|---|
| `queue/` | one JSON file per pending upload |
| `work/` | remuxed MP4s awaiting upload |
| `rejected/` | captures clipd refused permanently, preserved for inspection |
| `clipwatch.log` | rotating log — the only record in a windowed build |
