"""Static checks on the Blender fixture renderer, plus a repack that proves the committed packets are reproducible.

Blender itself is not needed here: scene.py is read as text (it imports bpy), build_packets.py runs in the venv.
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BLENDER = ROOT / "fixtures" / "blender"
SCENE = (BLENDER / "scene.py").read_text(encoding="utf-8")

OBJECTS = (
    "ARCH_wall_N ARCH_wall_S ARCH_wall_E ARCH_wall_W HARD_floor GLZ_pane SKY_dome SIGN_plate01 "
    "ARCH_panel_7016 ARCH_panel_3020 VEG_shrub_A PEOPLE_dummy WATER_pool"
).split()
MATERIALS = "GLZ_pane SIGN_a ARCH_panel_7016 ARCH_panel_3020 HARD_floor".split()
RENDER_SETTINGS = [
    'scene.render.engine = "CYCLES"',
    "use_pass_cryptomatte_object = True",
    "use_pass_cryptomatte_material = True",
    "pass_cryptomatte_depth = 6",
    'media_type = "MULTI_LAYER_IMAGE"',
    'file_format = "OPEN_EXR_MULTILAYER"',
    's.color_depth = "32"',
    's.exr_codec = "ZIP"',
    'cam.sensor_fit = "HORIZONTAL"',
]


def test_scene_py_parses_and_imports_bpy():
    tree = ast.parse(SCENE)
    imports = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
    assert "bpy" in imports


@pytest.mark.parametrize("name", OBJECTS + MATERIALS)
def test_scene_builds_the_named_object_or_material(name):
    assert f'"{name}"' in SCENE


def test_scene_uses_the_verified_blender_5_1_render_settings():
    for needle in RENDER_SETTINGS:
        assert needle in SCENE, needle
    assert re.search(r"^SAMPLES = 32$", SCENE, re.M)
    assert re.search(r"^SENSOR_WIDTH_MM = 36\.0$", SCENE, re.M)


def test_scene_cameras_match_the_plan():
    assert re.search(r'"CAM_a": dict\(.*?lens=35\.0, dof=True, fstop=2\.8, focus=2\.5', SCENE, re.S)
    assert re.search(r'"CAM_b": dict\(.*?lens=24\.0, dof=False', SCENE, re.S)
    assert re.search(r'"CAM_dolly": dict\(', SCENE)
    assert re.search(r"^DOLLY_FRAMES = 12$", SCENE, re.M) and re.search(
        r"^DOLLY_TRAVEL_M = 0\.6", SCENE, re.M
    )
    assert "res=(640, 360), frames=[0]" in SCENE and "res=(320, 180)" in SCENE


def test_scene_variants_and_markers_file():
    for variant in ("base", "ral3020", "v2"):
        assert f'"{variant}": [' in SCENE
    assert "markers.json" in SCENE


def test_render_driver_and_scene_agree_on_the_shots():
    driver = (BLENDER / "render.py").read_text(encoding="utf-8")
    scene_shots = set(re.findall(r'shot="([a-z0-9_]+)"', SCENE))
    driver_shots = set(re.findall(r'"([a-z0-9_]+)"', driver.split("VARIANTS = {", 1)[1].split("}\n", 1)[0]))
    driver_shots -= {"base", "ral3020", "v2"}
    assert (
        scene_shots
        == driver_shots
        == {"room_a", "room_b", "dolly_hero", "dolly_12f", "room_a_ral3020", "room_a_v2"}
    )


def test_render_wrapper_is_a_posix_script_with_lf_endings():
    wrapper = next(p for p in BLENDER.glob("render.*") if p.suffix != ".py")
    raw = wrapper.read_bytes()
    assert raw.startswith(b"#!/usr/bin/env bash\n") and b"\r" not in raw
    assert b"render.py" in raw


def test_work_folder_is_gitignored_and_not_committed():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "fixtures/**/_work/" in ignore


def _digest(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name != "README.md"
    }


def test_repack_is_idempotent_and_reproduces_the_committed_packets(tmp_path):
    work = BLENDER / "_work"
    if not (work / "base" / "room_a" / "ml.exr").is_file():
        pytest.skip(
            "no fixtures/blender/_work renders here (run the fixtures/blender/render.py driver first)"
        )
    out = tmp_path / "packets"
    cmd = [sys.executable, str(BLENDER / "build_packets.py"), str(work), str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    committed = ROOT / "fixtures" / "packets"
    built = {p.name for p in out.iterdir() if p.is_dir()}
    assert built == {p.name for p in committed.iterdir() if p.is_dir()}
    for shot in sorted(built):
        assert _digest(out / shot) == _digest(committed / shot), (
            f"{shot}: repack differs from the committed packet"
        )
