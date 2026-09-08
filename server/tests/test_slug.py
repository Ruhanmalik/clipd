import string

import pytest
from clipd.slug import slugify_game


@pytest.mark.parametrize("raw,expected", [
    ("Counter-Strike 2", "counter-strike-2"),
    ("VALORANT  ", "valorant"),
    ("EA SPORTS FC 25", "ea-sports-fc-25"),
    ("Rock/Paper", "rock-paper"),
    ("Deep   Rock  Galactic", "deep-rock-galactic"),
    ("Game....", "game"),
    ("  ---Halo---  ", "halo"),
])
def test_slugify_normalizes_names(raw, expected):
    assert slugify_game(raw) == expected


@pytest.mark.parametrize("raw", ['A<B', 'A>B', 'A:B', 'A"B', 'A/B', "A\\B", "A|B", "A?B", "A*B"])
def test_slugify_strips_windows_illegal_characters(raw):
    # Assert the actual contract: the output is drawn from [a-z0-9-] only.
    # Asserting merely "no illegal characters" cannot fail, because the
    # non-alphanumeric collapse already guarantees it.
    result = slugify_game(raw)
    assert set(result) <= set(string.ascii_lowercase + string.digits + "-")
    assert result == "a-b"


@pytest.mark.parametrize("reserved", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT9"])
def test_slugify_escapes_windows_reserved_names(reserved):
    # A folder literally named CON cannot be created on Windows, and the watcher
    # may stage paths locally before upload.
    assert slugify_game(reserved) == f"{reserved.lower()}-game"


@pytest.mark.parametrize("raw", [None, "", "   ", "???", "\x00\x01"])
def test_slugify_falls_back_to_unknown(raw):
    assert slugify_game(raw) == "unknown"


def test_slugify_is_idempotent():
    once = slugify_game("Counter-Strike 2")
    assert slugify_game(once) == once
