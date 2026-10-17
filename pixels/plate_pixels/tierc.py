"""Tier C packets: a client's plate plus estimated geometry, written in the Shot Packet v0.1 layout.

``build_tier_c_packet`` takes a plate (PNG, JPEG or TIFF), runs the depth, intrinsics and segmentation
backends on it and writes ``client_plate.png``, ``depth_estimated.exr`` (float32 ``Z``), ``segments.png``
(16-bit label map), ``id_legend.json`` and ``manifest.json`` (PACKET.md section 11). The result is validated
with ``plate_spec.validate_packet_dir`` (schema, semantic rules, file sizes and BLAKE3 hashes) before it is
returned, so a packet that comes out of here is one the daemon will ingest.

What tier C is, honestly: there is no world, so the camera pose is identity; depth is relative, never metric
(``estimated.depth_semantics`` says which kind); intrinsics are a centred square-pixel pinhole expressed on a
36 mm film. With no labelling every segment is class ``other`` with ``class_source: autotag`` (the auto-tagger
has no names to work from), which keeps every id LOCKED until a human labels it in the Mask node. A packet
built by stub backends says so in its ``estimated`` block (``stub-<model>``, version ``stub``): a stub
segmentation must never read as a measurement. A client consent record is required (decision 10).
"""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import blake3
import cv2
import numpy as np

import plate_spec

from . import __version__, exr, imageio
from .backends import Backends, InputError
from .licences import get as licence_get
from .licences import licence_summary

__all__ = ["ConsentRequired", "TierCError", "build_tier_c_packet", "new_packet_id", "write_label_png"]

FILM_WIDTH_MM = 36.0
FPS = 25
MAX_UNLABELLED_SHARE = 0.05
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CLASSES = frozenset(
    {
        "architecture",
        "glazing",
        "signage",
        "ground",
        "vegetation",
        "sky",
        "people",
        "water",
        "vehicle",
        "furniture",
        "other",
    }
)


class ConsentRequired(ValueError):
    """No client consent record was supplied (decision 10: no plate leaves the client without one)."""


class TierCError(RuntimeError):
    """The packet that was built does not validate; ``errors`` holds the ``plate_spec`` messages."""

    def __init__(self, errors: list[str]):
        super().__init__("tier C packet failed validation: " + "; ".join(errors[:5]))
        self.errors = errors


def new_packet_id(now_ms: int | None = None) -> str:
    """``pkt_`` + a 26-character Crockford base32 ULID (48-bit millisecond time, 80 random bits)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    value = ((now_ms & ((1 << 48) - 1)) << 80) | secrets.randbits(80)
    return "pkt_" + "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def _blake3_hex(path: Path) -> str:
    h = blake3.blake3()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path: Path, obj: Any) -> None:
    with imageio.atomic_target(path) as part:
        part.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def write_label_png(path: str | Path, labels: np.ndarray) -> None:
    """Write a segment label map as a 16-bit grey PNG (0 = no segment), atomically."""
    ok, encoded = cv2.imencode(".png", np.ascontiguousarray(np.asarray(labels).astype(np.uint16)))
    if not ok:
        raise OSError("cv2 could not encode the 16-bit segment map")
    with imageio.atomic_target(path) as part:
        part.write_bytes(bytes(encoded))


def _file_entry(root: Path, name: str, fmt: str, **dims: int) -> dict[str, Any]:
    path = root / name
    return {
        "kind": "file",
        "path": name,
        "bytes": path.stat().st_size,
        "hash": {"algo": "blake3", "hex": _blake3_hex(path)},
        "format": fmt,
        **dims,
    }


def _gravity_camera(roll_deg: float, pitch_deg: float) -> list[float]:
    """Unit gravity vector in camera axes (+X right, +Y up, +Z toward the viewer): a level camera reads
    (0, -1, 0); pitching the camera up by p tilts it to (0, -cos p, sin p)."""
    roll, pitch = math.radians(roll_deg), math.radians(pitch_deg)
    vec = (-math.sin(roll) * math.cos(pitch), -math.cos(roll) * math.cos(pitch), math.sin(pitch))
    return [round(c, 6) for c in vec]


def _camera(width: int, height: int, fov_h_deg: float) -> dict[str, Any]:
    """Centred square-pixel pinhole on a 36 mm film from a horizontal field of view; fx is rederived from
    the rounded focal length so the semantic check (fx = focal * width / film_width) holds exactly."""
    focal = round(FILM_WIDTH_MM / (2.0 * math.tan(math.radians(fov_h_deg) / 2.0)), 6)
    fx = focal * width / FILM_WIDTH_MM
    hfov = math.degrees(2.0 * math.atan(FILM_WIDTH_MM / (2.0 * focal)))
    vfov = math.degrees(2.0 * math.atan(math.tan(math.radians(hfov) / 2.0) * height / width))
    cx, cy = width / 2.0, height / 2.0
    identity = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
    return {
        "name": "client_plate",
        "source": "estimated",
        "projection": "perspective",
        "convention": {
            "id": plate_spec.CONVENTION_ID,
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
        },
        "world_from_camera": identity,
        "focal_length_mm": focal,
        "film_width_mm": FILM_WIDTH_MM,
        "zoom_factor": 1.0,
        "fov_deg": {"horizontal": round(hfov, 4), "vertical": round(vfov, 4)},
        "lens_shift": {"horizontal": 0.0, "vertical": 0.0, "raw_unit": "none"},
        "distortion": "none",
        "intrinsics_px": {
            "fx": fx,
            "fy": fx,
            "principal_point": [cx, cy],
            "K": [[fx, 0.0, cx], [0.0, fx, cy], [0.0, 0.0, 1.0]],
        },
        "resolution": {"width": width, "height": height, "pixel_aspect": 1.0},
        "safe_frame": {"x": 0, "y": 0, "width": width, "height": height},
        "f_number": None,
        "focus_distance": None,
        "dof_enabled": False,
        "exposure": {"mode": "none"},
        "white_balance": {"mode": "neutral"},
        "frames": {"hero": 0, "start": 0, "end": 0, "fps": FPS},
    }


def _estimated_model(backend: Any) -> dict[str, str]:
    entry = licence_get(backend.model_id)
    if entry is None:  # unreachable for a constructed backend (the licence gate ran), kept as a guard
        raise TierCError([f"{backend.model_id}: not in the licence registry"])
    stub = bool(getattr(backend, "is_stub", False))
    return {
        "model": f"stub-{entry.name}" if stub else entry.name,
        "version": "stub" if stub else entry.version,
        "licence": licence_summary(entry),
    }


def _legend(
    packet_id: str,
    labels: np.ndarray,
    segments: list[dict[str, Any]],
    user_labels: Mapping[int, Mapping[str, str]],
) -> tuple[dict[str, Any], dict[str, float]]:
    total = float(labels.shape[0] * labels.shape[1])
    entries = []
    for seg in segments:
        sid = int(seg["id"])
        given = user_labels.get(sid)
        if given:
            klass, source, name = given["class"], "manual", given.get("name") or f"seg_{sid:05d}"
        else:
            klass, source, name = "other", "autotag", f"seg_{sid:05d}"
        entries.append(
            {
                "id": f"s:{sid:05d}",
                "kind": "segment",
                "name": name,
                "tags": [],
                "class": klass,
                "class_source": source,
                "pixel_share": round(seg["area"] / total, 6),
            }
        )
    unlabelled = round(sum(e["pixel_share"] for e in entries if e["class_source"] == "unlabelled"), 6)
    labelled = round(sum(e["pixel_share"] for e in entries if e["class_source"] != "unlabelled"), 6)
    coverage = {
        "labelled_share": labelled,
        "unlabelled_share": unlabelled,
        "uncovered_share": round(max(0.0, 1.0 - labelled - unlabelled), 6),
    }
    return {"packet_id": packet_id, "entries": entries}, coverage


def _check_labels(user_labels: Mapping[int, Mapping[str, str]], present: set[int]) -> None:
    for sid, given in user_labels.items():
        if sid not in present:
            raise ValueError(f"label for segment {sid}, which is not in the segmentation")
        if given.get("class") not in _CLASSES:
            raise ValueError(f"segment {sid}: class {given.get('class')!r} is not one of {sorted(_CLASSES)}")


def build_tier_c_packet(
    plate_path: str | Path,
    out_dir: str | Path,
    backends: Backends,
    project_id: str,
    shot_id: str,
    consent_record: str,
    *,
    labels: Mapping[int, Mapping[str, str]] | None = None,
    nda: bool = False,
) -> Path:
    """Build and validate a tier C packet in ``out_dir`` (created; must be empty). Returns ``out_dir``.

    ``labels`` optionally names segments, ``{segment_id: {"class": <id class>, "name": <str>}}`` (the Mask
    node's later labelling); labelled ids of an editable class become editable, everything else stays locked.
    """
    if not isinstance(consent_record, str) or not consent_record.strip():
        raise ConsentRequired("a client consent record is required to build a tier C packet")
    for what, value in (("project_id", project_id), ("shot_id", shot_id)):
        if not isinstance(value, str) or not _SLUG.match(value):
            raise ValueError(f"invalid {what} {value!r}: expected a lower-case slug [a-z0-9_-]")
    plate = Path(plate_path)
    image = imageio.read(plate) if plate.is_file() else None
    if image is None:
        raise InputError(f"cannot read the plate {str(plate)!r}")
    if image.bit_depth not in (8, 16) or image.encoding != "srgb":
        raise InputError("a tier C plate must be a display-referred 8- or 16-bit image")
    rgb = np.ascontiguousarray(image.rgb, dtype=np.float32)
    height, width = rgb.shape[:2]

    depth_backend, intrinsics_backend, segment_backend = backends.depth, backends.intrinsics, backends.segment
    depth = depth_backend.infer(rgb)
    intr = intrinsics_backend.calibrate(rgb)
    label_map, segments = segment_backend.segment(rgb, None)
    user_labels = {int(k): v for k, v in (labels or {}).items()}
    _check_labels(user_labels, {int(s["id"]) for s in segments})

    root = Path(out_dir)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"{root} is not empty: a packet gets a folder of its own")
    root.mkdir(parents=True, exist_ok=True)

    (imageio.write_png16 if image.bit_depth == 16 else imageio.write_png8)(
        root / "client_plate.png", rgb, icc=image.icc
    )
    exr.write_multilayer(root / "depth_estimated.exr", {"": {"Z": depth}}, {"": "float"})
    write_label_png(root / "segments.png", label_map)

    packet_id = new_packet_id()
    legend, coverage = _legend(packet_id, label_map, segments, user_labels)
    _write_json(root / "id_legend.json", legend)

    editable = [
        e["id"]
        for e in legend["entries"]
        if e["class"] not in plate_spec.NEVER_EDITABLE and e["class_source"] != "unlabelled"
    ]
    locked = [e["id"] for e in legend["entries"] if e["id"] not in editable]
    manifest = {
        "packet_version": plate_spec.PACKET_VERSION,
        "packet_id": packet_id,
        "kind": "still",
        "tier": "C",
        "project_id": project_id,
        "shot_id": shot_id,
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "supersedes": None,
        "host": {
            "app": "none",
            "version": "n/a",
            "plugin": {"name": "plate_pixels.tierc", "version": __version__},
        },
        "renderer": {"name": "client", "version": "unknown"},
        "units": {"system_unit": "relative", "meters_per_unit": None, "up_axis": "Z"},
        "camera": _camera(width, height, float(intr["fov_deg"])),
        "colour": {
            "max_colour_mode": "not_applicable",
            "working_space": "display_only",
            "master": "client_plate",
            "vfb_corrections_baked": False,
            "display_encoding": {
                "primaries": "unknown",
                "transfer": "srgb",
                "icc": "embedded" if image.icc else "none",
            },
        },
        "files": {
            "client_plate": _file_entry(
                root, "client_plate.png", "png", width=width, height=height, bit_depth=image.bit_depth
            ),
            "depth_estimated": _file_entry(
                root, "depth_estimated.exr", "exr", width=width, height=height, bit_depth=32
            ),
            "segments": _file_entry(root, "segments.png", "png", width=width, height=height, bit_depth=16),
            "id_legend": _file_entry(root, "id_legend.json", "json"),
        },
        "id_legend": {
            "file": "id_legend",
            "source": "segmentation",
            "segmentation": {"model": segment_backend.model_id, "file": "segments"},
            "entry_count": len(legend["entries"]),
            "coverage": coverage,
        },
        "policy": {
            "rules_version": "1",
            "class_rules": [],
            "lock_by_default": True,
            "locked_ids": locked,
            "editable_ids": editable,
            "text_lock": True,
            "lint": {
                "max_unlabelled_share": MAX_UNLABELLED_SHARE,
                "unlabelled_share": coverage["unlabelled_share"],
                "passed": True,
            },
            "custody": {"nda": bool(nda), **({"allowed_grades": ["C0", "C1"]} if nda else {})},
        },
        "render": {
            "session": "client_supplied",
            "sampler": "unknown",
            "time_s": None,
            "denoiser": {"beauty": "unknown"},
        },
        "estimated": {
            "depth": _estimated_model(depth_backend),
            "intrinsics": _estimated_model(intrinsics_backend),
            "segments": _estimated_model(segment_backend),
            "depth_is_metric": False,
            "depth_semantics": depth_backend.depth_semantics,
            "gravity_camera": _gravity_camera(
                float(intr.get("roll_deg", 0.0)), float(intr.get("pitch_deg", 0.0))
            ),
            "client": {
                "original_filename_hash": {
                    "algo": "blake3",
                    "hex": blake3.blake3(plate.name.encode("utf-8")).hexdigest(),
                },
                "consent_record": consent_record.strip(),
            },
        },
    }
    _write_json(root / "manifest.json", manifest)

    errors = plate_spec.validate_packet_dir(root)
    if errors:
        raise TierCError(errors)
    return root
