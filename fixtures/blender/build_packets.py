"""Repack the raw Blender renders from scene.py into PLATE Shot Packets (spec/PACKET.md 0.1.1).

Runs in the project venv (OpenEXR 3.x, numpy, blake3, tifffile, plate_spec):

    .venv/bin/python fixtures/blender/build_packets.py <work_dir> <packets_dir>

<work_dir> holds <variant>/<shot>/{ml.exr, zu.exr, display.tif, info.json} and <variant>/scene_records.json
from scene.py. Every shot becomes one packet folder <packets_dir>/<shot>/ whose beauty.exr is a SINGLE-PART
multilayer EXR with V-Ray layer names, so every gate on this Mac reads the layout the Max plugin writes:

    R G B A (half)                           beauty, Blender Combined (filtered, not denoised)
    VRayZDepth.Z (float)                     planar, unfiltered, 1e10 where nothing was hit (from zu.exr)
    VRayNormals.X/Y/Z (half)                 CAMERA space, R^T n_world, renormalised
    CryptoObject00..02.R/G/B/A (float)       6 ranks, header attributes rewritten for the new layer name
    CryptoMaterial00..02.R/G/B/A (float)
    VRayWireColor.R/G/B (half)               per-object colour from the Cryptomatte rank-0 id (unfiltered)
    VRayDiffuseFilter.R/G/B (half)           Blender Diffuse Color
    VRayRawLighting.R/G/B (half)             Diffuse Direct (pre-albedo, V-Ray raw-lighting semantics)
    VRayRawGlobalIllumination.R/G/B (half)   Diffuse Indirect
    VRayReflection.R/G/B (half)              (Glossy Direct + Glossy Indirect) x Glossy Color

so beauty ~= diffuse_filter x (raw_lighting + raw_gi) + reflection holds on opaque, non-emissive pixels (the
fixture recomposition identity; refraction and emission are not split out). The three raw elements are opt-in
(PACKET.md 4.2) and written only for RAW_SHOTS, the shots the recolour validation (T31) reads. Output is
deterministic: the packet id is derived from the shot name, created_utc is fixed and render.time_s is null.

Size budget: Cycles noise and fine texture make a 640x360 render ~6 MB of float data, so the repack applies a
documented lossy QUANTISATION (QUANT below): half layers lose 3 to 6 low mantissa bits (<= 1.6% relative on the
beauty, 3% on the raw lighting layers: the recomposition identity then holds to ~2%), Z is rounded to a 0.5 mm
grid, the display TIFF and the clay PNGs drop 4 and 6 low bits and are deflate coded. Cryptomatte ids/coverage
are never touched (half or rounded ids corrupt the hashes).
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

import blake3
import numpy as np
import OpenEXR

import plate_spec as ps

HERE = Path(__file__).resolve().parent
PROJECT_ID = "plate-fixture"
CREATED_UTC = "2026-10-17T00:00:00Z"
PLUGIN = {"name": "plate_fixture", "version": "0.1.0"}
SENSOR_WIDTH_MM = 36.0
FPS = 25
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PLANAR_PREFIXES = ("ARCH_wall", "ARCH_ceiling", "ARCH_panel", "HARD_", "GLZ_")
DETAIL_SIZE_M = {"HARD_floor": 0.10, "VEG_shrub_A": 0.06}
EDITABLE_CLASSES = {"vegetation", "people", "sky", "water"}
# CAM_a is delivered 2.39:1 inside the 16:9 render (overscan); every other camera is full frame
SAFE_FRAME_ASPECT = {"CAM_a": 2.39}
Z_BACKGROUND = 1e10
RAW_SHOTS = {"room_a", "room_a_ral3020"}  # carry raw_lighting / raw_gi / reflection (opt-in elements)
# mantissa bits dropped per layer family (half has 10, float32 has 23); see the module docstring
QUANT = {"beauty": 5, "diffuse": 4, "normals": 3, "raw": 6, "z": 11}
TIFF_DROP_BITS = 4
PNG_DROP_BITS = 6
SHOT_KIND = {"dolly_12f": "motion"}
SUPERSEDES = {"room_a_v2": "room_a"}


# --------------------------------------------------------------------------- small helpers


def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def b3(data: bytes) -> str:
    return blake3.blake3(data).hexdigest()


def b3_file(path: Path) -> str:
    return ps._blake3_file(path)  # noqa: SLF001 (the spec's own file hasher: same bytes the validator checks)


def write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")


def packet_id(shot: str) -> str:
    """Deterministic pkt_ + 26 Crockford base32 characters (ULID-shaped, opaque)."""
    n = int.from_bytes(blake3.blake3(f"plate-fixture/{shot}".encode()).digest()[:16], "big")
    chars = [CROCKFORD[(n >> (5 * i)) & 31] for i in range(26)]
    return "pkt_" + "".join(reversed(chars))


def layer_of(name: str) -> str:
    return name.split("_")[0] + "_"


def wire_colour(name: str) -> tuple[float, float, float]:
    h = ps.murmur3_32(name.encode("utf-8"), 7)
    return tuple(0.15 + 0.85 * (((h >> s) & 255) / 255.0) for s in (0, 8, 16))  # type: ignore[return-value]


def quant_half(a: np.ndarray, drop: int) -> np.ndarray:
    """Round a half-float array to 10 - drop mantissa bits (round to nearest, never into inf)."""
    if drop <= 0:
        return a.astype(np.float16)
    u = a.astype(np.float16).view(np.uint16).astype(np.uint32)
    sign, mag = u & 0x8000, u & 0x7FFF
    mag = np.minimum(((mag + (1 << (drop - 1))) >> drop) << drop, 0x7BFF)
    return (sign | mag).astype(np.uint16).view(np.float16)


def quant_float(a: np.ndarray, drop: int) -> np.ndarray:
    """Round a float32 array to 23 - drop mantissa bits (finite, positive values only: Z and its 1e10 background)."""
    u = np.ascontiguousarray(a, dtype=np.float32).view(np.uint32).astype(np.uint64)
    return ((((u + (1 << (drop - 1))) >> drop) << drop).astype(np.uint32)).view(np.float32)


# --------------------------------------------------------------------------- EXR in and out


def spec_hashes(manifest: dict) -> dict:
    """Blender writes the raw MurmurHash3 into the Cryptomatte manifest but the denormal/inf-safe value into the
    pixels (Cryptomatte spec: flip bit 23 when the float exponent is 0 or 255). ARCH_marker_04 is one such name,
    so the manifest would not match its pixels. Rewrite every entry with the spec's hash (plate_spec), which is
    what the pixels hold; entries that already agree are unchanged."""
    return {n: ps.cryptomatte_id(n).split(":")[1] for n in manifest}


class Exr:
    """All channels of a (multi-part) EXR by full channel name, plus the first part's header."""

    def __init__(self, path: Path):
        f = OpenEXR.File(str(path), separate_channels=True)
        self.channels = {k: v.pixels for part in f.parts for k, v in part.channels.items()}
        self.header = dict(f.parts[0].header)
        self.path = path

    def rgb(self, layer: str) -> np.ndarray:
        return np.stack([self.channels[f"ViewLayer.{layer}.{c}"] for c in "RGB"], -1).astype(np.float32)

    def crypto(self, kind: str):
        """(ids uint32 (H,W,6), coverage float32 (H,W,6), manifest {name: hex}) for Object|Material."""
        ids, cov = [], []
        for i in range(3):
            base = f"ViewLayer.Crypto{kind}{i:02d}"
            r, g, b, a = (self.channels[f"{base}.{c}"] for c in "rgba")
            ids += [r, b]
            cov += [g, a]
        ids = np.ascontiguousarray(np.stack(ids, -1).astype(np.float32)).view(np.uint32)
        cov = np.stack(cov, -1).astype(np.float32)
        key = next(
            k.split("/")[1]
            for k, v in self.header.items()
            if k.startswith("cryptomatte/") and k.endswith("/name") and v == f"ViewLayer.Crypto{kind}"
        )
        manifest = json.loads(self.header[f"cryptomatte/{key}/manifest"])
        return ids, cov, spec_hashes(manifest)


def crypto_header(layer: str, manifest: dict) -> dict:
    key = f"{ps.murmur3_32(layer.encode('utf-8')):08x}"[:7]
    return {
        f"cryptomatte/{key}/name": layer,
        f"cryptomatte/{key}/hash": "MurmurHash3_32",
        f"cryptomatte/{key}/conversion": "uint32_to_float32",
        f"cryptomatte/{key}/manifest": json.dumps(manifest, separators=(",", ":")),
    }


def write_exr(path: Path, channels: dict[str, np.ndarray], header: dict | None = None, codec=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = {"compression": codec or OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage}
    hdr.update(header or {})
    tmp = path.with_suffix(path.suffix + ".part")
    OpenEXR.File(hdr, channels).write(str(tmp))
    tmp.replace(path)


def crypto_channels(ids: np.ndarray, cov: np.ndarray, layer: str) -> dict[str, np.ndarray]:
    """Re-pack decoded ranks into <layer>00..02.R/G/B/A float32 (R=id, G=coverage, B=id2, A=coverage2)."""
    out = {}
    idf = ids.view(np.float32)
    for i in range(3):
        base = f"{layer}{i:02d}"
        out[f"{base}.R"] = np.ascontiguousarray(idf[..., 2 * i])
        out[f"{base}.G"] = np.ascontiguousarray(cov[..., 2 * i])
        out[f"{base}.B"] = np.ascontiguousarray(idf[..., 2 * i + 1])
        out[f"{base}.A"] = np.ascontiguousarray(cov[..., 2 * i + 1])
    return out


def crypto_channel_names(layer: str) -> list[str]:
    return [f"{layer}{i:02d}.{c}" for i in range(3) for c in "RGBA"]


# --------------------------------------------------------------------------- camera and manifest


def camera_block(shot: str, info: dict, rows: list[list[float]], convention: dict) -> dict:
    cam = info["cam"]
    w, h = cam["resolution"]
    lens, film = cam["lens_mm"], cam["sensor_width_mm"]
    hfov = math.degrees(2 * math.atan(film / (2 * lens)))
    vfov = math.degrees(2 * math.atan(math.tan(math.radians(hfov) / 2) * h / w))
    fx = lens * w / film
    aspect = SAFE_FRAME_ASPECT.get(cam["name"])
    if aspect:
        sh = round(w / aspect)
        safe = {"x": 0, "y": (h - sh) // 2, "width": w, "height": sh}
    else:
        safe = {"x": 0, "y": 0, "width": w, "height": h}
    n_frames = len(info["frames"]) or 1
    out = {
        "name": cam["name"],
        "source": "scene",
        "node_type": "BlenderCamera",
        "projection": "perspective",
        "convention": convention,
        "world_from_camera": [[float(v) for v in r] for r in rows],
        "focal_length_mm": lens,
        "film_width_mm": film,
        "zoom_factor": 1.0,
        "fov_deg": {"horizontal": round(hfov, 4), "vertical": round(vfov, 4)},
        "lens_shift": {"horizontal": 0, "vertical": 0, "raw_unit": "none", "vertical_tilt_correction": False},
        "distortion": "none",
        "intrinsics_px": {
            "fx": fx,
            "fy": fx,
            "principal_point": [w / 2, h / 2],
            "K": [[fx, 0.0, w / 2], [0.0, fx, h / 2], [0.0, 0.0, 1.0]],
        },
        "resolution": {"width": w, "height": h, "pixel_aspect": 1.0},
        "safe_frame": safe,
        "f_number": cam["f_number"],
        "focus_distance": cam["focus_distance_m"],
        "dof_enabled": cam["dof_enabled"],
        "exposure": {"mode": "none"},
        "white_balance": {"mode": "neutral"},
        "frames": {"hero": 0, "start": 0, "end": n_frames - 1, "fps": FPS},
    }
    if SHOT_KIND.get(shot) == "motion":
        out["frames_file"] = "camera_frames"
    if aspect:
        out["overscan"] = {
            "engine_aspect": "16:9",
            "note": "2.39:1 deliverable rendered inside a 16:9 frame so vendors see real context",
        }
    return out


def element_list(raw: bool) -> list[dict]:
    def rgb(element, layer, **kw):
        return dict(
            element=element,
            layer=layer,
            channels=["R", "G", "B"],
            dtype="half",
            filtered=True,
            denoised=False,
            **kw,
        )

    return [
        dict(
            element="beauty",
            layer="RGBA",
            channels=["R", "G", "B", "A"],
            dtype="half",
            filtered=True,
            denoised=False,
        ),
        dict(
            element="zdepth",
            layer="VRayZDepth",
            channels=["Z"],
            dtype="float",
            filtered=False,
            denoised=False,
            depth_kind="planar",
            normalized=False,
            clamped=False,
            background_value=Z_BACKGROUND,
        ),
        dict(
            element="normals",
            layer="VRayNormals",
            channels=["X", "Y", "Z"],
            dtype="half",
            filtered=True,
            denoised=False,
            space="camera",
            encoding="signed",
        ),
        dict(
            element="cryptomatte_object",
            layer="CryptoObject",
            channels=crypto_channel_names("CryptoObject"),
            dtype="float",
            filtered=True,
            denoised=False,
            rank_levels=6,
        ),
        dict(
            element="cryptomatte_material",
            layer="CryptoMaterial",
            channels=crypto_channel_names("CryptoMaterial"),
            dtype="float",
            filtered=True,
            denoised=False,
            rank_levels=6,
        ),
        dict(
            element="wire_colour",
            layer="VRayWireColor",
            channels=["R", "G", "B"],
            dtype="half",
            filtered=False,
            denoised=False,
        ),
        rgb("diffuse_filter", "VRayDiffuseFilter"),
        *(
            [
                rgb("raw_lighting", "VRayRawLighting"),
                rgb("raw_gi", "VRayRawGlobalIllumination"),
                rgb("reflection", "VRayReflection"),
            ]
            if raw
            else []
        ),
    ]


# --------------------------------------------------------------------------- legend


def build_legend(
    pid: str, ids, cov, manifest_o, mat_ids, mat_cov, manifest_m, records: dict, class_rules: list
):
    """Entries for every object and material that has pixels in this camera. pixel_share is the rank-0
    coverage sum over the frame (PACKET.md 4.3). Returns (entries, locked, editable, coverage)."""
    n = ids.shape[0] * ids.shape[1]
    objects, materials = records["objects"], records["materials"]

    def cls_of(name: str) -> str:
        layer = layer_of(name)
        for r in class_rules:
            if r["source"] == "layer" and layer.startswith(r["pattern"]):
                return r["class"]
        return "other"

    def visible(h: int, ids_, cov_) -> tuple[float, float]:
        m = ids_ == h
        return float((cov_ * m).sum()), float((cov_[..., 0] * m[..., 0]).sum())

    entries, class_of_material = [], {}
    for name, hexid in manifest_o.items():
        if name not in objects:
            continue  # lights, the default entry
        total, rank0 = visible(int(hexid, 16), ids, cov)
        if total < 0.5:
            continue
        obj = objects[name]
        mname = obj["material"]
        cls = cls_of(name)
        class_of_material.setdefault(mname, cls)
        e = {
            "id": ps.cryptomatte_id(name, "object"),
            "kind": "object",
            "name": name,
            "layer": layer_of(name),
            "material": mname,
            "tags": [],
            "class": cls,
            "class_source": "rule",
            "pixel_share": round(rank0 / n, 4),
            "digest": b3(canon({"node": obj, "material": materials[mname]})),
        }
        if name.startswith(PLANAR_PREFIXES):
            e["planar"] = True
        if name in DETAIL_SIZE_M:
            e["detail_size_m"] = DETAIL_SIZE_M[name]
        entries.append(e)
    for name, hexid in manifest_m.items():
        if name not in materials or name not in class_of_material:
            continue
        total, rank0 = visible(int(hexid, 16), mat_ids, mat_cov)
        if total < 0.5:
            continue
        entries.append(
            {
                "id": ps.cryptomatte_id(name, "material"),
                "kind": "material",
                "name": name,
                "tags": [],
                "class": class_of_material[name],
                "class_source": "autotag",
                "pixel_share": round(rank0 / n, 4),
                "digest": b3(canon(materials[name])),
            }
        )
    locked, editable = [], []
    for e in entries:
        (editable if e["class"] in EDITABLE_CLASSES else locked).append(e["id"])
    lab = round(sum(e["pixel_share"] for e in entries if e["kind"] == "object"), 4)
    coverage = {"labelled_share": lab, "unlabelled_share": 0.0, "uncovered_share": round(1.0 - lab, 4)}
    return entries, locked, editable, coverage


# --------------------------------------------------------------------------- one packet


def tiff_info(path: Path) -> tuple[int, int]:
    import tifffile

    with tifffile.TiffFile(str(path)) as t:
        page = t.pages[0]
        assert page.dtype == np.uint16 and page.samplesperpixel == 3, "display master must be RGB uint16"
        return page.imagewidth, page.imagelength


def clean_z(z: np.ndarray) -> np.ndarray:
    """Point-sampled Z with the exact background value, foreground rounded to a ~0.5 mm grid (QUANT['z'])."""
    z = z.astype(np.float32)
    bg = z > 1e9
    return np.where(bg, np.float32(Z_BACKGROUND), quant_float(np.where(bg, 1.0, z), QUANT["z"]))


def write_display(src: Path, dst: Path) -> None:
    """display.tif: same pixels at 12 effective bits, deflate + horizontal predictor (uint16 RGB, still 16-bit)."""
    import tifffile

    px = tifffile.imread(str(src))
    d = TIFF_DROP_BITS
    px = (((px.astype(np.uint32) + (1 << (d - 1))) >> d) << d).clip(0, 65535).astype(np.uint16)
    tifffile.imwrite(
        str(dst), px, photometric="rgb", compression="zlib", compressionargs={"level": 9}, predictor=True
    )


def write_clay_png(src: Path, dst: Path) -> None:
    """clay.%04d.png: uint16 RGB at 10 effective bits (PNG_DROP_BITS), maximum deflate."""
    import cv2

    px = cv2.imdecode(np.fromfile(str(src), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert px.dtype == np.uint16 and px.ndim == 3, "clay frames must be 16-bit RGB"
    d = PNG_DROP_BITS
    px = (((px.astype(np.uint32) + (1 << (d - 1))) >> d) << d).clip(0, 65535).astype(np.uint16)
    ok, buf = cv2.imencode(".png", px, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    assert ok
    dst.write_bytes(buf.tobytes())


def build_still_files(sdir: Path, pdir: Path, info: dict, rows, raw: bool) -> dict:
    """Repack ml.exr + zu.exr into beauty.exr and copy display.tif. Returns the pieces the manifest needs."""
    ml = Exr(sdir / "ml.exr")
    zu = Exr(sdir / "zu.exr")
    h, w = ml.channels["ViewLayer.Combined.R"].shape
    rot = np.array(rows, dtype=np.float64)[:3, :3]

    beauty = np.stack([ml.channels[f"ViewLayer.Combined.{c}"] for c in "RGBA"], -1).astype(np.float32)
    z = clean_z(zu.channels["ViewLayer.Depth.Z"])
    n_world = np.stack([ml.channels[f"ViewLayer.Normal.{c}"] for c in "XYZ"], -1).astype(np.float64)
    n_cam = np.einsum("ij,hwi->hwj", rot, n_world)  # R^T n: camera axes are the columns of R
    length = np.linalg.norm(n_cam, axis=-1, keepdims=True)
    n_cam = np.where(length > 1e-6, n_cam / np.maximum(length, 1e-6), 0.0).astype(np.float32)
    ids, cov, man_o = ml.crypto("Object")
    mids, mcov, man_m = ml.crypto("Material")

    lut_ids = np.array(sorted(int(v, 16) for v in man_o.values()), dtype=np.uint32)
    by_hex = {int(v, 16): k for k, v in man_o.items()}
    lut_rgb = np.array([wire_colour(by_hex[int(i)]) for i in lut_ids], dtype=np.float32)
    pos = np.clip(np.searchsorted(lut_ids, ids[..., 0]), 0, len(lut_ids) - 1)
    hit = lut_ids[pos] == ids[..., 0]
    wire = np.where(hit[..., None], lut_rgb[pos], 0.0).astype(np.float32)

    ch: dict[str, np.ndarray] = {}

    def put(prefix: str, arr: np.ndarray, names: str, drop: int) -> None:
        for i, c in enumerate(names):
            ch[f"{prefix}{c}"] = np.ascontiguousarray(quant_half(arr[..., i], drop))

    put("", beauty, "RGBA", QUANT["beauty"])
    ch["VRayZDepth.Z"] = np.ascontiguousarray(z)
    put("VRayNormals.", n_cam, "XYZ", QUANT["normals"])
    ch.update(crypto_channels(ids, cov, "CryptoObject"))
    ch.update(crypto_channels(mids, mcov, "CryptoMaterial"))
    put("VRayWireColor.", wire, "RGB", 0)
    put("VRayDiffuseFilter.", ml.rgb("Diffuse Color"), "RGB", QUANT["diffuse"])
    if raw:
        reflection = (ml.rgb("Glossy Direct") + ml.rgb("Glossy Indirect")) * ml.rgb("Glossy Color")
        put("VRayRawLighting.", ml.rgb("Diffuse Direct"), "RGB", QUANT["raw"])
        put("VRayRawGlobalIllumination.", ml.rgb("Diffuse Indirect"), "RGB", QUANT["raw"])
        put("VRayReflection.", reflection, "RGB", QUANT["raw"])
    header = {**crypto_header("CryptoObject", man_o), **crypto_header("CryptoMaterial", man_m)}
    write_exr(pdir / "beauty.exr", ch, header)

    write_display(sdir / "display.tif", pdir / "display.tif")
    tw, th = tiff_info(pdir / "display.tif")
    assert (tw, th) == (w, h), f"display.tif is {tw}x{th}, beauty is {w}x{h}"
    return dict(w=w, h=h, ids=ids, cov=cov, mids=mids, mcov=mcov, man_o=man_o, man_m=man_m)


def file_ref(pdir: Path, rel: str, fmt: str, **extra) -> dict:
    p = pdir / rel
    return {
        "kind": "file",
        "path": rel,
        "bytes": p.stat().st_size,
        "hash": {"algo": "blake3", "hex": b3_file(p)},
        "format": fmt,
        **extra,
    }


def build_motion_files(sdir: Path, pdir: Path, info: dict, pid: str) -> dict:
    """clay PNG16 + id/z EXR sequences, hashes files and camera_frames.json."""
    n = len(info["frames"])
    seq = sdir / "seq"
    (pdir / "motion").mkdir(parents=True, exist_ok=True)
    lines = {"clay": [], "id": [], "z": []}
    w = h = 0
    for f in range(n):
        write_clay_png(seq / f"clay_{f:04d}.png", pdir / "motion" / f"clay.{f:04d}.png")
        ml = Exr(seq / f"ml_{f:04d}.exr")
        zu = Exr(seq / f"zu_{f:04d}.exr")
        ids, cov, man_o = ml.crypto("Object")
        h, w = ids.shape[:2]
        write_exr(
            pdir / "motion" / f"id.{f:04d}.exr",
            crypto_channels(ids, cov, "CryptoObject"),
            crypto_header("CryptoObject", man_o),
        )
        z = clean_z(zu.channels["ViewLayer.Depth.Z"])
        write_exr(pdir / "motion" / f"z.{f:04d}.exr", {"VRayZDepth.Z": np.ascontiguousarray(z)})
        for role, name in (("clay", f"clay.{f:04d}.png"), ("id", f"id.{f:04d}.exr"), ("z", f"z.{f:04d}.exr")):
            lines[role].append(f"{b3_file(pdir / 'motion' / name)}  motion/{name}")
    for role, ls in lines.items():
        (pdir / "motion" / f"{role}.blake3").write_text("\n".join(ls) + "\n", encoding="utf-8", newline="\n")
    frames_doc = {
        "packet_id": pid,
        "convention": ps.CONVENTION_ID,
        "fps": FPS,
        "frames": [{"frame": fr["frame"], "world_from_camera": fr["matrix_world"]} for fr in info["frames"]],
    }
    write_json(pdir / "camera_frames.json", frames_doc)

    def seq_ref(role: str, pattern: str, fmt: str, dtype: str) -> dict:
        return {
            "kind": "sequence",
            "pattern": pattern,
            "first": 0,
            "last": n - 1,
            "format": fmt,
            "width": w,
            "height": h,
            "dtype": dtype,
            "hashes": f"motion/{role}.blake3",
        }

    return {
        "camera_frames": file_ref(pdir, "camera_frames.json", "json"),
        "clay_seq": seq_ref("clay", "motion/clay.%04d.png", "png", "uint16"),
        "id_seq": seq_ref("id", "motion/id.%04d.exr", "exr", "float"),
        "z_seq": seq_ref("z", "motion/z.%04d.exr", "exr", "float"),
    }


def build_packet(work: Path, variant: str, shot: str, out: Path, records: dict, template: dict) -> Path:
    sdir = work / variant / shot
    info = json.loads((sdir / "info.json").read_text(encoding="utf-8"))
    pdir = out / shot
    if pdir.exists():
        shutil.rmtree(pdir)
    pdir.mkdir(parents=True)
    pid = packet_id(shot)
    kind = SHOT_KIND.get(shot, "still")
    rows = info["hero"]["matrix_world"]

    pieces = build_still_files(sdir, pdir, info, rows, shot in RAW_SHOTS)
    class_rules = template["policy"]["class_rules"]
    entries, locked, editable, coverage = build_legend(
        pid,
        pieces["ids"],
        pieces["cov"],
        pieces["man_o"],
        pieces["mids"],
        pieces["mcov"],
        pieces["man_m"],
        records,
        class_rules,
    )
    write_json(pdir / "id_legend.json", {"packet_id": pid, "entries": entries})

    camera = camera_block(shot, info, rows, template["camera"]["convention"])
    visible_digests = sorted(e["digest"] for e in entries)
    scene_hash = b3(
        canon(
            {"objects": records["objects"], "materials": records["materials"], "globals": records["globals"]}
        )
    )
    view_hash = b3(canon({"camera": camera, "globals": records["globals"], "digests": visible_digests}))

    files = {
        "beauty_exr": file_ref(
            pdir,
            "beauty.exr",
            "exr",
            width=pieces["w"],
            height=pieces["h"],
            compression="zip",
            parts="single",
            elements=element_list(shot in RAW_SHOTS),
        ),
        "display_tiff16": file_ref(
            pdir, "display.tif", "tiff", width=pieces["w"], height=pieces["h"], bit_depth=16, channels="RGB"
        ),
        "id_legend": file_ref(pdir, "id_legend.json", "json"),
    }
    manifest = {
        "packet_version": ps.PACKET_VERSION,
        "packet_id": pid,
        "kind": kind,
        "tier": "A",
        "project_id": PROJECT_ID,
        "shot_id": shot.replace("_", "-"),
        "created_utc": CREATED_UTC,
        "supersedes": packet_id(SUPERSEDES[shot]) if shot in SUPERSEDES else None,
        "host": {"app": "none", "version": f"blender-{info['blender']}", "plugin": PLUGIN},
        "renderer": {"name": "vray", "version": "fixture-cycles-5.1"},
        "scene_hash": {"algo": "blake3", "hex": scene_hash, "digest_version": "1"},
        "view_hash": {"algo": "blake3", "hex": view_hash},
        "units": {"system_unit": "meters", "meters_per_unit": 1.0, "up_axis": "Z"},
        "camera": camera,
        "colour": {
            "max_colour_mode": "legacy_gamma",
            "legacy_gamma": 2.2,
            "working_space": "lin_rec709",
            "master": "display_tiff16",
            "vfb_corrections_baked": False,
            "display_encoding": {"primaries": "rec709", "transfer": "srgb", "icc": "none"},
        },
        "files": files,
        "id_legend": {
            "file": "id_legend",
            "source": "cryptomatte",
            "cryptomatte": {
                "object": {
                    "layer": "CryptoObject",
                    "hash": "murmur3_32",
                    "id_type": "node_name",
                    "rank_levels": 6,
                    "manifest": "exr_metadata",
                },
                "material": {
                    "layer": "CryptoMaterial",
                    "hash": "murmur3_32",
                    "id_type": "material_name",
                    "rank_levels": 6,
                    "manifest": "exr_metadata",
                },
            },
            "entry_count": len(entries),
            "coverage": coverage,
        },
        "policy": {
            "rules_version": "1",
            "class_rules": class_rules,
            "lock_by_default": True,
            "locked_ids": locked,
            "editable_ids": editable,
            "text_lock": True,
            "lint": {
                "max_unlabelled_share": 0.05,
                "unlabelled_share": coverage["unlabelled_share"],
                "passed": True,
            },
        },
        "render": {
            "session": "fixture",
            "sampler": "progressive",
            "time_s": None,
            "denoiser": {"beauty": "none", "denoised_elements": []},
        },
    }
    if kind == "motion":
        hero_id = packet_id("dolly_hero")
        files.update(build_motion_files(sdir, pdir, info, pid))
        manifest["motion"] = {
            "hero_still_packet_id": hero_id,
            "sequence_resolution": {
                "width": files["clay_seq"]["width"],
                "height": files["clay_seq"]["height"],
            },
            "frame0_matches_hero": True,
            "clay_override": {"material": info["clay_material"], "keeps_glass": False, "keeps_lights": True},
            "path_crossings": [],
        }
    write_json(pdir / "manifest.json", manifest)
    errs = ps.validate_packet_dir(pdir, check_files=True)
    if errs:
        raise SystemExit(f"{shot}: packet invalid:\n  " + "\n  ".join(errs))
    size = sum(p.stat().st_size for p in pdir.rglob("*") if p.is_file())
    print(f"[plate-fixture] {shot}: valid, {size / 1e6:.2f} MB, {len(entries)} legend entries")
    return pdir


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("work_dir", type=Path)
    ap.add_argument("packets_dir", type=Path)
    ap.add_argument("--only", help="comma-separated shot names")
    args = ap.parse_args()
    template = ps.load_example("tierA_still_clubhouse")  # class rules and the camera convention, verbatim
    only = set(args.only.split(",")) if args.only else None
    built = 0
    for vdir in sorted(p for p in args.work_dir.iterdir() if (p / "scene_records.json").is_file()):
        records = json.loads((vdir / "scene_records.json").read_text(encoding="utf-8"))
        for sdir in sorted(p for p in vdir.iterdir() if (p / "info.json").is_file()):
            if only and sdir.name not in only:
                continue
            build_packet(args.work_dir, vdir.name, sdir.name, args.packets_dir, records, template)
            built += 1
    if not built:
        sys.exit("no shots found under " + str(args.work_dir))


if __name__ == "__main__":
    main()
