"""Resolve operator-owned secret references without exposing their values."""

import os


def read_secret(environment, path=None):
    if path is None:
        return os.environ.get(environment or "")
    try:
        with path.open(encoding="utf-8") as file:
            value = file.read(16_385)
    except (OSError, UnicodeError):
        return None
    return value.strip() if len(value) <= 16_384 else None
