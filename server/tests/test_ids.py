from clipd.ids import ALPHABET, new_id, new_public_slug


def test_new_id_is_seven_base62_chars():
    value = new_id()
    assert len(value) == 7
    assert all(c in ALPHABET for c in value)


def test_new_id_is_not_constant():
    assert len({new_id() for _ in range(200)}) > 190


def test_public_slug_has_at_least_16_chars_of_entropy():
    # Spec §7 of plan.md requires >= 16 chars of entropy for unguessable share links.
    value = new_public_slug()
    assert len(value) >= 16
    assert all(c in ALPHABET for c in value)
