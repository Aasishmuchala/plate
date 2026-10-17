"""Reprojection-drift gate for motion clips, and the flow-based camera-lock gate.

Drift (BUILD_PLAN decision 14, `video.reproj_edge_recall >= 0.8`, hard). A motion packet carries the clay, Z and
Cryptomatte id sequences rendered along the real camera path at the scene's frame rate; an engine clip comes
back at its own rate. For each sampled engine frame j:

1. the time map gives the fractional packet frame k = start + j * packet_fps / engine_fps and the nearest
   packet frame n (timemap.TimeMap, method 'nearest');
2. the camera is interpolated at k (translation lerp, rotation slerp, focal lerp: timemap.camera_at);
3. the reference edges are the G-buffer edges of packet frame n (gbuffer.structural_edges on Z_n and the rank-0
   ids; depth + id kinds, a motion packet has no normals), weighted by what the packet's own clay frame n
   shows (T15's visibility rule: an edge the clay render cannot show is not demanded of the clip) and by the
   circle of confusion when the camera has DOF (coc.weights, all ones otherwise);
4. every weighted edge pixel is back-projected with Z_n, moved into the interpolated camera k and projected
   again (PACKET.md 3.3, vectorised), so the reference sits where the geometry IS at engine time j, not where
   it was up to half a packet frame earlier; the warped pixels are scaled to the working width;
5. the clip's frame j is Canny-detected (gbuffer.test_edges, same detector as the still gate) at the working
   width, dilated by r = 2, and the weighted recall of the warped reference pixels is the frame's
   `edge_recall_r2`; `median_flow_px` is the weighted median distance from a warped reference pixel to the
   nearest clip edge (a distance transform of the clip edges, capped at MAX_DIST_PX), `shift_px` the
   structure.best_shift of the rasterised reference against the clip edges (+-8 px).

`min_recall` over the samples is the gate value: a clip whose camera slides away from the packet path loses the
edges frame by frame and fails; a clip that follows the path but softens detail fails the texture gates, not
this one. Working width = min(width, clip width, packet width) and is never upsampled (the report says which).

Occlusion is not re-solved in the warp: the camera delta is at most half a packet frame (the nearest frame is
used), so a disoccluded sliver is below the pixel tolerance; the per-frame record carries the delta so a report
reader can see when a packet was rendered too coarsely for its engine rate.

Camera lock (`video.camera_lock_median_flow_px <= 1.0 @960`): optical flow from the clip's first sampled frame
to each later sampled frame on a plate_pixels FlowBackend (RAFT on the box: hard; cv2 DIS on the Mac: advisory,
thresholds.yaml `status_by_backend`), frames brought to <= 960 wide first so the pixels are the threshold's
pixels. The clip-level number is the WORST sampled moment (max over pairs of the per-pair median), so a camera
that drifts late in the clip cannot hide behind a steady start. The phase-correlation lock in video.py stays
triage.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import coc, gbuffer, structure, thresholds
from . import io as pio
from .gbuffer import EdgeParams
from .timemap import TimeMap, camera_at

__all__ = [
    "METRIC_ID",
    "LOCK_METRIC_ID",
    "EVAL_WIDTH",
    "MAX_DIST_PX",
    "load_packet",
    "sequence_paths",
    "load_camera_frames",
    "read_z",
    "read_rank0_ids",
    "warp_pixels",
    "reference_edges",
    "drift",
    "camera_lock",
    "backend_name",
]

METRIC_ID = "video.reproj_edge_recall"
LOCK_METRIC_ID = "video.camera_lock_median_flow_px"
EVAL_WIDTH = 960
MAX_DIST_PX = 32.0  # cap on the distance-to-nearest-edge diagnostic (a missing edge is "far", not infinite)
SEQUENCE_ROLES = {"z": "z_seq", "id": "id_seq", "clay": "clay_seq"}


# --------------------------------------------------------------------------- packet files


def load_packet(packet: str | Path | Mapping[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    """(packet_dir | None, manifest) from a packet folder, a manifest.json path or a manifest mapping."""
    if isinstance(packet, Mapping):
        return None, dict(packet)
    p = Path(packet)
    if p.is_dir():
        root, mp = p, p / "manifest.json"
    else:
        root, mp = p.parent, p
    manifest = json.loads(mp.read_text(encoding="utf-8"))
    return root, manifest


def _frame_range(ref: Mapping[str, Any]) -> tuple[int, int]:
    return int(ref["first"]), int(ref["last"])


def sequence_paths(packet_dir: str | Path, manifest: Mapping[str, Any], role: str) -> dict[int, Path]:
    """{packet frame number: file} for a manifest sequence (`z_seq`, `id_seq`, `clay_seq` or the short names
    z / id / clay), expanding `pattern` (printf style, e.g. motion/z.%04d.exr) from `first` to `last`."""
    key = SEQUENCE_ROLES.get(role, role)
    ref = manifest["files"].get(key)
    if ref is None or ref.get("kind") != "sequence":
        raise KeyError(f"manifest has no sequence {key!r}")
    first, last = _frame_range(ref)
    pattern = str(ref["pattern"])
    root = Path(packet_dir)
    return {n: root / (pattern % n) for n in range(first, last + 1)}


def _as_frame_dict(
    seq: Mapping[int, Any] | Sequence[Any] | None,
    start: int,
) -> dict[int, Path] | None:
    """Normalise a sequence argument: a {frame: path} mapping stays, a list is indexed from `start`."""
    if seq is None:
        return None
    if isinstance(seq, Mapping):
        return {int(k): Path(v) for k, v in seq.items()}
    return {start + i: Path(p) for i, p in enumerate(seq)}


def load_camera_frames(packet_dir: str | Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    ref = manifest["files"].get("camera_frames") or {"path": "camera_frames.json"}
    return json.loads((Path(packet_dir) / ref["path"]).read_text(encoding="utf-8"))


def _elements(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    try:
        return {e["element"]: e for e in manifest["files"]["beauty_exr"]["elements"]}
    except (KeyError, TypeError):
        return {}


def _open_exr(path: str | Path) -> dict[str, np.ndarray]:
    """{channel name: 2-D array} of a single- or multi-part EXR (OpenEXR 3.x, channels kept separate)."""
    import OpenEXR  # gates stay import-light; only the motion gate needs the EXR reader

    exr = OpenEXR.File(str(path), separate_channels=True)
    out: dict[str, np.ndarray] = {}
    for part in exr.parts:
        for name, ch in part.channels.items():
            out.setdefault(name, ch.pixels)
    return out


def _pick(channels: Mapping[str, np.ndarray], preferred: list[str], suffixes: tuple[str, ...]) -> np.ndarray:
    for name in preferred:
        if name in channels:
            return channels[name]
    for name, arr in channels.items():
        if name.endswith(suffixes) or name in suffixes:
            return arr
    raise KeyError(f"no channel among {preferred} or ending in {suffixes} (have {sorted(channels)})")


def read_z(path: str | Path, manifest: Mapping[str, Any] | None = None) -> np.ndarray:
    """Planar depth float32 (H, W) of one z-sequence frame. The channel comes from the manifest's `zdepth`
    element (layer + channel, e.g. VRayZDepth.Z); without a manifest the first channel ending in 'Z' is used."""
    el = _elements(manifest or {}).get("zdepth")
    preferred = [f"{el['layer']}.{el['channels'][0]}", el["channels"][0]] if el else []
    arr = _pick(_open_exr(path), preferred, (".Z", "Z"))
    return np.ascontiguousarray(arr, dtype=np.float32)


def read_rank0_ids(path: str | Path, manifest: Mapping[str, Any] | None = None) -> np.ndarray:
    """Rank-0 Cryptomatte object ids uint32 (H, W) of one id-sequence frame (the R channel of layer 00,
    reinterpreted bit for bit, as plate_pixels.exr.read_cryptomatte does)."""
    el = _elements(manifest or {}).get("cryptomatte_object")
    preferred = []
    if el:
        ch = el["channels"][0]
        preferred = [ch if "." in ch else f"{el['layer']}.{ch}", ch]
    arr = _pick(_open_exr(path), preferred, ("00.R", "00.r"))
    return np.ascontiguousarray(arr, dtype=np.float32).view(np.uint32)


def read_clay(path: str | Path) -> np.ndarray:
    """Clay frame as float32 sRGB (H, W, 3) in 0..1 (8- or 16-bit PNG / TIFF)."""
    return pio.load_rgb(path)


# --------------------------------------------------------------------------- geometry


def _intr(camera: Mapping[str, Any]) -> tuple[float, float, float, float]:
    k = camera["intrinsics_px"]
    return float(k["fx"]), float(k["fy"]), float(k["principal_point"][0]), float(k["principal_point"][1])


def warp_pixels(
    us: np.ndarray,
    vs: np.ndarray,
    z: np.ndarray,
    cam_src: Mapping[str, Any],
    cam_dst: Mapping[str, Any],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Move pixel centres (us + 0.5, vs + 0.5) with planar depth `z` (per pixel, scene units) from `cam_src`
    into `cam_dst`: back-project (PACKET.md 3.3 inverted), to world with src world_from_camera, then the
    normative projection into dst. Returns (u, v, depth) in dst render pixels; depth <= 0 means behind."""
    fx, fy, cx, cy = _intr(cam_src)
    zz = np.asarray(z, dtype=np.float64)
    x = (np.asarray(us, dtype=np.float64) + 0.5 - cx) / fx * zz
    y = -(np.asarray(vs, dtype=np.float64) + 0.5 - cy) / fy * zz
    pc = np.stack([x, y, -zz, np.ones_like(zz)], axis=0)
    m_src = np.asarray(cam_src["world_from_camera"], dtype=np.float64)
    m_dst = np.asarray(cam_dst["world_from_camera"], dtype=np.float64)
    pw = m_src @ pc
    r, t = m_dst[:3, :3], m_dst[:3, 3]
    c = r.T @ (pw[:3] - t[:, None])
    depth = -c[2]
    fx2, fy2, cx2, cy2 = _intr(cam_dst)
    with np.errstate(divide="ignore", invalid="ignore"):
        u = cx2 + fx2 * c[0] / depth
        v = cy2 - fy2 * c[1] / depth
    return u, v, depth


# --------------------------------------------------------------------------- reference edges


def _ellipse(r: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * int(r) + 1, 2 * int(r) + 1))


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    if r <= 0:
        return mask.astype(bool)
    return cv2.dilate(mask.astype(np.uint8), _ellipse(r)) > 0


def reference_edges(
    z: np.ndarray,
    ids: np.ndarray | None,
    camera: Mapping[str, Any],
    clay: np.ndarray | None = None,
    *,
    r: int = 2,
    detector: str = "rgb",
    params: EdgeParams | None = None,
    meters_per_unit: float | None = None,
) -> tuple[gbuffer.EdgeMap, np.ndarray, dict[str, Any]]:
    """(EdgeMap at the packet's native size, weights float32 (H, W), info).

    Edges: gbuffer.structural_edges on Z and rank-0 ids (depth + id kinds; no normals in a motion packet).
    Weights: visibility (1 where the clay frame's own Canny edges, dilated by r, hit the reference edge; 0
    elsewhere; all ones without a clay) times coc.weights (all ones with DOF off).
    """
    p = params or EdgeParams(width=None)
    emap = gbuffer.structural_edges(z, None, ids, camera, p)
    h, w = emap.shape
    weights = np.ones((h, w), dtype=np.float32)
    info: dict[str, Any] = {"visibility_weighted": False, "coc_weighted": False, "detector": detector}
    if clay is not None:
        clay_w = gbuffer.resample_rgb(clay, w, h)
        vis = _dilate(gbuffer.test_edges(clay_w, detector), r)
        weights *= vis.astype(np.float32)
        info["visibility_weighted"] = True
        n_ref = int(emap.edges.sum())
        info["plate_visibility"] = float((emap.edges & vis).sum() / n_ref) if n_ref else 1.0
    if camera.get("dof_enabled"):
        zw = gbuffer.resample_z(np.asarray(z), w, h)
        cw = coc.weights(zw, camera, w, meters_per_unit)
        weights *= cw
        info["coc_weighted"] = bool((cw[emap.edges] < 1.0).any())
    return emap, weights, info


def _sample_frames(n_usable: int, samples: int) -> list[int]:
    if n_usable <= 0:
        return []
    if samples >= n_usable:
        return list(range(n_usable))
    picks = np.round(np.linspace(0, n_usable - 1, int(samples))).astype(int)
    return sorted({int(j) for j in picks})


def _frame_time(j: int, fps: float) -> float:
    """A -ss time that lands on frame j: a quarter frame before its timestamp (io.frame_at takes the first
    frame whose timestamp is >= t)."""
    return max((float(j) - 0.25) / float(fps), 0.0)


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    v, w = values[order], weights[order]
    cum = np.cumsum(w)
    if cum[-1] <= 0:
        return float("nan")
    return float(v[int(np.searchsorted(cum, 0.5 * cum[-1]))])


def _resize_rgb(rgb: np.ndarray, w: int, h: int) -> np.ndarray:
    if rgb.shape[1] == w and rgb.shape[0] == h:
        return rgb
    interp = cv2.INTER_AREA if w < rgb.shape[1] else cv2.INTER_LINEAR
    return cv2.resize(rgb, (w, h), interpolation=interp)


def _bit_depth(pix_fmt: str | None) -> int | None:
    """8 for yuv420p / rgb24, 10 for yuv420p10le, 12 for gbrp12le, 16 for rgb48le; None when unknown."""
    if not pix_fmt:
        return None
    import re

    m = re.search(r"p(\d{1,2})(?:le|be)?$", pix_fmt)
    if m:
        return int(m.group(1))
    m = re.match(r"(?:rgb|bgr|gbr|gray|ya|rgba|bgra)(\d+)", pix_fmt)
    if m:
        bits = int(m.group(1))
        return {24: 8, 32: 8, 48: 16, 64: 16, 8: 8, 16: 16}.get(bits, bits)
    if pix_fmt.endswith(("p", "a")) or pix_fmt in ("gray", "nv12", "nv21", "yuyv422", "uyvy422"):
        return 8
    return None


def drift(
    video: str | Path,
    packet_motion: str | Path | Mapping[str, Any],
    z_seq: Mapping[int, Any] | Sequence[Any] | None,
    id_seq: Mapping[int, Any] | Sequence[Any] | None,
    camera_frames: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    timemap: TimeMap,
    samples: int = 8,
    width: int = EVAL_WIDTH,
    *,
    clay_seq: Mapping[int, Any] | Sequence[Any] | None = None,
    r: int = 2,
    detector: str = "rgb",
    use_clay: bool = True,
) -> dict[str, Any]:
    """Reprojection drift of `video` against a motion packet through `timemap` (module doc).

    `packet_motion` is the packet folder (then z_seq / id_seq / clay_seq / camera_frames may be None and are
    read from the manifest) or the manifest mapping (then the sequences must be given). Sequences are
    {packet frame: path} mappings or lists indexed from the packet's first frame. `use_clay=False` drops the
    visibility weighting (every reference edge counts).

    Returns {per_frame: [{engine_frame, packet_frame, nearest_packet_frame, edge_recall_r2, median_flow_px,
    shift_px, camera_delta_px, n_ref, n_ref_weighted, n_out_of_frame, clamped}], min_recall, mean_recall,
    verdict, status, threshold, width, height, samples, timemap, backend, notes, ...}.
    """
    root, manifest = load_packet(packet_motion)
    camera = manifest["camera"]
    mpu = float(manifest.get("units", {}).get("meters_per_unit", 1.0))
    start = timemap.start
    zs = _as_frame_dict(z_seq, start)
    ids_ = _as_frame_dict(id_seq, start)
    clays = _as_frame_dict(clay_seq, start) if use_clay else None
    if root is not None:
        zs = zs or sequence_paths(root, manifest, "z")
        ids_ = ids_ or sequence_paths(root, manifest, "id")
        if use_clay and clays is None and manifest["files"].get("clay_seq"):
            clays = sequence_paths(root, manifest, "clay")
        if camera_frames is None:
            camera_frames = load_camera_frames(root, manifest)
    if zs is None or ids_ is None:
        raise ValueError("drift needs z_seq and id_seq (or a packet folder to read them from)")
    if camera_frames is None:
        raise ValueError("drift needs camera_frames (or a packet folder to read them from)")

    v = Path(video)
    info = pio.probe(v)
    n_video = int(info["nb_frames"])
    fps = float(info["fps"])
    usable = min(int(timemap.engine_frames or 0), n_video)
    picks = _sample_frames(usable, samples)
    notes: list[str] = []
    if n_video < int(timemap.engine_frames or 0):
        notes.append(
            f"clip has {n_video} frames, the time map covers {timemap.engine_frames}: sampled the clip"
        )
    if abs(fps - float(timemap.engine_fps)) > 1e-3:
        notes.append(f"clip fps {fps:g} differs from the time map's engine fps {float(timemap.engine_fps):g}")

    first_z = read_z(zs[min(zs)], manifest)
    ph, pw = first_z.shape
    work_w = min(int(width), int(info["width"]), int(pw))
    work_h = max(int(round(info["height"] * work_w / info["width"])), 1)
    scale_x = work_w / float(pw)
    scale_y = work_h / float(ph)
    per_frame: list[dict[str, Any]] = []
    cache: dict[int, tuple[gbuffer.EdgeMap, np.ndarray, dict[str, Any], np.ndarray, dict[str, Any]]] = {}
    vis_flag = coc_flag = False
    for j in picks:
        k_raw = timemap.packet_frame_for(j)
        k, clamped = timemap.clamp(k_raw)
        n = timemap.nearest_packet_frame(j)
        if n not in cache:
            z_n = read_z(zs[n], manifest)
            id_n = read_rank0_ids(ids_[n], manifest) if n in ids_ else None
            clay_n = read_clay(clays[n]) if clays and n in clays else None
            cam_n = camera_at(camera_frames, float(n), camera)
            emap, wts, einfo = reference_edges(
                z_n, id_n, cam_n, clay_n, r=r, detector=detector, meters_per_unit=mpu
            )
            cache[n] = (emap, wts, einfo, z_n, cam_n)
        emap, wts, einfo, z_n, cam_n = cache[n]
        vis_flag |= bool(einfo["visibility_weighted"])
        coc_flag |= bool(einfo["coc_weighted"])
        cam_k = camera_at(camera_frames, k, camera)
        vs_, us_ = np.nonzero(emap.edges)
        w_ref = wts[vs_, us_].astype(np.float64)
        keep = w_ref > 0
        us_, vs_, w_ref = us_[keep], vs_[keep], w_ref[keep]
        n_ref = int(emap.edges.sum())
        if us_.size == 0:
            per_frame.append(
                {
                    "engine_frame": int(j),
                    "packet_frame": float(k_raw),
                    "nearest_packet_frame": int(n),
                    "edge_recall_r2": None,
                    "median_flow_px": None,
                    "shift_px": None,
                    "camera_delta_px": 0.0,
                    "n_ref": n_ref,
                    "n_ref_weighted": 0.0,
                    "n_out_of_frame": 0,
                    "clamped": bool(clamped),
                }
            )
            continue
        u, vv, depth = warp_pixels(us_, vs_, z_n[vs_, us_], cam_n, cam_k)
        delta = float(np.hypot(u - (us_ + 0.5), vv - (vs_ + 0.5)).mean()) * scale_x
        ui = np.floor(u * scale_x).astype(np.int64)
        vi = np.floor(vv * scale_y).astype(np.int64)
        inside = (depth > 0) & (ui >= 0) & (ui < work_w) & (vi >= 0) & (vi < work_h)
        n_out = int((~inside).sum())
        ui, vi, w_in = ui[inside], vi[inside], w_ref[inside]
        frame = pio.frame_at(v, _frame_time(j, fps), info=info)
        test = gbuffer.test_edges(_resize_rgb(frame, work_w, work_h), detector)
        dil = _dilate(test, r)
        hit = dil[vi, ui]
        total = float(w_in.sum())
        recall = float((w_in * hit).sum() / total) if total > 0 else None
        if test.any():
            dist = cv2.distanceTransform((~test).astype(np.uint8), cv2.DIST_L2, 5)
            d = np.minimum(dist[vi, ui].astype(np.float64), MAX_DIST_PX)
            median_d = _weighted_median(d, w_in) if total > 0 else None
        else:
            median_d = MAX_DIST_PX
        ref_map = np.zeros((work_h, work_w), dtype=bool)
        ref_map[vi, ui] = True
        shift = structure.best_shift(ref_map, test, rng=8, r=r) if (ref_map.any() and test.any()) else None
        per_frame.append(
            {
                "engine_frame": int(j),
                "packet_frame": float(k_raw),
                "nearest_packet_frame": int(n),
                "edge_recall_r2": recall,
                "median_flow_px": median_d,
                "shift_px": [int(shift["dx"]), int(shift["dy"])] if shift else None,
                "camera_delta_px": delta,
                "n_ref": n_ref,
                "n_ref_weighted": total,
                "n_out_of_frame": n_out,
                "clamped": bool(clamped),
            }
        )

    recalls = [f["edge_recall_r2"] for f in per_frame if f["edge_recall_r2"] is not None]
    min_recall = float(min(recalls)) if recalls else None
    mean_recall = float(np.mean(recalls)) if recalls else None
    if min_recall is None:
        status, bound = "advisory", float(thresholds.load().get(METRIC_ID).bound())
        notes.append("no reference edge carried weight: drift not measurable")
    else:
        st, thr = thresholds.check(METRIC_ID, min_recall)
        status, bound = st, float(thr.bound())
    backend = (
        f"canny-{detector}(sigma {gbuffer.CANNY_SIGMA}) vs reprojected G-buffer edges (depth+id)"
        f"{' with clay visibility' if vis_flag else ''} @{work_w}"
    )
    if not vis_flag:
        notes.append("no clay sequence: every G-buffer edge counts (unweighted)")
    if work_w < int(width):
        notes.append(f"working width {work_w} (clip {info['width']}, packet {pw}; never upsampled)")
    return {
        "metric_id": METRIC_ID,
        "per_frame": per_frame,
        "min_recall": min_recall,
        "mean_recall": mean_recall,
        "verdict": "pass" if status == "pass" else "fail" if status == "fail" else "advisory",
        "status": status,
        "threshold": bound,
        "width": int(work_w),
        "height": int(work_h),
        "packet_width": int(pw),
        "packet_height": int(ph),
        "video_width": int(info["width"]),
        "video_height": int(info["height"]),
        "video_frames": n_video,
        "engine_frames_used": usable,
        "samples": len(per_frame),
        "r": int(r),
        "visibility_weighted": vis_flag,
        "coc_weighted": coc_flag,
        "edge_kinds": ["depth", "id"],
        "timemap": timemap.to_json(),
        "backend": backend,
        "notes": notes,
    }


# --------------------------------------------------------------------------- camera lock (flow)


def backend_name(backend: Any) -> str:
    """'raft' | 'dis' | 'stub' | ... : the FlowBackend's `impl`, else its class name lower-cased."""
    impl = getattr(backend, "impl", None)
    if isinstance(impl, str) and impl:
        return impl
    return type(backend).__name__.lower()


def _default_flow_backend() -> Any:
    from plate_pixels.flow import DisFlow  # the Mac / CI fallback; RAFT is chosen by the caller on the box

    return DisFlow()


def camera_lock(
    video: str | Path,
    backend: Any = None,
    width: int = EVAL_WIDTH,
    samples: int = 8,
    *,
    t0: float = 0.0,
) -> dict[str, Any]:
    """Flow-based camera lock: {median_flow_px, p95, backend, status, per_pair, width, height, samples}.

    `samples` frames are taken evenly over the clip (the first at t0); flow runs from the first sampled frame
    to each later one on `backend` (a plate_pixels FlowBackend; None = DisFlow) after resizing to at most
    `width` wide, so the numbers are in pixels of that width. median_flow_px / p95 are the maxima over pairs
    of the per-pair median / 95th percentile of the flow magnitude. status comes from thresholds.yaml with the
    backend applied (raft: hard, dis: advisory).
    """
    if backend is None:
        backend = _default_flow_backend()
    name = backend_name(backend)
    v = Path(video)
    info = pio.probe(v)
    n_video = int(info["nb_frames"])
    fps = float(info["fps"])
    work_w = min(int(width), int(info["width"]))
    work_h = max(int(round(info["height"] * work_w / info["width"])), 1)
    j0 = min(max(int(round(t0 * fps)), 0), max(n_video - 1, 0))
    picks = [j0 + j for j in _sample_frames(n_video - j0, max(int(samples), 2))]

    def grab(j: int) -> np.ndarray:
        f = pio.frame_at(v, _frame_time(j, fps), info=info)
        return _resize_rgb(f, work_w, work_h).astype(np.float32) / 255.0

    base = grab(picks[0])
    per_pair: list[dict[str, Any]] = []
    for j in picks[1:]:
        u, vv = backend.flow(base, grab(j))
        mag = np.hypot(np.asarray(u, dtype=np.float32), np.asarray(vv, dtype=np.float32))
        per_pair.append(
            {
                "engine_frame": int(j),
                "t_s": float(j / fps),
                "median": float(np.median(mag)),
                "p95": float(np.percentile(mag, 95)),
                "mean": float(mag.mean()),
            }
        )
    median = max((p["median"] for p in per_pair), default=0.0)
    p95 = max((p["p95"] for p in per_pair), default=0.0)
    status, thr = thresholds.check(LOCK_METRIC_ID, median, backend=name)
    return {
        "metric_id": LOCK_METRIC_ID,
        "median_flow_px": float(median),
        "p95": float(p95),
        "backend": name,
        "backend_info": backend.info() if hasattr(backend, "info") else {"impl": name},
        "status": status,
        "threshold": float(thr.bound()),
        "threshold_status": thr.effective_status(name),
        "per_pair": per_pair,
        "width": int(work_w),
        "height": int(work_h),
        "samples": len(picks),
        "video_frames": n_video,
    }
