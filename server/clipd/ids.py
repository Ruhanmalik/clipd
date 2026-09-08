"""URL-safe random identifiers."""
from __future__ import annotations

import secrets

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

ID_LENGTH = 7          # ~62^7 = 3.5e12, ample for a personal store
PUBLIC_SLUG_LENGTH = 22  # ~131 bits; plan.md §7 requires >= 16 chars of entropy


def _random_string(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def new_id() -> str:
    return _random_string(ID_LENGTH)


def new_public_slug() -> str:
    return _random_string(PUBLIC_SLUG_LENGTH)
