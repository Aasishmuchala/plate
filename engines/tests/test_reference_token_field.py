"""A manifest may state its engine's wording for a reference image on the images input (T113 follow-up)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plate_engines.models import IOField


def test_reference_token_is_accepted_on_an_images_input():
    field = IOField(
        name="reference_images", type="images", required=False, max_items=4, reference_token="image {k}"
    )
    assert field.reference_token == "image {k}"


def test_reference_token_is_refused_on_other_input_types():
    with pytest.raises(ValidationError):
        IOField(name="prompt", type="text", reference_token="image {k}")


def test_absent_by_default():
    assert IOField(name="reference_images", type="images").reference_token is None
