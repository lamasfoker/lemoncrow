"""N6 (savings gate) + N7 (columnar + string-intern encoding)."""

from __future__ import annotations

import json

from lemoncrow.pro.capabilities.tool_supervision.compact_output import (
    COLUMNAR_FORMAT,
    DEFAULT_SAVINGS_THRESHOLD,
    columnar_decode,
    columnar_encode,
    columnar_encode_json,
    gate_compact,
    savings_ratio,
)


def test_compact_gate_contract() -> None:
    """case: test_gate_picks_json_below_threshold"""
    original = json.dumps([{"a": 1, "b": 2}])
    compact_form = original[:-1]
    gate = gate_compact(original, compact_form)
    assert gate.used_compact is False
    assert gate.chosen == original
    assert gate.savings_ratio < DEFAULT_SAVINGS_THRESHOLD
    "case: test_gate_picks_compact_above_threshold"
    original = "x" * 1000
    compact_form = "x" * 500
    gate = gate_compact(original, compact_form)
    assert gate.used_compact is True
    assert gate.chosen == compact_form
    assert gate.savings_ratio >= DEFAULT_SAVINGS_THRESHOLD
    "case: test_gate_never_inflates"
    original = "small"
    inflated = "this is much much longer than the original payload"
    gate = gate_compact(original, inflated)
    assert gate.used_compact is False
    assert gate.chosen == original
    assert savings_ratio(original, inflated) == 0.0
    "case: test_gate_respects_custom_threshold"
    original = "y" * 100
    compact_form = "y" * 80
    assert gate_compact(original, compact_form, threshold=0.25).used_compact is False
    assert gate_compact(original, compact_form, threshold=0.2).used_compact is True
    "case: test_savings_ratio_empty_original"
    assert savings_ratio("", "anything") == 0.0


def test_columnar_encoding_contract() -> None:
    """case: test_columnar_round_trip_basic"""
    rows = [
        {"path": "src/a.py", "line": 1, "sym": "Foo"},
        {"path": "src/a.py", "line": 9, "sym": "Bar"},
        {"path": "src/b.py", "line": 3, "sym": "Foo"},
    ]
    encoded = columnar_encode(rows)
    assert encoded["format"] == COLUMNAR_FORMAT
    assert columnar_decode(encoded) == rows
    "case: test_columnar_interns_repeated_strings_into_legend"
    rows = [{"path": "src/a.py"} for _ in range(5)]
    encoded = columnar_encode(rows)
    assert encoded["legend"] == ["src/a.py"]
    assert all(cell == {"$": 0} for cell in encoded["columns"]["path"])
    assert columnar_decode(encoded) == rows
    "case: test_columnar_does_not_intern_singletons"
    rows = [{"path": "only_once.py", "line": 1}]
    encoded = columnar_encode(rows)
    assert encoded["legend"] == []
    assert encoded["columns"]["path"] == ["only_once.py"]
    assert columnar_decode(encoded) == rows
    "case: test_columnar_lossless_with_missing_keys_and_none"
    rows = [{"path": "a", "line": 1}, {"path": "a", "line": 2, "extra": None}, {"path": "b"}]
    encoded = columnar_encode(rows)
    decoded = columnar_decode(encoded)
    assert decoded == rows
    assert "line" not in decoded[2]
    assert decoded[1]["extra"] is None
    "case: test_columnar_self_describing_header"
    rows = [{"path": "a", "sym": "X"}, {"path": "a", "sym": "Y"}]
    encoded = columnar_encode(rows)
    assert set(encoded) >= {"format", "n", "keys", "legend", "columns", "present"}
    assert encoded["n"] == 2
    assert encoded["keys"] == ["path", "sym"]
    "case: test_columnar_beats_json_on_redundant_rows"
    rows = [{"path": "src/services/orders.py", "fqn": "orders.OrderService"} for _ in range(40)]
    raw_json = json.dumps(rows, separators=(",", ":"))
    columnar_json = columnar_encode_json(rows)
    assert len(columnar_json) < len(raw_json)
    assert gate_compact(raw_json, columnar_json).used_compact is True
    "case: test_columnar_decode_rejects_unknown_format"
    import pytest

    with pytest.raises(ValueError):
        columnar_decode({"format": "some-other-format", "n": 0})
