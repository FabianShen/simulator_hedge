from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

import pytest

from sim_hedge.jsonio import (
    coerce_int,
    normalize_positions,
    parse_iso_utc,
    read_json,
    require_list,
    require_object,
    write_json,
)


def test_json_round_trip_preserves_requested_format() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "nested" / "value.json"

        write_json(
            path, {"label": "期权"}, ensure_ascii=False, trailing_newline=False
        )

        assert path.read_text(encoding="utf-8") == json.dumps(
            {"label": "期权"}, ensure_ascii=False, indent=2
        )
        assert read_json(path) == {"label": "期权"}


@pytest.mark.parametrize("value", [True, False, 1.0, "1", "3.0", None])
def test_coerce_int_rejects_non_integral_types(value) -> None:
    with pytest.raises(ValueError, match="quantity must be an integer"):
        coerce_int(value, "quantity")


def test_coerce_int_accepts_int_and_normalizes_positions() -> None:
    assert coerce_int(3, "quantity") == 3
    assert normalize_positions({2: 0, "B": -1, "A": 2}, "position") == {
        "A": 2,
        "B": -1,
    }


def test_protocol_value_guards() -> None:
    assert require_object({"a": 1}, "value") == {"a": 1}
    assert require_list([1], "value") == [1]
    assert parse_iso_utc("2026-09-28T10:00:00Z", "timestamp") == datetime(
        2026, 9, 28, 10, tzinfo=timezone.utc
    )
    with pytest.raises(ValueError, match="timestamp must be timezone-aware"):
        parse_iso_utc("2026-09-28T10:00:00", "timestamp")
