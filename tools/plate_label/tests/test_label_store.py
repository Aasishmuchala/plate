from __future__ import annotations

import json
import os
import warnings

import pytest

from plate_label import store
from plate_label.pairs import Pair
from plate_label.store import (
    LabelRecord,
    append_label,
    clean_seconds,
    labelled_keys,
    read_labels,
    remove_line,
    replace_with_retry,
)


def pair(**kw):
    base = dict(take_id="S3", candidate=None, candidate_ref="out/a.png", plate=None, plate_ref="in/a.png")
    base.update(kw)
    return Pair(**base)


def test_record_schema_key_order_and_optional_keys():
    full = LabelRecord.for_pair(
        pair(run_id="r12", gate_report="gates/r12.1.json"), "fix", ["P1", "A2"], 14.23, "aasish"
    )
    d = json.loads(full.to_json())
    assert list(d) == [
        "v", "at", "rater", "take_id", "plate", "candidate", "run_id", "verdict", "reasons", "seconds", "gate_report",
    ]  # fmt: skip
    assert d["v"] == 1 and d["reasons"] == ["P1", "A2"] and d["seconds"] == 14.2
    assert d["at"].endswith("Z") and "T" in d["at"]
    assert "\n" not in full.to_json()

    bare = LabelRecord.for_pair(pair(plate_ref=None), "ship", [], 0, "aasish")
    d = json.loads(bare.to_json())
    assert "run_id" not in d and "gate_report" not in d  # absent, not null
    assert d["plate"] is None and d["verdict"] == "ship" and d["reasons"] == []


def test_record_validation():
    with pytest.raises(ValueError, match="verdict"):
        LabelRecord.for_pair(pair(), "maybe", [], 1, "r")
    with pytest.raises(ValueError, match="Z9"):
        LabelRecord.for_pair(pair(), "fix", ["Z9"], 1, "r")
    with pytest.raises(ValueError, match="rater"):
        LabelRecord.for_pair(pair(), "ship", [], 1, " ")
    for bad in [-1, float("nan"), float("inf"), 90_000, "3", True, None]:
        with pytest.raises(ValueError, match="seconds"):
            clean_seconds(bad)
    assert clean_seconds(0) == 0.0 and clean_seconds(3) == 3.0 and clean_seconds(2.26) == 2.3


def test_append_read_round_trip_and_crash_recovery(tmp_path):
    out = tmp_path / "calib" / "labels.jsonl"  # parent is created on demand
    a = LabelRecord.for_pair(pair(), "ship", [], 2, "r1")
    b = LabelRecord.for_pair(pair(take_id="S4"), "fix", ["P3"], 5, "r1")
    append_label(out, a)
    assert out.read_bytes().endswith(b"\n") and b"\r" not in out.read_bytes()
    with open(out, "ab") as f:  # a crash mid-write leaves a partial line without a newline
        f.write(b'{"v":1,"at"')
    append_label(out, b)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        rows = read_labels(out)
    assert [r["take_id"] for r in rows] == ["S3", "S4"]
    assert any("unreadable" in str(w.message) for w in caught)
    assert read_labels(tmp_path / "nope.jsonl") == []


def test_labelled_keys_are_per_rater(tmp_path):
    out = tmp_path / "labels.jsonl"
    append_label(out, LabelRecord.for_pair(pair(), "ship", [], 1, "alice"))
    assert labelled_keys(out, "alice") == {("S3", "in/a.png", "out/a.png")}
    assert labelled_keys(out, "bob") == set()
    assert pair().key in labelled_keys(out, "alice")


def test_remove_line_removes_only_the_last_match(tmp_path):
    out = tmp_path / "labels.jsonl"
    a = LabelRecord.for_pair(pair(), "ship", [], 1, "r")
    b = LabelRecord.for_pair(pair(take_id="S4"), "fix", ["P1"], 2, "r")
    la, lb = append_label(out, a), append_label(out, b)
    assert remove_line(out, lb) is True
    assert [r["take_id"] for r in read_labels(out)] == ["S3"]
    assert out.read_bytes() == (la + "\n").encode("utf-8")
    assert remove_line(out, lb) is False  # already gone
    assert remove_line(tmp_path / "none.jsonl", la) is False
    assert not list(tmp_path.glob("*.tmp"))  # no temp file left behind
    # two identical lines (same text): only one goes
    append_label(out, a)
    twin = a.to_json()
    with open(out, "a", encoding="utf-8", newline="\n") as f:
        f.write(twin + "\n")
    before = len(read_labels(out))
    assert remove_line(out, twin) is True and len(read_labels(out)) == before - 1


def test_replace_with_retry_survives_a_brief_lock(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.tmp", tmp_path / "a.jsonl"
    src.write_text("x", encoding="utf-8", newline="\n")
    real = os.replace
    calls = {"n": 0}

    def flaky(s, d):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("held by an indexer")
        return real(s, d)

    monkeypatch.setattr(store.os, "replace", flaky)
    monkeypatch.setattr(store.time, "sleep", lambda _s: None)
    replace_with_retry(src, dst)
    assert calls["n"] == 3 and dst.read_text(encoding="utf-8") == "x"

    monkeypatch.setattr(store.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(PermissionError):
        replace_with_retry(src, tmp_path / "b", tries=2)
