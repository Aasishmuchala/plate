from __future__ import annotations

import pytest

from plate_label import codes
from plate_label.codes import VERDICT_KEYS, VERDICTS, reason_codes, reason_groups, validate_reasons
from plate_label.page import render_page

PLAN_CODES = {
    # Lensbook A/F codes and PLATE's P codes, exactly as the router plan (T23) lists them.
    "A1": "geometry warp",
    "A2": "texture crawl",
    "A3": "scale drift",
    "A4": "vegetation slop",
    "A5": "re-cladding drift",
    "F2": "slideshow",
    "F4": "action ignored",
    "F6": "camera improvises",
    "F7": "continuity",
    "F9": "extra people or objects",
    "F11": "text or logos",
    "F12": "light shift",
    "P1": "registration",
    "P2": "invented edge",
    "P3": "colour shift",
    "P4": "texture band",
    "P5": "tone band",
    "P6": "outside-mask leak",
    "P7": "people implausible",
}


def test_table_matches_the_plan():
    assert reason_codes() == PLAN_CODES
    assert list(reason_codes()) == list(PLAN_CODES)  # file order is the UI order


def test_every_code_has_a_group_heading():
    groups = reason_groups()
    assert set(groups) == {"A", "F", "P"}
    assert {c[0] for c in reason_codes()} <= set(groups)


def test_keys_and_verdicts():
    assert VERDICT_KEYS == {"1": "ship", "2": "fix", "3": "render_it"}
    assert VERDICTS == ("ship", "fix", "render_it")


def test_validate_reasons_rules():
    assert validate_reasons(["P1", "A2", "P1"], "fix") == ["P1", "A2"]  # de-duplicated, order kept
    assert validate_reasons([], "fix") == []  # reasons are optional: there may be no matching code
    assert validate_reasons([], "ship") == []
    assert validate_reasons(["F12"]) == ["F12"]  # no verdict given: codes only
    for bad, needle in [
        (["Z9"], "Z9"),
        (["p1"], "p1"),  # case-sensitive: the CLI upper-cases before calling
        ("P1", "list"),
        ([1], "1"),
    ]:
        with pytest.raises(ValueError, match=needle):
            validate_reasons(bad, "fix")
    with pytest.raises(ValueError, match="verdict"):
        validate_reasons([], "maybe")
    with pytest.raises(ValueError, match="no reason"):
        validate_reasons(["P1"], "ship")


def test_table_can_be_pointed_elsewhere(tmp_path, monkeypatch):
    other = tmp_path / "codes.yaml"
    other.write_text("codes:\n  X1: custom\ngroups:\n  X: Custom\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("PLATE_REASON_CODES", str(other))
    assert codes.reason_codes() == {"X1": "custom"}
    monkeypatch.delenv("PLATE_REASON_CODES")
    assert "P1" in codes.reason_codes()


def test_bad_table_is_refused(tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("nothing: here\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("PLATE_REASON_CODES", str(bad))
    with pytest.raises(ValueError, match="codes"):
        codes.reason_codes()


def test_page_offers_every_code_and_the_three_keys():
    html = render_page("nonce123", "tester")
    for code in PLAN_CODES:
        assert f'data-code="{code}"' in html
    for verdict in VERDICTS:
        assert f'data-verdict="{verdict}"' in html
    assert "nonce123" in html and "tester" in html
    assert "http://" not in html and "https://" not in html  # no CDN, no external fetch
