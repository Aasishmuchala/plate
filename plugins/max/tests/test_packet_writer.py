"""The manifest the writer produces must satisfy the spec's own validators, on a written folder too."""

from __future__ import annotations

import copy
import json
import re

import pytest
from fake_pymxs import make_node
from packet_fixture import EXAMPLE, b3, build_still, put, read_json, view_payload

import plate_spec
from plate_max import PACKET_VERSION, digest, legend, packet, policy
from plate_max.packet_id import PACKET_ID_RE, new_packet_id


def test_packet_version_matches_the_spec():
    assert PACKET_VERSION == plate_spec.load_example(EXAMPLE)["packet_version"] == plate_spec.PACKET_VERSION


def test_packet_ids_match_the_schema_and_sort_by_time():
    a, b = new_packet_id(now_ms=1_000), new_packet_id(now_ms=2_000)
    assert PACKET_ID_RE.match(a) and PACKET_ID_RE.match(b) and a < b
    assert new_packet_id() != new_packet_id()
    assert PACKET_ID_RE.match(new_packet_id(now_ms=(1 << 48) - 1, entropy=b"\xff" * 10))
    with pytest.raises(ValueError):
        new_packet_id(now_ms=1 << 48)


def test_manifest_from_the_fake_scene_validates(scene, tmp_path):
    manifest, leg, cam, _ = build_still(scene, tmp_path / "pkt")
    assert plate_spec.validate_manifest(manifest) == []
    assert manifest["packet_id"] == leg["packet_id"]
    assert manifest["camera"]["world_from_camera"][:3] == [row for row in cam["world_from_camera"][:3]]
    assert manifest["camera"]["frames"] == {"hero": 0, "start": 0, "end": 0, "fps": 25.0}
    assert manifest["units"] == {"system_unit": "millimeters", "meters_per_unit": 0.001, "up_axis": "Z"}
    assert manifest["host"]["plugin"] == {"name": "plate_max", "version": "0.1.0"}
    assert manifest["camera"]["exposure"]["ev100"] == pytest.approx(12.9658, abs=1e-3)


def test_camera_projection_agrees_with_the_spec(scene, tmp_path):
    manifest, *_ = build_still(scene, tmp_path / "pkt")
    cam = manifest["camera"]
    # a point 10 m dead ahead of the camera lands on the principal point
    m = cam["world_from_camera"]
    ahead = [m[i][3] - 10000.0 * m[i][2] for i in range(3)]
    u, v, depth = plate_spec.project_point(cam, ahead)
    assert (u, v) == pytest.approx((1920.0, 1080.0), abs=1e-6)
    assert depth == pytest.approx(10000.0)


def test_written_folder_passes_validate_packet_dir(scene, tmp_path):
    root = tmp_path / "pkt"
    manifest, leg, *_ = build_still(scene, root)
    out = packet.write_packet(root, manifest, leg)
    assert out == root
    assert plate_spec.validate_packet_dir(root, check_files=True) == []
    assert read_json(root / "manifest.json") == manifest
    assert read_json(root / "id_legend.json") == leg
    names = [p.name for p in root.iterdir()]
    assert sorted(names) == ["beauty.exr", "display.tif", "id_legend.json", "manifest.json"]
    assert all(":" not in n for n in names)
    assert b"\r\n" not in (root / "manifest.json").read_bytes()


def test_no_temp_files_left_behind(scene, tmp_path):
    root = tmp_path / "pkt"
    manifest, leg, *_ = build_still(scene, root)
    packet.write_packet(root, manifest, leg)
    assert not list(root.glob("*.tmp"))


def test_non_ascii_names_survive_utf8(scene, tmp_path):
    scene.objects.append(make_node("VEG_café_palm", "VEG_café", "MAT_feuille"))
    root = tmp_path / "pkt"
    manifest, leg, *_ = build_still(scene, root)
    packet.write_packet(root, manifest, leg)
    assert plate_spec.validate_packet_dir(root, check_files=True) == []
    assert "café" in (root / "id_legend.json").read_text(encoding="utf-8")


def test_example_rewritten_through_write_packet_stays_valid(tmp_path):
    root = plate_spec.materialize_example(EXAMPLE, tmp_path / "ex")
    manifest = read_json(root / "manifest.json")
    packet.write_packet(root, manifest)  # manifest only: the folder's own legend stays
    assert read_json(root / "manifest.json") == manifest
    assert plate_spec.validate_packet_dir(root, check_files=True) == []


def test_rewriting_the_legend_with_different_bytes_is_refused(tmp_path):
    root = plate_spec.materialize_example(EXAMPLE, tmp_path / "ex")
    manifest = read_json(root / "manifest.json")
    leg = read_json(root / "id_legend.json")
    with pytest.raises(packet.ManifestError, match="bytes"):
        packet.write_packet(root, manifest, leg)  # indent differs from the example's file


def test_architecture_in_editable_ids_is_rejected(scene, tmp_path):
    manifest, leg, *_ = build_still(scene, tmp_path / "pkt")
    arch = legend.object_id("ARCH_walls")
    # added without being removed from locked: the manifest alone catches it
    bad = copy.deepcopy(manifest)
    bad["policy"]["editable_ids"].append(arch)
    assert any("both locked and editable" in e for e in plate_spec.validate_manifest(bad))
    # moved from locked to editable: the legend check catches it
    moved = copy.deepcopy(manifest)
    moved["policy"]["locked_ids"].remove(arch)
    moved["policy"]["editable_ids"].append(arch)
    assert plate_spec.validate_manifest(moved) == []
    assert any("can never be AI-editable" in e for e in plate_spec.validate_legend(leg, moved))


def test_tampering_after_writing_is_caught_by_the_hash_check(scene, tmp_path):
    root = tmp_path / "pkt"
    manifest, leg, *_ = build_still(scene, root)
    packet.write_packet(root, manifest, leg)
    (root / "display.tif").write_bytes(b"PLACEHOLDER display\n!")
    assert any("blake3 mismatch" in e or "size" in e for e in plate_spec.validate_packet_dir(root))


# ---- the plugin refuses what must not be sent


def test_unlabelled_coverage_over_the_share_blocks_send(scene, tmp_path):
    extra = [{"name": "Box042", "material": "MAT_default", "layer": "0", "tags": [], "pixel_share": 0.08}]
    with pytest.raises(packet.ManifestError, match="policy lint blocks Send"):
        build_still(scene, tmp_path / "pkt", extra_nodes=extra)


def test_unlabelled_coverage_under_the_share_is_recorded(scene, tmp_path):
    extra = [{"name": "Box042", "material": "MAT_default", "layer": "0", "tags": [], "pixel_share": 0.02}]
    manifest, leg, *_ = build_still(scene, tmp_path / "pkt", extra_nodes=extra)
    assert manifest["policy"]["lint"]["unlabelled_share"] == pytest.approx(0.02)
    assert manifest["id_legend"]["coverage"]["unlabelled_share"] == pytest.approx(0.02)
    assert legend.object_id("Box042") in manifest["policy"]["locked_ids"]
    assert plate_spec.validate_manifest(manifest) == []
    assert plate_spec.validate_legend(leg, manifest) == []


def test_shifted_camera_is_refused(scene, tmp_path):
    scene.cameras["CAM_main"].vertical_shift = 0.2
    with pytest.raises(packet.ManifestError, match="lens shift"):
        build_still(scene, tmp_path / "pkt")


def test_scaled_camera_is_refused(scene, tmp_path):
    from fake_pymxs import FakeMatrix3

    scene.cameras["CAM_main"].objecttransform = FakeMatrix3(((2, 0, 0), (0, 2, 0), (0, 0, 2), (0, 0, 0)))
    with pytest.raises(packet.ManifestError, match="orthonormal"):
        build_still(scene, tmp_path / "pkt")


def test_missing_camera_values_name_the_field(scene, tmp_path):
    del scene.cameras["CAM_main"].focus_distance
    with pytest.raises(packet.ManifestError, match="focus_distance"):
        build_still(scene, tmp_path / "pkt")


def test_unsendable_units_are_refused(scene, tmp_path):
    scene.units.SystemType = "kilometers"
    with pytest.raises(packet.ManifestError, match="units"):
        build_still(scene, tmp_path / "pkt")


def test_missing_blake3_hashes_are_refused(scene, tmp_path):
    with pytest.raises(packet.ManifestError, match="scene_hash"):
        build_still(scene, tmp_path / "pkt", scene_hash=None)


def test_bad_inputs_are_refused(scene, tmp_path):
    leg = {"packet_id": new_packet_id(), "entries": []}
    with pytest.raises(packet.ManifestError, match="slug"):
        packet.build_manifest({}, {}, {}, leg, {}, "Bad Id", "s", {}, {}, {}, {})
    with pytest.raises(packet.ManifestError, match="BLAKE3"):
        build_still(scene, tmp_path / "b", scene_hash="not-hex", view_hash="0" * 64)
    with pytest.raises(packet.ManifestError, match="colour"):
        packet.build_manifest({}, {}, {}, leg, {}, "p", "s", {}, {}, {"max_colour_mode": "unknown"}, {})


def test_slugify():
    assert packet.slugify("CAM_C03_clubhouse entry!") == "cam-c03-clubhouse-entry"
    with pytest.raises(packet.ManifestError):
        packet.slugify("!!!")


def test_hash_file_sha256_matches_hashlib(tmp_path):
    import hashlib

    p = tmp_path / "f.bin"
    p.write_bytes(b"plate" * 1000)
    assert packet.hash_file_sha256(p) == hashlib.sha256(b"plate" * 1000).hexdigest()


def test_file_ref_shape():
    ref = packet.file_ref("beauty.exr", 5, "a" * 64, "exr", width=16)
    assert ref == {
        "kind": "file",
        "path": "beauty.exr",
        "bytes": 5,
        "hash": {"algo": "blake3", "hex": "a" * 64},
        "format": "exr",
        "width": 16,
    }


# ---- motion


def _build_motion(scene, root, mutate=None):
    scene.animationRange.end.frame = 4
    cam = digest.camera_digest("CAM_main")
    sc = digest.scene_digest()
    nodes = [{**n, "pixel_share": 0.16} for n in digest.scene_nodes()]
    leg = legend.build_id_legend(nodes, digest_fn=b3)
    pol = policy.derive_policy(leg)
    w, h = cam["resolution"]
    from packet_fixture import elements

    beauty, display = put(root, "beauty.exr", b"B\n"), put(root, "display.tif", b"D\n")
    lb = packet.legend_bytes(leg)

    frames = []
    base = cam["world_from_camera"]
    for n in range(5):
        m = [list(r) for r in base]
        m[0][3] += 100.0 * n
        frames.append({"frame": n, "world_from_camera": m})

    def seq(role, ext, fmt, dtype):
        lines = []
        for n in range(5):
            rel = f"motion/{role}.{n:04d}.{ext}"
            _, hexd = put(root, rel, f"{role} {n}\n".encode())
            lines.append(f"{hexd}  {rel}")
        put(root, f"motion/{role}.blake3", ("\n".join(lines) + "\n").encode())
        return {
            "kind": "sequence",
            "pattern": f"motion/{role}.%04d.{ext}",
            "first": 0,
            "last": 4,
            "format": fmt,
            "width": 1280,
            "height": 720,
            "dtype": dtype,
            "hashes": f"motion/{role}.blake3",
        }

    files = {
        "beauty_exr": packet.file_ref(
            "beauty.exr",
            beauty[0],
            beauty[1],
            "exr",
            width=w,
            height=h,
            compression="zip",
            elements=elements(),
        ),
        "display_tiff16": packet.file_ref(
            "display.tif", display[0], display[1], "tiff", width=w, height=h, bit_depth=16, channels="RGB"
        ),
        "id_legend": packet.file_ref("id_legend.json", len(lb), b3(lb), "json"),
        "clay_seq": seq("clay", "png", "png", "uint16"),
        "id_seq": seq("id", "exr", "exr", "float"),
        "z_seq": seq("z", "exr", "exr", "float"),
    }
    # camera_frames.json needs the manifest's fps, which comes from the camera block
    probe = packet.build_camera_block(cam, "motion")
    fb = packet.dump_json_bytes(
        {
            "packet_id": leg["packet_id"],
            "convention": "plate-cam-v0",
            "fps": probe["frames"]["fps"],
            "frames": frames,
        }
    )
    files["camera_frames"] = packet.file_ref("camera_frames.json", len(fb), b3(fb), "json")
    if mutate:
        mutate(files)
    from packet_fixture import colour_block, render_block

    manifest = packet.build_manifest(
        cam,
        sc,
        files,
        leg,
        pol,
        "tula-wot",
        "drive-in",
        {"version": "2026.2"},
        {"name": "vray", "version": "7.00.03"},
        colour_block(),
        render_block(),
        kind="motion",
        scene_hash=b3(digest.scene_hash_payload(sc)),
        view_hash=b3(view_payload(probe, leg)),
        motion={
            "hero_still_packet_id": new_packet_id(),
            "sequence_resolution": {"width": 1280, "height": 720},
            "frame0_matches_hero": True,
            "clay_override": {"material": "PLATE_clay", "keeps_glass": True, "keeps_lights": True},
        },
    )
    return manifest, leg, frames


def test_motion_packet_validates_on_disk(scene, tmp_path):
    root = tmp_path / "motion"
    manifest, leg, frames = _build_motion(scene, root)
    packet.write_packet(root, manifest, leg, frames)
    assert manifest["camera"]["frames"] == {"hero": 0, "start": 0, "end": 4, "fps": 25.0}
    assert manifest["camera"]["frames_file"] == "camera_frames"
    assert plate_spec.validate_packet_dir(root, check_files=True) == []


def test_motion_packet_requires_camera_frames_and_a_still_refuses_them(scene, tmp_path):
    root = tmp_path / "motion"
    manifest, leg, frames = _build_motion(scene, root)
    with pytest.raises(packet.ManifestError, match="camera_frames"):
        packet.write_packet(root, manifest, leg)
    still_root = tmp_path / "still"
    still, still_leg, *_ = build_still(scene, still_root)
    with pytest.raises(packet.ManifestError, match="motion packets only"):
        packet.write_packet(still_root, still, still_leg, frames)


def test_motion_hero_must_be_the_first_frame(scene, tmp_path):
    scene.currentTime.frame = 2
    with pytest.raises(packet.ManifestError, match="hero frame"):
        _build_motion(scene, tmp_path / "motion")


def test_json_is_valid_utf8_without_bom(scene, tmp_path):
    root = tmp_path / "pkt"
    manifest, leg, *_ = build_still(scene, root)
    packet.write_packet(root, manifest, leg)
    raw = (root / "manifest.json").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    json.loads(raw.decode("utf-8"))
    assert re.search(rb"\n$", raw)


# ---- what v0.1 refuses or requires (spec revision of 2026-10-17)


@pytest.mark.parametrize(
    ("attr", "value", "why"),
    [
        ("horizontal_shift", 0.05, "lens shift"),
        ("vertical_shift", -0.2, "lens shift"),
        ("vertical_tilt", 3.5, "tilt"),
        ("horizontal_tilt", 1.0, "tilt"),
        ("auto_vertical_tilt", True, "tilt"),
        ("distortion", 0.02, "distortion"),
        ("distortion_type", 1, "distortion"),
    ],
)
def test_shifted_tilted_or_distorted_cameras_are_refused(scene, tmp_path, attr, value, why):
    setattr(scene.cameras["CAM_main"], attr, value)
    with pytest.raises(packet.ManifestError, match=why):
        build_still(scene, tmp_path / "pkt")


def test_mirrored_camera_is_refused(scene, tmp_path):
    from fake_pymxs import FakeMatrix3

    scene.cameras["CAM_main"].objecttransform = FakeMatrix3(((-1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, 0)))
    with pytest.raises(packet.ManifestError, match="mirrored"):
        build_still(scene, tmp_path / "pkt")


def test_camera_block_carries_the_required_constants(scene, tmp_path):
    manifest, *_ = build_still(scene, tmp_path / "pkt")
    cam = manifest["camera"]
    assert cam["distortion"] == "none"
    assert cam["lens_shift"] == {
        "horizontal": 0,
        "vertical": 0,
        "raw_unit": "vray_physical",
        "vertical_tilt_correction": False,
    }
    assert cam["convention"]["id"] == "plate-cam-v0"


@pytest.mark.parametrize("focal", [18.0, 27.3, 31.7, 85.0])
def test_intrinsics_use_full_precision(scene, tmp_path, focal):
    cam = scene.cameras["CAM_main"]
    cam.focal_length = focal
    del cam.fov  # let the digest derive nothing: focal is authoritative
    manifest, *_ = build_still(scene, tmp_path / "pkt")
    k = manifest["camera"]["intrinsics_px"]
    assert k["fx"] == focal * 1.0 * 3840 / 36.0  # not rounded
    assert k["K"][0][0] == k["fx"] and k["K"][1][2] == 1080.0
    assert plate_spec.validate_manifest(manifest) == []


def test_uncovered_share_makes_the_three_shares_sum_to_one(scene, tmp_path):
    from packet_fixture import node_inputs

    root = tmp_path / "pkt"
    # leave 12% of the frame to the environment background: no node covers it
    nodes = node_inputs(scene, spare=0.12)
    manifest, leg, *_ = build_still(scene, root, nodes=nodes)
    cov = manifest["id_legend"]["coverage"]
    assert cov == {
        "labelled_share": pytest.approx(0.88),
        "unlabelled_share": 0.0,
        "uncovered_share": pytest.approx(0.12),
    }
    assert plate_spec.validate_manifest(manifest) == []


def test_over_full_shares_are_refused(scene, tmp_path):
    nodes = [{**n, "pixel_share": 0.5} for n in digest.scene_nodes()]
    with pytest.raises(packet.ManifestError, match="whole frame"):
        build_still(scene, tmp_path / "pkt", nodes=nodes)


def test_cryptomatte_id_types_and_entry_ids_match_the_names(scene, tmp_path):
    root = tmp_path / "pkt"
    manifest, leg, *_ = build_still(scene, root)
    crypto = manifest["id_legend"]["cryptomatte"]
    assert (crypto["object"]["id_type"], crypto["material"]["id_type"]) == ("node_name", "material_name")
    for e in leg["entries"]:
        assert e["id"] == plate_spec.cryptomatte_id(e["name"], e["kind"])
        assert len(e["digest"]) == 64
    assert plate_spec.validate_legend(leg, manifest) == []


def test_exr_compression_must_be_lossless(scene, tmp_path):
    for bad in ("dwaa", "dwab", "pxr24", "b44"):
        with pytest.raises(packet.ManifestError, match="compression"):
            build_still(scene, tmp_path / bad, compression=bad)
    for ok in packet.LOSSLESS_EXR:
        manifest, *_ = build_still(scene, tmp_path / ok, compression=ok)
        assert plate_spec.validate_manifest(manifest) == []


def test_partial_safe_frame_needs_matching_overscan(scene, tmp_path):
    cam = digest.camera_digest("CAM_main")
    cam["safe_frame"] = {"x": 0, "y": 277, "width": 3840, "height": 1606}
    with pytest.raises(packet.ManifestError, match="overscan"):
        packet.build_camera_block(cam)
    cam["overscan"] = {"engine_aspect": "21:9"}
    with pytest.raises(packet.ManifestError, match="engine_aspect"):
        packet.build_camera_block(cam)
    cam["overscan"] = {"engine_aspect": "16:9", "note": "2.39:1 inside 16:9"}
    assert packet.build_camera_block(cam)["safe_frame"]["y"] == 277
    cam["safe_frame"] = {"x": 0, "y": 600, "width": 3840, "height": 1606}
    with pytest.raises(packet.ManifestError, match="outside"):
        packet.build_camera_block(cam)


def test_partial_safe_frame_validates_end_to_end(scene, tmp_path):
    import packet_fixture as fx

    original = digest.camera_digest

    def with_safe_frame(name):
        out = original(name)
        out["safe_frame"] = {"x": 0, "y": 277, "width": 3840, "height": 1606}
        out["overscan"] = {"engine_aspect": "16:9"}
        return out

    digest.camera_digest = with_safe_frame
    try:
        manifest, *_ = fx.build_still(scene, tmp_path / "pkt")
    finally:
        digest.camera_digest = original
    assert plate_spec.validate_manifest(manifest) == []


def test_nda_custody_needs_allowed_grades(scene, tmp_path):
    with pytest.raises(packet.ManifestError, match="allowed_grades"):
        build_still(scene, tmp_path / "a", custody={"nda": True})
    with pytest.raises(packet.ManifestError, match="C0 and C1"):
        build_still(scene, tmp_path / "b", custody={"nda": True, "allowed_grades": ["C0", "C2"]})
    manifest, *_ = build_still(scene, tmp_path / "c", custody={"nda": True, "allowed_grades": ["C0", "C1"]})
    assert plate_spec.validate_manifest(manifest) == []


def test_view_hash_payload_reacts_to_pixel_state(scene):
    from packet_fixture import colour_block, render_block

    cam = packet.build_camera_block(digest.camera_digest("CAM_main"))
    leg = legend.build_id_legend(digest.scene_nodes(), digest_fn=b3)

    def payload(**changes):
        args = {
            "leg": leg,
            "colour": colour_block(),
            "render": render_block(),
            "globals_": digest.scene_globals(),
        }
        args.update(changes)
        return packet.view_hash_payload(cam, args["leg"], args["colour"], args["render"], args["globals_"])

    base = payload()
    assert payload() == base
    # a light changed
    scene.lights[0].multiplier = 3.0
    assert payload() != base
    scene.lights[0].multiplier = 1.5
    assert payload() == base
    # render timings do not matter, noise settings do
    assert payload(render={**render_block(), "time_s": 1.0, "peak_rss_gb": 3}) == base
    assert payload(render={**render_block(), "noise_threshold": 0.001}) != base
    # colour management matters
    assert payload(colour={**colour_block(), "vfb_corrections_baked": False}) != base
    # an edited node (same name, new transform) changes its digest and so the view
    scene.objects[2].objecttransform.row4 = (9.0, 9.0, 9.0)
    moved = legend.build_id_legend(digest.scene_nodes(), digest_fn=b3)
    assert payload(leg=moved) != base


def test_data_sequences_must_be_float_exr_with_one_frame_token_and_the_render_aspect(scene, tmp_path):
    def z_as_png(files):
        files["z_seq"]["format"] = "png"

    def two_tokens(files):
        files["clay_seq"]["pattern"] = "motion/clay.%04d.%04d.png"

    def wrong_aspect(files):
        files["id_seq"]["height"] = 700

    def half_float(files):
        files["z_seq"]["dtype"] = "half"

    for n, (mutate, match) in enumerate(
        [
            (z_as_png, "float EXR"),
            (two_tokens, "%0Nd"),
            (wrong_aspect, "aspect"),
            (half_float, "float EXR"),
        ]
    ):
        with pytest.raises(packet.ManifestError, match=match):
            _build_motion(scene, tmp_path / f"m{n}", mutate)
