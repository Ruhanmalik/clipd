import json


def test_healthz_reports_ok_on_empty_store(client):
    payload = client.get("/healthz").json()
    assert payload == {"status": "ok", "clips": 0, "bytes": 0,
                       "budget_bytes": 1000, "used_pct": 0.0}


def test_healthz_counts_ingested_clips(client):
    meta = {"kind": "clip", "capture_uuid": "u-1", "game": "Halo",
            "source_host": "h", "captured_at": 1757260800}
    client.post("/ingest", headers={"Authorization": "Bearer secret-token"},
                files={"file": ("a.mp4", b"x" * 250, "video/mp4")},
                data={"meta": json.dumps(meta)})

    payload = client.get("/healthz").json()
    assert payload["clips"] == 1
    assert payload["bytes"] == 250
    assert payload["used_pct"] == 25.0
