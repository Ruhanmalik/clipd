# clipd — self-hosted clip & screenshot capture (Medal replacement)

> Handoff spec. Written on the homelab box (`midget`) so a later Claude session on
> the MacBook has full context. Everything below was verified on the actual machine
> on 2026-09-05, not assumed.

---

## 1. Goal

Press a hotkey on the gaming PC → the last ~90 seconds of gameplay is saved,
uploaded to the homelab, and a short URL is on the clipboard within a few seconds.
Same for screenshots. A web gallery on the homelab lists everything, with
browser-side trimming.

Private (tailnet-only) for now. Public per-clip share links later, without a rewrite.

**Explicit non-goal:** do not write a capture/encoder engine. OBS's replay buffer
already does the ring-buffer-in-RAM + NVENC flush correctly. The value of this
project is everything *after* the file hits disk.

---

## 2. The machines

### `midget` — the homelab (deploy target for the server)
A repurposed 2017 gaming laptop running Linux (kernel 7.0.0-29-generic).

| | |
|---|---|
| CPU | Intel i7-7700HQ, 8 threads |
| RAM | 14 GiB (~12 GiB available) |
| Disk | 468G total, 190G used, **254G free** on `/dev/nvme0n1p2` mounted at `/` |
| iGPU | Intel HD 630 — QuickSync at `/dev/dri/renderD128` — **already used by Jellyfin, do not contend** |
| dGPU | NVIDIA GTX 1050 Mobile (Pascal) — NVENC at `/dev/dri/renderD129` — **completely idle** |
| Uptime | 21+ days, stable |

### Gaming PC — the capture client
- **Ryzen 9 7900X** (12c/24t) + **RTX 4070** (Ada, 8th-gen NVENC, AV1-capable)
- Assumed **Windows**. If it's actually Linux, swap OBS for `gpu-screen-recorder`
  (lower capture overhead); nothing else in this spec changes.
- **Not yet on the tailnet.** This is step 0.

### MacBook Pro — the dev machine
- On the tailnet as `macbook-pro-3` / `100.116.189.109`, macOS.
- Code here, deploy to `midget` over SSH (port 22 is open on the tailnet).

---

## 3. Network

Tailscale tailnet, MagicDNS suffix `tail0056d7.ts.net`:

| Host | Tailscale IP | MagicDNS |
|---|---|---|
| midget (server) | `100.72.75.62` | `midget.tail0056d7.ts.net` |
| macbook-pro-3 | `100.116.189.109` | `macbook-pro-3.tail0056d7.ts.net` |
| iphone-14-pro | `100.68.42.28` | `iphone-14-pro.tail0056d7.ts.net` |

`tailscale serve` is **not** configured (`No serve config`). Worth setting up
separately — it gives real HTTPS certs on `*.ts.net` with no port numbers.

### Ports already bound on midget — pick around these

```
22    sshd                 3000  homepage           7878  radarr
53    adguardhome          3001  uptime-kuma        8080  qbittorrent (via gluetun)
80    adguardhome          5055  jellyseerr         8096  jellyfin
6246  (misc)               6767  bazarr             8989  sonarr
9696  prowlarr             7359/udp jellyfin discovery
```

**clipd will use port `8000`** — confirmed free.

---

## 4. Existing homelab conventions — follow these

- **One directory per service** under `/home/batman/`, each with its own
  `docker-compose.yml`. Existing: `adguard/`, `homepage/`, `jellyfin/`,
  `minecraft/`, `uptime-kuma/`. So: **`/home/batman/clipd/`**.
- Bind-mount local dirs (`./data:/data`), not named volumes.
- `restart: unless-stopped` on everything.
- `TZ: "America/Chicago"` set explicitly.
- Secrets in `.env`, never in the compose file or committed config.
- Compose files carry **comments explaining non-obvious decisions** — especially
  anything that looks wrong but is load-bearing. Match this style.
- **Notifications go to ntfy** (hosted ntfy.sh), **one topic per service**.
  Use a dedicated topic for clips. Not Discord, not Telegram.
- Docker socket, where mounted, is `:ro` — and the existing comments note that
  read-only socket access is *still effectively root on the host*. Don't mount it
  for clipd; it doesn't need it.

---

## 5. Architecture

```
[ Gaming PC — 7900X / RTX 4070 ]              [ midget — over Tailscale ]

  OBS replay buffer  ──┐
  (hotkey: last ~90s)  │
                       ├─→ watcher daemon ──POST /ingest──→   clipd :8000
  screenshot hotkey  ──┘   - remux to faststart MP4            ├─ store file
                           - POST multipart + metadata         ├─ 1 thumbnail frame
                           - short URL → clipboard             ├─ SQLite row
                                                               ├─ ntfy push
                                                               └─ return short URL
                                                                     │
                                              GET  /          ───────┤ gallery
                                              GET  /v/<id>    ───────┤ private view (tailnet)
                                              GET  /c/<slug>  ───────┤ public share (404s for now)
                                              POST /trim/<id> ───────┘ keyframe cut, -c copy
```

### The core design decision: the server never transcodes

The RTX 4070 is generationally ahead of the server's GTX 1050. Shipping video to a
2017 laptop to be re-encoded by the weaker chip would be slower, uglier, and would
fight Jellyfin for the box. So:

1. **Client encodes** via NVENC to **H.264 `yuv420p`**.
   Not AV1 — AV1 is smaller, but locks out Safari on the iPhone and MacBook, which
   are two of the three devices on this tailnet. H.264 plays everywhere.
2. **Client remuxes** MKV → faststart MP4 (`-c copy -movflags +faststart`).
   OBS records MKV because it survives a crash; MKV doesn't play in browsers.
   The remux is a container swap, no re-encode — sub-second on a 7900X.
   `+faststart` moves the moov atom to the front so it streams without a full download.
3. **Server does no video work** beyond extracting a single thumbnail frame.
   Ingest is disk-speed, and the GTX 1050 stays free.

---

## 6. Server: `clipd`

Single service, `/home/batman/clipd/`. Language is open — Go (single static binary,
easy container) or Python/FastAPI both fine. SQLite for the index; there is exactly
one writer, so no Postgres.

### Schema

```sql
CREATE TABLE clips (
  id           TEXT PRIMARY KEY,   -- base62, ~7 chars, URL-safe
  public_slug  TEXT UNIQUE,        -- NULL until explicitly shared. See §7.
  kind         TEXT NOT NULL,      -- 'clip' | 'screenshot'
  filename     TEXT NOT NULL,
  bytes        INTEGER NOT NULL,
  duration_s   REAL,               -- NULL for screenshots
  width        INTEGER,
  height       INTEGER,
  game         TEXT,               -- foreground window title at capture time
  source_host  TEXT NOT NULL,      -- which machine captured it
  created_at   INTEGER NOT NULL,   -- unix epoch
  thumb_path   TEXT
);
CREATE INDEX idx_clips_created ON clips(created_at DESC);
CREATE INDEX idx_clips_game    ON clips(game);
```

### Endpoints

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/ingest` | Bearer token | multipart: file + JSON metadata. Returns `{id, url}`. |
| GET | `/` | tailnet | Gallery. Filter by game/date/kind. |
| GET | `/v/<id>` | tailnet | Private view page + player. |
| GET | `/c/<slug>` | public | Share page w/ OpenGraph tags. **404s for everything until §7.** |
| POST | `/trim/<id>` | tailnet | `{start, end}` → new clip, keyframe-aligned `-c copy`. |
| POST | `/share/<id>` | tailnet | Mints a `public_slug`. Build the route, leave it disabled. |
| DELETE | `/v/<id>` | tailnet | Delete file + row + thumb. |

### Storage layout

```
/home/batman/clipd/
├── docker-compose.yml
├── .env                  # INGEST_TOKEN, NTFY_TOPIC, TZ
└── data/
    ├── clipd.db
    ├── clips/YYYY/MM/<id>.mp4
    ├── shots/YYYY/MM/<id>.png
    └── thumbs/<id>.jpg
```

Date-sharded so no directory ever holds tens of thousands of entries.

### On upload

1. Write file to its dated path.
2. `ffmpeg -ss <10% in> -frames:v 1` → `thumbs/<id>.jpg`. Cheap, CPU-only.
3. Insert SQLite row.
4. ntfy push: title = game name, body = duration + size, **click action = the URL**.
5. Return `{id, url}` to the client.

### Retention — do not skip this

Radarr and Sonarr are actively consuming the same 254G. Clips must not be the
thing that fills the disk at 3am.

- Config: max total clip-store size **and** max age.
- Prune oldest-first when over budget; **never touch** anything with a
  `public_slug` (someone has that link) or a `pinned` flag.
- Run the sweep on a timer inside clipd. Log what it deletes.
- ntfy warning when the store crosses ~80% of budget.

---

## 7. Private now, shareable later

The requirement: **tailnet-only today, per-clip public links eventually, no migration.**

Every clip has two identities from day one:
- `id` — internal, used by the tailnet gallery.
- `public_slug` — nullable, unguessable (≥16 chars of entropy), **always NULL initially**.

Build the `GET /c/<slug>` route now. It 404s on everything, because nothing has a
slug. Also build `POST /share/<id>` but leave it behind a config flag that's off.

When public sharing is wanted:
1. Cloudflare Tunnel pointed at `:8000`, exposing **only** `/c/*` and the media
   paths those pages reference. The gallery, `/ingest`, and `/v/*` stay tailnet-only.
2. Flip the config flag → a Share button appears in the UI.
3. Rate-limit `/c/*`.

Sharing is opt-in per clip. There is never a switch that exposes the whole library.

The share page needs OpenGraph tags so links unfurl into an inline player in
Discord — `og:type=video.other`, `og:video`, `og:video:type`, `og:image` (thumb),
plus `twitter:card=player`. This is the single highest-value feature; it's also
~30 lines of meta tags.

---

## 8. Client (gaming PC)

### OBS setup
- Replay buffer enabled, ~90s, NVENC H.264, `yuv420p`, recording format **MKV**.
- Output dir is the watched folder.
- Hotkey → Save Replay Buffer.

### Watcher daemon
Small Go binary (portable — same code runs on macOS/Linux if capture moves).

1. Watch the OBS output dir (fsnotify).
2. Wait for the file to stop growing (OBS is still flushing on the first event).
3. `ffmpeg -i in.mkv -c copy -movflags +faststart out.mp4`
4. Grab foreground window title → `game` metadata.
5. POST to `http://midget.tail0056d7.ts.net:8000/ingest` with the bearer token.
6. Put the returned URL on the clipboard; toast on success.
7. **Queue and retry on failure** — laptop asleep, tailnet down, etc. Never lose a
   clip because the server was unreachable. Delete the local file only after a
   confirmed 200.

Screenshots: same pipeline, PNG, no remux step.

---

## 9. Build order

**Step 0 — Tailscale on the gaming PC.** Prerequisite for everything. Also fixes
remote Jellyfin access from that machine.

**Step 1 — `clipd` on midget.** Ingest + auth + SQLite + thumbnail + ntfy.
Fully testable with `curl` from the MacBook before any client exists:
```
curl -H "Authorization: Bearer $TOKEN" -F file=@test.mp4 \
     -F 'meta={"kind":"clip","game":"test","source_host":"macbook"}' \
     http://midget.tail0056d7.ts.net:8000/ingest
```

**Step 2 — Client.** OBS config + watcher + clipboard. Hotkey→link in ~4s.

**Step 3 — Web UI.** Gallery, `/v/<id>`, OG share pages (dormant), trim UI.

**Step 4 — Integrate.** Homepage tile, Uptime Kuma monitor, retention sweep live.

Steps 1 and 3 are the interesting engineering. Step 2 is mostly glue.

---

## 10. Gotchas found on the actual hardware

- **Do not use QuickSync (`renderD128`) for anything.** Jellyfin owns it
  (see `jellyfin/SETUP.md` §8). If clipd ever needs GPU work, use the idle
  GTX 1050 at `renderD129` instead. Ideally clipd needs neither.
- **MKV → MP4 must happen before upload**, not after. Browsers can't play MKV,
  and doing it server-side wastes the 7900X sitting idle on the other end.
- **`+faststart` is mandatory.** Without it, playback waits on a full download.
- **Trim with `-c copy` snaps to keyframes.** In/out points will land on the
  nearest keyframe, not the exact frame. That's the correct tradeoff — it's
  instant and lossless. Only re-encode if frame-exact trimming is ever required.
- **Adding clipd to Homepage requires updating `HOMEPAGE_ALLOWED_HOSTS`** in
  `/home/batman/homepage/docker-compose.yml` — Homepage rejects requests whose
  Host header isn't listed, and the list currently includes the Tailscale name,
  LAN IP, and localhost.
- **Uptime Kuma monitor:** use an HTTP check against a `/healthz` endpoint.
  (Note the existing Minecraft monitor deliberately uses the Docker-container
  monitor type instead of a port ping — a ping would wake itzg's autopause every
  heartbeat. Not an issue for clipd, but it's the house style to think about it.)
- The `prominence` Minecraft container is `Exited (0)` and unrelated to this
  project — ignore it.

---

## 11. Open questions for the next session

1. Go or Python for clipd? (Go = single binary, tiny container. Python = faster to write.)
2. Is the gaming PC Windows or Linux? Only affects the watcher + OBS vs `gpu-screen-recorder`.
3. Retention budget — how many GB of clips is acceptable against 254G free and shrinking?
4. Should screenshots and clips share one gallery or be separate tabs?
