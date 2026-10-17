from __future__ import annotations

import pytest

from plate_label.codes import REASON_CODES, REASON_GROUPS, VERDICT_KEYS, VERDICTS, validate_reasons


def test_code_table_is_the_fixed_eighteen():
    assert len(REASON_CODES) == 18
    assert list(REASON_CODES) == [
        "G01", "G02", "G03", "G04", "C01", "C02", "C03", "T01", "T02", "T03",
        "P01", "P02", "M01", "M02", "M03", "X01", "O01", "O99",
    ]  # fmt: skip
    assert REASON_CODES["G03"] == "unseen geometry guessed"
    assert REASON_CODES["O99"] == "other (note required)"
    assert len({c.lower() for c in REASON_CODES}) == 18  # case-insensitive filesystem / CSV safe
    assert {c[0] for c in REASON_CODES} <= set(REASON_GROUPS)


def test_verdicts_and_keys_are_unique():
    assert VERDICTS == ("ship", "fix", "render_it")
    assert VERDICT_KEYS == {"S": "ship", "F": "fix", "R": "render_it"}
    assert len(set(VERDICT_KEYS)) == len(VERDICT_KEYS)


def test_unknown_codes_raise():
    with pytest.raises(ValueError, match="Z99"):
        validate_reasons(["G01", "Z99"])
    with pytest.raises(ValueError):
        validate_reasons(["g01"])  # case-sensitive; the CLI upper-cases first
    with pytest.raises(ValueError):
        validate_reasons("G01")  # type: ignore[arg-type]


def test_reasons_are_deduplicated_in_order():
    assert validate_reasons(["C01", "G01", "C01"]) == ["C01", "G01"]
    assert validate_reasons([]) == []


@pytest.mark.parametrize("verdict", ["fix", "render_it"])
def test_fix_and_render_it_need_a_reason(verdict):
    with pytest.raises(ValueError, match="at least one"):
        validate_reasons([], verdict)
    assert validate_reasons(["G01"], verdict) == ["G01"]


def test_ship_needs_no_reason_and_unknown_verdict_raises():
    assert validate_reasons([], "ship") == []
    with pytest.raises(ValueError, match="verdict"):
        validate_reasons([], "maybe")


def test_o99_needs_a_note():
    with pytest.raises(ValueError, match="O99"):
        validate_reasons(["O99"], "fix", "")
    with pytest.raises(ValueError, match="O99"):
        validate_reasons(["O99"], "fix", "   ")
    assert validate_reasons(["O99"], "fix", "wrong crop") == ["O99"]
