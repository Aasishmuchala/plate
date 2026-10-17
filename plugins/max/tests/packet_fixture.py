"""Builds a complete tier A packet from fake_scene() for the writer and transport tests.

The Max-side hashes need BLAKE3, which plate_max cannot compute, so this test helper does it with the
dev venv's blake3 package, the way the render step will. Binary payloads are small placeholders.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import blake3
from fake_pymxs import scene_pixel_shares

import plate_spec
from plate_max import digest, legend, packet, policy

EXAMPLE = "tierA_still_clubhouse"


def b3(data: bytes) -> str:
    return blake3.blake3(data).hexdigest()


def put(root: Path, rel: str, data: bytes) -> tuple[int, str]:
    path = root.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data), b3(data)


def node_inputs(rt: Any, spare: float = 0.0) -> list[dict[str, Any]]:
    """The scene's nodes with their render-side coverage, scaled to leave `spare` of the frame to others."""
    shares = scene_pixel_shares()
    return [{**n, "pixel_share": shares.get(n["name"], 0.0) * (1 - spare)} for n in digest.scene_nodes()]


def colour_block() -> dict[str, Any]:
    return copy.deepcopy(plate_spec.load_example(EXAMPLE)["colour"])


def render_block() -> dict[str, Any]:
    return {
        "session": "interactive",
        "sampler": "bucket",
        "noise_threshold": 0.005,
        "max_subdivs": 24,
        "time_s": 1874.2,
        "denoiser": {"beauty": "nvidia_ai", "denoised_elements": ["RGBA"]},
    }


def elements() -> list[dict[str, Any]]:
    return copy.deepcopy(plate_spec.load_example(EXAMPLE)["files"]["beauty_exr"]["elements"])


def build_still(
    rt: Any,
    root: Path,
    extra_nodes: list[dict[str, Any]] | None = None,
    nodes: list[dict[str, Any]] | None = None,
    compression: str = "zip",
    **kwargs: Any,
):
    """Write placeholder beauty/display files into root, then build the manifest and legend.
    nodes replaces the scene's nodes (shares already set); extra_nodes adds to them.
    Returns (manifest, legend_doc, camera_digest, scene_digest)."""
    cam = digest.camera_digest("CAM_main")
    scene = digest.scene_digest()
    extra = list(extra_nodes or [])
    if nodes is None:
        nodes = node_inputs(rt, spare=sum(n["pixel_share"] for n in extra)) + extra
    leg = legend.build_id_legend(nodes, digest_fn=b3)
    pol = policy.derive_policy(leg)

    w, h = cam["resolution"]
    beauty = put(root, "beauty.exr", b"PLACEHOLDER beauty\n")
    display = put(root, "display.tif", b"PLACEHOLDER display\n")
    lb = packet.legend_bytes(leg)
    files = {
        "beauty_exr": packet.file_ref(
            "beauty.exr",
            beauty[0],
            beauty[1],
            "exr",
            width=w,
            height=h,
            compression=compression,
            elements=elements(),
        ),
        "display_tiff16": packet.file_ref(
            "display.tif", display[0], display[1], "tiff", width=w, height=h, bit_depth=16, channels="RGB"
        ),
        "id_legend": packet.file_ref("id_legend.json", len(lb), b3(lb), "json"),
    }
    camera_block = packet.build_camera_block(cam)
    options = {
        "scene_hash": b3(digest.scene_hash_payload(scene)),
        "view_hash": b3(view_payload(camera_block, leg)),
        **kwargs,
    }
    manifest = packet.build_manifest(
        cam,
        scene,
        files,
        leg,
        pol,
        "tula-wot",
        packet.slugify(cam["name"]),
        {"version": "2026.2", "python": "3.11.9"},
        {"name": "vray", "version": "7.00.03"},
        colour_block(),
        render_block(),
        **options,
    )
    return manifest, leg, cam, scene


def view_payload(camera_block: dict[str, Any], leg: dict[str, Any]) -> bytes:
    return packet.view_hash_payload(camera_block, leg, colour_block(), render_block(), digest.scene_globals())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
