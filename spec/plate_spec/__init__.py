"""plate_spec: the Shot Packet v0.1 contract as code.

JSON Schema (spec/packet.schema.json) checks structure. This module adds the semantic rules the
schema cannot express (spec/PACKET.md section 7) and the normative reference implementations of
the camera projection (PACKET.md 3.3), the 3ds Max matrix conversion (3.2) and the Cryptomatte id
hash (4.2). Consumers (daemon ingest, plugin-writer tests, gates) call these instead of
re-implementing them.
"""

from __future__ import annotations

import json
import math
import struct
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

SPEC_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = SPEC_DIR / "packet.schema.json"
EXAMPLES_DIR = SPEC_DIR / "examples"
EXAMPLE_NAMES = ("tierA_still_clubhouse", "tierA_motion_drive_in", "tierC_client_plate")
PACKET_VERSION = "0.1.1"
CONVENTION_ID = "plate-cam-v0"

# Classes an AI edit may never touch (doctrine: geometry first; text-lock).
NEVER_EDITABLE = frozenset({"architecture", "glazing", "signage", "other"})

# Tolerances for the semantic checks.
ORTHO_TOL = 1e-4
FOV_TOL_DEG = 0.005
FX_REL_TOL = 1e-4
EV_TOL = 0.05
SHARE_TOL = 1e-3
HERO_MATRIX_TOL = 1e-6

__all__ = [
    "SCHEMA_PATH",
    "EXAMPLES_DIR",
    "EXAMPLE_NAMES",
    "PACKET_VERSION",
    "CONVENTION_ID",
    "load_schema",
    "load_example",
    "load_example_dir",
    "validate_manifest",
    "validate_legend",
    "validate_camera_frames",
    "validate_packet_dir",
    "materialize_example",
    "world_from_camera_from_max_rows",
    "project_point",
    "murmur3_32",
    "cryptomatte_id",
    "cryptomatte_float",
]


# --------------------------------------------------------------------------- loading


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_example_dir(name: str) -> Path:
    if name not in EXAMPLE_NAMES:
        raise KeyError(f"unknown example {name!r}; known: {EXAMPLE_NAMES}")
    return EXAMPLES_DIR / name


def load_example(name: str) -> dict[str, Any]:
    return json.loads((load_example_dir(name) / "manifest.json").read_text(encoding="utf-8"))


def _validator(defs_ref: str | None = None):
    import jsonschema

    schema = load_schema()
    if defs_ref is not None:
        schema = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{defs_ref}"}
    cls = jsonschema.Draft202012Validator
    return cls(schema, format_checker=cls.FORMAT_CHECKER)


def _schema_errors(obj: Any, defs_ref: str | None = None, prefix: str = "") -> list[str]:
    errs = []
    for e in sorted(_validator(defs_ref).iter_errors(obj), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in e.absolute_path) or "<root>"
        errs.append(f"{prefix}schema: {where}: {e.message}")
    return errs


# --------------------------------------------------------------------------- math references


def _matmul3(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _rot(m: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[float(m[i][j]) for j in range(3)] for i in range(3)]


def _det3(r: Sequence[Sequence[float]]) -> float:
    return (
        r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
        - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
        + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0])
    )


def _orthonormal_error(m: Sequence[Sequence[float]]) -> str | None:
    r = _rot(m)
    rt = [[r[j][i] for j in range(3)] for i in range(3)]
    rtr = _matmul3(rt, r)
    worst = max(abs(rtr[i][j] - (1.0 if i == j else 0.0)) for i in range(3) for j in range(3))
    if worst > ORTHO_TOL:
        return f"rotation is not orthonormal (max |RᵀR - I| = {worst:.2e}; scaled or sheared camera?)"
    if _det3(r) < 0:
        return "rotation has det < 0 (mirrored camera)"
    return None


def world_from_camera_from_max_rows(rows: Sequence[Sequence[float]]) -> list[list[float]]:
    """PACKET.md 3.2. 3ds Max Matrix3 (row-vector convention, 4 rows of 3: local X, Y, Z axes in
    world, then translation) -> plate-cam-v0 world_from_camera (column vectors, row-major 4x4).

    M[i][j] = rows[j][i] for i < 3, j < 4; last row [0, 0, 0, 1]. Max cameras already look down
    their local -Z with +Y up, so no axis flip is needed.
    """
    if len(rows) != 4 or any(len(r) != 3 for r in rows):
        raise ValueError("expected 4 rows of 3 (Max Matrix3 row1..row4)")
    m = [[float(rows[j][i]) for j in range(4)] for i in range(3)]
    m.append([0.0, 0.0, 0.0, 1.0])
    return m


def project_point(camera: dict[str, Any], p_world: Sequence[float]) -> tuple[float, float, float]:
    """PACKET.md 3.3, the normative projection. Returns (u, v, depth) where u, v are render-frame
    pixel coordinates (origin at the top-left corner of the top-left pixel, centres at +0.5, v
    down) and depth is the planar distance along the view axis in scene units (> 0 in front).
    """
    m = camera["world_from_camera"]
    r = _rot(m)
    t = [float(m[i][3]) for i in range(3)]
    d = [float(p_world[i]) - t[i] for i in range(3)]
    # camera_from_world = [Rᵀ | -Rᵀ t]
    xc = sum(r[k][0] * d[k] for k in range(3))
    yc = sum(r[k][1] * d[k] for k in range(3))
    zc = sum(r[k][2] * d[k] for k in range(3))
    depth = -zc
    if depth <= 0:
        raise ValueError("point is behind the camera")
    k = camera["intrinsics_px"]
    cx, cy = k["principal_point"]
    u = cx + k["fx"] * (xc / depth)
    v = cy - k["fy"] * (yc / depth)
    return u, v, depth


def murmur3_32(data: bytes, seed: int = 0) -> int:
    """MurmurHash3_x86_32 (Austin Appleby), the hash Cryptomatte uses."""
    c1, c2 = 0xCC9E2D51, 0x1B873593
    h = seed & 0xFFFFFFFF
    n = len(data)
    nblocks = n // 4
    for i in range(nblocks):
        (k,) = struct.unpack_from("<I", data, i * 4)
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF
    tail = data[nblocks * 4 :]
    k = 0
    if len(tail) >= 3:
        k ^= tail[2] << 16
    if len(tail) >= 2:
        k ^= tail[1] << 8
    if len(tail) >= 1:
        k ^= tail[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
    h ^= n
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def _cryptomatte_hash(name: str) -> int:
    h = murmur3_32(name.encode("utf-8"))
    exp = (h >> 23) & 0xFF
    if exp in (0, 255):  # Cryptomatte spec: avoid denormals, inf and NaN in the float32 id
        h ^= 1 << 23
    return h


def cryptomatte_id(name: str, kind: str = "object") -> str:
    """Legend id for a Cryptomatte name: 'o:<8 hex>' (object) or 'm:<8 hex>' (material)."""
    prefix = {"object": "o", "material": "m"}[kind]
    return f"{prefix}:{_cryptomatte_hash(name):08x}"


def cryptomatte_float(name: str) -> float:
    """The float32 value Cryptomatte stores in the EXR id channel for this name."""
    return struct.unpack("<f", struct.pack("<I", _cryptomatte_hash(name)))[0]


# --------------------------------------------------------------------------- semantic checks


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def _camera_errors(m: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    cam = m["camera"]
    res = cam["resolution"]
    w, h, pa = res["width"], res["height"], res["pixel_aspect"]

    e = _orthonormal_error(cam["world_from_camera"])
    if e:
        errs.append(f"camera.world_from_camera: {e}")
    if m["tier"] == "C":
        ident = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
        if any(abs(cam["world_from_camera"][i][j] - ident[i][j]) > 1e-9 for i in range(4) for j in range(4)):
            errs.append("camera.world_from_camera: tier C has no world; the matrix must be identity")

    f_eff = cam["focal_length_mm"] * cam["zoom_factor"]
    fw = cam["film_width_mm"]
    hfov = math.degrees(2 * math.atan(fw / (2 * f_eff)))
    if not _close(cam["fov_deg"]["horizontal"], hfov, FOV_TOL_DEG):
        errs.append(
            f"camera.fov_deg.horizontal {cam['fov_deg']['horizontal']} != {hfov:.4f} from focal/film/zoom"
        )
    vfov = math.degrees(2 * math.atan(math.tan(math.radians(hfov) / 2) * h / (w * pa)))
    if not _close(cam["fov_deg"]["vertical"], vfov, FOV_TOL_DEG):
        errs.append(f"camera.fov_deg.vertical {cam['fov_deg']['vertical']} != {vfov:.4f} from aspect")

    k = cam["intrinsics_px"]
    fx_expect = f_eff * w / fw
    if abs(k["fx"] - fx_expect) > FX_REL_TOL * fx_expect:
        errs.append(f"camera.intrinsics_px.fx {k['fx']} != {fx_expect:.3f} (focal*zoom*width/film_width)")
    if abs(k["fy"] - k["fx"] * pa) > FX_REL_TOL * k["fy"]:
        errs.append("camera.intrinsics_px.fy must equal fx * pixel_aspect")
    cx, cy = k["principal_point"]
    if not (0 <= cx <= w and 0 <= cy <= h):
        errs.append("camera.intrinsics_px.principal_point lies outside the frame")
    kk = k["K"]
    expect_k = [[k["fx"], 0.0, cx], [0.0, k["fy"], cy], [0.0, 0.0, 1.0]]
    if any(abs(kk[i][j] - expect_k[i][j]) > 1e-6 for i in range(3) for j in range(3)):
        errs.append("camera.intrinsics_px.K must be [[fx,0,cx],[0,fy,cy],[0,0,1]] (no skew)")
    ls = cam["lens_shift"]
    if ls["horizontal"] == 0 and ls["vertical"] == 0:
        if not (_close(cx, w / 2, 0.5) and _close(cy, h / 2, 0.5)):
            errs.append("camera: zero lens shift but principal point is off-centre")

    sf = cam["safe_frame"]
    if sf["x"] + sf["width"] > w or sf["y"] + sf["height"] > h:
        errs.append("camera.safe_frame extends outside the rendered resolution")
    full = (sf["x"], sf["y"], sf["width"], sf["height"]) == (0, 0, w, h)
    if not full and "overscan" not in cam:
        errs.append("camera.overscan is required when safe_frame is not the whole frame")
    if "overscan" in cam:
        a, b = (int(x) for x in cam["overscan"]["engine_aspect"].split(":"))
        if a == 0 or b == 0 or abs((w / h) - (a / b)) > 0.01 * (a / b):
            errs.append(f"camera.overscan.engine_aspect {a}:{b} does not match the rendered {w}x{h}")

    fr = cam["frames"]
    if not (fr["start"] <= fr["hero"] <= fr["end"]):
        errs.append("camera.frames: hero must lie in [start, end]")
    if m["kind"] == "still" and not (fr["start"] == fr["hero"] == fr["end"]):
        errs.append("camera.frames: a still packet has start == hero == end")
    if m["kind"] == "motion" and fr["hero"] != fr["start"]:
        errs.append("camera.frames: a motion packet's first frame is the hero frame (hero == start)")

    ex = cam["exposure"]
    if ex["mode"] == "physical" and cam["f_number"]:
        ev = math.log2(cam["f_number"] ** 2 / ex["shutter_s"]) - math.log2(ex["iso"] / 100.0)
        if not _close(ex["ev100"], ev, EV_TOL):
            errs.append(f"camera.exposure.ev100 {ex['ev100']} != {ev:.3f} from f_number/shutter/iso")
    return errs


def _files_errors(m: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    cam = m["camera"]
    w, h = cam["resolution"]["width"], cam["resolution"]["height"]
    files = m["files"]
    for role in ("beauty_exr", "display_tiff16", "client_plate", "depth_estimated", "segments"):
        f = files.get(role)
        if f and (f.get("width"), f.get("height")) != (w, h):
            errs.append(f"files.{role}: {f.get('width')}x{f.get('height')} != camera.resolution {w}x{h}")
    # Packets are written on Windows and may sit on SMB: names must not collide case-insensitively.
    names = [ref["path"] if ref["kind"] == "file" else ref["pattern"] for ref in files.values()]
    names += [ref["hashes"] for ref in files.values() if ref["kind"] == "sequence"]
    folded = [n.casefold() for n in names]
    if len(set(folded)) != len(folded):
        errs.append("files: two paths differ only by case (Windows and SMB are case-insensitive)")
    exr = files.get("beauty_exr")
    if exr:
        seen = set()
        for el in exr["elements"]:
            key = (el["element"], el.get("light_name"))
            if key in seen:
                errs.append(f"files.beauty_exr.elements: duplicate element {key}")
            seen.add(key)
        crypto = m["id_legend"].get("cryptomatte")
        if crypto:
            for el in exr["elements"]:
                which = {"cryptomatte_object": "object", "cryptomatte_material": "material"}.get(
                    el["element"]
                )
                if which and el["rank_levels"] != crypto[which]["rank_levels"]:
                    errs.append(
                        f"files.beauty_exr: {el['element']} rank_levels != id_legend.cryptomatte.{which}"
                    )
                if which and el["layer"] != crypto[which]["layer"]:
                    errs.append(
                        f"files.beauty_exr: {el['element']} layer != id_legend.cryptomatte.{which}.layer"
                    )
        if m["render"]["denoiser"].get("denoised_elements"):
            for name in m["render"]["denoiser"]["denoised_elements"]:
                for el in exr["elements"]:
                    if el["layer"] == name and el["element"] in (
                        "zdepth",
                        "normals",
                        "cryptomatte_object",
                        "cryptomatte_material",
                        "world_position",
                    ):
                        errs.append(f"render.denoiser: data pass {name} must never be denoised")
    if m["kind"] == "motion":
        fr = cam["frames"]
        mres = m["motion"]["sequence_resolution"]
        for role in ("clay_seq", "id_seq", "z_seq"):
            s = files[role]
            if (s["first"], s["last"]) != (fr["start"], fr["end"]):
                errs.append(
                    f"files.{role}: frames {s['first']}..{s['last']} != camera.frames {fr['start']}..{fr['end']}"
                )
            if (s["width"], s["height"]) != (mres["width"], mres["height"]):
                errs.append(f"files.{role}: size != motion.sequence_resolution")
        if mres["width"] * h != mres["height"] * w:
            errs.append("motion.sequence_resolution must have exactly the render's aspect (w*H == h*W)")
        if m["motion"]["hero_still_packet_id"] == m["packet_id"]:
            errs.append("motion.hero_still_packet_id must name the still packet, not this one")
    return errs


def _colour_errors(m: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    c = m["colour"]
    if m["tier"] == "A" and c["max_colour_mode"] == "not_applicable":
        errs.append("colour.max_colour_mode: a tier A packet must state the host colour mode")
    if m["tier"] == "C" and c["max_colour_mode"] != "not_applicable":
        errs.append("colour.max_colour_mode: tier C has no host; use not_applicable")
    return errs


def _policy_errors(m: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    pol = m["policy"]
    locked, editable = set(pol["locked_ids"]), set(pol["editable_ids"])
    both = locked & editable
    if both:
        errs.append(f"policy: ids both locked and editable: {sorted(both)}")
    lint = pol["lint"]
    if lint["unlabelled_share"] > lint["max_unlabelled_share"]:
        errs.append("policy.lint: unlabelled_share exceeds max_unlabelled_share (Send must be blocked)")
    cov = m["id_legend"]["coverage"]
    if not _close(lint["unlabelled_share"], cov["unlabelled_share"], SHARE_TOL):
        errs.append("policy.lint.unlabelled_share != id_legend.coverage.unlabelled_share")
    total = cov["labelled_share"] + cov["unlabelled_share"] + cov["uncovered_share"]
    if not _close(total, 1.0, SHARE_TOL):
        errs.append(f"id_legend.coverage labelled + unlabelled + uncovered = {total:.4f}, must be 1")
    custody = pol.get("custody")
    if custody and custody["nda"] and set(custody.get("allowed_grades", [])) - {"C0", "C1"}:
        errs.append("policy.custody: NDA projects run on C0 and C1 only (decision 9)")
    return errs


def validate_manifest(manifest: Any) -> list[str]:
    """Schema plus every semantic rule that needs only the manifest. Empty list = valid."""
    errs = _schema_errors(manifest)
    if errs:
        return errs  # semantic checks assume the structure is right
    for fn in (_camera_errors, _files_errors, _colour_errors, _policy_errors):
        errs.extend(fn(manifest))
    return errs


def _rule_matches(rule: dict[str, Any], entry: dict[str, Any]) -> bool:
    value = {"layer": entry.get("layer"), "node": entry.get("name"), "material": entry.get("material")}[
        rule["source"]
    ]
    if rule["source"] == "material" and entry["kind"] == "material":
        value = entry["name"]
    if not value:
        return False
    return value.startswith(rule["pattern"]) if rule["match"] == "prefix" else value == rule["pattern"]


def validate_legend(legend: Any, manifest: dict[str, Any]) -> list[str]:
    """id_legend.json against its schema and against the manifest's policy and coverage."""
    errs = _schema_errors(legend, "id_legend_file", prefix="id_legend.json ")
    if errs:
        return errs
    entries = legend["entries"]
    if legend["packet_id"] != manifest["packet_id"]:
        errs.append("id_legend.json: packet_id does not match the manifest")
    if len(entries) != manifest["id_legend"]["entry_count"]:
        errs.append("id_legend.json: entry count != id_legend.entry_count")
    ids = [e["id"] for e in entries]
    if len(ids) != len(set(ids)):
        errs.append("id_legend.json: duplicate ids")
    source = manifest["id_legend"]["source"]
    kind_for = {"o": "object", "m": "material", "s": "segment"}
    for e in entries:
        if kind_for[e["id"][0]] != e["kind"]:
            errs.append(f"id_legend.json: {e['id']} prefix does not match kind {e['kind']}")
        if (source == "cryptomatte") == (e["kind"] == "segment"):
            errs.append(f"id_legend.json: {e['id']} kind {e['kind']} not allowed for source {source}")
        if e["kind"] in ("object", "material") and e["id"] != cryptomatte_id(e["name"], e["kind"]):
            errs.append(f"id_legend.json: {e['id']} is not the Cryptomatte hash of its name {e['name']!r}")
        if e["kind"] == "segment" and not 1 <= int(e["id"][2:]) <= 65535:
            errs.append(f"id_legend.json: {e['id']} is outside the 16-bit label range 1..65535")
        if manifest["tier"] == "A" and "digest" not in e:
            errs.append(f"id_legend.json: tier A entry {e['id']} needs a digest (decision 17 carry-over)")
    pol = manifest["policy"]
    locked, editable = set(pol["locked_ids"]), set(pol["editable_ids"])
    all_ids = set(ids)
    unknown = (locked | editable) - all_ids
    if unknown:
        errs.append(f"policy: ids not in id_legend.json: {sorted(unknown)}")
    uncovered = all_ids - locked - editable
    if uncovered:
        errs.append(f"policy: legend ids neither locked nor editable: {sorted(uncovered)}")
    by_id = {e["id"]: e for e in entries}
    for i in sorted(editable & all_ids):
        e = by_id[i]
        if e["class"] in NEVER_EDITABLE:
            errs.append(f"policy: {i} is class {e['class']} and can never be AI-editable")
        if e["class_source"] == "unlabelled":
            errs.append(f"policy: {i} is unlabelled and must stay locked")
    for e in entries:
        hits = [r for r in pol["class_rules"] if _rule_matches(r, e)]
        if e["class_source"] == "rule" and not hits:
            errs.append(f"id_legend.json: {e['id']} says class_source=rule but no class_rule matches it")
        if hits and e["class_source"] != "manual" and hits[0]["class"] != e["class"]:
            errs.append(
                f"id_legend.json: {e['id']} class {e['class']} != first matching rule {hits[0]['class']} "
                "(only a manual override may differ from the rule)"
            )
        if hits and hits[0]["class"] in NEVER_EDITABLE and e["id"] in editable:
            errs.append(f"policy: {e['id']} is on a {hits[0]['class']} layer and can never be AI-editable")
    # Object ids decide; material ids only narrow an editable object, never widen a locked one.
    locked_materials = {e.get("material") for e in entries if e["kind"] == "object" and e["id"] in locked}
    for e in entries:
        if e["kind"] == "material" and e["id"] in editable and e["name"] in locked_materials:
            errs.append(f"policy: editable material {e['id']} ({e['name']}) is used by a locked object")
    share_kind = "segment" if source == "segmentation" else "object"
    unl = sum(
        e["pixel_share"] for e in entries if e["kind"] == share_kind and e["class_source"] == "unlabelled"
    )
    lab = sum(
        e["pixel_share"] for e in entries if e["kind"] == share_kind and e["class_source"] != "unlabelled"
    )
    cov = manifest["id_legend"]["coverage"]
    if not _close(unl, cov["unlabelled_share"], SHARE_TOL):
        errs.append(
            f"id_legend.coverage.unlabelled_share {cov['unlabelled_share']} != {unl:.4f} summed from entries"
        )
    if not _close(lab, cov["labelled_share"], SHARE_TOL):
        errs.append(
            f"id_legend.coverage.labelled_share {cov['labelled_share']} != {lab:.4f} summed from entries"
        )
    return errs


def validate_camera_frames(frames_doc: Any, manifest: dict[str, Any]) -> list[str]:
    errs = _schema_errors(frames_doc, "camera_frames_file", prefix="camera_frames.json ")
    if errs:
        return errs
    cam = manifest["camera"]
    if frames_doc["packet_id"] != manifest["packet_id"]:
        errs.append("camera_frames.json: packet_id does not match the manifest")
    if frames_doc["fps"] != cam["frames"]["fps"]:
        errs.append("camera_frames.json: fps != camera.frames.fps")
    numbers = [f["frame"] for f in frames_doc["frames"]]
    expect = list(range(cam["frames"]["start"], cam["frames"]["end"] + 1))
    if numbers != expect:
        errs.append("camera_frames.json: frames must be contiguous and ordered start..end")
    for f in frames_doc["frames"]:
        e = _orthonormal_error(f["world_from_camera"])
        if e:
            errs.append(f"camera_frames.json frame {f['frame']}: {e}")
            break
    if numbers and numbers[0] == cam["frames"]["hero"]:
        first = frames_doc["frames"][0]["world_from_camera"]
        hero = cam["world_from_camera"]
        if any(abs(first[i][j] - hero[i][j]) > HERO_MATRIX_TOL for i in range(4) for j in range(4)):
            errs.append("camera_frames.json: hero frame matrix != camera.world_from_camera")
    return errs


def _blake3_file(path: Path) -> str:
    import blake3

    h = blake3.blake3()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inside(root: Path, rel: str) -> Path | None:
    p = (root / rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p


def _check_file(root: Path, role: str, ref: dict[str, Any]) -> list[str]:
    p = _inside(root, ref["path"])
    if p is None:
        return [f"files.{role}: path escapes the packet folder"]
    if not p.is_file():
        return [f"files.{role}: missing {ref['path']}"]
    errs = []
    if p.stat().st_size != ref["bytes"]:
        errs.append(f"files.{role}: size {p.stat().st_size} != manifest bytes {ref['bytes']}")
    if _blake3_file(p) != ref["hash"]["hex"]:
        errs.append(f"files.{role}: blake3 mismatch")
    return errs


def _check_sequence(root: Path, role: str, ref: dict[str, Any]) -> list[str]:
    hp = _inside(root, ref["hashes"])
    if hp is None or not hp.is_file():
        return [f"files.{role}: hashes file missing"]
    try:
        raw = hp.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return [f"files.{role}: hashes file is not UTF-8"]
    lines = [ln.split(None, 1) for ln in raw.splitlines() if ln.strip()]
    if any(len(parts) != 2 for parts in lines):
        return [f"files.{role}: hashes file has a line without '<hex>  <path>'"]
    if len(lines) != ref["last"] - ref["first"] + 1:
        return [
            f"files.{role}: hashes file lists {len(lines)} frames, expected {ref['last'] - ref['first'] + 1}"
        ]
    errs = []
    for n, (hexd, rel) in zip(range(ref["first"], ref["last"] + 1), lines, strict=True):
        if rel.strip() != ref["pattern"] % n:
            errs.append(f"files.{role}: hashes line for frame {n} names {rel.strip()}")
            break
        fp = _inside(root, rel.strip())
        if fp is None or not fp.is_file():
            errs.append(f"files.{role}: missing frame {rel.strip()}")
            break
        if _blake3_file(fp) != hexd:
            errs.append(f"files.{role}: blake3 mismatch on {rel.strip()}")
            break
    return errs


def validate_packet_dir(path: str | Path, check_files: bool = True) -> list[str]:
    """Validate a packet folder: manifest, id_legend.json, camera_frames.json and (optionally)
    the presence, size and BLAKE3 of every file the manifest names."""
    root = Path(path)
    mpath = root / "manifest.json"
    if not mpath.is_file():
        return ["manifest.json missing"]
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return [f"manifest.json is not UTF-8 JSON: {e}"]
    errs = validate_manifest(manifest)
    if errs and any(e.startswith("schema:") for e in errs):
        return errs
    files = manifest["files"]

    def _load(role: str) -> Any | None:
        p = _inside(root, files[role]["path"])
        if p is None or not p.is_file():
            errs.append(f"files.{role}: missing {files[role]['path']}")
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            errs.append(f"files.{role}: not UTF-8 JSON: {e}")
            return None

    legend = _load("id_legend")
    if legend is not None:
        errs.extend(validate_legend(legend, manifest))
    if manifest["kind"] == "motion":
        frames = _load("camera_frames")
        if frames is not None:
            errs.extend(validate_camera_frames(frames, manifest))
    if check_files:
        for role, ref in files.items():
            if ref["kind"] == "file":
                errs.extend(_check_file(root, role, ref))
            else:
                errs.extend(_check_sequence(root, role, ref))
    return errs


def iter_example_dirs() -> Iterable[Path]:
    for name in EXAMPLE_NAMES:
        yield load_example_dir(name)


def materialize_example(name: str, dest: str | Path) -> Path:
    """Copy an example into dest as a complete packet folder that passes
    validate_packet_dir(check_files=True). Binary payloads (EXR, TIFF, PNG frames) are small
    PLACEHOLDER bytes, not real images: this is for ingest/transport tests, never for gates.
    The manifest's bytes and hashes are rewritten to match what was written."""
    import blake3

    src = load_example_dir(name)
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))

    def put(rel: str, data: bytes) -> tuple[int, str]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return len(data), blake3.blake3(data).hexdigest()

    for role, ref in manifest["files"].items():
        if ref["kind"] == "file":
            srcfile = src / ref["path"]
            if ref["format"] == "json" and srcfile.is_file():
                data = srcfile.read_bytes()
            else:
                data = f"PLATE-PLACEHOLDER {name} {role}\n".encode()
            ref["bytes"], ref["hash"]["hex"] = put(ref["path"], data)
        else:
            lines = []
            for n in range(ref["first"], ref["last"] + 1):
                rel = ref["pattern"] % n
                _, hexd = put(rel, f"PLATE-PLACEHOLDER {name} {role} {n}\n".encode())
                lines.append(f"{hexd}  {rel}")
            put(ref["hashes"], ("\n".join(lines) + "\n").encode())
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return root
