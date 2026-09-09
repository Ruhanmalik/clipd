# clipd

Self-hosted clip and screenshot capture for a homelab — a Medal replacement.

Press a hotkey on the gaming PC, and the last ~90 seconds of gameplay is stored,
indexed, thumbnailed, announced on ntfy, and returned as a short URL. The server
never transcodes: the capture client encodes and remuxes, and clipd's only video
work is extracting one thumbnail frame.

This repository implements **Step 1** (the ingest server), **Step 2** (the
capture client), and **Step 3a** (the read-only web UI). See `plan.md` for the
north-star spec and `docs/superpowers/` for the design and implementation
plans.

## Layout
- `server/` — the FastAPI ingest service (Steps 1 and 3a)
- `client/` — `clipwatch`, the OBS watcher for the gaming PC (Step 2). See `client/README.md`.
- `docker-compose.yml` — deploys to `/home/batman/clipd/` on midget
- `.env.example` — copy to `.env`, fill in, never commit

## Endpoints
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/ingest` | Bearer | multipart `file` + JSON `meta`. Returns `{id, url}`. Idempotent on `capture_uuid`. |
| GET | `/healthz` | none | `{status, clips, bytes, budget_bytes, used_pct}` for Uptime Kuma |
| GET | `/` | tailnet | Game tiles + a Recent strip |
| GET | `/g/<game-slug>` | tailnet | One game's captures; `?kind=clip\|screenshot`, `?before=<cursor>` |
| GET | `/v/<id>` | tailnet | Detail page and player — the URL the watcher puts on the clipboard |
| GET | `/m/<id>` | tailnet | The media itself, inline and Range-capable |
| GET | `/t/<id>` | tailnet | Thumbnail |
| GET | `/d/<id>` | tailnet | Download the original |

`meta` requires `kind`, `capture_uuid`, and `source_host`; `game`, `game_exe`,
`title`, and `captured_at` are optional. (The example in `plan.md` §9 predates
`capture_uuid` and now returns 422.)

The tailnet routes carry no application-level auth: Tailscale is the boundary.
See the design spec §8a.

Editing, trimming, deleting, and the dormant share routes are Step 3b.

## Development
    cd server && pip install -e ".[dev]" && python -m pytest
    cd client && pip install -e ".[dev]" && python -m pytest

## Deploy

Only three things belong on midget: the compose file, `server/`, and `.env`.
Syncing the repo root instead would carry a macOS `.venv` and the Windows client
onto a Linux server.

    ssh homelab-ts 'mkdir -p /home/batman/clipd/data'

    rsync -a --delete \
      --exclude '__pycache__' --exclude '*.egg-info' --exclude '.pytest_cache' \
      ./server/ homelab-ts:/home/batman/clipd/server/

    # .env is synced separately, and never under --delete, so a code sync can
    # not wipe the server's secrets.
    rsync -a ./docker-compose.yml .env homelab-ts:/home/batman/clipd/
    ssh homelab-ts 'chmod 600 /home/batman/clipd/.env'

    ssh homelab-ts 'cd /home/batman/clipd && docker compose up --build -d'

`homelab-ts` is the `~/.ssh/config` alias for `batman@100.72.75.62`.

Check it came up:

    curl -s http://midget.tail0056d7.ts.net:8000/healthz

Two `.env` values are worth confirming before the first deploy, because both fail
silently rather than loudly: `BASE_URL` must be midget's tailnet URL (the
`localhost:8000` default puts a link to the *reading* device on your clipboard),
and an empty `NTFY_TOPIC` disables notifications entirely.
