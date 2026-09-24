"""Option reference-data adapter backed by ``ymm_data_sdk``."""

from datetime import date, datetime
from importlib import import_module
from typing import Any

from sim_hedge.domain.contract import OptionContract, OptionType


class ReferenceDataError(RuntimeError):
    """Reference data is unavailable or has an unexpected shape."""


class YmmReferenceDataSource:
    """Load static option metadata without using Data SDK for live prices."""

    def __init__(self, token: str, mode: str = "lan", sdk: Any | None = None) -> None:
        if not token:
            raise ValueError("token must not be empty")
        if mode not in {"lan", "TS"}:
            raise ValueError("mode must be 'lan' or 'TS'")

        self._sdk = sdk or import_module("ymm_data_sdk")
        self._sdk.init(token=token, mode=mode)

    def close(self) -> None:
        close = getattr(self._sdk, "close", None)
        if close is not None:
            close()

    def __enter__(self) -> "YmmReferenceDataSource":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def load_option_chain(
        self,
        underlying: str,
        trading_date: date,
    ) -> list[OptionContract]:
        try:
            instrument_ids = self._sdk.options.get_contracts(
                underlying,
                trading_date=trading_date,
            )
            if not instrument_ids:
                return []
            instruments = self._sdk.instruments(instrument_ids)
            return [normalize_option_contract(item, underlying) for item in instruments]
        except ReferenceDataError:
            raise
        except Exception as exc:
            raise ReferenceDataError(
                f"option-chain request failed: {type(exc).__name__}: {exc}"
            ) from exc


def normalize_option_contract(item: Any, underlying: str) -> OptionContract:
    """Convert one vendor instrument object into an internal contract."""

    try:
        raw = item.to_dict()
    except (AttributeError, TypeError) as exc:
        raise ReferenceDataError("option instrument does not support to_dict()") from exc

    try:
        option_type = {
            "C": OptionType.CALL,
            "CALL": OptionType.CALL,
            "P": OptionType.PUT,
            "PUT": OptionType.PUT,
        }[str(raw["option_type"]).upper()]
        return OptionContract(
            instrument=str(raw["order_book_id"]),
            underlying=underlying,
            option_type=option_type,
            strike=float(raw["strike_price"]),
            maturity=_parse_maturity(raw["maturity_date"]),
            contract_multiplier=int(raw["contract_multiplier"]),
            price_tick=float(raw["tick_size"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReferenceDataError(f"invalid option instrument: {raw!r}") from exc


def _parse_maturity(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
