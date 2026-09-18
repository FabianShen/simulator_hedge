"""Small environment-file loader for local command-line use."""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(start: str | Path | None = None) -> Path | None:
    """Load the nearest ``.env`` without replacing existing environment values."""

    path = _find_env(Path(start) if start is not None else Path.cwd())
    if path is None:
        return None
    for line_number, original in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid .env line {line_number} in {path}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            raise ValueError(f"invalid .env key on line {line_number} in {path}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return path


def _find_env(start: Path) -> Path | None:
    current = start.resolve()
    if current.is_file():
        return current
    for directory in (current, *current.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None
