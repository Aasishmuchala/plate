"""bridge.hashing and bridge.colour against the stdlib stub daemon (packet_fixture.StubDaemon): every
BLAKE3 a packet carries comes from POST /api/v0/hash, and the written folder passes
plate_spec.validate_packet_dir(check_files=True)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from packet_fixture import STUB_TOKEN, StubDaemon, b3, build_still_via_daemon, node_inputs, put, serve

import plate_spec
from plate_max import digest, legend, packet, transport
from plate_max.bridge import colour, hashing


@pytest.fixture
def packets_dir(tmp_path):
    root = tmp_path / "packets"
    root.mkdir()
    return root


@pytest.fixture
def daemon(packets_dir):
    stub = StubDaemon(packets_dir)
    with serve(stub) as config:
        yield stub, config


def test_hash_packet_files_hashes_every_file_in_the_folder(daemon, packets_dir):
    stub, config = daemon
    root = packets_dir / "pkt_a"
    put(root, "beauty.exr", b"B" * 100)
    put(root, "display.tif", b"D" * 50)
    put(root, "motion/z.0000.exr", b"Z0")
    (root / "plate-x.tmp").write_bytes(b"half written")  # a write in progress is never hashed
    out = hashing.hash_packet_files(root, config)  # no manifest yet: every file in the folder
    assert out == {
        "beauty.exr": {"hex": b3(b"B" * 100), "bytes": 100},
        "display.tif": {"hex": b3(b"D" * 50), "bytes": 50},
        "motion/z.0000.exr": {"hex": b3(b"Z0"), "bytes": 2},
    }
    assert len(stub.requests) == 1 and len(stub.requests[0]["paths"]) == 3
    (root / "manifest.json").write_text("{}\n", encoding="utf-8")
    assert "manifest.json" not in hashing.folder_files(root)
    assert hashing.hash_packet_files(root, config) == {}  # a manifest that references nothing


def test_with_a_manifest_only_the_referenced_files_are_hashed(daemon, packets_dir):
    _, config = daemon
    root = plate_spec.materialize_example("tierA_still_clubhouse", packets_dir / "ex")
    (root / "notes.txt").write_text("not part of the packet\n", encoding="utf-8")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    out = hashing.hash_packet_files(root, config)
    assert (
        sorted(out)
        == hashing.referenced_files(manifest)
        == sorted(ref["path"] for ref in manifest["files"].values())
    )
    for role, ref in manifest["files"].items():
        assert out[ref["path"]]["hex"] == ref["hash"]["hex"], role


def test_a_daemon_built_still_packet_validates_on_disk(scene, daemon, packets_dir):
    stub, config = daemon
    root, manifest, leg, hashes = build_still_via_daemon(scene, packets_dir / "pkt_still", config)
    assert plate_spec.validate_packet_dir(root, check_files=True) == []
    assert manifest["scene_hash"]["digest_version"] == "1"
    assert manifest["scene_hash"]["hex"] == b3(digest.scene_hash_payload(digest.scene_digest()))
    # the legend digests the daemon gave are the BLAKE3 a local digest_fn gives
    local = legend.build_id_legend(node_inputs(scene), leg["packet_id"], digest_fn=b3)
    assert local == leg
    # one request for the files, one for the legend digests, one each for sidecar, scene, view
    assert len(stub.requests) == 5


def test_the_schema_hash_stays_blake3_only():
    schema = plate_spec.load_schema()
    assert schema["$defs"]["hash"]["properties"]["algo"] == {"const": "blake3"}


def test_a_folder_outside_the_packets_dir_is_refused(daemon, tmp_path):
    _, config = daemon
    outside = tmp_path / "elsewhere"
    put(outside, "beauty.exr", b"B")
    with pytest.raises(transport.HashRefused) as info:
        hashing.hash_packet_files(outside, config)
    assert info.value.status_code == 403


def test_a_wrong_token_is_unauthorized(daemon, packets_dir):
    _, config = daemon
    put(packets_dir / "p", "beauty.exr", b"B")
    with pytest.raises(transport.Unauthorized):
        hashing.hash_packet_files(packets_dir / "p", {**config, "token": "wrong" + STUB_TOKEN})


def test_an_answer_that_disagrees_with_the_disk_is_refused(daemon, packets_dir):
    stub, config = daemon
    root = packets_dir / "p"
    put(root, "beauty.exr", b"B" * 10)
    path = str((root / "beauty.exr").resolve())
    stub.override = (200, {"hashes": {path: {"algo": "blake3", "hex": "a" * 64, "bytes": 11}}})
    with pytest.raises(hashing.HashAnswerError, match="11 bytes"):
        hashing.hash_packet_files(root, config)


def test_payloads_are_batched_under_the_cap(daemon, monkeypatch):
    stub, config = daemon
    monkeypatch.setattr(transport, "HASH_PAYLOAD_LIMIT", 10)
    out = hashing.hash_payload_bytes({"a": b"123456", "b": b"7890", "c": b"abcdefgh"}, config)
    assert out == {
        "a": {"hex": b3(b"123456"), "bytes": 6},
        "b": {"hex": b3(b"7890"), "bytes": 4},
        "c": {"hex": b3(b"abcdefgh"), "bytes": 8},
    }
    assert [sorted(r["payloads"]) for r in stub.requests] == [["a", "b"], ["c"]]
    with pytest.raises(ValueError, match="cap"):
        hashing.hash_payload_bytes({"big": b"x" * 11}, config)


def test_scene_and_view_hash_blocks(scene, daemon):
    _, config = daemon
    sc = digest.scene_digest()
    assert hashing.hash_scene(sc, config) == {
        "algo": "blake3",
        "hex": b3(digest.scene_hash_payload(sc)),
        "digest_version": "1",
    }
    assert hashing.hash_view(b"view bytes", config) == {"algo": "blake3", "hex": b3(b"view bytes")}


def test_with_daemon_digests_needs_one_request_for_all_records(scene, daemon):
    stub, config = daemon
    nodes = node_inputs(scene)
    leg = hashing.with_daemon_digests(lambda fn: legend.build_id_legend(nodes, "pkt_X", digest_fn=fn), config)
    assert len(stub.requests) == 1
    assert all(len(e["digest"]) == 64 and e["digest"] != hashing.PLACEHOLDER_HEX for e in leg["entries"])
    assert leg == legend.build_id_legend(nodes, "pkt_X", digest_fn=b3)


def test_a_file_changed_after_hashing_gets_no_manifest(scene, daemon, packets_dir):
    _, config = daemon
    root, manifest, leg, hashes = build_still_via_daemon(scene, packets_dir / "pkt", config)
    (root / "manifest.json").unlink()
    (root / "display.tif").write_bytes(b"re-saved by the artist\n")
    fresh = hashing.hash_packet_files(root, config)
    with pytest.raises(packet.ManifestError, match="changed since it was hashed"):
        packet.write_packet(root, manifest, leg, hashes=fresh)


def test_sequence_listing_matches_the_spec_format(daemon, packets_dir):
    _, config = daemon
    root = packets_dir / "m"
    for n in range(3):
        put(root, f"motion/z.{n:04d}.exr", f"z{n}".encode())
    hashes = hashing.hash_packet_files(root, config)
    listing = hashing.sequence_listing("motion/z.%04d.exr", 0, 2, hashes)
    assert listing.decode("utf-8").splitlines() == [
        f"{b3(f'z{n}'.encode())}  motion/z.{n:04d}.exr" for n in range(3)
    ]
    with pytest.raises(KeyError, match="frame 3"):
        hashing.sequence_listing("motion/z.%04d.exr", 0, 3, hashes)


# --------------------------------------------------------------------------- colour pipeline


def _manager(**props):
    return SimpleNamespace(**props)


def test_ocio_with_a_config_file_is_hashed_by_the_daemon(scene, daemon, tmp_path):
    stub, config = daemon
    cfg = tmp_path / "ocio" / "studio-config-v2.1.0_aces-v1.3_ocio-v2.3.ocio"
    cfg.parent.mkdir()
    cfg.write_bytes(b"ocio_profile_version: 2.3\n")
    scene.ColorPipelineMgr = _manager(
        Mode="#OCIO_Custom",
        OCIOConfigPath=str(cfg),
        displayName="sRGB - Display",
        viewName="ACES 1.0 - SDR Video",
        renderingSpace="ACEScg",
    )
    out = colour.detect_colour_pipeline(config)
    assert out["mode"] == "ocio" and out["reason"] is None
    assert out["config_hash"] == {"algo": "blake3", "hex": b3(cfg.read_bytes())}
    assert out["config_hash_source"] == "file"
    assert out["config_name"] == "studio-config-v2.1.0_aces-v1.3_ocio-v2.3"
    assert (out["display"], out["view"], out["working_space"]) == (
        "sRGB - Display",
        "ACES 1.0 - SDR Video",
        "acescg",
    )
    assert out["raw"]["Mode"] == "#OCIO_Custom"
    assert "payloads" in stub.requests[-1]  # the config lives outside the packets dir: sent as bytes
    block = colour.manifest_colour(out, vfb_corrections_baked=True, vfb_layers=["Exposure"])
    example = plate_spec.load_example("tierA_still_clubhouse")
    manifest = {**example, "colour": block}
    assert plate_spec.validate_manifest(manifest) == []
    assert colour.blocks_send(out) is None


def test_a_builtin_ocio_config_hashes_its_uri(scene):
    scene.ColorPipelineMgr = _manager(
        Mode="OCIO_Default",
        OCIOConfigPath="ocio://studio-config-v2.1.0_aces-v1.3_ocio-v2.3",
        displayName="sRGB - Display",
        viewName="ACES 1.0 - SDR Video",
    )
    out = colour.detect_colour_pipeline(hasher=b3)
    assert out["mode"] == "ocio" and out["config_hash_source"] == "builtin_uri"
    assert out["config_hash"]["hex"] == b3(b"ocio://studio-config-v2.1.0_aces-v1.3_ocio-v2.3")
    assert out["config_name"] == "studio-config-v2.1.0_aces-v1.3_ocio-v2.3"


def test_legacy_gamma(scene):
    scene.ColorPipelineMgr = _manager(Mode="#Gamma", gammaEnabled=True, outputGamma=2.2)
    out = colour.detect_colour_pipeline()
    assert out["mode"] == "legacy_gamma" and out["legacy_gamma"] == 2.2
    block = colour.manifest_colour(out, vfb_corrections_baked=False)
    assert block["legacy_gamma"] == 2.2 and block["working_space"] == "lin_rec709" and "ocio" not in block


@pytest.mark.parametrize(
    ("props", "why"),
    [
        (None, "no rt.ColorPipelineMgr"),
        ({"Mode": "#Unmanaged"}, "neither OCIO nor gamma"),
        ({}, "unreadable"),
        (
            {
                "Mode": "OCIO_Custom",
                "OCIOConfigPath": "Z:/gone.ocio",
                "displayName": "sRGB",
                "viewName": "Raw",
            },
            "config_hash",
        ),
        ({"Mode": "OCIO_Custom", "OCIOConfigPath": "ocio://cg", "viewName": "Raw"}, "display"),
        ({"Mode": "#Gamma"}, "display gamma"),
    ],
)
def test_an_unknown_colour_mode_blocks_send(scene, props, why):
    if props is not None:
        scene.ColorPipelineMgr = _manager(**props)
    out = colour.detect_colour_pipeline(hasher=b3)
    assert out["mode"] == "unknown" and why in out["reason"]
    assert why in colour.blocks_send(out)
    with pytest.raises(ValueError):
        colour.manifest_colour(out, vfb_corrections_baked=True)


def test_an_unreachable_daemon_leaves_the_config_unhashed(scene, tmp_path):
    cfg = tmp_path / "c.ocio"
    cfg.write_bytes(b"ocio_profile_version: 2\n")
    scene.ColorPipelineMgr = _manager(
        Mode="OCIO", OCIOConfigPath=str(cfg), displayName="sRGB", viewName="Raw"
    )
    out = colour.detect_colour_pipeline({"token": "t", "daemon_url": "http://127.0.0.1:9"})
    assert out["mode"] == "unknown" and out["config_hash"] is None and "config_hash" in out["reason"]
