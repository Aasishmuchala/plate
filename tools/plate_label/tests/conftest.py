"""Shared fixtures for the labeller tests: a small folder of fake image files (the tool hashes bytes
and never decodes them, so distinct byte strings with image extensions are enough)."""

from __future__ import annotations

import pytest


@pytest.fixture
def images(tmp_path):
    folder = tmp_path / "shots"
    (folder / "sub").mkdir(parents=True)
    files = {
        "b_second.PNG": b"png-bytes-b",
        "A_first.jpg": b"jpg-bytes-a",
        "sub/c_third.webp": b"webp-bytes-c",
        "notes.txt": b"not an image",
        ".hidden.png": b"hidden",
    }
    for rel, data in files.items():
        (folder / rel).write_bytes(data)
    return folder
