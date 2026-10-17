"""Builds a complete tier A packet from fake_scene() for the writer, transport and bridge tests.

The Max-side hashes need BLAKE3, which plate_max cannot compute. build_still() computes them with the
dev venv's blake3 package directly; StubDaemon is a stdlib http.server stand-in for the PLATE daemon's
/api/v0/hash route (decision 5b: token check, paths confined to the packets folder, payloads as
base64) that computes them the same way, so build_still_via_daemon() exercises bridge.hashing end to
end. Binary payloads are small placeholders.
"""

from __future__ import annotations

import base64
import copy
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


# --------------------------------------------------------------------------- stub daemon (/api/v0/hash)

STUB_TOKEN = "fedcba9876543210fedcba9876543210"


class StubDaemon:
    """What the stub answers and what it saw. root is the packets folder /hash confines paths to."""

    def __init__(self, root: Path, token: str = STUB_TOKEN):
        self.root = Path(root)
        self.token = token
        self.requests: list[dict[str, Any]] = []
        self.override: tuple[int, Any] | None = None  # answer every /hash with (status, body)

    def answer(self, headers: Any, body: dict[str, Any]) -> tuple[int, Any]:
        self.requests.append(body)
        if headers.get("Authorization") != f"Bearer {self.token}":
            return 401, {"error": "unauthorized"}
        if self.override:
            return self.override
        hashes: dict[str, Any] = {}
        for path in body.get("paths", []):
            real = Path(path).resolve()
            if not real.is_relative_to(self.root.resolve()):
                return 403, {"error": "outside the packets folders", "path": path}
            if not real.is_file():
                return 404, {"error": "no such file", "path": path}
            data = real.read_bytes()
            hashes[path] = {"algo": "blake3", "hex": b3(data), "bytes": len(data)}
        for name, text in body.get("payloads", {}).items():
            data = base64.b64decode(text)
            hashes[name] = {"algo": "blake3", "hex": b3(data), "bytes": len(data)}
        return 200, {"hashes": hashes}


@contextmanager
def serve(stub: StubDaemon):
    """Run stub on 127.0.0.1 (an ephemeral port); yields the plate_max config for it."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/api/v0/hash":
                status, answer = stub.answer(self.headers, body)
            else:
                status, answer = 404, {"error": "unknown route"}
            raw = json.dumps(answer).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"token": stub.token, "daemon_url": f"http://127.0.0.1:{server.server_address[1]}"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def build_still_via_daemon(rt: Any, root: Path, config: dict[str, Any], **kwargs: Any):
    """build_still with every BLAKE3 from the daemon through plate_max.bridge.hashing: the files in the
    folder (hash_packet_files), the legend node and material digests (with_daemon_digests), the legend
    sidecar, the scene digest and the view payload. Writes the packet and returns
    (root, manifest, legend_doc, hashes)."""
    from plate_max.bridge import hashing
    from plate_max.packet_id import new_packet_id

    cam = digest.camera_digest("CAM_main")
    scene = digest.scene_digest()
    nodes = node_inputs(rt)
    pid = new_packet_id()
    leg = hashing.with_daemon_digests(lambda fn: legend.build_id_legend(nodes, pid, digest_fn=fn), config)
    pol = policy.derive_policy(leg)

    w, h = cam["resolution"]
    put(root, "beauty.exr", b"\x76\x2f\x31\x01 PLACEHOLDER beauty\n")
    put(root, "display.tif", b"PLACEHOLDER display\n")
    hashes = hashing.hash_packet_files(root, config)
    hashes.update(hashing.sidecar_hashes(config, legend=leg))
    files = {
        "beauty_exr": packet.file_ref_from_hashes(
            "beauty.exr", hashes, "exr", width=w, height=h, compression="zip", elements=elements()
        ),
        "display_tiff16": packet.file_ref_from_hashes(
            "display.tif", hashes, "tiff", width=w, height=h, bit_depth=16, channels="RGB"
        ),
        "id_legend": packet.file_ref_from_hashes("id_legend.json", hashes, "json"),
    }
    camera_block = packet.build_camera_block(cam)
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
        scene_hash=hashing.hash_scene(scene, config),
        view_hash=hashing.hash_view(view_payload(camera_block, leg), config),
        **kwargs,
    )
    packet.write_packet(root, manifest, leg, hashes=hashes)
    return root, manifest, leg, hashes
