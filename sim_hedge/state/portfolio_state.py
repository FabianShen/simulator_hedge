"""Thread-safe current portfolio state rebuilt from absolute snapshots."""

from __future__ import annotations

from threading import Lock

from sim_hedge.portfolio import PortfolioSnapshot, PositionSnapshot


class PortfolioState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._snapshot: PortfolioSnapshot | None = None
        self._revision = 0
        self._synchronized = False

    def replace(
        self, snapshot: PortfolioSnapshot, *, synchronized: bool = False
    ) -> int:
        """Replace all local facts; simulator snapshots are not deltas."""

        with self._lock:
            self._snapshot = snapshot
            self._synchronized = synchronized
            self._revision += 1
            return self._revision

    def upsert_position(
        self,
        position: PositionSnapshot,
        business_version: str | None,
    ) -> bool:
        with self._lock:
            current = self._require_snapshot()
            if not _is_newer(business_version, current.business_version):
                return False
            positions = {
                item.position_id: item for item in current.positions
            }
            positions[position.position_id] = position
            self._snapshot = PortfolioSnapshot(
                account=current.account,
                positions=tuple(sorted(positions.values(), key=lambda item: item.position_id)),
                active_orders=current.active_orders,
                business_version=business_version or current.business_version,
            )
            self._revision += 1
            return True

    def remove_position(
        self, position_id: str, business_version: str | None
    ) -> bool:
        with self._lock:
            current = self._require_snapshot()
            if not _is_newer(business_version, current.business_version):
                return False
            positions = tuple(
                item for item in current.positions if item.position_id != position_id
            )
            self._snapshot = PortfolioSnapshot(
                account=current.account,
                positions=positions,
                active_orders=current.active_orders,
                business_version=business_version or current.business_version,
            )
            self._revision += 1
            return True

    def mark_unsynchronized(self) -> None:
        with self._lock:
            if self._synchronized:
                self._synchronized = False
                self._revision += 1

    def snapshot(self) -> PortfolioSnapshot | None:
        with self._lock:
            return self._snapshot

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def synchronized(self) -> bool:
        with self._lock:
            return self._synchronized

    def _require_snapshot(self) -> PortfolioSnapshot:
        if self._snapshot is None:
            raise RuntimeError("portfolio has not been initialized")
        return self._snapshot


def _is_newer(incoming: str | None, current: str | None) -> bool:
    if incoming is None or current is None:
        return True
    try:
        return int(incoming) > int(current)
    except ValueError as exc:
        raise ValueError("business_version must be an integer string") from exc
