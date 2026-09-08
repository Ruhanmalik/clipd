import pytest
from clipd.config import Config
from clipd.notify import human_bytes, notify_capture, notify_retention


def cfg(topic="clipd-test"):
    return Config.from_env({"INGEST_TOKEN": "t", "NTFY_TOPIC": topic or ""})


@pytest.mark.parametrize("raw,expected", [
    (512, "512 B"), (1536, "1.5 KB"), (5 * 1024**2, "5.0 MB"),
    (int(1.5 * 1024**3), "1.5 GB"),
])
def test_human_bytes_formats_sizes(raw, expected):
    assert human_bytes(raw) == expected


async def test_notify_capture_posts_to_topic_with_click_action(monkeypatch):
    sent = {}

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, content=None, headers=None):
            sent.update(url=url, content=content, headers=headers)
            class R:
                status_code = 200
                def raise_for_status(self): pass
            return R()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())

    ok = await notify_capture(
        cfg(), title="Counter-Strike 2", body="90s · 142.0 MB",
        click_url="http://midget:8000/v/aB3xY9z",
    )

    assert ok is True
    assert sent["url"] == "https://ntfy.sh/clipd-test"
    assert sent["content"] == "90s · 142.0 MB"
    assert sent["headers"]["Title"] == "Counter-Strike 2"
    assert sent["headers"]["Click"] == "http://midget:8000/v/aB3xY9z"


async def test_notify_is_a_no_op_when_topic_unset(monkeypatch):
    def explode(**kw):
        raise AssertionError("must not build a client without a topic")
    monkeypatch.setattr("httpx.AsyncClient", explode)

    assert await notify_capture(cfg(topic=None), title="t", body="b", click_url="u") is False


async def test_notify_swallows_transport_errors(monkeypatch):
    # ntfy.sh being unreachable must never fail an ingest.
    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            raise OSError("dns failure")

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())

    assert await notify_capture(cfg(), title="t", body="b", click_url="u") is False


async def test_notify_retention_sets_priority_header(monkeypatch):
    sent = {}

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, content=None, headers=None):
            sent.update(headers=headers)
            class R:
                status_code = 200
                def raise_for_status(self): pass
            return R()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())

    await notify_retention(cfg(), title="Store full", body="over budget", priority="high")
    assert sent["headers"]["Priority"] == "high"
