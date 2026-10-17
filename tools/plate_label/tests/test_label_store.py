from __future__ import annotations

import json
import re
import warnings

import pytest
from blake3 import blake3

from plate_label import __version__
from plate_label.store import (
    LabelRecord,
    append_label,
    file_blake3,
    labelled_hashes,
    list_images,
    read_labels,
    record_label,
)


def test_list_images_sorted_case_insensitively_and_filtered(images):
    names = [p.relative_to(images).as_posix() for p in list_images(images)]
    assert names == ["A_first.jpg", "b_second.PNG", "sub/c_third.webp"]  # no .txt, no dot-files


def test_record_schema_and_round_trip(tmp_path):
    out = tmp_path / "calib" / "labels.jsonl"  # parent is created on demand
    rec = LabelRecord.new("sub/a.png", "ab" * 32, "fix", ["G01", "C01"], "ledge moved", "aasish", "take-7")
    append_label(out, rec)
    append_label(out, LabelRecord.new("b.png", "cd" * 32, "ship"))

    raw = out.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n") and raw.count(b"\n") == 2
    rows = read_labels(out)
    assert [r["image"] for r in rows] == ["sub/a.png", "b.png"]
    first = rows[0]
    assert list(first) == [
        "ts", "image", "image_blake3", "verdict", "reasons", "note", "rater", "take_id", "tool_version",
    ]  # fmt: skip
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", first["ts"])
    assert first["reasons"] == ["G01", "C01"] and first["take_id"] == "take-7"
    assert rows[1]["take_id"] is None and rows[1]["reasons"] == [] and rows[1]["tool_version"] == __version__
    assert json.loads(rec.to_json()) == first or json.loads(rec.to_json())["image"] == "sub/a.png"


def test_labelled_hashes_and_skip_logic(images, tmp_path):
    out = tmp_path / "labels.jsonl"
    a, b, _c = list_images(images)
    record_label(out, images, a, "ship")
    done = labelled_hashes(out)
    assert done == {file_blake3(a)}
    todo = [p for p in list_images(images) if file_blake3(p) not in done]
    assert a not in todo and b in todo and len(todo) == 2


def test_record_label_hashes_and_validates(images, tmp_path):
    out = tmp_path / "labels.jsonl"
    img = images / "A_first.jpg"
    rec = record_label(out, images, img, "render_it", ["G03"], "", "me", None)
    assert rec.image == "A_first.jpg" and rec.image_blake3 == blake3(img.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        record_label(out, images, img, "fix", [])
    with pytest.raises(ValueError):
        record_label(out, images, tmp_path / "elsewhere.png", "ship")  # outside the labelled folder
    assert len(read_labels(out)) == 1  # failures wrote nothing


def test_missing_file_and_corrupt_line(tmp_path):
    out = tmp_path / "labels.jsonl"
    assert read_labels(out) == [] and labelled_hashes(out) == set()
    out.write_bytes(
        b'{"image":"a.png","image_blake3":"aa","verdict":"ship"}\n{"image":"b.p'
    )  # crash mid-write
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert len(read_labels(out)) == 1
    assert any("unreadable" in str(w.message) for w in caught)
    append_label(out, LabelRecord.new("c.png", "cc", "ship"))  # must not glue onto the torn line
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert [r["image"] for r in read_labels(out)] == ["a.png", "c.png"]


def test_non_ascii_note_is_utf8(tmp_path):
    out = tmp_path / "labels.jsonl"
    append_label(out, LabelRecord.new("a.png", "aa", "ship", note="stüdio – ok"))
    assert "stüdio – ok".encode() in out.read_bytes()
    assert read_labels(out)[0]["note"] == "stüdio – ok"
