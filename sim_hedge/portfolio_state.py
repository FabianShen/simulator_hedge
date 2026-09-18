"""Thread-safe current portfolio state rebuilt from absolute snapshots."""

from __future__ import annotations

from threading import Lock

from sim_hedge.portfolio import PortfolioSnapshot


class PortfolioState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._snapshot: PortfolioSnapshot | None = None
        self._revision = 0

    def replace(self, snapshot: PortfolioSnapshot) -> int:
        """Replace all local facts; simulator snapshots are not deltas."""

        with self._lock:
            self._snapshot = snapshot
            self._revision += 1
            return self._revision

    def snapshot(self) -> PortfolioSnapshot | None:
        with self._lock:
            return self._snapshot

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

