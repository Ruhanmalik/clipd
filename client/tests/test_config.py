import pytest
from pathlib import Path
from clipwatch.config import Config, DEFAULT_IGNORE_EXES

TOML = """
server_url = "http://midget.tail0056d7.ts.net:8000"
watch_dir = "C:/captures"
source_host = "desktop-amtr56i"
ignore_exes = ["explorer.exe", "Discord.exe"]
"""


def write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_load_reads_toml(tmp_path):
    cfg = Config.load(write(tmp_path, TOML), {"CLIPD_TOKEN": "t"})
    assert cfg.server_url == "http://midget.tail0056d7.ts.net:8000"
    assert cfg.watch_dir == Path("C:/captures")
    assert cfg.source_host == "desktop-amtr56i"


def test_env_token_is_used(tmp_path):
    cfg = Config.load(write(tmp_path, TOML), {"CLIPD_TOKEN": "secret"})
    assert cfg.ingest_token == "secret"


def test_env_token_overrides_toml(tmp_path):
    cfg = Config.load(write(tmp_path, TOML + '\ningest_token = "from-file"\n'),
                      {"CLIPD_TOKEN": "from-env"})
    assert cfg.ingest_token == "from-env"


def test_token_may_come_from_toml_alone(tmp_path):
    cfg = Config.load(write(tmp_path, TOML + '\ningest_token = "from-file"\n'), {})
    assert cfg.ingest_token == "from-file"


def test_missing_token_is_fatal(tmp_path):
    with pytest.raises(ValueError, match="CLIPD_TOKEN"):
        Config.load(write(tmp_path, TOML), {})


def test_server_url_strips_trailing_slash(tmp_path):
    cfg = Config.load(write(tmp_path, TOML.replace(":8000", ":8000/")), {"CLIPD_TOKEN": "t"})
    assert cfg.server_url == "http://midget.tail0056d7.ts.net:8000"


def test_defaults_are_applied(tmp_path):
    cfg = Config.load(write(tmp_path, TOML), {"CLIPD_TOKEN": "t"})
    assert cfg.sample_interval_s == 5.0
    assert cfg.detect_window_s == 90.0
    assert cfg.stability_checks == 3
    assert cfg.max_backoff_s == 300.0


def test_ignore_exes_are_lowercased_for_comparison(tmp_path):
    cfg = Config.load(write(tmp_path, TOML), {"CLIPD_TOKEN": "t"})
    assert "discord.exe" in cfg.ignore_exes
    assert "explorer.exe" in cfg.ignore_exes


def test_ignore_exes_default_when_absent(tmp_path):
    body = TOML.replace('ignore_exes = ["explorer.exe", "Discord.exe"]', "")
    cfg = Config.load(write(tmp_path, body), {"CLIPD_TOKEN": "t"})
    assert cfg.ignore_exes == DEFAULT_IGNORE_EXES


def test_queue_and_work_dirs_default_under_watch_dir(tmp_path):
    body = TOML.replace('watch_dir = "C:/captures"', f'watch_dir = "{tmp_path.as_posix()}"')
    cfg = Config.load(write(tmp_path, body), {"CLIPD_TOKEN": "t"})
    assert cfg.queue_dir == tmp_path / ".clipwatch" / "queue"
    assert cfg.work_dir == tmp_path / ".clipwatch" / "work"
