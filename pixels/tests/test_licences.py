"""The commercial-safe licence registry, the construction gate and the CI check (`--check`)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from plate_pixels import licences
from plate_pixels.backends import Backend, _real_classes, _stub_classes
from plate_pixels.licences import (
    BY_NAME,
    REGISTRY,
    LicenceError,
    ModelLicence,
    assert_commercial_safe,
    blocked_models,
    commercial_models,
    names_blocked_model,
)

PIXELS = Path(__file__).resolve().parent.parent
PACKAGE = PIXELS / "plate_pixels"

ALLOWED = [
    "depth-anything-v2-small",
    "geocalib",
    "sam2.1",
    "raft",
    "opencv-dis",
    "paddleocr",
    "real-esrgan",
    "lpips",
    "open-clip",
    "qwen-image-edit-2509",
    "wan2.2",
]
BLOCKED = [
    "depth-anything-v2-base",
    "depth-anything-v2-large",
    "unidepth",
    "superpoint",
    "flux-dev",
    "matanyone",
    "rvm",
]


# --------------------------------------------------------------------------- the table


def test_registry_is_exactly_the_planned_models():
    assert [e.name for e in REGISTRY if e.commercial_ok] == ALLOWED
    assert [e.name for e in REGISTRY if not e.commercial_ok] == BLOCKED
    assert commercial_models() == ALLOWED
    assert blocked_models() == BLOCKED
    assert len(BY_NAME) == len(REGISTRY) == 18


def test_every_entry_has_the_spec_fields():
    for entry in REGISTRY:
        assert isinstance(entry, ModelLicence)
        assert entry.name and entry.version and entry.weights_licence and entry.code_licence
        assert entry.weights_source
        assert isinstance(entry.commercial_ok, bool)
        assert isinstance(entry.note, str)
        if not entry.commercial_ok:
            assert entry.note, f"{entry.name}: a blocked model says why"


def test_entries_are_frozen():
    with pytest.raises(AttributeError):
        REGISTRY[0].commercial_ok = False  # type: ignore[misc]


@pytest.mark.parametrize(
    ("name", "weights", "code", "ok"),
    [
        ("depth-anything-v2-small", "Apache-2.0", "Apache-2.0", True),
        ("depth-anything-v2-base", "CC-BY-NC-4.0", "Apache-2.0", False),
        ("depth-anything-v2-large", "CC-BY-NC-4.0", "Apache-2.0", False),
        ("geocalib", "CC-BY-4.0", "Apache-2.0", True),
        ("sam2.1", "Apache-2.0", "Apache-2.0", True),
        ("raft", "BSD-3-Clause", "BSD-3-Clause", True),
        ("paddleocr", "Apache-2.0", "Apache-2.0", True),
        ("real-esrgan", "BSD-3-Clause", "BSD-3-Clause", True),
        ("lpips", "BSD-2-Clause", "BSD-2-Clause", True),
        ("unidepth", "CC-BY-NC-4.0", "CC-BY-NC-4.0", False),
        ("rvm", "GPL-3.0", "GPL-3.0", False),
        ("qwen-image-edit-2509", "Apache-2.0", "Apache-2.0", True),
        ("wan2.2", "Apache-2.0", "Apache-2.0", True),
    ],
)
def test_licences_match_the_plan(name, weights, code, ok):
    entry = BY_NAME[name]
    assert (entry.weights_licence, entry.code_licence, entry.commercial_ok) == (weights, code, ok)


def test_notes_carry_the_conditions():
    assert "attribution" in BY_NAME["geocalib"].note
    assert "STAFFAGE" in BY_NAME["rvm"].note
    assert "cleared" in BY_NAME["matanyone"].note
    assert "non-commercial" in BY_NAME["flux-dev"].weights_licence.lower()
    assert "window" in BY_NAME["qwen-image-edit-2509"].note
    assert "never the plate" in BY_NAME["real-esrgan"].note
    assert [e.name for e in REGISTRY if e.confirm_licence_file] == ["qwen-image-edit-2509"]


# --------------------------------------------------------------------------- the gate


@pytest.mark.parametrize("name", BLOCKED)
def test_blocked_models_raise(name):
    entry = BY_NAME[name]
    with pytest.raises(LicenceError) as err:
        assert_commercial_safe(name)
    assert str(err.value) == f"{name}: {entry.weights_licence} is not commercial-safe - {entry.note}"


@pytest.mark.parametrize("name", ALLOWED)
def test_allowed_models_pass(name):
    assert assert_commercial_safe(name).name == name


def test_unknown_model_raises():
    with pytest.raises(LicenceError, match="not-a-model: unregistered is not commercial-safe"):
        assert_commercial_safe("not-a-model")


@pytest.mark.parametrize(
    ("spelling", "name"),
    [
        ("real_esrgan", "real-esrgan"),
        ("Real-ESRGAN", "real-esrgan"),
        ("wan2.2-vace", "wan2.2"),
        ("wan2.2_vace", "wan2.2"),
        ("open_clip", "open-clip"),
        ("depth_anything_v2_small", "depth-anything-v2-small"),
        ("flux1_dev", "flux-dev"),
        ("vitl", "depth-anything-v2-large"),
    ],
)
def test_aliases_resolve(spelling, name):
    assert licences.get(spelling).name == name


@pytest.mark.parametrize("name", BLOCKED)
def test_a_blocked_backend_can_never_be_constructed(name):
    class Rogue(Backend):
        pass

    Rogue.model_id = name
    with pytest.raises(LicenceError):
        Rogue()


def test_an_unregistered_backend_can_never_be_constructed():
    class Rogue(Backend):
        model_id = "some-new-model"

    with pytest.raises(LicenceError, match="unregistered"):
        Rogue()


def test_every_shipped_backend_class_is_commercial_safe():
    classes = [*_stub_classes().values(), *_real_classes().values()]
    assert len(classes) == 14
    for cls in classes:
        assert_commercial_safe(cls.model_id)


def test_stub_backends_construct_without_weights_or_packages():
    for cls in _stub_classes().values():
        assert cls().model_id in ALLOWED


# --------------------------------------------------------------------------- name matching


@pytest.mark.parametrize(
    "text",
    [
        "unidepth",
        "UniDepth",
        "uni-depth",
        "import unidepth.models",
        "FLUX.1-dev",
        "flux1_dev",
        "flux-dev",
        "Depth_Anything_V2_Large",
        "depth-anything-v2-base",
        "super_point",
        "MatAnyone",
        "robust_video_matting",
        "vitl",
        "rvm",
        "model=rvm.pt",
    ],
)
def test_names_that_must_trip(text):
    assert names_blocked_model(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "depth-anything-v2-small",
        "depth_anything_v2_small",
        "geocalib",
        "sam2.1",
        "servm",
        "preserving mode",
        "the vitality of the plate",
        "0.1.0",
        "",
        "stub-depth-anything-v2-small",
    ],
)
def test_names_that_must_not_trip(text):
    assert names_blocked_model(text) is None


# --------------------------------------------------------------------------- the CI check


def test_the_shipped_package_is_clean():
    violations, count = licences.scan()
    assert violations == []
    assert count >= 10  # every module plus requirements-cuda.txt


def test_check_exits_zero_on_the_package(capsys):
    assert licences.main(["--check"]) == 0
    assert "licence check OK" in capsys.readouterr().out


def clone_package(tmp_path: Path) -> Path:
    """A copy of ``pixels/`` (package + requirements-cuda.txt) the test can poison."""
    root = tmp_path / "pixels"
    shutil.copytree(PACKAGE, root / "plate_pixels", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(PIXELS / "requirements-cuda.txt", root / "requirements-cuda.txt")
    return root / "plate_pixels"


def test_the_clone_is_clean_before_it_is_poisoned(tmp_path):
    assert licences.main(["--check", "--root", str(clone_package(tmp_path))]) == 0


@pytest.mark.parametrize(
    ("source", "model"),
    [
        ('MODEL = "unidepth"\n', "unidepth"),
        ("import unidepth\n", "unidepth"),
        ("from superpoint.net import SuperPoint\n", "superpoint"),
        ('"""Uses MatAnyone for hair."""\n', "matanyone"),
        ('CKPT = "FLUX.1-dev"\n', "flux-dev"),
        ('SIZE = "vitl"\n', "depth-anything-v2-large"),
        ("def f(name='rvm'): ...\n", "rvm"),
    ],
)
def test_check_fails_when_a_module_names_a_blocked_model(tmp_path, capsys, source, model):
    package = clone_package(tmp_path)
    (package / "poison.py").write_text(source, encoding="utf-8", newline="\n")
    assert licences.main(["--check", "--root", str(package)]) == 1
    err = capsys.readouterr().err
    assert "poison.py:1" in err
    assert model in err
    assert "licence check FAILED" in err


def test_check_fails_on_a_blocked_requirement(tmp_path, capsys):
    package = clone_package(tmp_path)
    req = package.parent / "requirements-cuda.txt"
    req.write_text(req.read_text(encoding="utf-8") + "\nunidepth>=1.0\n", encoding="utf-8", newline="\n")
    assert licences.main(["--check", "--root", str(package)]) == 1
    assert "requirements-cuda.txt" in capsys.readouterr().err


def test_check_ignores_blocked_names_in_requirement_comments(tmp_path):
    package = clone_package(tmp_path)
    req = package.parent / "requirements-cuda.txt"
    req.write_text(req.read_text(encoding="utf-8") + "\n# never unidepth\n", encoding="utf-8", newline="\n")
    assert licences.main(["--check", "--root", str(package)]) == 0


def test_only_the_registry_file_is_exempt(tmp_path):
    package = clone_package(tmp_path)
    assert "unidepth" in (package / "licences.py").read_text(encoding="utf-8").lower()
    (package / "sub").mkdir()
    (package / "sub" / "licences.py").write_text('z = "superpoint"\n', encoding="utf-8", newline="\n")
    # The exemption is by file name, so a same-named file in a sub-folder is exempt too: pin that down.
    assert licences.main(["--check", "--root", str(package)]) == 0
    (package / "sub" / "deep.py").write_text('y = "superpoint"\n', encoding="utf-8", newline="\n")
    assert licences.main(["--check", "--root", str(package)]) == 1  # sub-folders are scanned recursively


def test_python_dash_m_check_exit_codes(tmp_path):
    """The CI command: exit 0 on the package, exit 1 on a poisoned copy (a real process, not a function call)."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(PIXELS), str(PIXELS.parent / "spec")])}
    good = subprocess.run(
        [sys.executable, "-m", "plate_pixels.licences", "--check"], env=env, capture_output=True, text=True
    )
    assert good.returncode == 0, good.stderr
    assert "licence check OK" in good.stdout

    package = clone_package(tmp_path)
    (package / "poison.py").write_text('MODEL = "unidepth"\n', encoding="utf-8", newline="\n")
    bad = subprocess.run(
        [sys.executable, "-m", "plate_pixels.licences", "--check", "--root", str(package)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1
    assert "unidepth" in bad.stderr


def test_list_prints_every_entry(capsys):
    assert licences.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == len(REGISTRY)
    assert "OFF unidepth" in out and "ok  geocalib" in out
