"""Publish the latest live risk state without owning hedge policy."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


def write_live_risk_snapshot(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Atomically publish the latest state for another process to consume."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
