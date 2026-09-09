"""Pure display helpers. No clock, no database, no I/O."""
import pytest
from clipd import presenters


@pytest.mark.parametrize("seconds,expected", [
    (None, ""),
    (0, ""),
    (7.4, "0:07"),
    (60, "1:00"),
    (90.6, "1:30"),
    (599, "9:59"),
    (3600, "1:00:00"),
    (3725, "1:02:05"),
])
def test_human_duration(seconds, expected):
    assert presenters.human_duration(seconds) == expected


def test_display_title_prefers_a_custom_title(make_clip):
    assert presenters.display_title(make_clip()) == "Halo"
    assert presenters.display_title(make_clip(title="the 1v5")) == "the 1v5"


def test_display_title_falls_back_to_unknown_when_the_game_is_missing(make_clip):
    clip = make_clip("a", created_at=1757260800, game=None)
    assert presenters.display_title(clip) == "Unknown"


@pytest.mark.parametrize("delta,expected", [
    (0, "just now"),
    (45, "just now"),
    (60, "1 minute ago"),
    (120, "2 minutes ago"),
    (3600, "1 hour ago"),
    (7200, "2 hours ago"),
    (86400, "yesterday"),
    (172800, "2 days ago"),
    (2592000, "30 days ago"),
])
def test_relative_time(delta, expected):
    now = 1757260800
    assert presenters.relative_time(now - delta, now) == expected


def test_relative_time_does_not_report_the_future_as_ago():
    """A capture whose clock ran ahead must not read '-3 minutes ago'.

    /ingest tolerates a day of skew (app.py CLOCK_SKEW_S), so rows genuinely
    can carry a created_at in the future.
    """
    now = 1757260800
    assert presenters.relative_time(now + 300, now) == "just now"


def test_human_bytes_is_reexported():
    assert presenters.human_bytes(1024) == "1.0 KB"
