import pytest
from pathlib import Path
from clipd.config import Config, parse_size


def test_parse_size_accepts_plain_bytes():
    assert parse_size("1024") == 1024


def test_parse_size_accepts_binary_suffixes():
    assert parse_size("50GB") == 50 * 1024**3
    assert parse_size("512mb") == 512 * 1024**2
    assert parse_size("2 TB") == 2 * 1024**4


def test_parse_size_rejects_garbage():
    with pytest.raises(ValueError):
        parse_size("banana")


def test_from_env_reads_all_fields():
    cfg = Config.from_env({
        "DATA_DIR": "/data",
        "INGEST_TOKEN": "secret",
        "NTFY_TOPIC": "clipd-abc",
        "MAX_STORE_BYTES": "50GB",
        "BASE_URL": "http://clipd-server.tailnet-name.ts.net:8000",
    })
    assert cfg.data_dir == Path("/data")
    assert cfg.ingest_token == "secret"
    assert cfg.ntfy_topic == "clipd-abc"
    assert cfg.max_store_bytes == 50 * 1024**3
    assert cfg.base_url == "http://clipd-server.tailnet-name.ts.net:8000"


def test_from_env_applies_defaults():
    cfg = Config.from_env({"INGEST_TOKEN": "secret"})
    assert cfg.data_dir == Path("/data")
    assert cfg.ntfy_topic is None
    assert cfg.ntfy_server == "https://ntfy.sh"
    assert cfg.max_store_bytes == 50 * 1024**3
    assert cfg.sweep_interval_s == 900
    assert cfg.share_enabled is False


def test_base_url_strips_trailing_slash():
    cfg = Config.from_env({"INGEST_TOKEN": "s", "BASE_URL": "http://host:8000/"})
    assert cfg.base_url == "http://host:8000"


def test_missing_ingest_token_is_fatal():
    with pytest.raises(ValueError, match="INGEST_TOKEN"):
        Config.from_env({})
