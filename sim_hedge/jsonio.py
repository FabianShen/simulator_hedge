"""Shared JSON persistence and strict protocol-value parsing helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import json
from numbers import Integral
import os
from pathlib import Path
from typing import Any


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(
    path: str | Path,
    payload: Any,
    *,
    default: Callable[[Any], Any] | None = None,
    ensure_ascii: bool = True,
    trailing_newline: bool = True,
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    serialized = json.dumps(
        payload,
        default=default,
        ensure_ascii=ensure_ascii,
        indent=2,
    )
    if trailing_newline:
        serialized += "\n"
    try:
        temporary.write_text(serialized, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def require_object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} is not an object")
    return value


def require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def parse_iso_utc(value: str, name: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return result


def coerce_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be an integer")
    return int(value)


def normalize_positions(values: Mapping[Any, Any], name: str) -> dict[str, int]:
    positions: dict[str, int] = {}
    for instrument, value in values.items():
        quantity = coerce_int(value, f"{name} {instrument}")
        if quantity:
            positions[str(instrument)] = quantity
    return dict(sorted(positions.items()))
