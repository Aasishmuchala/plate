"""Stub mode is the default: no torch anywhere, every route answers ``"stub": true``."""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

from plate_pixels import server
from plate_pixels.backends import Backends, ModelUnavailable
from plate_pixels.licences import REGISTRY

PKG = Path(__file__).resolve().parent.parent / "plate_pixels"


def test_importing_the_server_does_not_import_torch():
    importlib.import_module("plate_pixels.server")
    assert "torch" not in sys.modules


def test_no_module_imports_torch_at_all():
    """Availability is probed with find_spec; no source file may import torch/torchvision itself."""
    bad = []
    for path in sorted(PKG.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            bad += [f"{path.name}: {n}" for n in names if n.split(".")[0] in {"torch", "torchvision"}]
    assert not bad, bad


def test_health_is_stub_and_lists_every_registry_model():
    code, body = server.route("/health", {}, Backends())
    assert code == 200
    assert body["ok"] is True
    assert body["stub_mode"] is True
    assert body["torch"] is False
    assert set(body["models"]) == set(REGISTRY)
    for model_id, info in body["models"].items():
        assert info["available"] is False
        assert info["licence"] == REGISTRY[model_id].licence
        assert info["commercial_ok"] is REGISTRY[model_id].commercial_ok


EXPECTED = {
    "/depth": ({"png": "{png}"}, "depth_anything_v2_small", {"shape": [32, 48], "depth_png": None}),
    "/calibrate": ({"png": "{png}"}, "geocalib", {"fov_deg": 60.0, "roll_deg": 0.0, "pitch_deg": 0.0}),
    "/segment": (
        {"png": "{png}", "points": [[10, 12], [20, 8]]},
        "sam2.1",
        {"mask_png": None, "shape": [32, 48]},
    ),
    "/flow": ({"a": "{png}", "b": "{png}"}, "raft", {"eval_width": 960, "median_px": 0.0}),
    "/textlock": ({"png": "{png}"}, "paddleocr", {"boxes": []}),
    "/upscale": (
        {"png": "{png}", "scale": 2},
        "real_esrgan",
        {"note": "AI layers only; never the plate"},
    ),
    "/lpips": ({"a": "{png}", "b": "{png}"}, "lpips", {"distance": None}),
}


@pytest.mark.parametrize("path", sorted(EXPECTED))
def test_every_route_returns_a_stub(path, png):
    request, model, fields = EXPECTED[path]
    body = {k: (png if v == "{png}" else v) for k, v in request.items()}
    code, result = server.route(path, body, Backends())
    assert code == 200
    assert result == {"stub": True, "model": model, **fields}


def test_upscale_refuses_the_plate(png):
    code, result = server.route("/upscale", {"png": png, "scale": 2, "is_plate": True}, Backends())
    assert code == 400
    assert result == {"error": "plate upscaling is forbidden (BUILD_PLAN section 1)"}


def test_unknown_route_is_404():
    code, result = server.route("/nope", {}, Backends())
    assert code == 404
    assert result == {"error": "unknown route /nope"}


def test_missing_image_is_an_input_error(tmp_path):
    with pytest.raises(server.InputError):
        server.route("/depth", {"png": str(tmp_path / "absent.png")}, Backends())


def test_real_mode_raises_instead_of_stubbing(monkeypatch):
    monkeypatch.setenv("PLATE_PIXELS_REAL", "1")
    with pytest.raises(ModelUnavailable, match="PLATE_PIXELS_REAL=1"):
        Backends()


def test_models_dir_honours_the_override(monkeypatch, tmp_path):
    from plate_pixels.backends import models_dir

    monkeypatch.setenv("PLATE_MODELS", str(tmp_path / "m"))
    assert models_dir() == tmp_path / "m"
    monkeypatch.delenv("PLATE_MODELS")
    assert models_dir().name == "models"
    assert "Plate" in models_dir().parts
