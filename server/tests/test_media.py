import json
import pytest
from clipd.media import ProbeResult, probe, make_thumbnail

FFPROBE_JSON = json.dumps({
    "format": {"duration": "90.5"},
    "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
})


class FakeProc:
    def __init__(self, stdout=b"", returncode=0):
        self._stdout, self.returncode = stdout, returncode

    async def communicate(self):
        return self._stdout, b""


async def test_probe_extracts_duration_and_dimensions(monkeypatch, tmp_path):
    async def fake_exec(*args, **kwargs):
        return FakeProc(FFPROBE_JSON.encode())
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    result = await probe(tmp_path / "a.mp4")
    assert result == ProbeResult(duration_s=90.5, width=1920, height=1080)


async def test_probe_handles_image_without_duration(monkeypatch, tmp_path):
    payload = json.dumps({
        "format": {},
        "streams": [{"codec_type": "video", "width": 2560, "height": 1440}],
    }).encode()

    async def fake_exec(*args, **kwargs):
        return FakeProc(payload)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    result = await probe(tmp_path / "a.png")
    assert result == ProbeResult(duration_s=None, width=2560, height=1440)


async def test_probe_returns_empty_result_when_ffprobe_fails(monkeypatch, tmp_path):
    async def fake_exec(*args, **kwargs):
        return FakeProc(b"", returncode=1)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await probe(tmp_path / "bad.mp4") == ProbeResult(None, None, None)


async def test_make_thumbnail_reports_success(monkeypatch, tmp_path):
    dest = tmp_path / "thumbs" / "a.jpg"

    async def fake_exec(*args, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpegdata")
        return FakeProc()
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await make_thumbnail(tmp_path / "a.mp4", dest, 9.0) is True
    assert dest.exists()


async def test_make_thumbnail_returns_false_instead_of_raising(monkeypatch, tmp_path):
    # A failed thumbnail must not fail the ingest — the clip is already stored.
    async def fake_exec(*args, **kwargs):
        return FakeProc(returncode=1)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await make_thumbnail(tmp_path / "a.mp4", tmp_path / "t.jpg", 1.0) is False
