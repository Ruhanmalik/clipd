# clipd — design (Steps 1 & 3)

**Date:** 2026-09-07
**Status:** approved, ready for implementation planning
**Relationship to `plan.md`:** `plan.md` is the north-star handoff spec and remains
authoritative for machine inventory (§2), network (§3), homelab conventions (§4),
the no-server-transcode decision (§5), the client watcher (§8), and hardware
gotchas (§10). This document resolves the open questions in `plan.md` §11 and
supersedes it wherever the two differ — specifically the storage layout in §6, the
game-detection step in §8.4, and the retention policy in §6.

---

## 1. Resolved decisions

| # | Question (`plan.md` §11) | Decision |
|---|---|---|
| 1 | Go or Python? | **Python / FastAPI** |
| 2 | Gaming PC OS? | **Windows** — confirmed on the tailnet as `gaming-pc` / `<gaming-pc-ip>` |
| 3 | Retention budget? | **Size cap only, 50 GB, no age limit** |
| 4 | Screenshots and clips together? | **Together, game-first landing page** |

Decided in the same session, beyond §11:

- Captures are organized **by game on disk**, not only in the index.
- Games are identified by **executable name sampled over the capture window**, not
  by the foreground window title at hotkey time.
- The management UI is a **PWA served by clipd**, with a desktop shell possible later.

### Why Python over Go

`plan.md` §11 framed this as "Go = single binary, Python = faster to write." Two
corrections shift the balance:

1. The "share metadata structs between watcher and server" benefit belongs to
   *whichever* language is used on both ends, not to Go. Python covers the Windows
   watcher via `watchdog` + `pywin32` + `psutil` + `PyInstaller`.
2. Image size is near-irrelevant here. The server needs `ffmpeg` in the image
   regardless (thumbnails on ingest, `-c copy` on trim), so the Go image is not
   `FROM scratch` either. The real delta is ~100 MB on a 468 G disk and ~60 MB RSS
   on a box with 12 GiB free.

The deciding factor is maintainability by a single operator: every other project in
this workspace is Python or TypeScript, and there is no Go anywhere.

---

## 2. Stack

- **FastAPI** + **uvicorn**
- **stdlib `sqlite3`** — one writer, so no Postgres (`plan.md` §6)
- **Jinja2** server-rendered pages + vanilla JS for selection state. No SPA framework.
- **ffmpeg / ffprobe** shelled out
- **httpx** for ntfy
- Base image `python:3.12-slim` plus `ffmpeg`

Deployment follows the conventions in `plan.md` §4: `/home/<user>/clipd/` with its
own `docker-compose.yml`, bind-mounted `./data:/data`, `restart: unless-stopped`,
`TZ: "America/Chicago"`, secrets in `.env`, comments on load-bearing decisions.
Port **8000**. No Docker socket mount.

---

## 3. Schema

`plan.md` §6 plus four columns. Additions marked.

```sql
CREATE TABLE clips (
  id           TEXT PRIMARY KEY,   -- base62, ~7 chars, URL-safe
  public_slug  TEXT UNIQUE,        -- NULL until shared (plan.md §7)
  capture_uuid TEXT UNIQUE,        -- NEW: client-generated idempotency key
  kind         TEXT NOT NULL,      -- 'clip' | 'screenshot'
  title        TEXT,               -- NEW: custom title; NULL = derive from game + time
  filename     TEXT NOT NULL,
  rel_path     TEXT NOT NULL,      -- NEW: path under data/, so re-tagging can move files
  bytes        INTEGER NOT NULL,
  duration_s   REAL,               -- NULL for screenshots
  width        INTEGER,
  height       INTEGER,
  game         TEXT,               -- pretty display name, e.g. "Counter-Strike 2"
  game_slug    TEXT NOT NULL,      -- NEW: path-safe, e.g. "counter-strike-2"
  game_exe     TEXT,               -- NEW: e.g. "cs2.exe" — stable identity for re-mapping
  pinned       INTEGER NOT NULL DEFAULT 0,  -- NEW: exempt from retention sweep
  source_host  TEXT NOT NULL,
  created_at   INTEGER NOT NULL,   -- unix epoch
  thumb_path   TEXT
);
CREATE INDEX idx_clips_created ON clips(created_at DESC);
CREATE INDEX idx_clips_game    ON clips(game_slug);
CREATE INDEX idx_clips_pinned  ON clips(pinned) WHERE pinned = 1;
```

`capture_uuid` exists because `plan.md` §8.7 requires the watcher to queue and retry,
and to delete the local file only after a confirmed 200. If a POST succeeds but the
response is lost, the watcher retries and would otherwise create a duplicate. The
server treats `capture_uuid` as an idempotency key: on conflict it returns the
existing row with 200 rather than ingesting again.

`rel_path` is stored rather than derived because re-tagging a game moves the file.
URLs are always `/v/<id>`, so a move never breaks a link — including a shared one.

---

## 4. Storage layout

Replaces the flat date-sharding in `plan.md` §6:

```
/home/<user>/clipd/
├── docker-compose.yml
├── .env                  # INGEST_TOKEN, NTFY_TOPIC, MAX_STORE_BYTES, TZ
├── games.toml            # exe -> pretty name mapping, user-editable
└── data/
    ├── clipd.db
    ├── clips/<game-slug>/YYYY/MM/<id>.mp4
    ├── shots/<game-slug>/YYYY/MM/<id>.png
    └── thumbs/<id>.jpg
```

Game first so the store is browsable on disk. Date beneath it so no single directory
ever holds thousands of entries — the concern the original sharding addressed.
Retention prunes off the SQLite index, so it is unaffected by path shape.

### Slugging rules

`game_slug` must be safe on both Linux and Windows (the watcher may stage paths locally):

- lowercase; spaces and separators to `-`; collapse repeats; strip leading/trailing `-`
- strip the Windows-illegal set `< > : " / \ | ? *` and control characters
- strip trailing dots and spaces (invalid as Windows path components)
- if the result collides with a Windows reserved name (`CON`, `PRN`, `AUX`, `NUL`,
  `COM1`–`COM9`, `LPT1`–`LPT9`), suffix `-game`
- empty result falls back to `unknown`

---

## 5. Game detection (client-side, replaces `plan.md` §8.4)

The window title at hotkey time is unreliable: titles are unstable, contain
path-illegal characters, and reflect whatever was focused at the moment the key was
pressed — alt-tabbing to Discord first would file the clip under "Discord."

Instead the watcher:

1. Polls the foreground window's **process executable** every 5 s
   (`GetForegroundWindow` → `GetWindowThreadProcessId` → `psutil.Process().name()`),
   keeping a ring buffer of the last ~90 s to match the replay-buffer window.
2. At capture, picks the **most frequently seen non-ignored exe** in that buffer.
3. Resolves it through `games.toml` (`"cs2.exe" = "Counter-Strike 2"`), falling back
   to the sanitized window title, then to `Unknown`.
4. Sends `game`, `game_exe`, and lets the server compute `game_slug`.

Ignore list (configurable): `explorer.exe`, `Discord.exe`, `chrome.exe`, `msedge.exe`,
`firefox.exe`, `SearchHost.exe`, `ShellExperienceHost.exe`.

Misdetection is expected and recoverable: `Unknown` is a normal tile in the gallery
and doubles as the re-tagging queue.

---

## 6. API surface

All mutations live under `/api/*` returning JSON, so a later desktop shell reuses
them rather than growing its own surface. The Jinja2 pages call the same endpoints.

| Method | Path | Auth | Step | Notes |
|---|---|---|---|---|
| POST | `/ingest` | Bearer | 1 | multipart: file + JSON metadata. Returns `{id, url}`. Idempotent on `capture_uuid`. |
| GET | `/healthz` | none | 1 | `{status, clips, bytes}` for Uptime Kuma |
| GET | `/` | tailnet | 3 | Game-first landing: tiles + Recent strip |
| GET | `/g/<game-slug>` | tailnet | 3 | One game's captures, filterable by kind |
| GET | `/v/<id>` | tailnet | 3 | Detail page + player |
| GET | `/d/<id>` | tailnet | 3 | Download original (`Content-Disposition: attachment`) |
| PATCH | `/api/clips/<id>` | tailnet | 3 | `{title?, game?, pinned?}`; a game change moves the file |
| DELETE | `/api/clips/<id>` | tailnet | 3 | File + thumb + row |
| POST | `/api/clips/bulk-delete` | tailnet | 3 | `{ids: [...]}` |
| POST | `/api/clips/<id>/trim` | tailnet | 3 | `{start, end}` → new clip, keyframe-aligned `-c copy` |
| POST | `/api/clips/<id>/share` | tailnet | 3 | Mints `public_slug`. Built, behind a config flag that is **off**. |
| GET | `/c/<slug>` | public | 3 | Share page + OpenGraph. **404s for everything until `plan.md` §7.** |

### `/ingest` contract

```
Authorization: Bearer <INGEST_TOKEN>      # compared with hmac.compare_digest
Content-Type: multipart/form-data
  file = <binary>
  meta = {"kind":"clip","capture_uuid":"<uuid4>","game":"Counter-Strike 2",
          "game_exe":"cs2.exe","source_host":"gaming-pc","captured_at":1757260800}
```

Server: streams the upload to disk in chunks (never fully into memory), runs
`ffprobe` for `duration_s`/`width`/`height`, extracts one thumbnail at 10% in
(`ffmpeg -ss <t> -frames:v 1`), inserts the row, pushes to ntfy with the clip URL as
the click action, returns `{id, url}`.

---

## 7. Retention

Size cap only — **no age limit**. `MAX_STORE_BYTES` in `.env`, default **50 GB**.

Sweep runs on a timer inside clipd (every 15 min) and logs every deletion:

1. If total store size ≤ budget, do nothing.
2. Otherwise delete oldest-first, **skipping anything with a `public_slug` or
   `pinned = 1`** (`plan.md` §6).
3. ntfy warning when the store crosses 80% of budget.

### Unprunable-store case

With no age limit, rules 2 and 3 can collide: enough pinned or shared clips can hold
the store above budget with nothing eligible to delete. In that case clipd fires a
**distinct** ntfy alert naming how many bytes are unprunable — and **keeps accepting
uploads**. Filling the disk is bad; silently dropping a capture is worse, and
`plan.md` §8.7's retry design exists precisely to guarantee no capture is lost.
This is a shout, not a refusal.

---

## 8. Web app (Step 3)

Server-rendered Jinja2 + vanilla JS, installable as a PWA (`manifest.json`, icons,
`theme-color`, a minimal service worker for installability and shell caching — no
offline video caching, which is pointless on a tailnet-only store).

- **`/` — game-first landing.** Grid of game tiles, each with a cover thumbnail and
  capture count (one `GROUP BY game_slug`). Above it, a **Recent strip** of the last
  ~8 captures across all games, so "did that upload land?" costs no clicks.
- **`/g/<game-slug>`** — that game's captures, reverse-chronological, filter chips for
  clip/screenshot.
- **`/v/<id>`** — player, metadata, and the per-clip controls.

Controls: delete, re-tag game, trim, **bulk select + bulk delete**, **pin**,
**custom title**, **download original**. Custom title also becomes the OpenGraph
title when `plan.md` §7 sharing is switched on.

---

## 8a. Step 3 scope decisions (2026-09-09)

Resolved when Step 3 implementation planning began. These extend §8; where they
differ from it, these win.

| # | Decision | Rationale |
|---|---|---|
| 1 | **Step 3 splits into 3a and 3b**, each its own branch and PR | §6 lists eleven routes, roughly double either shipped step. 3a is read-only and produces something visible on its own. |
| 2 | **Dark, media-forward presentation** | The thumbnail is the content; chrome stays out of its way. Sits beside Jellyfin on the same tailnet without looking foreign. |
| 3 | **The PWA shell is deferred** (moved to §10) | Installability is additive and needs real icon assets. Nothing about deferring it forces a rewrite. |
| 4 | **Re-tagging offers to re-map every clip sharing the same `game_exe`** | That is what `game_exe` is for (§3). It makes the `Unknown` re-tagging queue (§5) drainable in a few clicks rather than one clip at a time. |

### 3a — read-only (first branch)

`GET /`, `GET /g/<game-slug>`, `GET /v/<id>`, `GET /d/<id>`, the Jinja2 template
layer, and the read-side query functions in `db.py`.

Two routes §6 does not list are required, and land here:

- **`GET /m/<id>`** — the player's media source, served **inline with HTTP Range
  support**. `/d/<id>` sets `Content-Disposition: attachment`, so it cannot double
  as a `<video>` source: without Range the player cannot seek.
- **`GET /t/<id>`** — the thumbnail.

Serving `data/` through StaticFiles is not an option: URLs are keyed by `id` while
paths are game-sharded, and a re-tag moves the file (§3).

`/g/<game-slug>` paginates by **keyset on `created_at`**, not offset. The store is
capped at 50 GB (§7), but that is still potentially thousands of rows, and an
offset scan degrades as the library grows.

### 3b — mutations (second branch)

`PATCH /api/clips/<id>` (including the `game_exe` bulk re-map), `DELETE
/api/clips/<id>`, `POST /api/clips/bulk-delete`, `POST /api/clips/<id>/trim`,
`POST /api/clips/<id>/share`, `GET /c/<slug>`, and the UI controls that drive them.

### Authentication

Every Step 3 route carries `tailnet` in §6's auth column, and that means exactly
what it says: **no application-level auth**. Tailscale is the entire boundary.
That includes the destructive routes in 3b — anything on the tailnet can delete
any clip. Accepted deliberately for a single-operator homelab; revisit before the
Cloudflare Tunnel in `plan.md` §7 exposes anything beyond `/c/*`.

---

## 9. Step 1 — definition of done

Ships: `POST /ingest` (bearer auth, streaming write, ffprobe, thumbnail, SQLite
insert, ntfy push, idempotency), `GET /healthz`, the retention sweep, the full schema
above, and the storage layout. Deployed to `/home/<user>/clipd/` via compose.

Verified end-to-end from the MacBook before any client exists (`plan.md` §9):

```
curl -H "Authorization: Bearer $TOKEN" -F file=@test.mp4 \
     -F 'meta={"kind":"clip","capture_uuid":"...","game":"test","source_host":"macbook"}' \
     http://clipd-server.tailnet-name.ts.net:8000/ingest
```

Done means: the row exists, the file is at
`data/clips/test/2026/09/<id>.mp4`, the thumbnail exists, ntfy fired, the same
request repeated with the same `capture_uuid` returns the same `id` without a second
file, and `/healthz` reports accurate counts.

The gallery, `/v/<id>`, trim, and the dormant share routes are Step 3 — but the
schema and storage layout land in Step 1, so Step 3 needs no migration.

---

## 10. Deferred, deliberately

- `parent_id` linking a trimmed clip to its source. Not needed until trims are common.
- Public sharing (`plan.md` §7) — routes built, flag off, no Cloudflare Tunnel yet.
- The PWA shell itself — `manifest.json`, icons, service worker (§8a #3).
- Desktop shell (Tauri) wrapping the PWA.
- Homepage tile and Uptime Kuma monitor (`plan.md` §9 Step 4) — note that adding the
  tile requires updating `HOMEPAGE_ALLOWED_HOSTS` (`plan.md` §10).

---

## 11. Prerequisite

SSH key auth from the MacBook to `<user>@<server-ip>` is not currently working
(`Permission denied (publickey)`). Required before anything deploys to clipd-server.
