"""Send Motion: the clay sequence of an animated camera as a motion Shot Packet (PACKET.md section 10).

A motion packet belongs to a hero still that was sent first. In this order (any failure stops, says
why, and leaves nothing the daemon would ingest):

  hero      the hero still's folder (<out_root>/<hero_packet_id>, or a folder path) and its manifest
            and legend; it must be a tier A still of this camera
  daemon    health + transport.compatible, before anything renders
  scene     the default render elements in place, the scene digest hashed (a scene that changed since
            the hero still is a warning: frame 0 can match while the beauty is stale)
  camera    the path read per frame (bridge.motion.camera_frames) at the sequence size, with the hero's
            overscan widening reapplied when the hero grew wider; frame 0 must be the hero's camera
            within 1e-6 and the scaled hero fx, else FRAME0_MISMATCH and nothing renders
  clay      V-Ray's material override to PLATE_clay_matte_0.7, glass and light materials kept
  render    bridge.motion.render_sequence: clay PNG16, Z and object Cryptomatte EXRs per frame at
            1280x720 or the exact-aspect size, into <out_root>/<packet_id>/motion/
  copy      the hero's beauty.exr and display.tif copied in (a tier A packet carries both; their
            hashes must still be the hero manifest's)
  hash      BLAKE3 of every file and frame from the daemon (decision 5b); the per-sequence hash
            listings written
  manifest  the hero manifest as the base: kind motion, a new packet_id, frames f0..f1, the motion
            block (hero_still_packet_id, sequence_resolution, frame0_matches_hero, clay_override,
            path_crossings); camera_frames.json and the legend (new packet_id) written, manifest last
  post      transport.post_packet (200/202 ok; 409, 422, 426 reported)

run() never raises; every outcome is a MotionResult with a code for the dock.
"""

from __future__ import annotations

import copy
import json
import math
import platform
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import PACKET_VERSION, SENDER, digest, packet, transport
from ..bridge import elements, hashing, motion
from ..bridge.scene import get_camera, rt_attr
from ..config import DEFAULT_DAEMON_URL, load_config
from ..fsutil import atomic_write_bytes
from ..packet_id import PACKET_ID_RE, new_packet_id
from . import send_packet

FRAME0_MISMATCH = motion.FRAME0_MISMATCH
HERO_PACKET = "HERO_PACKET"
HERO_MISMATCH = "HERO_MISMATCH"
CAMERA_MISSING = "CAMERA_MISSING"
BAD_CROSSINGS = "BAD_CROSSINGS"
BAD_SEQUENCE_SIZE = "BAD_SEQUENCE_SIZE"
DAEMON = "DAEMON"
HASH_FAILED = "HASH_FAILED"
MANIFEST_FAILED = "MANIFEST_FAILED"
POST_FAILED = "POST_FAILED"
ERROR = "ERROR"

CROSSING_KINDS = ("threshold", "room_change", "occluder")
FX_REL_TOL = 1e-4  # PACKET.md section 8 rule 3
WIDEN_TOL = 1e-6
CAMERA_FRAMES_NAME = "camera_frames.json"
OK_STATUS = (200, 202)

Progress = Callable[[int, int, str], None]


class MotionAbort(Exception):
    """Stops Send Motion at a stage with a code and the reason for the dock."""

    def __init__(self, stage: str, code: str, reason: str):
        super().__init__(reason)
        self.stage = stage
        self.code = code
        self.reason = reason


@dataclass
class MotionResult:
    camera: str
    hero_packet_id: str
    ok: bool = False
    packet_id: str | None = None
    packet_dir: str | None = None
    stage: str = "start"
    code: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status_code: int | None = None
    daemon: Any = None
    frames: list[int] | None = None  # [f0, f1]
    sequence_resolution: dict[str, int] | None = None
    clay: dict[str, Any] | None = None
    sequence: dict[str, Any] | None = None
    frame0_deviation: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera": self.camera,
            "hero_packet_id": self.hero_packet_id,
            "ok": self.ok,
            "packet_id": self.packet_id,
            "packet_dir": self.packet_dir,
            "stage": self.stage,
            "code": self.code,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "status_code": self.status_code,
            "daemon": self.daemon,
            "frames": list(self.frames) if self.frames else None,
            "sequence_resolution": dict(self.sequence_resolution) if self.sequence_resolution else None,
            "clay": self.clay,
            "sequence": self.sequence,
            "frame0_deviation": self.frame0_deviation,
        }


# --------------------------------------------------------------------------- the hero still


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def hero_folder(hero_packet_id: str | Path, out_root: Path | str) -> Path:
    """The hero still's folder: hero_packet_id itself when it is a packet folder, else
    <out_root>/<hero_packet_id> (where Send Packet put it)."""
    given = Path(str(hero_packet_id))
    if (given / "manifest.json").is_file():
        return given
    return Path(out_root) / str(hero_packet_id)


def load_hero(
    hero_packet_id: str | Path, out_root: Path | str
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """(folder, manifest, legend) of the hero still, or MotionAbort(HERO_PACKET)."""
    folder = hero_folder(hero_packet_id, out_root)
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        raise MotionAbort("hero", HERO_PACKET, f"no hero packet at {folder} (send the still first)")
    try:
        manifest = _read_json(manifest_path)
    except (OSError, ValueError) as exc:
        raise MotionAbort("hero", HERO_PACKET, f"hero manifest.json unreadable: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("kind") != "still" or manifest.get("tier") != "A":
        raise MotionAbort("hero", HERO_PACKET, f"{folder.name} is not a tier A still packet")
    if not PACKET_ID_RE.match(str(manifest.get("packet_id"))):
        raise MotionAbort("hero", HERO_PACKET, f"{folder.name}: the manifest has no valid packet_id")
    files = manifest.get("files") or {}
    for role in packet.STILL_ROLES:
        ref = files.get(role)
        if not isinstance(ref, dict) or not (folder / str(ref.get("path", ""))).is_file():
            raise MotionAbort("hero", HERO_PACKET, f"the hero packet's {role} file is missing")
    try:
        legend = _read_json(folder / files["id_legend"]["path"])
    except (OSError, ValueError) as exc:
        raise MotionAbort("hero", HERO_PACKET, f"hero id_legend.json unreadable: {exc}") from exc
    if legend.get("packet_id") != manifest["packet_id"]:
        raise MotionAbort("hero", HERO_PACKET, "the hero legend's packet_id does not match its manifest")
    return folder, manifest, legend


# --------------------------------------------------------------------------- small checks


def check_crossings(crossings: Any, f0: int, f1: int) -> list[dict[str, Any]]:
    """path_crossings as the schema wants them: {frame (int, inside f0..f1), kind (threshold,
    room_change, occluder), note?}. Raises ValueError naming the bad entry."""
    out: list[dict[str, Any]] = []
    for entry in list(crossings or []):
        if not isinstance(entry, dict):
            raise ValueError(f"path crossing {entry!r} is not a dict")
        kind = entry.get("kind")
        if kind not in CROSSING_KINDS:
            raise ValueError(f"path crossing kind {kind!r}: use one of {', '.join(CROSSING_KINDS)}")
        frame = entry.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, int) or not f0 <= frame <= f1:
            raise ValueError(f"path crossing frame {frame!r} is not a whole frame in {f0}..{f1}")
        item: dict[str, Any] = {"frame": frame, "kind": kind}
        if entry.get("note"):
            item["note"] = str(entry["note"])
        extra = set(entry) - {"frame", "kind", "note"}
        if extra:
            raise ValueError(f"path crossing at frame {frame}: unknown keys {sorted(extra)}")
        out.append(item)
    return out


def sequence_resolution(hero_camera: dict[str, Any], requested: Any = None) -> tuple[int, int]:
    """The sequence size: requested (w, h) when given (it must have exactly the render's aspect),
    else bridge.motion.sequence_size of the hero's rendered resolution."""
    width = int(hero_camera["resolution"]["width"])
    height = int(hero_camera["resolution"]["height"])
    if requested is None:
        return motion.sequence_size(width, height)
    if isinstance(requested, dict):
        w, h = int(requested["width"]), int(requested["height"])
    else:
        w, h = (int(v) for v in requested)
    if w < 16 or h < 16 or w * height != h * width:
        raise ValueError(f"sequence size {w}x{h} does not have the render's exact aspect ({width}x{height})")
    return w, h


def widen_factor(hero_camera: dict[str, Any], cam0: dict[str, Any]) -> float:
    """How much the camera's horizontal field of view must grow to be the hero's: 1.0 normally, the
    hero's overscan widening when the hero frame grew wider (tan(hfov / 2) ratio). Raises ValueError
    when the lens differs any other way (a narrower view, or wider without an overscan block)."""
    width = float(hero_camera["resolution"]["width"])
    hero_tan = width / (2.0 * float(hero_camera["intrinsics_px"]["fx"]))
    fov = (cam0.get("fov_deg") or {}).get("horizontal")
    if not fov:
        raise ValueError("the camera's field of view is unreadable")
    cam_tan = math.tan(math.radians(float(fov)) / 2.0)
    factor = hero_tan / cam_tan
    if abs(factor - 1.0) <= WIDEN_TOL:
        return 1.0
    hero_fov = math.degrees(2.0 * math.atan(hero_tan))
    if factor > 1.0 and hero_camera.get("overscan"):
        return factor
    raise ValueError(
        f"the camera's horizontal field of view is {float(fov):.4f} deg, the hero still's {hero_fov:.4f} deg"
    )


# --------------------------------------------------------------------------- the manifest


def sequence_files(
    f0: int, f1: int, width: int, height: int, clay_dtype: str = "uint16"
) -> dict[str, dict[str, Any]]:
    """files.clay_seq / id_seq / z_seq for frames f0..f1 at width x height (bridge.motion patterns)."""
    formats = {"clay_seq": ("png", clay_dtype), "id_seq": ("exr", "float"), "z_seq": ("exr", "float")}
    return {
        role: {
            "kind": "sequence",
            "pattern": motion.SEQUENCE_PATTERNS[role],
            "first": int(f0),
            "last": int(f1),
            "format": fmt,
            "width": int(width),
            "height": int(height),
            "dtype": dtype,
            "hashes": motion.SEQUENCE_HASHES[role],
        }
        for role, (fmt, dtype) in formats.items()
    }


def build_motion_manifest(
    hero: dict[str, Any],
    packet_id: str,
    f0: int,
    f1: int,
    fps: float,
    files: dict[str, Any],
    motion_block: dict[str, Any],
    scene_hash: dict[str, Any],
    host: dict[str, Any],
    renderer: dict[str, Any],
    render: dict[str, Any],
    supersedes: str | None = None,
    created_utc: str | None = None,
) -> dict[str, Any]:
    """The motion manifest, built on the hero still's: its camera block (the hero's pose, lens and
    resolution, frames now f0..f1 with hero == start), colour, legend block, policy and view_hash (the
    beauty and legend in the folder are the hero's), with this packet's id, files, host, renderer,
    scene_hash, render block and motion block."""
    if not PACKET_ID_RE.match(str(packet_id)):
        raise packet.ManifestError("packet_id is not a pkt_ + 26 character ULID")
    if int(f1) <= int(f0):
        raise packet.ManifestError(f"frames {f0}..{f1}: a motion packet needs at least two frames")
    for role in packet.STILL_ROLES + packet.MOTION_ROLES:
        if role not in files:
            raise packet.ManifestError(f"files is missing {role}")
    manifest = copy.deepcopy(hero)
    camera = manifest["camera"]
    camera["frames"] = {"hero": int(f0), "start": int(f0), "end": int(f1), "fps": fps}
    camera["frames_file"] = "camera_frames"
    manifest.update(
        {
            "packet_version": PACKET_VERSION,
            "packet_id": packet_id,
            "kind": "motion",
            "created_utc": created_utc or packet.utc_now(),
            "supersedes": supersedes,
            "host": copy.deepcopy(host),
            "renderer": copy.deepcopy(renderer),
            "scene_hash": copy.deepcopy(scene_hash),
            "files": copy.deepcopy(files),
            "render": copy.deepcopy(render),
            "motion": copy.deepcopy(motion_block),
        }
    )
    return manifest


# --------------------------------------------------------------------------- the action


def _post(result: MotionResult, root: Path, config: dict[str, Any]) -> None:
    answer = transport.post_packet(root, config)
    status, body = answer["status_code"], answer["body"]
    result.status_code, result.daemon = status, body
    if status in OK_STATUS:
        result.ok, result.stage, result.code = True, "done", None
        return
    if status == 426:
        raise MotionAbort(
            "post", POST_FAILED, f"daemon refused packet version {PACKET_VERSION} (HTTP 426): {body}"
        )
    if status == 409:
        raise MotionAbort(
            "post", POST_FAILED, f"daemon already holds a different packet under this id (HTTP 409): {body}"
        )
    raise MotionAbort("post", POST_FAILED, f"daemon rejected the packet (HTTP {status}): {body}")


def _send(
    result: MotionResult,
    camera: str,
    hero_packet_id: str,
    f0: int,
    f1: int,
    out_root: Path,
    config: dict[str, Any],
    crossings: Any,
    opts: dict[str, Any],
) -> None:
    progress = opts.get("progress")

    def step(stage: str, done: int = 0, total: int = 1) -> None:
        result.stage = stage
        send_packet.notify_progress(progress, done, total, f"{camera}: {stage}")

    step("hero")
    hero_dir, hero, hero_legend = load_hero(hero_packet_id, out_root)
    result.hero_packet_id = hero["packet_id"]
    node = get_camera(camera)
    if node is None:
        raise MotionAbort("hero", CAMERA_MISSING, f"camera {camera!r} not found in the scene")
    name = str(getattr(node, "name", camera))
    hero_cam = hero["camera"]
    if hero_cam.get("name") != name:
        raise MotionAbort(
            "hero", HERO_MISMATCH, f"the hero packet is of camera {hero_cam.get('name')!r}, not {name!r}"
        )
    try:
        path_crossings = check_crossings(crossings, f0, f1)
    except ValueError as exc:
        raise MotionAbort("hero", BAD_CROSSINGS, str(exc)) from exc
    try:
        seq_w, seq_h = sequence_resolution(hero_cam, opts.get("sequence_resolution"))
    except (ValueError, KeyError, TypeError) as exc:
        raise MotionAbort("hero", BAD_SEQUENCE_SIZE, str(exc)) from exc
    result.frames = [f0, f1]
    result.sequence_resolution = {"width": seq_w, "height": seq_h}

    step("daemon")
    gate_ok, gate_reason = send_packet.daemon_gate(config)
    if not gate_ok:
        raise MotionAbort("daemon", DAEMON, gate_reason)

    step("scene")
    defaults = elements.ensure_default_set()
    if not defaults.ok:
        raise MotionAbort(
            "scene",
            motion.ELEMENTS_MISSING,
            f"render elements missing: {', '.join(defaults.missing + defaults.unknown)}",
        )
    scene_hash = hashing.hash_scene(digest.scene_digest(), config)
    if scene_hash.get("hex") != (hero.get("scene_hash") or {}).get("hex"):
        result.warnings.append(
            "the scene changed since the hero still (scene_hash differs): frame 0 matches the camera, "
            "but the hero beauty may no longer show this scene"
        )
    fps_value = rt_attr("frameRate")
    fps = float(fps_value) if fps_value else float(hero_cam["frames"]["fps"])
    fps = int(fps) if float(fps).is_integer() else fps
    if float(fps) != float(hero_cam["frames"]["fps"]):
        result.warnings.append(
            f"the scene runs at {fps} fps, the hero still recorded {hero_cam['frames']['fps']}"
        )

    step("camera")
    cam0 = digest.camera_digest(name, frame=f0)
    try:
        factor = widen_factor(hero_cam, cam0)
    except ValueError as exc:
        raise MotionAbort("camera", FRAME0_MISMATCH, f"frame {f0}: {exc}") from exc
    hero_fx = float(hero_cam["intrinsics_px"]["fx"])
    scale = seq_w / float(hero_cam["resolution"]["width"])
    clay_kw = {
        "material_name": str(opts.get("clay_material") or motion.CLAY_MATERIAL),
        "keep_glass": bool(opts.get("keep_glass", True)),
        "keep_lights": bool(opts.get("keep_lights", True)),
    }
    try:
        with send_packet._render_size(seq_w, seq_h), send_packet._widened(node, cam0, factor) as widened:
            if widened:
                result.warnings.append(
                    f"the hero's overscan widening is reapplied for the sequence: {widened}"
                )
            frames = motion.camera_frames(node, f0, f1)
            result.frame0_deviation = motion.check_frame0(frames, hero_cam["world_from_camera"])
            fx0 = (digest.camera_digest(name, frame=f0).get("intrinsics_px") or {}).get("fx")
            if not fx0 or abs(float(fx0) - hero_fx * scale) > FX_REL_TOL * hero_fx * scale:
                raise MotionAbort(
                    "camera",
                    FRAME0_MISMATCH,
                    f"frame {f0}: fx {fx0} at {seq_w} wide, the hero's scaled fx is {hero_fx * scale:.6g}",
                )

            step("render")
            packet_id = new_packet_id()
            root = out_root / packet_id
            root.mkdir(parents=True, exist_ok=False)
            result.packet_id, result.packet_dir = packet_id, str(root)
            total = f1 - f0 + 1

            def frame_progress(done: int, _total: int, message: str) -> None:
                send_packet.notify_progress(progress, done, total, f"{name}: {message}")

            with motion.clay(**clay_kw) as clay_report:
                result.clay = clay_report.as_dict()
                result.warnings.extend(clay_report.warnings)
                seq = motion.render_sequence(
                    node,
                    f0,
                    f1,
                    seq_w,
                    seq_h,
                    packet_dir=root,
                    staging_dir=opts.get("staging_dir"),
                    progress=frame_progress,
                    should_cancel=opts.get("should_cancel"),
                )
    except send_packet.SendAbort as abort:  # the overscan widening could not be applied
        raise MotionAbort("camera", FRAME0_MISMATCH, abort.reason) from abort
    result.sequence = seq.as_dict()
    result.warnings.extend(seq.warnings)

    step("copy")
    rels = [str(hero["files"][role]["path"]) for role in ("beauty_exr", "display_tiff16")]
    for rel in rels:
        target = root.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(hero_dir.joinpath(*rel.split("/")), target)
    legend = copy.deepcopy(hero_legend)
    legend["packet_id"] = packet_id
    legend_rel = str(hero["files"]["id_legend"]["path"])

    step("hash")
    frame_rels = [
        motion.SEQUENCE_PATTERNS[role] % n
        for role in ("clay_seq", "id_seq", "z_seq")
        for n in range(f0, f1 + 1)
    ]
    hashes = hashing.hash_packet_files(root, config, rels=rels + frame_rels)
    for role, rel in zip(("beauty_exr", "display_tiff16"), rels, strict=True):
        if hashes[rel]["hex"] != hero["files"][role]["hash"]["hex"]:
            raise MotionAbort("hash", HERO_PACKET, f"the hero's {rel} no longer matches its manifest")
    frames_bytes = packet.camera_frames_bytes(
        {"packet_id": packet_id, "camera": {"frames": {"fps": fps}}}, frames
    )
    hashes.update(
        hashing.sidecar_hashes(
            config,
            legend=legend,
            frames_bytes=frames_bytes,
            legend_path=legend_rel,
            frames_path=CAMERA_FRAMES_NAME,
        )
    )
    for role, pattern in motion.SEQUENCE_PATTERNS.items():
        listing = hashing.sequence_listing(pattern, f0, f1, hashes)
        atomic_write_bytes(root.joinpath(*motion.SEQUENCE_HASHES[role].split("/")), listing)

    step("manifest")
    try:
        files: dict[str, Any] = {}
        for role, rel in zip(("beauty_exr", "display_tiff16"), rels, strict=True):
            ref = copy.deepcopy(hero["files"][role])
            ref["bytes"] = int(hashes[rel]["bytes"])
            ref["hash"] = {"algo": "blake3", "hex": str(hashes[rel]["hex"])}
            files[role] = ref
        files["id_legend"] = packet.file_ref_from_hashes(legend_rel, hashes, "json")
        files["camera_frames"] = packet.file_ref_from_hashes(CAMERA_FRAMES_NAME, hashes, "json")
        files.update(sequence_files(f0, f1, seq_w, seq_h, seq.clay_dtype))
        motion_block: dict[str, Any] = {
            "hero_still_packet_id": hero["packet_id"],
            "sequence_resolution": {"width": seq_w, "height": seq_h},
            "frame0_matches_hero": True,
            "clay_override": clay_report.manifest_block(),
        }
        if path_crossings:
            motion_block["path_crossings"] = path_crossings
        render_block = {
            **copy.deepcopy(hero.get("render") or {}),
            "session": str(opts.get("session") or "interactive"),
            "time_s": seq.time_s,
        }
        supersedes = opts.get("supersedes")
        manifest = build_motion_manifest(
            hero,
            packet_id,
            f0,
            f1,
            fps,
            files,
            motion_block,
            scene_hash,
            {
                "app": "3dsmax",
                "version": send_packet.max_version(),
                "python": platform.python_version(),
                "plugin": dict(SENDER),
            },
            {"name": "vray", "version": send_packet.vray_version(send_packet._renderer_class())},
            render_block,
            supersedes=str(supersedes) if supersedes else None,
        )
        packet.write_packet(root, manifest, legend, camera_frames=frames, hashes=hashes)
    except (packet.ManifestError, KeyError) as exc:
        raise MotionAbort("manifest", MANIFEST_FAILED, f"manifest: {exc}") from exc

    step("post")
    _post(result, root, config)


def run(
    camera: str,
    hero_packet_id: str,
    f0: int,
    f1: int,
    out_root: Path | str,
    daemon_base: str | None = None,
    crossings: Any = (),
    options: Any = None,
) -> MotionResult:
    """Send camera's move f0..f1 as a motion packet under out_root (the daemon's packets folder),
    referencing the hero still hero_packet_id (an id under out_root, or its folder). daemon_base is
    the daemon URL (default: config.json's). crossings: [{frame, kind, note?}] path crossings
    (threshold, room_change, occluder). options: config, progress(done, total, message),
    should_cancel(), sequence_resolution (w, h), supersedes, session, staging_dir, clay_material,
    keep_glass, keep_lights. Never raises."""
    opts = send_packet.normalise_options(options)
    config = dict(opts.get("config") or load_config())
    if daemon_base:
        config["daemon_url"] = str(daemon_base)
    config.setdefault("daemon_url", DEFAULT_DAEMON_URL)
    result = MotionResult(camera=str(camera), hero_packet_id=str(hero_packet_id))
    try:
        f0, f1 = int(f0), int(f1)
        if f1 <= f0:
            raise MotionAbort(
                "start", motion.CAMERA_FRAMES, f"frames {f0}..{f1}: a move needs at least two frames"
            )
        _send(result, str(camera), str(hero_packet_id), f0, f1, Path(out_root), config, crossings, opts)
    except MotionAbort as abort:
        result.ok, result.stage, result.code = False, abort.stage, abort.code
        result.errors.append(abort.reason)
    except motion.MotionError as exc:
        result.ok, result.code = False, exc.code
        result.errors.append(f"{result.stage}: {exc.message}")
    except transport.TransportError as exc:  # hashing or posting: the daemon said no or went away
        result.ok, result.code = False, HASH_FAILED if result.stage == "hash" else DAEMON
        result.errors.append(f"{result.stage}: daemon: {exc}")
    except Exception as exc:  # anything unexpected is reported, never raised into the dock
        result.ok, result.code = False, ERROR
        result.errors.append(f"{result.stage}: {type(exc).__name__}: {exc}")
    if result.packet_dir and not (Path(result.packet_dir) / "manifest.json").is_file():
        result.warnings.append(
            f"the frames are kept in {result.packet_dir} for inspection; it has no manifest, so the "
            "daemon never ingests it (delete it when done)"
        )
    send_packet.notify_progress(
        opts.get("progress"),
        1,
        1,
        f"{camera}: {'sent ' + str(result.packet_id) if result.ok else 'failed at ' + result.stage}",
    )
    return result
