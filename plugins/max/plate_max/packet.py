"""Builds and writes a Shot Packet folder (spec/PACKET.md, schema spec/packet.schema.json).

build_manifest turns the camera digest, the scene digest, the legend and the policy into a tier A
manifest; write_packet writes manifest.json, id_legend.json and (motion) camera_frames.json with
temp-then-replace, the manifest last so a half-written folder is never ingestible.

BLAKE3 is not in the standard library and Max ships no blake3 module, so this module never
computes the hashes the schema demands (files[*].hash, scene_hash, view_hash). The loopback daemon
does (BUILD_PLAN decision 5b): bridge.hashing.hash_packet_files hashes the files in the folder,
hash_payloads the in-memory bytes that legend_bytes, camera_frames_bytes, scene_hash_payload
(digest.py) and view_hash_payload produce; file_ref_from_hashes turns an answer into a files{} entry
and write_packet(hashes=) checks the manifest against it before anything is written. The schema's
hash.algo stays "blake3". Validation of the result against the schema is plate_spec's job and runs
in the tests, not here.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import CONVENTION_ID, PACKET_VERSION, SENDER
from . import policy as policy_mod
from .digest import DIGEST_VERSION, SCENE_DIGEST_VERSION, intrinsics, rotation_error
from .fsutil import atomic_write_bytes, canonical_json_bytes, dump_json_bytes
from .packet_id import PACKET_ID_RE

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
SYSTEM_UNITS = ("millimeters", "centimeters", "meters", "inches", "feet")

CONVENTION: dict[str, Any] = {
    "id": CONVENTION_ID,
    "handedness": "right",
    "world_up": "+Z",
    "camera_forward": "-Z",
    "camera_up": "+Y",
    "camera_right": "+X",
    "matrix": "world_from_camera",
    "layout": "row_major",
    "vectors": "column",
    "length_unit": "scene",
    "pixel_origin": "top_left_corner",
    "pixel_center": 0.5,
}

# What V-Ray hashes for each Cryptomatte layer; legend names are exactly these strings.
ID_TYPES = {"object": "node_name", "material": "material_name"}
STILL_ROLES = ("beauty_exr", "display_tiff16", "id_legend")
MOTION_ROLES = ("camera_frames", "clay_seq", "id_seq", "z_seq")


class ManifestError(ValueError):
    """The scene cannot be turned into a valid packet; the message says what to fix."""


# --------------------------------------------------------------------------- small helpers


def slugify(text: str) -> str:
    """Lower-case slug for project_id / shot_id: letters and digits, '-' between, max 64 chars."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64].strip("-")
    if not slug:
        raise ManifestError(f"cannot make a slug from {text!r}")
    return slug


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_file_sha256(path: Path | str) -> str:
    """Stdlib fallback hash (NOT what the schema wants; the manifest carries BLAKE3)."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_ref(rel_path: str, size: int, blake3_hex: str, fmt: str, **extra: Any) -> dict[str, Any]:
    """A files{} entry for one file: caller supplies the size and the BLAKE3 hex."""
    return {
        "kind": "file",
        "path": rel_path,
        "bytes": int(size),
        "hash": {"algo": "blake3", "hex": blake3_hex},
        "format": fmt,
        **extra,
    }


def file_ref_from_hashes(rel_path: str, hashes: dict[str, Any], fmt: str, **extra: Any) -> dict[str, Any]:
    """file_ref for rel_path from a bridge.hashing.hash_packet_files answer ({rel: {hex, bytes}})."""
    entry = hashes.get(rel_path)
    if not isinstance(entry, dict) or not entry.get("hex"):
        raise ManifestError(f"no daemon hash for {rel_path!r}: hash the folder first (bridge.hashing)")
    return file_ref(rel_path, int(entry["bytes"]), str(entry["hex"]), fmt, **extra)


def legend_bytes(legend: dict[str, Any]) -> bytes:
    """The exact bytes write_packet puts in id_legend.json (hash these for files.id_legend)."""
    return dump_json_bytes(legend)


def camera_frames_doc(manifest: dict[str, Any], frames: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "packet_id": manifest["packet_id"],
        "convention": CONVENTION_ID,
        "fps": manifest["camera"]["frames"]["fps"],
        "frames": frames,
    }


def camera_frames_bytes(manifest: dict[str, Any], frames: list[dict[str, Any]]) -> bytes:
    return dump_json_bytes(camera_frames_doc(manifest, frames))


VIEW_RENDER_KEYS = ("sampler", "noise_threshold", "max_subdivs", "denoiser")


def view_hash_payload(
    camera_block: dict[str, Any],
    legend: dict[str, Any],
    colour: dict[str, Any],
    render: dict[str, Any],
    globals_: dict[str, Any] | None = None,
) -> bytes:
    """Canonical bytes for view_hash (PACKET.md section 5): the camera block, the global state that
    affects every pixel (scene_globals(): lights, environment, renderer; colour management; the render
    settings that change pixels, not the timings) and the digest of every visible ID. The manifest's
    view_hash is the BLAKE3 of these bytes (digest_version "0": see digest.scene_globals)."""
    state = {k: v for k, v in (globals_ or {}).items() if k != "warnings"}
    return canonical_json_bytes(
        {
            "camera": camera_block,
            "globals": state,
            "colour": colour,
            "render": {k: render.get(k) for k in VIEW_RENDER_KEYS},
            "ids": {e["id"]: e["digest"] for e in sorted(legend["entries"], key=lambda e: e["id"])},
        }
    )


def _hash_block(value: Any, what: str) -> dict[str, str]:
    hex_value = value.get("hex") if isinstance(value, dict) else value
    if not isinstance(hex_value, str) or not HEX64_RE.match(hex_value):
        raise ManifestError(f"{what}: expected a BLAKE3 hex digest (64 lowercase hex chars)")
    return {"algo": "blake3", "hex": hex_value}


def _safe_rel(rel: str) -> str:
    bad = (
        not rel
        or rel.startswith("/")
        or "\\" in rel
        or ":" in rel
        or any(part in ("", "..") for part in rel.split("/"))
    )
    if bad:
        raise ManifestError(f"unsafe packet-relative path {rel!r}")
    return rel


# --------------------------------------------------------------------------- manifest blocks


def _units_block(scene: dict[str, Any]) -> dict[str, Any]:
    system, mpu = scene.get("units_system"), scene.get("meters_per_unit")
    if system not in SYSTEM_UNITS or not mpu:
        raise ManifestError(
            f"scene units {system!r} (meters_per_unit {mpu!r}) are not sendable; "
            f"use one of {', '.join(SYSTEM_UNITS)}"
        )
    return {"system_unit": system, "meters_per_unit": mpu, "up_axis": "Z"}


def _refuse_unsupported_lens(cam: dict[str, Any]) -> None:
    """v0.1 has a pinhole model only (PACKET.md 3.3): shift, tilt and distortion are refused, not guessed."""
    why = []
    shift = cam.get("lens_shift") or {}
    if shift.get("horizontal") or shift.get("vertical"):
        why.append("lens shift")
    tilt = cam.get("tilt") or {}
    if tilt.get("vertical") or tilt.get("horizontal") or tilt.get("auto"):
        why.append("vertical/horizontal tilt")
    distortion = cam.get("distortion") or {}
    if distortion.get("amount") or distortion.get("type"):
        why.append("lens distortion")
    if why:
        raise ManifestError(
            f"camera {cam.get('name')!r} uses {', '.join(why)}: Shot Packet v0.1 supports unshifted, "
            "untilted, undistorted cameras only (spec/PACKET.md 3.3). Zero them and Send again."
        )


def build_camera_block(cam: dict[str, Any], kind: str = "still") -> dict[str, Any]:
    """The manifest's camera block from a camera_digest(): constants, intrinsics, exposure, frames.
    Raises ManifestError for a camera that must not be sent (missing values, lens shift, a scaled
    or mirrored node)."""
    needed = (
        "name",
        "world_from_camera",
        "focal_mm",
        "film_gate_mm",
        "f_number",
        "focus_distance",
        "resolution",
        "frame_range",
        "hero_frame",
        "fps",
    )
    missing = [k for k in needed if cam.get(k) is None]
    if missing:
        raise ManifestError(
            f"camera digest is missing {', '.join(missing)}; digest warnings: {cam.get('warnings') or 'none'}"
        )
    _refuse_unsupported_lens(cam)
    problem = rotation_error(cam["world_from_camera"])
    if problem:
        raise ManifestError(f"camera {cam['name']}: {problem}")

    width, height = int(cam["resolution"][0]), int(cam["resolution"][1])
    pa = float(cam.get("pixel_aspect") or 1.0)
    focal, film = float(cam["focal_mm"]), float(cam["film_gate_mm"])
    zoom = float(cam.get("zoom_factor") or 1.0)
    if focal <= 0 or film <= 0 or zoom <= 0:
        raise ManifestError("camera focal length, film gate and zoom must be positive")

    # full float precision: the spec checks fx to 0.01% and the FOV to 0.005 degrees
    derived = intrinsics(focal, film, zoom, width, height, pa)
    hfov, vfov = derived["fov_deg"]["horizontal"], derived["fov_deg"]["vertical"]
    k = derived["intrinsics_px"]
    fx, fy, (cx, cy) = k["fx"], k["fy"], k["principal_point"]

    exp = cam.get("exposure") or {}
    iso, shutter = exp.get("iso"), exp.get("shutter_s")
    f_number = float(cam["f_number"])
    exposure: dict[str, Any]
    if exp.get("enabled") is False:
        exposure = {"mode": "none"}  # the camera's exposure switch is off: ISO and shutter do nothing
    elif exp.get("ev") is not None:
        exposure = {"mode": "ev", "ev100": round(float(exp["ev"]), 4)}
    elif iso and shutter:
        ev100 = math.log2(f_number**2 / shutter) - math.log2(iso / 100.0)
        exposure = {
            "mode": "physical",
            "iso": iso,
            "shutter_s": shutter,
            "ev100": round(ev100, 4),
        }
    else:
        exposure = {"mode": "none"}

    kelvin = cam.get("white_balance_k")
    if kelvin and 1000 <= kelvin <= 40000:
        white_balance: dict[str, Any] = {"mode": "kelvin", "kelvin": kelvin}
    else:
        white_balance = {"mode": "neutral"}

    start, end = int(cam["frame_range"][0]), int(cam["frame_range"][1])
    hero = int(cam["hero_frame"])
    if kind == "still":
        frames = {"hero": hero, "start": hero, "end": hero, "fps": cam["fps"]}
    else:
        if hero != start:
            raise ManifestError(
                f"motion packet: the first frame is the hero frame, but the scene time is {hero} "
                f"and the animation range starts at {start}"
            )
        frames = {"hero": start, "start": start, "end": end, "fps": cam["fps"]}

    safe = cam.get("safe_frame") or {"x": 0, "y": 0, "width": width, "height": height}
    safe = {k: int(safe[k]) for k in ("x", "y", "width", "height")}
    if safe["x"] + safe["width"] > width or safe["y"] + safe["height"] > height:
        raise ManifestError("safe_frame extends outside the rendered resolution")
    overscan = cam.get("overscan")
    if safe != {"x": 0, "y": 0, "width": width, "height": height}:
        if not overscan:
            raise ManifestError(
                "a partial safe_frame needs camera overscan (engine_aspect) in the camera digest"
            )
    if overscan:
        try:
            a, b = (int(v) for v in str(overscan["engine_aspect"]).split(":"))
            aspect_ok = a > 0 and b > 0 and abs(width / height - a / b) <= 0.01 * (a / b)
        except (KeyError, ValueError):
            aspect_ok = False
        if not aspect_ok:
            raise ManifestError(
                f"overscan.engine_aspect {overscan.get('engine_aspect')!r} does not match the rendered {width}x{height}"
            )
    block: dict[str, Any] = {
        "name": cam["name"],
        "source": "scene",
    }
    if cam.get("node_type"):
        block["node_type"] = cam["node_type"]
    block.update(
        {
            "projection": "perspective",
            "convention": copy.deepcopy(CONVENTION),
            "world_from_camera": [[float(v) for v in row] for row in cam["world_from_camera"][:3]]
            + [[0, 0, 0, 1]],
            "focal_length_mm": focal,
            "film_width_mm": film,
            "zoom_factor": zoom,
            "fov_deg": {"horizontal": round(hfov, 4), "vertical": round(vfov, 4)},
            "lens_shift": {
                "horizontal": 0,
                "vertical": 0,
                "raw_unit": "vray_physical",
                "vertical_tilt_correction": False,
            },
            "distortion": "none",
            "intrinsics_px": {
                "fx": fx,
                "fy": fy,
                "principal_point": [cx, cy],
                "K": [[fx, 0, cx], [0, fy, cy], [0, 0, 1]],
            },
            "resolution": {"width": width, "height": height, "pixel_aspect": pa},
            "safe_frame": safe,
        }
    )
    if overscan:
        block["overscan"] = copy.deepcopy(overscan)
    block.update(
        {
            "f_number": f_number,
            "focus_distance": float(cam["focus_distance"]),
            "dof_enabled": bool(cam.get("dof_enabled", False)),
            "exposure": exposure,
            "white_balance": white_balance,
            "frames": frames,
        }
    )
    if kind == "motion":
        block["frames_file"] = "camera_frames"
    return block


LOSSLESS_EXR = ("none", "rle", "zip", "zips", "piz")
SEQUENCE_PATTERN_RE = re.compile(r"^[^%]*%0[1-8]d[^%]*$")


def _check_files(
    files: dict[str, Any], kind: str, width: int, height: int, motion: dict[str, Any] | None
) -> None:
    roles = STILL_ROLES + (MOTION_ROLES if kind == "motion" else ())
    absent = [r for r in roles if r not in files]
    if absent:
        raise ManifestError(f"files is missing {', '.join(absent)}")
    for role, ref in files.items():
        if ref.get("kind") == "file":
            _safe_rel(ref.get("path", ""))
            _hash_block(ref.get("hash"), f"files.{role}.hash")
        elif ref.get("kind") == "sequence":
            _safe_rel(ref.get("pattern", ""))
            _safe_rel(ref.get("hashes", ""))
            if not SEQUENCE_PATTERN_RE.match(ref["pattern"]):
                raise ManifestError(f"files.{role}: the pattern needs exactly one %0Nd and no other '%'")
            if ref.get("width", 0) * height != ref.get("height", 0) * width:
                raise ManifestError(
                    f"files.{role}: {ref.get('width')}x{ref.get('height')} does not have the render's exact aspect "
                    f"({width}x{height})"
                )
            if role in ("z_seq", "id_seq") and (ref.get("format"), ref.get("dtype")) != ("exr", "float"):
                raise ManifestError(f"files.{role}: data sequences must be float EXR")
        else:
            raise ManifestError(f"files.{role}: kind must be 'file' or 'sequence'")
    if files["beauty_exr"].get("compression") not in LOSSLESS_EXR:
        raise ManifestError(
            f"files.beauty_exr.compression must be one of {', '.join(LOSSLESS_EXR)} "
            "(DWA and PXR24 corrupt Cryptomatte ids and float data passes)"
        )
    if kind == "motion":
        res = motion["sequence_resolution"]
        if res["width"] * height != res["height"] * width:
            raise ManifestError("motion.sequence_resolution must have exactly the render's aspect")


def _id_legend_block(
    legend: dict[str, Any], files: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, float]]:
    """The manifest's id_legend block plus the object-id -> share map the policy lint uses."""
    layers = {"object": ("CryptoObject", 6), "material": ("CryptoMaterial", 6)}
    for el in files.get("beauty_exr", {}).get("elements", []):
        which = {"cryptomatte_object": "object", "cryptomatte_material": "material"}.get(el.get("element"))
        if which:
            layers[which] = (el["layer"], el["rank_levels"])
    objects = [e for e in legend["entries"] if e["kind"] == "object"]
    labelled = sum(e["pixel_share"] for e in objects if e["class_source"] != "unlabelled")
    unlabelled = sum(e["pixel_share"] for e in objects if e["class_source"] == "unlabelled")
    if labelled + unlabelled > 1 + 1e-3:
        raise ManifestError(
            f"visible object shares sum to {labelled + unlabelled:.4f}, more than the whole frame"
        )
    uncovered = max(0.0, 1.0 - labelled - unlabelled)
    labelled, unlabelled = min(labelled, 1.0), min(unlabelled, 1.0)  # six-decimal rounding can overshoot
    block = {
        "file": "id_legend",
        "source": "cryptomatte",
        "cryptomatte": {
            which: {
                "layer": name,
                "hash": "murmur3_32",
                "id_type": ID_TYPES[which],
                "rank_levels": levels,
                "manifest": "exr_metadata",
            }
            for which, (name, levels) in layers.items()
        },
        "entry_count": len(legend["entries"]),
        "coverage": {
            "labelled_share": round(labelled, 6),
            "unlabelled_share": round(unlabelled, 6),
            "uncovered_share": round(uncovered, 6),
        },
    }
    return block, {e["id"]: e["pixel_share"] for e in objects}


def build_manifest(
    camera: dict[str, Any],
    scene: dict[str, Any],
    files: dict[str, Any],
    legend: dict[str, Any],
    policy: dict[str, Any],
    project_id: str,
    shot_id: str,
    host: dict[str, Any],
    renderer: dict[str, Any],
    colour: dict[str, Any],
    render: dict[str, Any],
    *,
    kind: str = "still",
    scene_hash: Any = None,
    view_hash: Any = None,
    supersedes: str | None = None,
    created_utc: str | None = None,
    motion: dict[str, Any] | None = None,
    custody: dict[str, Any] | None = None,
    max_unlabelled_share: float = policy_mod.DEFAULT_MAX_UNLABELLED_SHARE,
) -> dict[str, Any]:
    """A tier A manifest that plate_spec.validate_manifest accepts.

    camera is camera_digest(), scene is scene_digest(), legend is build_id_legend() (it carries the
    packet_id), policy is derive_policy(legend). files holds complete refs (see file_ref) whose
    BLAKE3 the caller computed; scene_hash and view_hash are BLAKE3 hex strings or {"hex": ...}
    (see scene_hash_payload / view_hash_payload). host is {"version", "python"?} (the plugin is
    filled in here), renderer {"name", "version"}, colour and render are the schema blocks.
    Raises ManifestError for anything that must stop a Send.
    """
    if kind not in ("still", "motion"):
        raise ManifestError("kind must be 'still' or 'motion'")
    for label, value in (("project_id", project_id), ("shot_id", shot_id)):
        if not SLUG_RE.match(value):
            raise ManifestError(f"{label} {value!r} is not a lower-case slug (use slugify)")
    if not PACKET_ID_RE.match(str(legend.get("packet_id"))):
        raise ManifestError("legend.packet_id is not a pkt_ + 26 character ULID (use new_packet_id)")
    if colour.get("max_colour_mode") not in ("ocio", "legacy_gamma"):
        raise ManifestError(
            "colour.max_colour_mode must be 'ocio' or 'legacy_gamma': a packet with an unknown mode is not sent"
        )
    if kind == "motion" and not motion:
        raise ManifestError("a motion packet needs its motion block")
    if not 0 <= max_unlabelled_share <= policy_mod.SCHEMA_MAX_UNLABELLED_SHARE:
        raise ManifestError(
            f"max_unlabelled_share must be within 0..{policy_mod.SCHEMA_MAX_UNLABELLED_SHARE} (the schema cap)"
        )
    if scene_hash is None or view_hash is None:
        raise ManifestError("tier A needs scene_hash and view_hash (BLAKE3); the plugin cannot compute them")

    camera_block = build_camera_block(camera, kind)
    _check_files(
        files, kind, camera_block["resolution"]["width"], camera_block["resolution"]["height"], motion
    )
    legend_block, coverage = _id_legend_block(legend, files)

    problems = policy_mod.lint(policy, coverage, max_unlabelled_share)
    if problems:
        raise ManifestError("policy lint blocks Send: " + "; ".join(problems))
    policy_block: dict[str, Any] = {
        "rules_version": policy_mod.RULES_VERSION,
        "class_rules": copy.deepcopy(policy.get("class_rules") or policy_mod.class_rules()),
        "lock_by_default": True,
        "locked_ids": list(policy["locked_ids"]),
        "editable_ids": list(policy["editable_ids"]),
        "text_lock": True,
        "lint": {
            "max_unlabelled_share": max_unlabelled_share,
            "unlabelled_share": legend_block["coverage"]["unlabelled_share"],
            "passed": True,
        },
    }
    if custody:
        if custody.get("nda") and not custody.get("allowed_grades"):
            raise ManifestError("policy.custody: an NDA project must list allowed_grades")
        if custody.get("nda") and set(custody["allowed_grades"]) - {"C0", "C1"}:
            raise ManifestError("policy.custody: NDA projects run on C0 and C1 only")
        policy_block["custody"] = copy.deepcopy(custody)

    sh: dict[str, Any] = dict(_hash_block(scene_hash, "scene_hash"))
    # the format scene_hash_payload hashed: content-v1 ("1") when the digest has lines, else file-meta ("0")
    default_version = str(
        scene.get("digest_version")
        or (SCENE_DIGEST_VERSION if scene.get("lines") is not None else DIGEST_VERSION)
    )
    sh["digest_version"] = (
        str(scene_hash.get("digest_version") or default_version)
        if isinstance(scene_hash, dict)
        else default_version
    )
    if scene.get("max_file"):
        max_file: dict[str, Any] = {"name": scene["max_file"]}
        if scene.get("size_bytes") is not None:
            max_file["bytes"] = scene["size_bytes"]
        if scene.get("mtime_utc"):
            max_file["mtime_utc"] = scene["mtime_utc"]
        sh["max_file"] = max_file

    manifest: dict[str, Any] = {
        "packet_version": PACKET_VERSION,
        "packet_id": legend["packet_id"],
        "kind": kind,
        "tier": "A",
        "project_id": project_id,
        "shot_id": shot_id,
        "created_utc": created_utc or utc_now(),
        "supersedes": supersedes,
        "host": {
            "app": host.get("app", "3dsmax"),
            "version": host["version"],
            **({"python": host["python"]} if host.get("python") else {}),
            "plugin": dict(SENDER),
        },
        "renderer": {"name": renderer["name"], "version": renderer["version"]},
        "scene_hash": sh,
        "view_hash": _hash_block(view_hash, "view_hash"),
        "units": _units_block(scene),
        "camera": camera_block,
        "colour": copy.deepcopy(colour),
        "files": copy.deepcopy(files),
        "id_legend": legend_block,
        "policy": policy_block,
        "render": copy.deepcopy(render),
    }
    if kind == "motion":
        manifest["motion"] = copy.deepcopy(motion)
    return manifest


# --------------------------------------------------------------------------- writing


def _write_sidecar(root: Path, manifest: dict[str, Any], role: str, data: bytes) -> None:
    ref = manifest["files"].get(role)
    if ref is None or ref.get("kind") != "file":
        raise ManifestError(f"manifest.files has no {role} file entry")
    rel = _safe_rel(ref["path"])
    if ref["bytes"] != len(data):
        raise ManifestError(
            f"{rel} would be {len(data)} bytes but the manifest says {ref['bytes']}: hash the bytes "
            "from legend_bytes() / camera_frames_bytes() and rebuild the manifest"
        )
    atomic_write_bytes(root.joinpath(*rel.split("/")), data)


SIDECAR_ROLES = ("id_legend", "camera_frames")  # the files write_packet itself writes


def check_hashes(manifest: dict[str, Any], hashes: dict[str, Any]) -> None:
    """Every file the manifest references (except the sidecars write_packet writes) must carry the
    size and BLAKE3 the daemon reported for it (bridge.hashing.hash_packet_files). Sequence frames are
    checked through their hashes listing by plate_spec, not here. Raises ManifestError."""
    for role, ref in manifest["files"].items():
        if ref.get("kind") != "file" or role in SIDECAR_ROLES:
            continue
        rel = _safe_rel(ref["path"])
        entry = hashes.get(rel)
        if not isinstance(entry, dict):
            raise ManifestError(f"files.{role}: {rel} was not hashed by the daemon (hash the folder again)")
        if entry.get("hex") != ref["hash"]["hex"] or int(entry.get("bytes", -1)) != ref["bytes"]:
            raise ManifestError(
                f"files.{role}: {rel} has changed since it was hashed (manifest {ref['bytes']} bytes "
                f"{ref['hash']['hex'][:12]}..., daemon {entry.get('bytes')} bytes {str(entry.get('hex'))[:12]}...)"
            )


def write_packet(
    out_dir: Path | str,
    manifest: dict[str, Any],
    legend: dict[str, Any] | None = None,
    camera_frames: list[dict[str, Any]] | None = None,
    hashes: dict[str, Any] | None = None,
) -> Path:
    """Write the JSON files of a packet into out_dir (the beauty, display and sequence files are
    written by the render step). legend / camera_frames are optional: leave one out to keep the file
    already in the folder. hashes is the daemon's answer for the folder (bridge.hashing
    .hash_packet_files): when given, the manifest is checked against it first (check_hashes), so a
    file that changed after hashing never gets a manifest. manifest.json goes last."""
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    if hashes is not None:
        check_hashes(manifest, hashes)
    if (
        manifest["kind"] == "motion"
        and camera_frames is None
        and not (root / _safe_rel(manifest["files"]["camera_frames"]["path"])).is_file()
    ):
        raise ManifestError("a motion packet needs camera_frames (none given, none in the folder)")
    if manifest["kind"] == "still" and camera_frames is not None:
        raise ManifestError("camera_frames.json is for motion packets only")
    if legend is not None:
        if legend["packet_id"] != manifest["packet_id"]:
            raise ManifestError("legend.packet_id does not match the manifest")
        _write_sidecar(root, manifest, "id_legend", legend_bytes(legend))
    if camera_frames is not None:
        _write_sidecar(root, manifest, "camera_frames", camera_frames_bytes(manifest, camera_frames))
    atomic_write_bytes(root / "manifest.json", dump_json_bytes(manifest))
    return root
