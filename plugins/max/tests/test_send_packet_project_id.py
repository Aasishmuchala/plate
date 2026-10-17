"""A project id the daemon already accepts is sent as-is; only free text is slugified."""

from __future__ import annotations

import pytest

from plate_max.actions import send_packet


@pytest.mark.parametrize("given", ["tula_wot", "wot-2026", "p1", "a_b-c_9"])
def test_valid_ids_pass_through_unchanged(monkeypatch, given):
    monkeypatch.setattr(send_packet, "option", lambda options, key: given if key == "project_id" else None)
    assert send_packet._project_id(object()) == given


@pytest.mark.parametrize(
    ("given", "want"), [("Tula WoT", "tula-wot"), ("Clubhouse  Film!", "clubhouse-film")]
)
def test_free_text_is_slugified(monkeypatch, given, want):
    monkeypatch.setattr(send_packet, "option", lambda options, key: given if key == "project_id" else None)
    assert send_packet._project_id(object()) == want
