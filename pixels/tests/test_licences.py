"""The commercial-safe licence registry: blocked ids raise, allowed ids pass, and the table is exact."""

from __future__ import annotations

import pytest

from plate_pixels.backends import Backend
from plate_pixels.licences import (
    REGISTRY,
    LicenceError,
    ModelLicence,
    assert_commercial_safe,
    commercial_models,
)

ALLOWED = [
    "depth_anything_v2_small",
    "geocalib",
    "sam2.1",
    "raft",
    "paddleocr",
    "real_esrgan",
    "lpips",
    "open_clip",
    "qwen_image_edit_2509",
    "wan2.2_vace",
]
BLOCKED = [
    "depth_anything_v2_base",
    "depth_anything_v2_large",
    "unidepth",
    "superpoint",
    "flux1_dev",
    "matanyone",
    "rvm",
]


@pytest.mark.parametrize("model_id", BLOCKED)
def test_blocked_ids_raise(model_id):
    entry = REGISTRY[model_id]
    assert entry.commercial_ok is False
    with pytest.raises(LicenceError) as err:
        assert_commercial_safe(model_id)
    assert str(err.value) == f"{model_id}: {entry.licence} is not commercial-safe — {entry.note}"


@pytest.mark.parametrize("model_id", ALLOWED)
def test_allowed_ids_pass(model_id):
    entry = assert_commercial_safe(model_id)
    assert entry.id == model_id
    assert entry.commercial_ok is True


def test_unknown_id_raises():
    with pytest.raises(LicenceError, match="not_a_model: unregistered is not commercial-safe"):
        assert_commercial_safe("not_a_model")


def test_commercial_models_is_exactly_the_ten_allowed_ids():
    assert commercial_models() == ALLOWED
    assert len(commercial_models()) == 10


def test_registry_has_all_seventeen_ids():
    assert set(REGISTRY) == set(ALLOWED) | set(BLOCKED)
    assert len(REGISTRY) == 17
    assert all(key == entry.id for key, entry in REGISTRY.items())


def test_entries_are_frozen_and_complete():
    entry = REGISTRY["lpips"]
    assert isinstance(entry, ModelLicence)
    with pytest.raises(AttributeError):
        entry.commercial_ok = False  # type: ignore[misc]
    for item in REGISTRY.values():
        assert item.name and item.licence and item.weights_source


def test_specific_licences_match_the_plan():
    assert REGISTRY["depth_anything_v2_base"].licence == "CC-BY-NC-4.0"
    assert REGISTRY["depth_anything_v2_large"].licence == "CC-BY-NC-4.0"
    assert REGISTRY["unidepth"].licence == "CC-BY-NC-4.0"
    assert REGISTRY["rvm"].licence == "GPL-3.0"
    assert "STAFFAGE" in REGISTRY["rvm"].note
    assert "off until cleared in writing" in REGISTRY["matanyone"].note
    assert "confirm the licence file" in REGISTRY["qwen_image_edit_2509"].note
    assert "previs only" in REGISTRY["wan2.2_vace"].note


@pytest.mark.parametrize("model_id", BLOCKED)
def test_a_blocked_backend_can_never_be_constructed(model_id):
    class Rogue(Backend):
        pass

    Rogue.model_id = model_id
    with pytest.raises(LicenceError):
        Rogue()


def test_every_shipped_backend_is_commercial_safe():
    from plate_pixels.backends import Backends

    assert all(b.model_id in ALLOWED for b in Backends().all())
