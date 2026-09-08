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


def test_resolve_returns_unknown_for_an_unmapped_exe_with_no_title(mapping):
    # design §5 stops at Unknown rather than inventing a name from the exe:
    # Unknown is the re-tagging queue, and game_exe carries the exe anyway.
    assert resolve_game("DeepRockGalactic.exe", mapping) == "Unknown"
