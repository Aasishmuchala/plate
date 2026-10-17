from __future__ import annotations

import json

import pytest

from plate_label.pairs import PairsError, load_manifest, scan_folder


def test_manifest_resolves_relative_paths_and_keeps_the_strings(manifest):
    pairs = load_manifest(manifest)
    assert [p.take_id for p in pairs] == ["S3", "S4"]
    first = pairs[0]
    assert first.plate.is_absolute() and first.plate.name == "a_plate.png"
    assert first.candidate.is_file() and first.candidate.is_absolute()
    assert first.plate_ref == "in/a_plate.png" and first.candidate_ref == "out/a_ai.png"
    assert first.run_id == "r12" and first.gate_report == "gates/r12.1.json"
    assert pairs[1].run_id is None and pairs[1].gate_report is None


def test_manifest_accepts_a_pairs_object_backslashes_and_a_missing_plate(tmp_path, make_image):
    root = tmp_path / "m"
    make_image(root / "sub" / "c.png")
    entry = {"take_id": "T", "candidate": "sub\\c.png"}  # a Windows-typed path
    (root / "pairs.json").write_text(json.dumps({"pairs": [entry]}), encoding="utf-8", newline="\n")
    (pair,) = load_manifest(root / "pairs.json")
    assert pair.candidate_ref == "sub/c.png" and pair.plate is None and pair.plate_ref is None


def test_manifest_reports_every_problem_at_once(tmp_path, make_image):
    root = tmp_path / "m"
    make_image(root / "ok.png")
    (root / "doc.txt").write_text("x", encoding="utf-8", newline="\n")
    entries = [
        {"take_id": "", "candidate": "ok.png"},
        {"take_id": "B", "candidate": "missing.png"},
        {"take_id": "C", "candidate": "doc.txt"},
        {"take_id": "D", "candidate": "ok.png", "plate": "nope.png"},
        {"take_id": "E", "candidate": "ok.png", "gate_report": ""},
        "not an object",
        {"take_id": "F", "candidate": "ok.png"},
        {"take_id": "F", "candidate": "ok.png"},
    ]
    (root / "pairs.json").write_text(json.dumps(entries), encoding="utf-8", newline="\n")
    with pytest.raises(PairsError) as err:
        load_manifest(root / "pairs.json")
    text = str(err.value)
    for needle in [
        "[0]",
        "take_id",
        "[1]",
        "does not exist",
        "[2]",
        "not an image",
        "[3]",
        "plate",
        "[4]",
        "gate_report",
        "[5]",
        "expected an object",
        "[7]",
        "duplicate",
    ]:
        assert needle in text, (needle, text)


def test_manifest_file_problems(tmp_path):
    with pytest.raises(PairsError, match="not found"):
        load_manifest(tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8", newline="\n")
    with pytest.raises(PairsError, match="JSON"):
        load_manifest(bad)
    bad.write_text('{"a": 1}', encoding="utf-8", newline="\n")
    with pytest.raises(PairsError, match="list"):
        load_manifest(bad)
    bad.write_text("[]", encoding="utf-8", newline="\n")
    with pytest.raises(PairsError, match="no pairs"):
        load_manifest(bad)


def test_scan_take_folders(shots):
    pairs = scan_folder(shots)
    assert [(p.take_id, p.candidate_ref, p.plate_ref) for p in pairs] == [
        ("t1", "t1/candidate_a.png", "t1/plate.png"),
        ("t1", "t1/candidate_b.png", "t1/plate.png"),
        ("t2", "t2/candidate.jpg", "t2/plate.jpg"),
    ]
    assert all(p.plate.is_file() and p.candidate.is_file() for p in pairs)


def test_scan_flat_names(tmp_path, make_image):
    root = tmp_path / "flat"
    for name in [
        "S3.plate.png",
        "S3.candidate.png",
        "S3_candidate_2.png",
        "S4-plate.jpg",
        "S4-candidate1.jpg",
        "other.png",
    ]:
        make_image(root / name)
    pairs = scan_folder(root)
    got = sorted((p.take_id, p.candidate_ref, p.plate_ref) for p in pairs)
    assert got == [
        ("S3", "S3.candidate.png", "S3.plate.png"),
        ("S3", "S3_candidate_2.png", "S3.plate.png"),
        ("S4", "S4-candidate1.jpg", "S4-plate.jpg"),
    ]  # other.png is not part of any pair


def test_scan_candidate_without_plate_is_still_labelled(tmp_path, make_image):
    root = tmp_path / "c"
    make_image(root / "t9" / "candidate.png")
    (pair,) = scan_folder(root)
    assert pair.take_id == "t9" and pair.plate is None and pair.plate_ref is None


def test_scan_plain_images_become_candidate_only_pairs(tmp_path, make_image):
    root = tmp_path / "plain"
    make_image(root / "b.PNG")
    make_image(root / "A.jpg")
    make_image(root / "sub" / "c.webp")
    make_image(root / ".hidden" / "x.png")
    (root / "notes.txt").write_text("x", encoding="utf-8", newline="\n")
    pairs = scan_folder(root)
    assert [(p.take_id, p.candidate_ref) for p in pairs] == [
        ("A", "A.jpg"),
        ("b", "b.PNG"),
        ("sub/c", "sub/c.webp"),
    ]
    assert all(p.plate is None for p in pairs)


def test_scan_empty_folder_is_an_error(tmp_path):
    (tmp_path / "e").mkdir()
    with pytest.raises(PairsError, match="no images"):
        scan_folder(tmp_path / "e")
