# clipwatch — the clipd capture client

Watches the OBS output folder, remuxes MKV to faststart MP4, uploads to clipd,
and puts the URL on the clipboard.

## OBS setup
- Settings → Output → Replay Buffer: **enabled**, ~90 s
- Encoder **NVENC H.264**, `yuv420p`, recording format **MKV**
- Settings → Output → Recording Path: the folder set as `watch_dir` in `config.toml`
- Settings → Hotkeys → **Save Replay Buffer**

MKV is deliberate: it survives a crash where an MP4 would be unplayable. The
watcher converts it with `-c copy`, so nothing is re-encoded and the conversion
is sub-second.

Screenshots need no extra code — any `.png` landing in `watch_dir` takes the
same path, minus the remux.

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
- A capture is only deleted locally after clipd confirms a 200.
- If clipd is unreachable the job waits in `.clipwatch/queue` with exponential
  backoff, and survives a reboot. On recovery it uploads under the same
  `capture_uuid`, so the server stores it once.
- A 4xx is treated as permanent: the job is dropped but the file is kept, so
  nothing is silently lost.
- The game is the most frequent non-ignored foreground executable over the last
  90 s, resolved through `games.toml`. Add your own entries there.
