"""The client/server seam: real clipwatch.upload() against a real clipd.

Neither unit suite can catch a drift here. client/tests/test_uploader.py
asserts against a hand-written MockTransport, and server/tests/test_ingest.py
against a hand-built multipart request. Rename a meta field on either side and
both suites stay green while every real capture 422s.
"""
import json

from clipd import db
from clipd.app import IngestMeta
from clipwatch.uploader import upload


def test_a_real_capture_round_trips_through_the_real_ingest(server, server_cfg,
                                                            client_cfg, make_job):
    job = make_job("round-trip", game="Counter-Strike 2", game_exe="cs2.exe")

    result = upload(client_cfg, job, client=server)

    assert result.ok is True
    assert result.retryable is False

    # The bytes are on disk, under the path the *server* chose from the
    # metadata the *client* sent.
    stored = server_cfg.data_dir / "clips" / "counter-strike-2" / "2025" / "09" / f"{result.clip_id}.mp4"
    assert stored.read_bytes() == b"videodata"

    # And the row carries what the client actually transmitted.
    clip = db.get_by_id(server.app.state.conn, result.clip_id)
    assert clip.game == "Counter-Strike 2"
    assert clip.game_exe == "cs2.exe"
    assert clip.source_host == "gaming-pc"
    assert clip.kind == "clip"


def test_the_url_the_client_reports_is_the_one_the_server_built(server, client_cfg,
                                                                make_job):
    # This string lands on the operator's clipboard, so a mismatch between the
    # server's BASE_URL and what the client hands on is directly user-visible.
    result = upload(client_cfg, make_job("url-check"), client=server)

    assert result.url == f"http://clipd-server:8000/v/{result.clip_id}"


def test_the_client_sends_no_field_the_server_would_ignore(server, client_cfg,
                                                           make_job, sent_meta):
    upload(client_cfg, make_job("drift-a"), client=server)

    accepted = set(IngestMeta.model_fields)
    unknown = sent_meta["keys"] - accepted
    assert not unknown, f"client sends fields clipd does not model: {unknown}"


def test_the_client_sends_every_field_the_server_requires(server, client_cfg,
                                                          make_job, sent_meta):
    upload(client_cfg, make_job("drift-b"), client=server)

    required = {n for n, f in IngestMeta.model_fields.items() if f.is_required()}
    missing = required - sent_meta["keys"]
    assert not missing, f"clipd requires fields the client never sends: {missing}"


def test_a_screenshot_also_satisfies_the_contract(server, server_cfg, client_cfg,
                                                  make_job):
    # "screenshot" is the other half of the server's Literal, and it takes a
    # different extension and a different storage root.
    job = make_job("shot-1", kind="screenshot", name="shot-1.png", body=b"pngdata")

    result = upload(client_cfg, job, client=server)

    assert result.ok is True
    clip = db.get_by_id(server.app.state.conn, result.clip_id)
    assert clip.kind == "screenshot"
    assert clip.rel_path.startswith("shots/")
    assert clip.rel_path.endswith(".png")


def test_the_same_capture_uuid_stores_one_file_and_returns_one_id(server, server_cfg,
                                                                  client_cfg, make_job):
    # The watcher retries from a durable queue, so the second attempt after a
    # timeout must not produce a second copy of a several-hundred-MB clip.
    first = upload(client_cfg, make_job("same-uuid"), client=server)
    second = upload(client_cfg, make_job("same-uuid"), client=server)

    assert first.clip_id == second.clip_id
    assert second.ok is True
    stored = list((server_cfg.data_dir / "clips").rglob("*.mp4"))
    assert len(stored) == 1


def test_a_wrong_token_is_a_real_401_the_client_keeps_queued(server, client_cfg,
                                                             make_job):
    # A rotated or not-yet-set token must not cost the capture: the client
    # classifies 401 as retryable, and this proves the server really returns
    # one rather than the test asserting against its own guess.
    wrong = client_cfg.__class__(**{**client_cfg.__dict__, "ingest_token": "not-the-token"})

    result = upload(wrong, make_job("bad-token"), client=server)

    assert result.ok is False
    assert result.retryable is True


def test_metadata_the_server_rejects_is_permanent_not_retryable(server, client_cfg,
                                                                make_job):
    # An unmodelled kind fails IngestMeta validation. Retrying it forever would
    # wedge the queue behind a capture that can never succeed.
    job = make_job("bad-kind", kind="recording")

    result = upload(client_cfg, job, client=server)

    assert result.ok is False
    assert result.retryable is False


def test_an_implausible_clock_is_refused_and_not_retried(server, client_cfg, make_job):
    # A fresh Windows install before NTP reports 1970. The server refuses it;
    # the client must treat that as permanent, not sit on it forever.
    job = make_job("bad-clock", captured_at=0)

    result = upload(client_cfg, job, client=server)

    assert result.ok is False
    assert result.retryable is False


def test_the_stored_metadata_matches_the_json_that_crossed_the_wire(server, client_cfg,
                                                                    make_job, sent_meta):
    # Ties the two sides together explicitly: what the client serialised is
    # what the server persisted, field for field.
    result = upload(client_cfg, make_job("wire-check", game="Halo Infinite",
                                         game_exe="halo.exe"), client=server)

    wire = json.loads(sent_meta["raw"])
    clip = db.get_by_id(server.app.state.conn, result.clip_id)
    assert wire["game"] == clip.game
    assert wire["game_exe"] == clip.game_exe
    assert wire["source_host"] == clip.source_host
    assert wire["kind"] == clip.kind
    assert wire["capture_uuid"] == "wire-check"
