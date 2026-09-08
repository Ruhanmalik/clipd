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
