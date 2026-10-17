"""Cryptomatte ids: the verified murmur3 vectors, the exponent fix, and agreement with the spec's
reference implementation (plate_spec) on random names."""

from __future__ import annotations

import random

import pytest

import plate_spec
from plate_max import legend

RAW_VECTORS = {
    "": "00000000",
    "hello": "248bfa47",
    "hello, world": "149bbb7f",
    "The quick brown fox jumps over the lazy dog": "2e4ff723",
    "VEG_tree_01": "c7d248d1",
    "SKY": "8c06691f",
}


@pytest.mark.parametrize(("text", "expected"), RAW_VECTORS.items())
def test_raw_murmur3_vectors(text, expected):
    assert f"{legend.murmur3_32(text.encode('utf-8')):08x}" == expected


def test_exponent_fix_only_changes_the_empty_name_among_the_vectors():
    assert legend.cryptomatte_hash("") == "00800000"
    for text, expected in RAW_VECTORS.items():
        if text:
            assert legend.cryptomatte_hash(text) == expected


def test_ids_are_prefixed_by_kind():
    assert legend.object_id("SKY") == "o:8c06691f"
    assert legend.material_id("SKY") == "m:8c06691f"


def test_agrees_with_plate_spec_on_random_names():
    rng = random.Random(20261017)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-. é中"
    fixed = 0
    for _ in range(20000):
        name = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        raw = legend.murmur3_32(name.encode("utf-8"))
        assert raw == plate_spec.murmur3_32(name.encode("utf-8"))
        assert legend.object_id(name) == plate_spec.cryptomatte_id(name, "object")
        assert legend.material_id(name) == plate_spec.cryptomatte_id(name, "material")
        if ((raw >> 23) & 0xFF) in (0, 255):
            fixed += 1
            assert legend.cryptomatte_hash(name) != f"{raw:08x}"
    assert fixed > 0, "the sample never exercised the exponent fix"


def test_seed_is_honoured():
    assert legend.murmur3_32(b"hello", 1) == plate_spec.murmur3_32(b"hello", 1)
