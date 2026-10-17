"""Shared fixtures for the labeller tests: small real PNG/JPG files laid out the way the folder scan and
the --pairs manifest expect (the server streams bytes, so the content only has to be a valid image)."""

from __future__ import annotations

import json

import pytest
from PIL import Image


def _make_image(path, colour=(120, 120, 120), size=(8, 6)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return path


@pytest.fixture
def make_image():
    """make_image(path, colour=..., size=...) writes a small valid image (parents created)."""
    return _make_image


@pytest.fixture
def shots(tmp_path, make_image):
    """shots/t1: plate + two candidates; shots/t2: plate.jpg + one candidate.jpg; plus a stray text file."""
    root = tmp_path / "shots"
    make_image(root / "t1" / "plate.png", (10, 10, 10))
    make_image(root / "t1" / "candidate_a.png", (200, 10, 10))
    make_image(root / "t1" / "candidate_b.png", (10, 200, 10))
    make_image(root / "t2" / "plate.jpg", (30, 30, 30))
    make_image(root / "t2" / "candidate.jpg", (10, 10, 200))
    (root / "notes.txt").write_text("not an image", encoding="utf-8")
    return root


@pytest.fixture
def manifest(tmp_path, make_image):
    """A --pairs manifest next to its images; paths are relative to the manifest, one entry has a gate report."""
    root = tmp_path / "run"
    make_image(root / "in" / "a_plate.png", (10, 10, 10))
    make_image(root / "out" / "a_ai.png", (200, 10, 10))
    make_image(root / "in" / "b_plate.png", (20, 20, 20))
    make_image(root / "out" / "b_ai.png", (10, 200, 10))
    entries = [
        {
            "take_id": "S3",
            "plate": "in/a_plate.png",
            "candidate": "out/a_ai.png",
            "gate_report": "gates/r12.1.json",
            "run_id": "r12",
        },
        {"take_id": "S4", "plate": "in/b_plate.png", "candidate": "out/b_ai.png"},
    ]
    path = root / "pairs.json"
    path.write_text(json.dumps(entries), encoding="utf-8", newline="\n")
    return path
