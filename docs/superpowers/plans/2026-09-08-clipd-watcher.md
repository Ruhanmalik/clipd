# clipd Watcher (Step 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Windows capture client so pressing the OBS replay hotkey puts a working clipd URL on the clipboard within a few seconds — and never loses a capture when the server is unreachable.

**Architecture:** A single long-running Python daemon on the gaming PC. A `watchdog` observer watches the OBS output directory; a background sampler polls the foreground window's executable every 5 s into a 90 s ring buffer. On a new file: wait for it to stop growing, remux MKV→MP4 with `-c copy`, resolve the game from the ring buffer, enqueue a durable job, then drain the queue against `POST /ingest`. All platform-specific calls (foreground window, clipboard, toast) sit behind one `PlatformAdapter` protocol so every other module is unit-testable on macOS.

**Tech Stack:** Python 3.12, watchdog, httpx, psutil, pywin32 (Windows only), stdlib `tomllib`, ffmpeg, pytest, PyInstaller.

**Spec:** `docs/superpowers/specs/2026-09-07-clipd-design.md` §5, and `plan.md` §8. Where `plan.md` §8 says "small Go binary", the design doc §1 supersedes it: **Python**.

## Global Constraints

Every task's requirements implicitly include this section.

- Python **3.12**, matching the server.
- The daemon runs on **`desktop-amtr56i` / `100.66.55.18`** (Windows). Development and unit testing happen on macOS, so **no module outside `clipwatch/platform/windows.py` may import `win32*` or `pywin32`.**
- Server endpoint: `POST http://midget.tail0056d7.ts.net:8000/ingest`, bearer auth.
- **`capture_uuid` is generated exactly once, when a job is enqueued, and reused on every retry.** This is what makes the server's idempotency work (design §3). Regenerating it on retry would create duplicates.
- **Delete the local capture only after a confirmed HTTP 200** (`plan.md` §8.7).
- **The client encodes and remuxes; the server never transcodes** (`plan.md` §5). The remux is `-c copy -movflags +faststart` — a container swap, never a re-encode.
- **`+faststart` is mandatory** (`plan.md` §10).
- Game detection uses the **foreground process executable sampled over the capture window**, never the window title at hotkey time (design §5).
- `games.toml` **ships locally with the watcher**. Detection is entirely client-side and must work while clipd is unreachable — which is exactly when the retry queue matters.
- Screenshots take the same pipeline as clips, minus the remux step (`plan.md` §8).
- Secrets are not committed. The token comes from `CLIPD_TOKEN` or a gitignored config file.

## Prerequisite

OpenSSH Server must be running on the gaming PC before Task 10. In an Administrator PowerShell:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
```

For an administrator account the public key goes in `C:\ProgramData\ssh\administrators_authorized_keys`, **not** `~/.ssh/authorized_keys`, which Windows silently ignores for admins. Tasks 1–9 need no access to that machine.

## File structure

```
client/
├── pyproject.toml
├── games.toml                    # exe -> pretty name, user-editable
├── config.example.toml
└── clipwatch/
    ├── __init__.py
    ├── config.py                 # TOML + env config
    ├── games.py                  # games.toml load + exe resolution
    ├── detector.py               # foreground-exe ring buffer (pure)
    ├── stability.py              # wait for a file to stop growing
    ├── remux.py                  # ffmpeg -c copy -movflags +faststart
    ├── jobs.py                   # durable retry queue
    ├── uploader.py               # POST /ingest
    ├── pipeline.py               # one capture, end to end
    ├── daemon.py                 # watchdog observer + sampler + drain loop
    ├── main.py                   # entrypoint
    └── platform/
        ├── __init__.py           # adapter selection
        ├── base.py               # PlatformAdapter protocol
        ├── null.py               # no-op adapter (macOS dev, tests)
        └── windows.py            # pywin32 implementation
```

---

### Task 1: Scaffold and configuration

**Files:**
- Create: `client/pyproject.toml`, `client/clipwatch/__init__.py`, `client/clipwatch/config.py`, `client/config.example.toml`
- Test: `client/tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Config` frozen dataclass with `server_url: str`, `ingest_token: str`, `watch_dir: Path`, `work_dir: Path`, `queue_dir: Path`, `source_host: str`, `sample_interval_s: float`, `detect_window_s: float`, `ignore_exes: frozenset[str]`, `stability_checks: int`, `stability_interval_s: float`, `max_backoff_s: float`; `Config.load(toml_path: Path | None, env: Mapping[str, str]) -> Config`.

- [ ] **Step 1: Write the failing test**

```python
# client/tests/test_config.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd client && python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipwatch'`

- [ ] **Step 3: Write the scaffold and implementation**

```toml
# client/pyproject.toml
[project]
name = "clipwatch"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "watchdog>=5.0",
    "httpx>=0.27",
    "psutil>=6.0",
    "pywin32>=306; sys_platform == 'win32'",
]

[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24", "pyinstaller>=6.10"]

[project.scripts]
clipwatch = "clipwatch.main:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["clipwatch*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

```python
# client/clipwatch/__init__.py
"""clipwatch — the clipd capture client."""
```

```python
# client/clipwatch/config.py
"""Configuration: a TOML file next to the executable, plus env overrides.

The token is accepted from either, but CLIPD_TOKEN wins — a config file is
easy to screenshot or paste into a support thread, an env var is not.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# design §5. Compared lowercased, because Windows reports executable casing
# inconsistently between processes.
DEFAULT_IGNORE_EXES = frozenset({
    "explorer.exe", "discord.exe", "chrome.exe", "msedge.exe", "firefox.exe",
    "searchhost.exe", "shellexperiencehost.exe",
})


@dataclass(frozen=True)
class Config:
    server_url: str
    ingest_token: str
    watch_dir: Path
    work_dir: Path
    queue_dir: Path
    source_host: str
    sample_interval_s: float
    detect_window_s: float
    ignore_exes: frozenset[str]
    stability_checks: int
    stability_interval_s: float
    max_backoff_s: float

    @classmethod
    def load(cls, toml_path: Path | None, env: Mapping[str, str]) -> "Config":
        raw: dict = {}
        if toml_path is not None and toml_path.exists():
            raw = tomllib.loads(toml_path.read_text())

        token = env.get("CLIPD_TOKEN") or raw.get("ingest_token") or ""
        if not token.strip():
            raise ValueError("CLIPD_TOKEN must be set, or ingest_token given in config")

        watch_dir = Path(raw["watch_dir"])
        state = watch_dir / ".clipwatch"

        ignore = raw.get("ignore_exes")
        ignore_exes = (
            frozenset(e.lower() for e in ignore) if ignore else DEFAULT_IGNORE_EXES
        )

        return cls(
            server_url=str(raw["server_url"]).rstrip("/"),
            ingest_token=token.strip(),
            watch_dir=watch_dir,
            work_dir=Path(raw.get("work_dir", state / "work")),
            queue_dir=Path(raw.get("queue_dir", state / "queue")),
            source_host=raw.get("source_host", "unknown"),
            sample_interval_s=float(raw.get("sample_interval_s", 5.0)),
            detect_window_s=float(raw.get("detect_window_s", 90.0)),
            ignore_exes=ignore_exes,
            stability_checks=int(raw.get("stability_checks", 3)),
            stability_interval_s=float(raw.get("stability_interval_s", 0.5)),
            max_backoff_s=float(raw.get("max_backoff_s", 300.0)),
        )
```

```toml
# client/config.example.toml — copy to config.toml beside the exe.
# The token is better supplied as the CLIPD_TOKEN environment variable.
server_url = "http://midget.tail0056d7.ts.net:8000"

# The OBS recording/screenshot output directory.
watch_dir = "C:/Users/ruhan/Videos/clipd"

source_host = "desktop-amtr56i"

# ingest_token = "only-if-you-cannot-use-CLIPD_TOKEN"

# Executables never treated as "the game". design §5.
ignore_exes = [
  "explorer.exe", "Discord.exe", "chrome.exe", "msedge.exe",
  "firefox.exe", "SearchHost.exe", "ShellExperienceHost.exe",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd client && pip install -e ".[dev]" && python -m pytest tests/test_config.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add client/pyproject.toml client/clipwatch/__init__.py client/clipwatch/config.py client/config.example.toml client/tests/test_config.py
git commit -m "feat: add clipwatch scaffold and configuration"
```

---

### Task 2: games.toml resolution

**Files:**
- Create: `client/clipwatch/games.py`, `client/games.toml`
- Test: `client/tests/test_games.py`

**Interfaces:**
- Consumes: nothing
- Produces: `load_games(path: Path) -> dict[str, str]` (keys lowercased); `resolve_game(exe: str | None, mapping: dict[str, str], window_title: str | None = None) -> str`.

Resolution order from design §5: `games.toml`, then the sanitized window title, then `"Unknown"`.

- [ ] **Step 1: Write the failing test**

```python
# client/tests/test_games.py
import pytest
from clipwatch.games import load_games, resolve_game


@pytest.fixture
def mapping(tmp_path):
    p = tmp_path / "games.toml"
    p.write_text('"cs2.exe" = "Counter-Strike 2"\n"VALORANT-Win64-Shipping.exe" = "VALORANT"\n')
    return load_games(p)


def test_load_lowercases_keys(mapping):
    assert mapping["cs2.exe"] == "Counter-Strike 2"
    assert mapping["valorant-win64-shipping.exe"] == "VALORANT"


def test_load_missing_file_is_empty(tmp_path):
    assert load_games(tmp_path / "absent.toml") == {}


def test_resolve_prefers_the_mapping(mapping):
    assert resolve_game("cs2.exe", mapping) == "Counter-Strike 2"


def test_resolve_is_case_insensitive(mapping):
    assert resolve_game("CS2.EXE", mapping) == "Counter-Strike 2"


def test_resolve_falls_back_to_window_title(mapping):
    assert resolve_game("unknown.exe", mapping, "Deep Rock Galactic") == "Deep Rock Galactic"


def test_resolve_strips_control_chars_from_title(mapping):
    assert resolve_game("x.exe", mapping, "Halo\x00\x1f  Infinite ") == "Halo Infinite"


def test_resolve_ignores_an_empty_title(mapping):
    assert resolve_game("x.exe", mapping, "   ") == "Unknown"


def test_resolve_falls_back_to_unknown(mapping):
    assert resolve_game(None, mapping) == "Unknown"


def test_resolve_derives_a_name_from_the_exe_when_no_title(mapping):
    # A bare exe is more useful than "Unknown" — the gallery can be re-tagged.
    assert resolve_game("DeepRockGalactic.exe", mapping) == "DeepRockGalactic"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd client && python -m pytest tests/test_games.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipwatch.games'`

- [ ] **Step 3: Write the implementation**

```python
# client/clipwatch/games.py
"""Map a process executable to a pretty game name.

Ships locally with the watcher: detection is entirely client-side and must
keep working while clipd is unreachable, which is exactly when the retry
queue matters.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

UNKNOWN = "Unknown"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


def load_games(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return {str(k).lower(): str(v) for k, v in raw.items()}


def _clean_title(title: str | None) -> str:
    if not title:
        return ""
    return _WHITESPACE.sub(" ", _CONTROL.sub(" ", title)).strip()


def resolve_game(
    exe: str | None, mapping: dict[str, str], window_title: str | None = None
) -> str:
    if exe:
        mapped = mapping.get(exe.lower())
        if mapped:
            return mapped

    title = _clean_title(window_title)
    if title:
        return title

    if exe:
        stem = Path(exe).stem.strip()
        if stem:
            return stem

    return UNKNOWN
```

```toml
# client/games.toml — exe -> display name. Edit freely; keys are case-insensitive.
"cs2.exe" = "Counter-Strike 2"
"VALORANT-Win64-Shipping.exe" = "VALORANT"
"RainbowSix.exe" = "Rainbow Six Siege"
"FortniteClient-Win64-Shipping.exe" = "Fortnite"
"r5apex.exe" = "Apex Legends"
"FSD-Win64-Shipping.exe" = "Deep Rock Galactic"
"MinecraftLauncher.exe" = "Minecraft"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd client && python -m pytest tests/test_games.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add client/clipwatch/games.py client/games.toml client/tests/test_games.py
git commit -m "feat: add local games.toml resolution"
```

---

### Task 3: Foreground-executable ring buffer

**Files:**
- Create: `client/clipwatch/detector.py`
- Test: `client/tests/test_detector.py`

**Interfaces:**
- Consumes: nothing (samples are pushed in by the caller, so this stays pure)
- Produces: `ExeRingBuffer(window_s: float)` with `.record(exe: str | None, now: float) -> None`, `.dominant(ignore: frozenset[str], now: float) -> str | None`, `.titles: dict[str, str]`; and `record_sample(buffer, adapter, now)`.

Pure and time-injected, so the whole detection rule is testable on macOS.

- [ ] **Step 1: Write the failing test**

```python
# client/tests/test_detector.py
from clipwatch.detector import ExeRingBuffer

IGNORE = frozenset({"discord.exe", "explorer.exe"})


def test_dominant_picks_the_most_frequent_exe():
    buf = ExeRingBuffer(window_s=90)
    for t in range(0, 50, 5):
        buf.record("cs2.exe", t)
    for t in range(50, 70, 5):
        buf.record("discord.exe", t)
    assert buf.dominant(IGNORE, now=70) == "cs2.exe"


def test_ignored_exes_never_win_even_when_most_frequent():
    buf = ExeRingBuffer(window_s=90)
    for t in range(0, 60, 5):
        buf.record("discord.exe", t)
    buf.record("cs2.exe", 60)
    assert buf.dominant(IGNORE, now=60) == "cs2.exe"


def test_samples_older_than_the_window_are_dropped():
    buf = ExeRingBuffer(window_s=90)
    for t in range(0, 100, 5):
        buf.record("oldgame.exe", t)
    for t in range(100, 190, 5):
        buf.record("cs2.exe", t)
    assert buf.dominant(IGNORE, now=190) == "cs2.exe"


def test_returns_none_when_everything_is_ignored():
    buf = ExeRingBuffer(window_s=90)
    buf.record("discord.exe", 0)
    buf.record("explorer.exe", 5)
    assert buf.dominant(IGNORE, now=5) is None


def test_returns_none_when_empty():
    assert ExeRingBuffer(window_s=90).dominant(IGNORE, now=0) is None


def test_none_samples_are_ignored():
    buf = ExeRingBuffer(window_s=90)
    buf.record(None, 0)
    buf.record("cs2.exe", 5)
    assert buf.dominant(IGNORE, now=5) == "cs2.exe"


def test_comparison_is_case_insensitive():
    buf = ExeRingBuffer(window_s=90)
    buf.record("CS2.exe", 0)
    buf.record("cs2.EXE", 5)
    assert buf.dominant(IGNORE, now=5) == "cs2.exe"


def test_ties_prefer_the_more_recent_exe():
    buf = ExeRingBuffer(window_s=90)
    buf.record("a.exe", 0)
    buf.record("b.exe", 5)
    assert buf.dominant(IGNORE, now=5) == "b.exe"


def test_titles_records_the_last_title_seen_per_exe():
    buf = ExeRingBuffer(window_s=90)
    buf.record("cs2.exe", 0, title="Counter-Strike 2 - old")
    buf.record("cs2.exe", 5, title="Counter-Strike 2")
    assert buf.titles["cs2.exe"] == "Counter-Strike 2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd client && python -m pytest tests/test_detector.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipwatch.detector'`

- [ ] **Step 3: Write the implementation**

```python
# client/clipwatch/detector.py
"""Which game was being played over the capture window.

design §5: the window title at hotkey time is unreliable — alt-tabbing to
Discord before hitting the hotkey would file the clip under "Discord". So we
sample the foreground process executable on a timer and take the most common
non-ignored one across the replay-buffer window.

Time is injected rather than read from the clock, so the rule is testable.
"""
from __future__ import annotations

from collections import deque


class ExeRingBuffer:
    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self._samples: deque[tuple[float, str]] = deque()
        self.titles: dict[str, str] = {}

    def record(self, exe: str | None, now: float, title: str | None = None) -> None:
        if not exe:
            return
        key = exe.lower()
        self._samples.append((now, key))
        if title:
            self.titles[key] = title
        self._evict(now)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def dominant(self, ignore: frozenset[str], now: float) -> str | None:
        self._evict(now)

        counts: dict[str, int] = {}
        last_seen: dict[str, float] = {}
        for at, exe in self._samples:
            if exe in ignore:
                continue
            counts[exe] = counts.get(exe, 0) + 1
            last_seen[exe] = at

        if not counts:
            return None
        # Ties break toward whatever was in front most recently.
        return max(counts, key=lambda e: (counts[e], last_seen[e]))


def record_sample(buffer: ExeRingBuffer, adapter, now: float) -> None:
    """Take one foreground sample from a PlatformAdapter into the buffer."""
    exe, title = adapter.foreground()
    buffer.record(exe, now, title)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd client && python -m pytest tests/test_detector.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add client/clipwatch/detector.py client/tests/test_detector.py
git commit -m "feat: add foreground-exe ring buffer for game detection"
```

---

### Task 4: Platform adapter

**Files:**
- Create: `client/clipwatch/platform/__init__.py`, `base.py`, `null.py`, `windows.py`
- Test: `client/tests/test_platform.py`

**Interfaces:**
- Consumes: nothing
- Produces: `PlatformAdapter` protocol with `foreground() -> tuple[str | None, str | None]`, `set_clipboard(text: str) -> bool`, `notify(title: str, body: str) -> bool`; `NullAdapter`; `WindowsAdapter`; `get_adapter(name: str | None = None) -> PlatformAdapter`.

**This is the only module that may import `win32*`.** Everything else takes an adapter. `windows.py` is imported lazily so importing the package on macOS never touches pywin32.

- [ ] **Step 1: Write the failing test**

```python
# client/tests/test_platform.py
import sys
import pytest
from clipwatch.platform import NullAdapter, get_adapter
from clipwatch.platform.base import PlatformAdapter


def test_null_adapter_satisfies_the_protocol():
    assert isinstance(NullAdapter(), PlatformAdapter)


def test_null_foreground_returns_nothing():
    assert NullAdapter().foreground() == (None, None)


def test_null_clipboard_and_notify_report_failure_without_raising():
    adapter = NullAdapter()
    assert adapter.set_clipboard("x") is False
    assert adapter.notify("t", "b") is False


def test_null_adapter_records_calls_for_assertions():
    adapter = NullAdapter()
    adapter.set_clipboard("http://midget:8000/v/abc")
    adapter.notify("Counter-Strike 2", "90s")
    assert adapter.clipboard == "http://midget:8000/v/abc"
    assert adapter.notifications == [("Counter-Strike 2", "90s")]


def test_get_adapter_returns_null_when_asked():
    assert isinstance(get_adapter("null"), NullAdapter)


def test_get_adapter_falls_back_to_null_off_windows():
    # The whole point: importing this package on macOS must never touch pywin32.
    if sys.platform != "win32":
        assert isinstance(get_adapter(), NullAdapter)


def test_get_adapter_rejects_an_unknown_name():
    with pytest.raises(ValueError):
        get_adapter("beos")


def test_importing_the_package_does_not_import_pywin32():
    assert "win32gui" not in sys.modules
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd client && python -m pytest tests/test_platform.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipwatch.platform'`

- [ ] **Step 3: Write the implementation**

```python
# client/clipwatch/platform/base.py
"""The only surface the rest of clipwatch sees for OS-specific behaviour."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PlatformAdapter(Protocol):
    def foreground(self) -> tuple[str | None, str | None]:
        """Return (executable name, window title) of the foreground window."""

    def set_clipboard(self, text: str) -> bool:
        """Put text on the clipboard. False on failure — never raises."""

    def notify(self, title: str, body: str) -> bool:
        """Show a desktop notification. False on failure — never raises."""
```

```python
# client/clipwatch/platform/null.py
"""No-op adapter: development on macOS, and assertions in tests."""
from __future__ import annotations


class NullAdapter:
    def __init__(self) -> None:
        self.clipboard: str | None = None
        self.notifications: list[tuple[str, str]] = []

    def foreground(self) -> tuple[str | None, str | None]:
        return (None, None)

    def set_clipboard(self, text: str) -> bool:
        self.clipboard = text
        return False  # nothing was really put on a clipboard

    def notify(self, title: str, body: str) -> bool:
        self.notifications.append((title, body))
        return False
```

```python
# client/clipwatch/platform/windows.py
"""Windows implementation. The ONLY module permitted to import win32*.

Every method swallows its errors: a failed clipboard write or toast must
never take down the daemon or fail an upload that already succeeded.
"""
from __future__ import annotations

import logging

import psutil
import win32con
import win32gui
import win32process

log = logging.getLogger(__name__)


class WindowsAdapter:
    def foreground(self) -> tuple[str | None, str | None]:
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return (None, None)
            title = win32gui.GetWindowText(hwnd) or None
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if not pid:
                return (None, title)
            return (psutil.Process(pid).name(), title)
        except Exception:
            log.debug("foreground sample failed", exc_info=True)
            return (None, None)

    def set_clipboard(self, text: str) -> bool:
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            log.warning("clipboard write failed", exc_info=True)
            return False

    def notify(self, title: str, body: str) -> bool:
        try:
            from windows_toasts import Toast, WindowsToaster

            toaster = WindowsToaster("clipd")
            toast = Toast()
            toast.text_fields = [title, body]
            toaster.show_toast(toast)
            return True
        except Exception:
            # windows-toasts is optional; the clipboard is the real signal.
            log.debug("toast failed", exc_info=True)
            return False
```

```python
# client/clipwatch/platform/__init__.py
"""Adapter selection. windows.py is imported lazily so macOS never sees pywin32."""
from __future__ import annotations

import sys

from .base import PlatformAdapter
from .null import NullAdapter

__all__ = ["PlatformAdapter", "NullAdapter", "get_adapter"]


def get_adapter(name: str | None = None) -> PlatformAdapter:
    if name is None:
        name = "windows" if sys.platform == "win32" else "null"

    if name == "null":
        return NullAdapter()
    if name == "windows":
        from .windows import WindowsAdapter

        return WindowsAdapter()
    raise ValueError(f"unknown platform adapter: {name!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd client && python -m pytest tests/test_platform.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add client/clipwatch/platform client/tests/test_platform.py
git commit -m "feat: add platform adapter with windows and null implementations"
```

---

### Task 5: File stability detection

**Files:**
- Create: `client/clipwatch/stability.py`
- Test: `client/tests/test_stability.py`

**Interfaces:**
- Consumes: nothing
- Produces: `wait_until_stable(path: Path, *, checks: int, interval_s: float, timeout_s: float, sleep=time.sleep, now=time.monotonic) -> bool`.

`plan.md` §8.2: OBS is still flushing when the first watchdog event fires, so the file must be observed to stop growing before anything touches it.

- [ ] **Step 1: Write the failing test**

```python
# client/tests/test_stability.py
from clipwatch.stability import wait_until_stable


class FakeClock:
    """Drives sleep and monotonic together so tests never actually wait."""

    def __init__(self):
        self.t = 0.0

    def sleep(self, seconds):
        self.t += seconds

    def now(self):
        return self.t


def test_returns_true_when_the_file_stops_growing(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x" * 10)
    clock = FakeClock()
    assert wait_until_stable(f, checks=3, interval_s=0.5, timeout_s=30,
                             sleep=clock.sleep, now=clock.now) is True


def test_waits_while_the_file_is_still_growing(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    clock = FakeClock()
    grew = {"n": 0}

    def growing_sleep(seconds):
        clock.sleep(seconds)
        if grew["n"] < 4:
            grew["n"] += 1
            f.write_bytes(b"x" * (10 * grew["n"]))

    assert wait_until_stable(f, checks=3, interval_s=0.5, timeout_s=30,
                             sleep=growing_sleep, now=clock.now) is True
    assert grew["n"] == 4  # it kept waiting while the size changed


def test_times_out_on_a_file_that_never_settles(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    clock = FakeClock()
    size = {"n": 1}

    def always_growing(seconds):
        clock.sleep(seconds)
        size["n"] += 1
        f.write_bytes(b"x" * size["n"])

    assert wait_until_stable(f, checks=3, interval_s=0.5, timeout_s=5,
                             sleep=always_growing, now=clock.now) is False


def test_returns_false_when_the_file_disappears(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    clock = FakeClock()

    def deleting_sleep(seconds):
        clock.sleep(seconds)
        f.unlink(missing_ok=True)

    assert wait_until_stable(f, checks=3, interval_s=0.5, timeout_s=30,
                             sleep=deleting_sleep, now=clock.now) is False


def test_returns_false_for_a_file_that_never_existed(tmp_path):
    clock = FakeClock()
    assert wait_until_stable(tmp_path / "nope.mkv", checks=3, interval_s=0.5,
                             timeout_s=5, sleep=clock.sleep, now=clock.now) is False


def test_a_zero_byte_file_is_not_considered_stable(tmp_path):
    # OBS creates the file before writing to it.
    f = tmp_path / "a.mkv"
    f.write_bytes(b"")
    clock = FakeClock()
    assert wait_until_stable(f, checks=3, interval_s=0.5, timeout_s=5,
                             sleep=clock.sleep, now=clock.now) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd client && python -m pytest tests/test_stability.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipwatch.stability'`

- [ ] **Step 3: Write the implementation**

```python
# client/clipwatch/stability.py
"""Wait for a capture to finish being written.

plan.md §8.2: OBS is still flushing the replay buffer when the first
filesystem event arrives, so the file must be seen to stop growing before it
is remuxed or uploaded. Clock functions are injected so tests do not sleep.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def wait_until_stable(
    path: Path,
    *,
    checks: int,
    interval_s: float,
    timeout_s: float,
    sleep=time.sleep,
    now=time.monotonic,
) -> bool:
    """True once the file has held the same non-zero size for `checks` polls."""
    deadline = now() + timeout_s
    last = _size(path)
    stable = 0

    while now() < deadline:
        sleep(interval_s)
        current = _size(path)

        if current is None:
            log.debug("%s vanished while waiting", path)
            return False

        if current == last and current > 0:
            stable += 1
            if stable >= checks:
                return True
        else:
            stable = 0

        last = current

    log.warning("%s never stopped changing within %ss", path, timeout_s)
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd client && python -m pytest tests/test_stability.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add client/clipwatch/stability.py client/tests/test_stability.py
git commit -m "feat: wait for captures to stop growing before processing"
```

---

### Task 6: Remux MKV to faststart MP4

**Files:**
- Create: `client/clipwatch/remux.py`
- Test: `client/tests/test_remux.py`

**Interfaces:**
- Consumes: nothing
- Produces: `KIND_FOR_SUFFIX: dict[str, str]`; `needs_remux(path: Path) -> bool`; `remux_to_mp4(src: Path, dest: Path, timeout_s: float = 300) -> bool`.

`plan.md` §5 and §10: a container swap with `-c copy`, never a re-encode, and `+faststart` is mandatory. These tests run for real against ffmpeg on macOS — no mocking — because the flags are the whole point.

- [ ] **Step 1: Write the failing test**

```python
# client/tests/test_remux.py
import json
import shutil
import subprocess
import pytest
from pathlib import Path
from clipwatch.remux import KIND_FOR_SUFFIX, needs_remux, remux_to_mp4

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


@pytest.fixture
def sample_mkv(tmp_path):
    out = tmp_path / "in.mkv"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc=size=320x240:rate=30", "-t", "2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
    ], check=True)
    return out


def probe(path):
    raw = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], capture_output=True, text=True, check=True).stdout
    return json.loads(raw)


def test_needs_remux_only_for_mkv():
    assert needs_remux(Path("a.mkv")) is True
    assert needs_remux(Path("a.MKV")) is True
    assert needs_remux(Path("a.mp4")) is False
    assert needs_remux(Path("a.png")) is False


def test_kind_mapping_covers_clips_and_screenshots():
    assert KIND_FOR_SUFFIX[".mkv"] == "clip"
    assert KIND_FOR_SUFFIX[".mp4"] == "clip"
    assert KIND_FOR_SUFFIX[".png"] == "screenshot"


def test_remux_produces_a_playable_mp4(tmp_path, sample_mkv):
    dest = tmp_path / "out.mp4"
    assert remux_to_mp4(sample_mkv, dest) is True
    assert dest.exists() and dest.stat().st_size > 0
    assert probe(dest)["format"]["format_name"].startswith("mov,mp4")


def test_remux_does_not_re_encode(tmp_path, sample_mkv):
    # The point of -c copy: the video stream must be bit-identical.
    dest = tmp_path / "out.mp4"
    remux_to_mp4(sample_mkv, dest)
    before = probe(sample_mkv)["streams"][0]
    after = probe(dest)["streams"][0]
    assert after["codec_name"] == before["codec_name"] == "h264"
    assert (after["width"], after["height"]) == (before["width"], before["height"])
    assert after["nb_frames"] == before["nb_frames"]


def test_remux_sets_faststart(tmp_path, sample_mkv):
    # plan.md §10: without +faststart, playback waits on a full download.
    # With it, the moov atom precedes the mdat payload.
    dest = tmp_path / "out.mp4"
    remux_to_mp4(sample_mkv, dest)
    head = dest.read_bytes()[:200_000]
    assert head.index(b"moov") < head.index(b"mdat")


def test_remux_returns_false_on_a_junk_input(tmp_path):
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"not a video")
    assert remux_to_mp4(junk, tmp_path / "out.mp4") is False


def test_remux_leaves_no_partial_output_on_failure(tmp_path):
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"not a video")
    dest = tmp_path / "out.mp4"
    remux_to_mp4(junk, dest)
    assert not dest.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd client && python -m pytest tests/test_remux.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipwatch.remux'`

- [ ] **Step 3: Write the implementation**

```python
# client/clipwatch/remux.py
"""MKV -> faststart MP4, without re-encoding.

OBS records MKV because it survives a crash; browsers cannot play MKV. The
conversion is a container swap (`-c copy`), so it is lossless and sub-second
on the 7900X — and it happens here rather than on the server, which must
never transcode (plan.md §5).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

KIND_FOR_SUFFIX = {
    ".mkv": "clip",
    ".mp4": "clip",
    ".mov": "clip",
    ".png": "screenshot",
    ".jpg": "screenshot",
    ".jpeg": "screenshot",
}


def needs_remux(path: Path) -> bool:
    return path.suffix.lower() == ".mkv"


def remux_to_mp4(src: Path, dest: Path, timeout_s: float = 300) -> bool:
    """Container swap only. Returns False on failure and leaves no partial file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(src),
                "-c", "copy",              # never re-encode
                "-movflags", "+faststart",  # plan.md §10, mandatory
                str(dest),
            ],
            capture_output=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.warning("remux failed to run for %s", src, exc_info=True)
        dest.unlink(missing_ok=True)
        return False

    if result.returncode != 0:
        log.warning("remux failed for %s: %s", src,
                    result.stderr.decode("utf-8", "replace")[:400])
        dest.unlink(missing_ok=True)
        return False

    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd client && python -m pytest tests/test_remux.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add client/clipwatch/remux.py client/tests/test_remux.py
git commit -m "feat: add lossless mkv to faststart mp4 remux"
```

---

### Task 7: Durable retry queue

**Files:**
- Create: `client/clipwatch/jobs.py`
- Test: `client/tests/test_jobs.py`

**Interfaces:**
- Consumes: `config.Config`
- Produces: `Job` dataclass (`capture_uuid, path, kind, game, game_exe, captured_at, attempts, next_attempt_at, source_path`); `JobQueue(queue_dir: Path, max_backoff_s: float)` with `.enqueue(...) -> Job`, `.ready(now: float) -> list[Job]`, `.all() -> list[Job]`, `.reschedule(job, now) -> Job`, `.done(job) -> None`; `backoff_for(attempts: int, max_backoff_s: float) -> float`.

`plan.md` §8.7 is the binding requirement: never lose a clip because the server was unreachable. The queue must therefore survive a reboot, which means it lives on disk, not in memory. **`capture_uuid` is minted here, once**, so every retry of the same capture carries the same key and the server's idempotency collapses them.

- [ ] **Step 1: Write the failing test**

```python
# client/tests/test_jobs.py
import json
import pytest
from clipwatch.jobs import Job, JobQueue, backoff_for


@pytest.fixture
def queue(tmp_path):
    return JobQueue(tmp_path / "queue", max_backoff_s=300)


def enqueue(queue, tmp_path, name="a.mp4", **over):
    path = tmp_path / name
    path.write_bytes(b"data")
    kwargs = dict(path=path, kind="clip", game="Halo", game_exe="halo.exe",
                  captured_at=1757260800, source_path=path)
    kwargs.update(over)
    return queue.enqueue(**kwargs)


def test_enqueue_mints_a_capture_uuid(queue, tmp_path):
    job = enqueue(queue, tmp_path)
    assert job.capture_uuid and len(job.capture_uuid) >= 32


def test_each_enqueue_gets_a_distinct_uuid(queue, tmp_path):
    a = enqueue(queue, tmp_path, "a.mp4")
    b = enqueue(queue, tmp_path, "b.mp4")
    assert a.capture_uuid != b.capture_uuid


def test_jobs_survive_a_restart(queue, tmp_path):
    job = enqueue(queue, tmp_path)
    reopened = JobQueue(queue.queue_dir, max_backoff_s=300)
    assert [j.capture_uuid for j in reopened.all()] == [job.capture_uuid]


def test_reschedule_keeps_the_same_uuid(queue, tmp_path):
    # The whole point: a retry must reuse the key so the server dedupes it.
    job = enqueue(queue, tmp_path)
    again = queue.reschedule(job, now=100)
    assert again.capture_uuid == job.capture_uuid
    assert again.attempts == 1


def test_reschedule_persists_across_a_restart(queue, tmp_path):
    job = queue.reschedule(enqueue(queue, tmp_path), now=100)
    reopened = JobQueue(queue.queue_dir, max_backoff_s=300)
    assert reopened.all()[0].attempts == job.attempts


def test_ready_excludes_jobs_still_in_backoff(queue, tmp_path):
    job = queue.reschedule(enqueue(queue, tmp_path), now=100)
    assert queue.ready(now=100) == []
    assert [j.capture_uuid for j in queue.ready(now=job.next_attempt_at)] == [job.capture_uuid]


def test_a_fresh_job_is_immediately_ready(queue, tmp_path):
    job = enqueue(queue, tmp_path)
    assert [j.capture_uuid for j in queue.ready(now=0)] == [job.capture_uuid]


def test_done_removes_the_job(queue, tmp_path):
    job = enqueue(queue, tmp_path)
    queue.done(job)
    assert queue.all() == []
    assert JobQueue(queue.queue_dir, max_backoff_s=300).all() == []


def test_backoff_grows_then_caps():
    assert backoff_for(1, 300) == 5
    assert backoff_for(2, 300) == 10
    assert backoff_for(3, 300) == 20
    assert backoff_for(20, 300) == 300  # capped


def test_ready_is_oldest_first(queue, tmp_path):
    a = enqueue(queue, tmp_path, "a.mp4", captured_at=100)
    b = enqueue(queue, tmp_path, "b.mp4", captured_at=200)
    assert [j.capture_uuid for j in queue.ready(now=0)] == [a.capture_uuid, b.capture_uuid]


def test_a_corrupt_job_file_is_skipped_not_fatal(queue, tmp_path):
    enqueue(queue, tmp_path)
    (queue.queue_dir / "broken.json").write_text("{ not json")
    assert len(queue.all()) == 1  # the good job still loads
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd client && python -m pytest tests/test_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipwatch.jobs'`

- [ ] **Step 3: Write the implementation**

```python
# client/clipwatch/jobs.py
"""Durable upload queue.

plan.md §8.7: never lose a clip because the server was unreachable. The PC
may reboot between the capture and a successful upload, so the queue is a
directory of JSON files rather than anything in memory — inspectable with a
text editor when something goes wrong.

capture_uuid is minted once, at enqueue, and carried through every retry.
That is what lets the server collapse duplicates (design §3); regenerating
it per attempt would defeat the entire idempotency design.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)

BASE_BACKOFF_S = 5.0


def backoff_for(attempts: int, max_backoff_s: float) -> float:
    """5s, 10s, 20s, 40s ... capped. Attempts are 1-based."""
    return min(BASE_BACKOFF_S * (2 ** max(0, attempts - 1)), max_backoff_s)


@dataclass(frozen=True)
class Job:
    capture_uuid: str
    path: str
    kind: str
    game: str | None
    game_exe: str | None
    captured_at: int
    attempts: int
    next_attempt_at: float
    source_path: str


class JobQueue:
    def __init__(self, queue_dir: Path, max_backoff_s: float) -> None:
        self.queue_dir = Path(queue_dir)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.max_backoff_s = max_backoff_s

    def _file(self, capture_uuid: str) -> Path:
        return self.queue_dir / f"{capture_uuid}.json"

    def _write(self, job: Job) -> None:
        """Atomic: a half-written job file must never be loadable."""
        target = self._file(job.capture_uuid)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(job), indent=2))
        os.replace(tmp, target)

    def enqueue(self, *, path: Path, kind: str, game: str | None,
                game_exe: str | None, captured_at: int, source_path: Path) -> Job:
        job = Job(
            capture_uuid=uuid.uuid4().hex,
            path=str(path),
            kind=kind,
            game=game,
            game_exe=game_exe,
            captured_at=captured_at,
            attempts=0,
            next_attempt_at=0.0,
            source_path=str(source_path),
        )
        self._write(job)
        return job

    def all(self) -> list[Job]:
        jobs: list[Job] = []
        for path in sorted(self.queue_dir.glob("*.json")):
            try:
                jobs.append(Job(**json.loads(path.read_text())))
            except (json.JSONDecodeError, TypeError, ValueError):
                # One unreadable file must not stall every other capture.
                log.warning("skipping unreadable job file %s", path)
        return sorted(jobs, key=lambda j: j.captured_at)

    def ready(self, now: float) -> list[Job]:
        return [j for j in self.all() if j.next_attempt_at <= now]

    def reschedule(self, job: Job, now: float) -> Job:
        attempts = job.attempts + 1
        updated = replace(
            job,
            attempts=attempts,
            next_attempt_at=now + backoff_for(attempts, self.max_backoff_s),
        )
        self._write(updated)
        return updated

    def done(self, job: Job) -> None:
        self._file(job.capture_uuid).unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd client && python -m pytest tests/test_jobs.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add client/clipwatch/jobs.py client/tests/test_jobs.py
git commit -m "feat: add durable retry queue with stable capture uuids"
```

---

### Task 8: Uploader

**Files:**
- Create: `client/clipwatch/uploader.py`
- Test: `client/tests/test_uploader.py`

**Interfaces:**
- Consumes: `config.Config`, `jobs.Job`
- Produces: `UploadResult` frozen dataclass (`ok: bool`, `url: str | None`, `clip_id: str | None`, `retryable: bool`); `upload(cfg: Config, job: Job, client=None) -> UploadResult`.

Maps the server's contract (design §6) onto the queue's retry policy. The distinction that matters: a **4xx is permanent** (a malformed request will fail identically forever, so retrying burns the disk), while a **5xx or a transport error is retryable**.

- [ ] **Step 1: Write the failing test**

```python
# client/tests/test_uploader.py
import httpx
import pytest
from pathlib import Path
from clipwatch.config import Config
from clipwatch.jobs import Job
from clipwatch.uploader import UploadResult, upload


@pytest.fixture
def cfg(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text(f'server_url = "http://midget:8000"\nwatch_dir = "{tmp_path.as_posix()}"\n'
                    'source_host = "desktop-amtr56i"\n')
    return Config.load(toml, {"CLIPD_TOKEN": "secret-token"})


@pytest.fixture
def job(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"videodata")
    return Job(capture_uuid="uuid-1", path=str(f), kind="clip", game="Counter-Strike 2",
               game_exe="cs2.exe", captured_at=1757260800, attempts=0,
               next_attempt_at=0.0, source_path=str(f))


def transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_successful_upload_returns_the_url(cfg, job):
    def handler(request):
        return httpx.Response(200, json={"id": "aB3xY9z", "url": "http://midget:8000/v/aB3xY9z"})

    result = upload(cfg, job, client=transport(handler))
    assert result == UploadResult(ok=True, url="http://midget:8000/v/aB3xY9z",
                                  clip_id="aB3xY9z", retryable=False)


def test_request_carries_bearer_token_and_metadata(cfg, job):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "x", "url": "u"})

    upload(cfg, job, client=transport(handler))
    assert seen["auth"] == "Bearer secret-token"
    body = seen["body"]
    assert b'"capture_uuid": "uuid-1"' in body or b'"capture_uuid":"uuid-1"' in body
    assert b"videodata" in body


def test_duplicate_response_is_still_success(cfg, job):
    # The server dedupes on capture_uuid; a duplicate means it is safely stored.
    def handler(request):
        return httpx.Response(200, json={"id": "aB3xY9z", "url": "u", "duplicate": True})

    assert upload(cfg, job, client=transport(handler)).ok is True


def test_server_error_is_retryable(cfg, job):
    def handler(request):
        return httpx.Response(503, text="down")

    result = upload(cfg, job, client=transport(handler))
    assert result.ok is False and result.retryable is True


def test_transport_failure_is_retryable(cfg, job):
    def handler(request):
        raise httpx.ConnectError("tailnet down")

    result = upload(cfg, job, client=transport(handler))
    assert result.ok is False and result.retryable is True


@pytest.mark.parametrize("status", [400, 401, 413, 422])
def test_client_errors_are_not_retryable(cfg, job, status):
    # Retrying a rejected request forever would just fill the disk.
    def handler(request):
        return httpx.Response(status, json={"detail": "nope"})

    result = upload(cfg, job, client=transport(handler))
    assert result.ok is False and result.retryable is False


def test_missing_file_is_not_retryable(cfg, job, tmp_path):
    Path(job.path).unlink()

    def handler(request):
        raise AssertionError("must not attempt a request for a missing file")

    result = upload(cfg, job, client=transport(handler))
    assert result.ok is False and result.retryable is False


def test_malformed_success_body_is_retryable(cfg, job):
    def handler(request):
        return httpx.Response(200, text="not json")

    result = upload(cfg, job, client=transport(handler))
    assert result.ok is False and result.retryable is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd client && python -m pytest tests/test_uploader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipwatch.uploader'`

- [ ] **Step 3: Write the implementation**

```python
# client/clipwatch/uploader.py
"""POST a queued capture to clipd's /ingest.

Retry policy: a 4xx will fail identically forever, so it is permanent and the
job is dropped. A 5xx or a transport error means the server or the tailnet is
having a moment, so the job stays queued — that is plan.md §8.7's "never lose
a clip because the server was unreachable".
"""
from __future__ import annotations

import json
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Config
from .jobs import Job

log = logging.getLogger(__name__)

TIMEOUT_S = 120.0  # a several-hundred-MB clip over the tailnet


@dataclass(frozen=True)
class UploadResult:
    ok: bool
    url: str | None
    clip_id: str | None
    retryable: bool


def _failure(retryable: bool) -> UploadResult:
    return UploadResult(ok=False, url=None, clip_id=None, retryable=retryable)


def upload(cfg: Config, job: Job, client: httpx.Client | None = None) -> UploadResult:
    path = Path(job.path)
    if not path.exists():
        log.error("queued file is gone: %s", path)
        return _failure(retryable=False)

    meta = {
        "kind": job.kind,
        "capture_uuid": job.capture_uuid,
        "game": job.game,
        "game_exe": job.game_exe,
        "source_host": cfg.source_host,
        "captured_at": job.captured_at,
    }
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        with path.open("rb") as handle:
            response = client.post(
                f"{cfg.server_url}/ingest",
                headers={"Authorization": f"Bearer {cfg.ingest_token}"},
                files={"file": (path.name, handle, content_type)},
                data={"meta": json.dumps(meta)},
            )
    except (httpx.HTTPError, OSError):
        log.warning("upload transport failure for %s", job.capture_uuid, exc_info=True)
        return _failure(retryable=True)
    finally:
        if owned:
            client.close()

    if response.status_code == 200:
        try:
            payload = response.json()
            return UploadResult(ok=True, url=payload["url"],
                                clip_id=payload["id"], retryable=False)
        except (ValueError, KeyError):
            log.warning("unparseable 200 from /ingest for %s", job.capture_uuid)
            return _failure(retryable=True)

    retryable = response.status_code >= 500
    log.warning("ingest returned %s for %s (retryable=%s)",
                response.status_code, job.capture_uuid, retryable)
    return _failure(retryable=retryable)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd client && python -m pytest tests/test_uploader.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add client/clipwatch/uploader.py client/tests/test_uploader.py
git commit -m "feat: add ingest uploader with retryable error classification"
```

---

### Task 9: Pipeline and daemon

**Files:**
- Create: `client/clipwatch/pipeline.py`, `client/clipwatch/daemon.py`, `client/clipwatch/main.py`
- Test: `client/tests/test_pipeline.py`

**Interfaces:**
- Consumes: everything above
- Produces: `prepare_capture(cfg, path, buffer, mapping, adapter, now) -> Job | None`; `drain(cfg, queue, adapter, now, upload_fn=upload) -> int`; `Daemon(cfg, adapter, mapping)` with `.start()`, `.stop()`; `main(argv=None) -> int`.

This is where "delete the local file only after a confirmed 200" is enforced, and where the clipboard gets written.

- [ ] **Step 1: Write the failing test**

```python
# client/tests/test_pipeline.py
import pytest
from pathlib import Path
from clipwatch.config import Config
from clipwatch.detector import ExeRingBuffer
from clipwatch.jobs import JobQueue
from clipwatch.platform import NullAdapter
from clipwatch.pipeline import drain, prepare_capture
from clipwatch.uploader import UploadResult


@pytest.fixture
def cfg(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text(f'server_url = "http://midget:8000"\nwatch_dir = "{tmp_path.as_posix()}"\n'
                    'source_host = "desktop-amtr56i"\nstability_checks = 1\n'
                    'stability_interval_s = 0.001\n')
    return Config.load(toml, {"CLIPD_TOKEN": "t"})


@pytest.fixture
def buffer():
    buf = ExeRingBuffer(window_s=90)
    for t in range(0, 60, 5):
        buf.record("cs2.exe", t, title="Counter-Strike 2")
    return buf


MAPPING = {"cs2.exe": "Counter-Strike 2"}


def make_mp4(tmp_path, name="replay.mp4"):
    f = tmp_path / name
    f.write_bytes(b"videodata")
    return f


def test_prepare_resolves_the_game_from_the_ring_buffer(cfg, tmp_path, buffer):
    job = prepare_capture(cfg, make_mp4(tmp_path), buffer, MAPPING, NullAdapter(), now=60)
    assert job.game == "Counter-Strike 2"
    assert job.game_exe == "cs2.exe"
    assert job.kind == "clip"


def test_prepare_marks_screenshots_as_screenshots(cfg, tmp_path, buffer):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"pngdata")
    job = prepare_capture(cfg, shot, buffer, MAPPING, NullAdapter(), now=60)
    assert job.kind == "screenshot"


def test_prepare_falls_back_to_unknown_with_no_samples(cfg, tmp_path):
    job = prepare_capture(cfg, make_mp4(tmp_path), ExeRingBuffer(90), MAPPING,
                          NullAdapter(), now=0)
    assert job.game == "Unknown"


def test_prepare_ignores_our_own_working_files(cfg, tmp_path, buffer):
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    internal = cfg.work_dir / "already.mp4"
    internal.write_bytes(b"x")
    assert prepare_capture(cfg, internal, buffer, MAPPING, NullAdapter(), now=60) is None


def test_prepare_ignores_unknown_extensions(cfg, tmp_path, buffer):
    other = tmp_path / "notes.txt"
    other.write_bytes(b"x")
    assert prepare_capture(cfg, other, buffer, MAPPING, NullAdapter(), now=60) is None


def test_drain_deletes_the_local_file_only_after_a_200(cfg, tmp_path, buffer):
    queue = JobQueue(cfg.queue_dir, cfg.max_backoff_s)
    source = make_mp4(tmp_path)
    job = prepare_capture(cfg, source, buffer, MAPPING, NullAdapter(), now=60)
    queue._write(job)

    def ok(config, j, client=None):
        return UploadResult(True, "http://midget:8000/v/abc", "abc", False)

    adapter = NullAdapter()
    assert drain(cfg, queue, adapter, now=0, upload_fn=ok) == 1
    assert queue.all() == []
    assert not Path(job.path).exists()
    assert adapter.clipboard == "http://midget:8000/v/abc"


def test_drain_keeps_the_file_when_the_upload_is_retryable(cfg, tmp_path, buffer):
    queue = JobQueue(cfg.queue_dir, cfg.max_backoff_s)
    job = prepare_capture(cfg, make_mp4(tmp_path), buffer, MAPPING, NullAdapter(), now=60)
    queue._write(job)

    def down(config, j, client=None):
        return UploadResult(False, None, None, retryable=True)

    assert drain(cfg, queue, NullAdapter(), now=0, upload_fn=down) == 0
    assert Path(job.path).exists()           # never lose a capture
    assert queue.all()[0].attempts == 1      # and it is scheduled to retry


def test_drain_drops_a_permanently_rejected_job(cfg, tmp_path, buffer):
    queue = JobQueue(cfg.queue_dir, cfg.max_backoff_s)
    job = prepare_capture(cfg, make_mp4(tmp_path), buffer, MAPPING, NullAdapter(), now=60)
    queue._write(job)

    def rejected(config, j, client=None):
        return UploadResult(False, None, None, retryable=False)

    assert drain(cfg, queue, NullAdapter(), now=0, upload_fn=rejected) == 0
    assert queue.all() == []


def test_drain_skips_jobs_still_in_backoff(cfg, tmp_path, buffer):
    queue = JobQueue(cfg.queue_dir, cfg.max_backoff_s)
    job = prepare_capture(cfg, make_mp4(tmp_path), buffer, MAPPING, NullAdapter(), now=60)
    queue.reschedule(queue._write(job) or job, now=1000)

    def explode(config, j, client=None):
        raise AssertionError("must not upload a job still in backoff")

    assert drain(cfg, queue, NullAdapter(), now=1000, upload_fn=explode) == 0


def test_drain_notifies_on_success(cfg, tmp_path, buffer):
    queue = JobQueue(cfg.queue_dir, cfg.max_backoff_s)
    job = prepare_capture(cfg, make_mp4(tmp_path), buffer, MAPPING, NullAdapter(), now=60)
    queue._write(job)
    adapter = NullAdapter()

    drain(cfg, queue, adapter, now=0,
          upload_fn=lambda c, j, client=None: UploadResult(True, "u", "id", False))
    assert adapter.notifications and "Counter-Strike 2" in adapter.notifications[0][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd client && python -m pytest tests/test_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipwatch.pipeline'`

- [ ] **Step 3: Write the pipeline**

```python
# client/clipwatch/pipeline.py
"""One capture, end to end: settle, remux, enqueue, upload, clean up."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import Config
from .detector import ExeRingBuffer
from .games import resolve_game
from .jobs import Job, JobQueue
from .remux import KIND_FOR_SUFFIX, needs_remux, remux_to_mp4
from .uploader import upload

log = logging.getLogger(__name__)

STABILITY_TIMEOUT_S = 120.0


def prepare_capture(
    cfg: Config, path: Path, buffer: ExeRingBuffer, mapping: dict[str, str],
    adapter, now: float,
) -> Job | None:
    """Settle, remux if needed, and enqueue. None if the file is not ours."""
    from .stability import wait_until_stable

    path = Path(path)

    # Our own remux output lands in work_dir; re-ingesting it would loop.
    if cfg.work_dir in path.parents or cfg.queue_dir in path.parents:
        return None

    kind = KIND_FOR_SUFFIX.get(path.suffix.lower())
    if kind is None:
        return None

    if not wait_until_stable(path, checks=cfg.stability_checks,
                             interval_s=cfg.stability_interval_s,
                             timeout_s=STABILITY_TIMEOUT_S):
        log.warning("giving up on unstable file %s", path)
        return None

    exe = buffer.dominant(cfg.ignore_exes, now)
    game = resolve_game(exe, mapping, buffer.titles.get(exe or ""))

    upload_path = path
    if needs_remux(path):
        upload_path = cfg.work_dir / f"{path.stem}.mp4"
        if not remux_to_mp4(path, upload_path):
            log.error("remux failed, not enqueuing %s", path)
            return None

    queue = JobQueue(cfg.queue_dir, cfg.max_backoff_s)
    job = queue.enqueue(
        path=upload_path, kind=kind, game=game, game_exe=exe,
        captured_at=int(path.stat().st_mtime), source_path=path,
    )
    log.info("queued %s as %s (%s)", path.name, job.capture_uuid, game)
    return job


def drain(cfg: Config, queue: JobQueue, adapter, now: float, upload_fn=upload) -> int:
    """Attempt every ready job. Returns how many succeeded."""
    succeeded = 0

    for job in queue.ready(now):
        result = upload_fn(cfg, job)

        if result.ok:
            # plan.md §8.7: only now is it safe to delete anything local.
            Path(job.path).unlink(missing_ok=True)
            if job.source_path != job.path:
                Path(job.source_path).unlink(missing_ok=True)
            queue.done(job)

            adapter.set_clipboard(result.url or "")
            adapter.notify(job.game or "Unknown", f"uploaded · {result.url}")
            log.info("uploaded %s -> %s", job.capture_uuid, result.url)
            succeeded += 1

        elif result.retryable:
            updated = queue.reschedule(job, now)
            log.warning("retry %s in %.0fs (attempt %d)", job.capture_uuid,
                        updated.next_attempt_at - now, updated.attempts)

        else:
            # Permanent rejection. Keep the file, drop the job: retrying a 4xx
            # forever would fill the disk, but the capture is still on disk to
            # inspect or re-submit by hand.
            log.error("dropping permanently rejected job %s (file kept at %s)",
                      job.capture_uuid, job.path)
            queue.done(job)

    return succeeded
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd client && python -m pytest tests/test_pipeline.py -v`
Expected: 10 passed

- [ ] **Step 5: Write the daemon and entrypoint**

```python
# client/clipwatch/daemon.py
"""Wire the pieces together: watch, sample, drain."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config
from .detector import ExeRingBuffer, record_sample
from .jobs import JobQueue
from .pipeline import drain, prepare_capture

log = logging.getLogger(__name__)

DRAIN_INTERVAL_S = 5.0


class _CaptureHandler(FileSystemEventHandler):
    def __init__(self, daemon: "Daemon") -> None:
        self.daemon = daemon

    def on_created(self, event):
        if not event.is_directory:
            self.daemon.handle_capture(Path(event.src_path))

    def on_moved(self, event):
        # OBS renames its temp file into place on some configurations.
        if not event.is_directory:
            self.daemon.handle_capture(Path(event.dest_path))


class Daemon:
    def __init__(self, cfg: Config, adapter, mapping: dict[str, str]) -> None:
        self.cfg = cfg
        self.adapter = adapter
        self.mapping = mapping
        self.buffer = ExeRingBuffer(cfg.detect_window_s)
        self.queue = JobQueue(cfg.queue_dir, cfg.max_backoff_s)
        self._stop = threading.Event()
        self._observer = Observer()
        self._threads: list[threading.Thread] = []

    def handle_capture(self, path: Path) -> None:
        try:
            if prepare_capture(self.cfg, path, self.buffer, self.mapping,
                               self.adapter, time.monotonic()):
                drain(self.cfg, self.queue, self.adapter, time.monotonic())
        except Exception:
            log.exception("failed to handle capture %s", path)

    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            try:
                record_sample(self.buffer, self.adapter, time.monotonic())
            except Exception:
                log.debug("sampler hiccup", exc_info=True)
            self._stop.wait(self.cfg.sample_interval_s)

    def _drain_loop(self) -> None:
        # Also picks up anything left queued by a previous run.
        while not self._stop.is_set():
            try:
                drain(self.cfg, self.queue, self.adapter, time.monotonic())
            except Exception:
                log.exception("drain failed; will retry")
            self._stop.wait(DRAIN_INTERVAL_S)

    def start(self) -> None:
        self.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.queue_dir.mkdir(parents=True, exist_ok=True)

        self._observer.schedule(_CaptureHandler(self), str(self.cfg.watch_dir),
                                recursive=False)
        self._observer.start()

        for target in (self._sample_loop, self._drain_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

        log.info("watching %s, uploading to %s", self.cfg.watch_dir, self.cfg.server_url)

    def stop(self) -> None:
        self._stop.set()
        self._observer.stop()
        self._observer.join(timeout=5)
        for thread in self._threads:
            thread.join(timeout=5)
```

```python
# client/clipwatch/main.py
"""Entrypoint."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from .config import Config
from .daemon import Daemon
from .games import load_games
from .platform import get_adapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipwatch")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--games", type=Path, default=Path("games.toml"))
    parser.add_argument("--adapter", default=None,
                        help="force a platform adapter: windows | null")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        cfg = Config.load(args.config, os.environ)
    except (KeyError, ValueError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    daemon = Daemon(cfg, get_adapter(args.adapter), load_games(args.games))
    daemon.start()

    stopping = signal.SIGTERM
    signal.signal(signal.SIGINT, lambda *_: daemon.stop())
    signal.signal(stopping, lambda *_: daemon.stop())

    try:
        while any(t.is_alive() for t in daemon._threads):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run the full suite**

Run: `cd client && python -m pytest -v`
Expected: all tests pass across every test file

- [ ] **Step 7: Commit**

```bash
git add client/clipwatch/pipeline.py client/clipwatch/daemon.py client/clipwatch/main.py client/tests/test_pipeline.py
git commit -m "feat: wire watcher pipeline and daemon"
```

---

### Task 10: Package, deploy to the gaming PC, verify end to end

**Files:**
- Create: `client/clipwatch.spec`, `client/README.md`
- Modify: `README.md` (root) — point at the client

**Interfaces:**
- Consumes: everything above
- Produces: a running watcher on `desktop-amtr56i`

Requires SSH to the gaming PC (see Prerequisite) and a reachable clipd. Until Step 1 is deployed to midget, point `server_url` at a clipd running locally.

- [ ] **Step 1: Write the PyInstaller spec**

```python
# client/clipwatch.spec
# Build on Windows: pyinstaller clipwatch.spec
# PyInstaller does not cross-compile — this cannot be built from macOS.
a = Analysis(
    ["clipwatch/main.py"],
    pathex=[],
    binaries=[],
    datas=[("games.toml", "."), ("config.example.toml", ".")],
    hiddenimports=["win32clipboard", "win32gui", "win32process", "win32con"],
    hookspath=[],
    excludes=[],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="clipwatch",
    console=False,      # runs in the background; the clipboard is the signal
    upx=False,
)
```

- [ ] **Step 2: Write the client README**

````markdown
# clipwatch — the clipd capture client

Watches the OBS output folder, remuxes MKV to faststart MP4, uploads to clipd,
and puts the URL on the clipboard.

## OBS setup
- Settings → Output → Replay Buffer: **enabled**, ~90 s
- Encoder **NVENC H.264**, `yuv420p`, recording format **MKV**
- Settings → Output → Recording Path: the folder in `config.toml` `watch_dir`
- Settings → Hotkeys → **Save Replay Buffer**

MKV is deliberate: it survives a crash where an MP4 would be unplayable. The
watcher converts it with `-c copy`, so nothing is re-encoded.

## Install
```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy config.example.toml config.toml   # then edit watch_dir
setx CLIPD_TOKEN "<the server's INGEST_TOKEN>"
```

## Run
```powershell
.venv\Scripts\python -m clipwatch.main --config config.toml --games games.toml
```

## Build the exe
```powershell
.venv\Scripts\pyinstaller clipwatch.spec
```

## Run at login
Task Scheduler → Create Task → Trigger "At log on" → Action: `clipwatch.exe`,
Start in: the install folder.
````

- [ ] **Step 3: Verify the suite and the no-pywin32 rule on macOS**

```bash
cd client && python -m pytest -q
# Nothing outside platform/windows.py may import win32*:
! grep -rn "^import win32\|^from win32\|import pywin32" clipwatch/ --include=*.py \
  | grep -v "clipwatch/platform/windows.py"
```

Expected: all tests pass, and the grep finds nothing outside `platform/windows.py`.

- [ ] **Step 4: Commit**

```bash
git add client/clipwatch.spec client/README.md README.md
git commit -m "feat: package clipwatch and document obs setup"
```

- [ ] **Step 5: Copy to the gaming PC**

```bash
ssh <user>@100.66.55.18 'mkdir -p C:/clipwatch'
rsync -av --exclude .venv --exclude __pycache__ client/ <user>@100.66.55.18:C:/clipwatch/
```

- [ ] **Step 6: Install and run there**

```bash
ssh <user>@100.66.55.18 'cd C:/clipwatch && py -3.12 -m venv .venv && .venv/Scripts/pip install -e ".[dev]"'
ssh <user>@100.66.55.18 'cd C:/clipwatch && .venv/Scripts/python -m pytest -q'
```

Expected: the same test count as on macOS, now including the Windows adapter
selection path.

- [ ] **Step 7: Verify the Windows adapter for real**

This is the part that cannot be tested from macOS. With a game in the
foreground:

```bash
ssh <user>@100.66.55.18 'cd C:/clipwatch && .venv/Scripts/python -c "
from clipwatch.platform import get_adapter
a = get_adapter()
print(type(a).__name__, a.foreground())
print(\"clipboard:\", a.set_clipboard(\"http://midget:8000/v/test\"))
print(\"toast:\", a.notify(\"clipd\", \"hello\"))
"'
```

Expected: `WindowsAdapter`, a real `(exe, title)` pair for the foreground
window, `clipboard: True`, and a visible toast.

- [ ] **Step 8: End-to-end capture**

Start the daemon, press the OBS replay hotkey while a game is focused, then:

Expected, in order — the MKV appears in `watch_dir`; the log shows `queued ...`
with the correct game name; an MP4 appears in `.clipwatch/work`; the log shows
`uploaded ... -> http://.../v/<id>`; the URL is on the clipboard; a toast fires;
both local files are gone; the queue directory is empty; and the clip is listed
by clipd's `/healthz`.

- [ ] **Step 9: Verify the retry path**

Stop clipd (or disconnect the tailnet), capture a clip, and confirm: the job
file stays in `.clipwatch/queue`, the local MP4 is **not** deleted, and the log
shows increasing backoff. Restart clipd and confirm the queued job uploads on
the next drain with the **same** `capture_uuid`, and that the server reports it
once — not twice.

- [ ] **Step 10: Commit any fixes and open the PR**

```bash
git add -A client
git commit -m "fix: address issues found during on-device verification"
```

---

## What this plan does not cover

- **Step 3 — the web app.** Gallery, `/v/<id>`, trim, share pages.
- **Step 4 — homelab integration.** Homepage tile, Uptime Kuma monitor.
- **A tray icon or GUI.** The clipboard and the toast are the entire interface.
- **Screenshot hotkeys in OBS.** Documented in the client README; no code needed,
  since `.png` files in `watch_dir` already take the screenshot path.

## Self-review notes

- **Spec coverage:** design §5 detection → Tasks 3, 4 (sampling) and 2 (resolution).
  `plan.md` §8.1 watch → Task 9. §8.2 settle → Task 5. §8.3 remux → Task 6.
  §8.4 game metadata → Tasks 2, 3. §8.5 POST → Task 8. §8.6 clipboard + toast →
  Tasks 4, 9. §8.7 queue, retry, delete-after-200 → Tasks 7, 9. Screenshots →
  Task 6 (`KIND_FOR_SUFFIX`) and Task 9. OBS setup → Task 10.
- **Placeholder scan:** clean — every code step carries real code.
- **Type consistency:** `Job` field names are identical across Tasks 7, 8 and 9;
  `UploadResult` matches between Tasks 8 and 9; `PlatformAdapter`'s three methods
  are used exactly as declared in Tasks 3, 4 and 9; `Config` field names in Task 1
  match every later use.
- **The macOS constraint is structural, not incidental:** `platform/windows.py` is
  imported lazily inside `get_adapter`, and Task 10 Step 3 enforces the rule with
  a grep, so the suite runs identically on both machines.
- **Deliberate choice on permanent rejection** (Task 9): a non-retryable failure
  drops the *job* but keeps the *file*. Deleting a capture the server refused
  would violate `plan.md` §8.7's spirit; retrying it forever would fill the disk.
