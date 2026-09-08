# clipd

Self-hosted clip and screenshot capture for a homelab — a Medal replacement.

Press a hotkey on the gaming PC, and the last ~90 seconds of gameplay is stored,
indexed, thumbnailed, announced on ntfy, and returned as a short URL. The server
never transcodes: the capture client encodes and remuxes, and clipd's only video
work is extracting one thumbnail frame.

This repository currently implements **Step 1** — the ingest server. See
`plan.md` for the north-star spec and `docs/superpowers/` for the design and
implementation plan.

## Layout
- `server/` — the FastAPI service
- `docker-compose.yml` — deploys to `/home/<user>/clipd/` on clipd-server
- `.env.example` — copy to `.env`, fill in, never commit

## Endpoints (Step 1)
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/ingest` | Bearer | multipart `file` + JSON `meta`. Returns `{id, url}`. Idempotent on `capture_uuid`. |
| GET | `/healthz` | none | `{status, clips, bytes, budget_bytes, used_pct}` for Uptime Kuma |

The gallery, `/v/<id>`, trim, and the dormant share routes are Step 3. The schema
and storage layout already accommodate them, so Step 3 needs no migration.

## Development
    cd server && pip install -e ".[dev]" && python -m pytest

## Deploy
    rsync -av --exclude data --exclude .env ./ <user>@<server-ip>:/home/<user>/clipd/
    ssh <user>@<server-ip> 'cd /home/<user>/clipd && docker compose up --build -d'
