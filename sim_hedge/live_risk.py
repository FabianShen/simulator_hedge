"""Publish the latest live risk state without owning hedge policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from sim_hedge.jsonio import write_json


def write_live_risk_snapshot(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Atomically publish the latest state for another process to consume."""

    write_json(path, payload)
