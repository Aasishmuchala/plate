"""Cross-view correspondences at world positions, and the per-material colour difference measured on them.

    correspondences(packet_a, packet_b, world_pos_a=None, world_pos_b=None, n=2000, seed=0) -> list[dict]
    delta_e(corr, rgb_a, rgb_b, window=5) -> {per_material: {m_id: {de00_median, de00_p90, n, ...}}, overall_p90}

Two cameras of one set see the same surfaces; a Lambertian surface point has one colour whatever the camera, so
the colour at matched world points is the only sound cross-view measurement (BUILD_PLAN P4: "cross-view dE gate at
world-position correspondences on Lambertian points"). Comparing whole-material means across views is not sound:
each camera sees a different part of the wall under different shading, and on the fixture room those means differ
by 2-10 dE00 on an identical render.

How a correspondence is found. Pixels of A are sampled (seeded, uniform over the candidate pool) on materials
that both packets carry. The world point comes from the opt-in ``world_position`` pass when A has it (``source:
'world_position'``), else from A's planar Z back-projected through the packet camera (``source: 'z_backproject'``;
Tier A always has Z). The point is projected into B with the normative ``plate_spec.project_point`` and kept when
it lands in frame, B's Z at that pixel agrees with the projected depth within 1 % (visible, not occluded), B's
dominant material id is the same material, and the point is a Lambertian candidate: class not glazing/water and
normal . view >= 0.3 in both views. The normals are the packet's camera-space normals (+Z toward the camera);
the view vector is the unit vector from the surface point to the camera. When B also carries ``world_position``
its value at the landing pixel must agree with the world point within the same 1 % of depth.

Two more checks make sure both ends are the same surface patch, not merely the same material at a similar depth:
the world-space normals of the two ends agree within ``max_normal_deg`` (20 deg; a point on a curved or faceted
object that lands on a neighbouring facet is rejected), and the ``window`` x ``window`` patch around each end is
one surface of that material: at least ``min_patch_fill`` (60 %) of it is the material, its depth stays within
the 1 % tolerance of the centre depth and its normals within 20 deg of the centre normal. The patch delta_e
averages over is then one surface, so silhouettes, leaf clusters, marker spheres and other structure smaller
than the window do not pass as correspondences (at 640 px the fixture's leaf proxies are such structure; at
delivery resolution a modelled leaf is many windows wide). The gate reports a material's median n . v: a material seen only at grazing angles is a
weak colour witness because Fresnel reflection makes even a semi-gloss surface view-dependent there
(setgate scores such materials as advisory).

Colour. ``delta_e`` converts both views to 8-bit gamma RGB (the fidelity_bench convention), blurs with sigma = 2,
takes the mean Lab over the ``window`` x ``window`` pixels around each end of the correspondence and scores
CIEDE2000 between the two means. With the views passed in, the blur is isolated per material (normalised
convolution, as material_de does) so an edited neighbour never bleeds across a border into the number.
Per material it reports the median and p90 and ``dlab_mean`` = mean(Lab_a - Lab_b): the additive Lab offset that
would move B's material onto A's at the matched points, which is what setgate.numeric_correction applies.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from plate_spec import project_point

from .color import ciede2000, srgb_to_lab
from .material_de import id_to_u32, legend_entries

__all__ = [
    "PacketView",
    "as_view",
    "backproject",
    "correspondences",
    "delta_e",
    "as_rgb8",
    "BG_VALUE",
    "MIN_N_DOT_V",
    "Z_TOL",
    "EXCLUDED_CLASSES",
    "SIGMA",
    "WINDOW",
    "MAX_NORMAL_DEG",
    "MIN_PATCH_FILL",
]

BG_VALUE = 1e10
MIN_N_DOT_V = 0.3
Z_TOL = 0.01
EXCLUDED_CLASSES = frozenset({"glazing", "water"})
SIGMA = 2.0
WINDOW = 5
MAX_NORMAL_DEG = 20.0
MIN_PATCH_FILL = 0.6
MASK_THRESHOLD = 0.5


# --------------------------------------------------------------------------- packet view


@dataclass
class PacketView:
    """The parts of a Tier A packet the cross-view gate needs, as arrays.

    ``z`` planar depth (H, W) in scene units; ``normals`` (H, W, 3) camera space, +Z toward the camera;
    ``ranks`` / ``coverage`` the material Cryptomatte as plate_pixels.exr.read_cryptomatte returns them;
    ``legend`` the id_legend entries; ``world_position`` the opt-in pass (H, W, 3) or None; ``editable_ids``
    the manifest's ``policy.editable_ids`` (the classes AI may regenerate per view), lower-cased.
    """

    camera: dict
    z: np.ndarray
    normals: np.ndarray
    ranks: np.ndarray
    coverage: np.ndarray
    legend: list[dict]
    world_position: np.ndarray | None = None
    bg_value: float = BG_VALUE
    packet_id: str | None = None
    editable_ids: frozenset[str] = frozenset()

    @property
    def shape(self) -> tuple[int, int]:
        return int(self.z.shape[0]), int(self.z.shape[1])

    @classmethod
    def load(cls, packet_dir: str | Path) -> PacketView:
        """Read a packet folder (manifest.json, beauty.exr, id_legend.json) into a view."""
        from plate_pixels.exr import read_cryptomatte, read_elements

        d = Path(packet_dir)
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        files = manifest["files"]
        exr = d / files["beauty_exr"]["path"]
        elements = {e["element"]: e for e in files["beauty_exr"]["elements"]}
        wanted = ["zdepth", "normals"] + (["world_position"] if "world_position" in elements else [])
        read = read_elements(exr, manifest, wanted)
        ranks, coverage = read_cryptomatte(exr, manifest, "material")
        legend_path = d / files["id_legend"]["path"] if "id_legend" in files else d / "id_legend.json"
        legend = legend_entries(json.loads(legend_path.read_text(encoding="utf-8")))
        z = np.ascontiguousarray(read["zdepth"][..., 0], dtype=np.float32)
        return cls(
            camera=manifest["camera"],
            z=z,
            normals=np.ascontiguousarray(read["normals"][..., :3], dtype=np.float32),
            ranks=ranks,
            coverage=coverage,
            legend=legend,
            world_position=read.get("world_position"),
            bg_value=float(elements["zdepth"].get("background_value", BG_VALUE)),
            packet_id=manifest.get("packet_id"),
            editable_ids=frozenset(
                str(i).lower() for i in manifest.get("policy", {}).get("editable_ids", [])
            ),
        )

    def valid(self) -> np.ndarray:
        """Pixels with a usable depth: finite, > 0 and not the renderer background."""
        return np.isfinite(self.z) & (self.z > 0) & (self.z < self.bg_value)

    def material_entries(self) -> dict[str, dict]:
        """{id: legend entry} for the material entries (entries without a kind count as materials)."""
        out: dict[str, dict] = {}
        for e in self.legend:
            if e.get("kind") in (None, "material"):
                out.setdefault(str(e["id"]).lower(), e)
        return out

    def material_share(self, m_id: str | int) -> np.ndarray:
        """Summed Cryptomatte coverage of one material id, (H, W) float in [0, 1]."""
        target = np.uint32(id_to_u32(m_id))
        return np.where(self.ranks == target, self.coverage, 0.0).sum(axis=2).astype(np.float32)

    def material_mask(self, m_id: str | int, threshold: float = MASK_THRESHOLD) -> np.ndarray:
        """Pixels where the material dominates (summed coverage >= threshold)."""
        return self.material_share(m_id) >= threshold


def as_view(packet: Any) -> PacketView:
    """A PacketView passes through; a packet folder (str/Path) is loaded."""
    if isinstance(packet, PacketView):
        return packet
    if isinstance(packet, (str, Path)):
        return PacketView.load(packet)
    raise TypeError(f"packet must be a PacketView or a packet folder path, not {type(packet).__name__}")


# --------------------------------------------------------------------------- geometry


def _camera_rt(camera: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    m = np.asarray(camera["world_from_camera"], dtype=np.float64)
    return m[:3, :3], m[:3, 3]


def _intrinsics(camera: Mapping[str, Any]) -> tuple[float, float, float, float]:
    k = camera["intrinsics_px"]
    cx, cy = k["principal_point"]
    return float(k["fx"]), float(k["fy"]), float(cx), float(cy)


def camera_point(camera: Mapping[str, Any], uv: np.ndarray, depth: np.ndarray) -> np.ndarray:
    """Pixel coordinates (N, 2) + planar depth (N,) -> points in camera space (N, 3), the inverse of
    project_point's intrinsics step (camera looks down -Z, +Y up, +X right)."""
    fx, fy, cx, cy = _intrinsics(camera)
    uv = np.asarray(uv, dtype=np.float64).reshape(-1, 2)
    d = np.asarray(depth, dtype=np.float64).reshape(-1)
    xc = (uv[:, 0] - cx) / fx * d
    yc = -(uv[:, 1] - cy) / fy * d
    return np.stack([xc, yc, -d], axis=1)


def backproject(camera: Mapping[str, Any], uv: np.ndarray, depth: np.ndarray) -> np.ndarray:
    """Render-frame pixel coordinates (N, 2) + planar depth (N,) -> world points (N, 3).

    Exact inverse of plate_spec.project_point (PACKET.md 3.3), which maps p_cam = R^T (p_world - t) with R, t from
    ``world_from_camera``; so p_world = (R^T)^-1 p_cam + t, inverted numerically rather than assumed orthonormal
    (packet matrices are rounded to 1e-6, enough to move a round trip by 1e-3 px at 9 m). ``uv`` follows the same
    convention (origin at the top-left corner, centres at +0.5).
    """
    r, t = _camera_rt(camera)
    return camera_point(camera, uv, depth) @ np.linalg.inv(r) + t


def _n_dot_v(normals: np.ndarray, p_cam: np.ndarray) -> np.ndarray:
    """normal . unit vector from the surface point to the camera, both in camera space. (N,)."""
    n = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
    n = n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)
    v = -p_cam / np.maximum(np.linalg.norm(p_cam, axis=1, keepdims=True), 1e-9)
    return np.einsum("ij,ij->i", n, v)


def _pixel_centres(h: int, w: int) -> np.ndarray:
    """(H*W, 2) pixel-centre coordinates in row-major order."""
    vs, us = np.mgrid[0:h, 0:w]
    return np.stack([us.ravel() + 0.5, vs.ravel() + 0.5], axis=1).astype(np.float64)


# --------------------------------------------------------------------------- correspondences


def _shared_materials(
    va: PacketView, vb: PacketView, exclude_classes: Iterable[str]
) -> dict[int, tuple[str, str]]:
    """{u32 id: (legend id, class)} for materials both legends list, outside the excluded classes."""
    excluded = {str(c).lower() for c in exclude_classes}
    ma, mb = va.material_entries(), vb.material_entries()
    out: dict[int, tuple[str, str]] = {}
    for mid, entry in ma.items():
        if mid not in mb:
            continue
        cls = str(entry.get("class") or mb[mid].get("class") or "other").lower()
        if cls in excluded:
            continue
        out[id_to_u32(mid)] = (mid, cls)
    return out


def _label_image(view: PacketView, ids: Iterable[int], threshold: float) -> np.ndarray:
    """(H, W) uint32: the dominant material id at each pixel when it is one of ``ids``, else 0."""
    label = np.zeros(view.shape, dtype=np.uint32)
    for u32 in ids:
        label[view.material_mask(u32, threshold)] = np.uint32(u32)
    return label


def _patch_is_smooth(
    z: np.ndarray,
    normals: np.ndarray,
    label: np.ndarray,
    u32: int,
    x: int,
    y: int,
    radius: int,
    tol: float,
    cos_max: float,
    min_fill: float,
) -> bool:
    """True when the (2r+1)^2 patch around (x, y) is one surface patch of material ``u32``: at least ``min_fill``
    of the window is that material (not a silhouette), every such pixel has depth within ``tol`` x the centre
    depth and a normal within ``cos_max`` of the centre normal (not a cluster of small structure such as leaf
    proxies or marker spheres, whose members shade differently per view)."""
    h, w = z.shape
    y0, y1 = max(y - radius, 0), min(y + radius + 1, h)
    x0, x1 = max(x - radius, 0), min(x + radius + 1, w)
    sel = label[y0:y1, x0:x1] == np.uint32(u32)
    if int(sel.sum()) < min_fill * (2 * radius + 1) ** 2:
        return False
    zc = float(z[y, x])
    vals = z[y0:y1, x0:x1][sel]
    if not np.all(np.abs(vals - zc) <= tol * zc):
        return False
    nc = normals[y, x].astype(np.float64)
    nc /= max(float(np.linalg.norm(nc)), 1e-9)
    ns = normals[y0:y1, x0:x1][sel].astype(np.float64)
    ns /= np.maximum(np.linalg.norm(ns, axis=1, keepdims=True), 1e-9)
    return bool(np.all(ns @ nc >= cos_max))


def correspondences(
    packet_a: Any,
    packet_b: Any,
    world_pos_a: np.ndarray | None = None,
    world_pos_b: np.ndarray | None = None,
    n: int = 2000,
    seed: int = 0,
    *,
    min_n_dot_v: float = MIN_N_DOT_V,
    z_tol: float = Z_TOL,
    exclude_classes: Iterable[str] = EXCLUDED_CLASSES,
    max_normal_deg: float = MAX_NORMAL_DEG,
    window: int = WINDOW,
    min_patch_fill: float = MIN_PATCH_FILL,
    threshold: float = MASK_THRESHOLD,
) -> list[dict]:
    """Sample up to ``n`` Lambertian world-point correspondences from view A into view B; see the module docstring.

    ``packet_a`` / ``packet_b``: PacketView or packet folder. ``world_pos_a`` / ``world_pos_b``: (H, W, 3) world
    positions overriding the packets' own opt-in pass (None = use the pass when the packet has one). ``window``
    is the patch delta_e will average over (at least ``min_patch_fill`` of it must be the material, with smooth
    depth and normals); ``max_normal_deg`` the allowed angle between the two ends' world normals and within a
    patch.
    Each record: {id_m, p_world, uv_a, uv_b, n_dot_v_a, n_dot_v_b, source}; ``uv_a`` is the sampled pixel's centre,
    ``uv_b`` the exact projected position in B. Same inputs and seed -> the same list.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if window < 1 or window % 2 == 0:
        raise ValueError("window must be a positive odd integer")
    radius = window // 2
    cos_max = float(np.cos(np.radians(max_normal_deg)))
    va, vb = as_view(packet_a), as_view(packet_b)
    wp_a = va.world_position if world_pos_a is None else np.asarray(world_pos_a, dtype=np.float64)
    wp_b = vb.world_position if world_pos_b is None else np.asarray(world_pos_b, dtype=np.float64)
    if wp_a is not None and wp_a.shape[:2] != va.shape:
        raise ValueError(f"world_pos_a {wp_a.shape} does not match view A {va.shape}")
    if wp_b is not None and wp_b.shape[:2] != vb.shape:
        raise ValueError(f"world_pos_b {wp_b.shape} does not match view B {vb.shape}")
    source = "world_position" if wp_a is not None else "z_backproject"

    shared = _shared_materials(va, vb, exclude_classes)
    if not shared:
        return []
    label_a = _label_image(va, shared, threshold)
    label_b = _label_image(vb, shared, threshold)
    # only materials with pixels in both views count as shared
    present = {u for u in shared if (label_a == u).any() and (label_b == u).any()}
    if not present:
        return []
    for u in list(shared):
        if u not in present:
            label_a[label_a == u] = 0

    h, w = va.shape
    uv_all = _pixel_centres(h, w)
    z_a = va.z.astype(np.float64).ravel()
    valid_a = va.valid().ravel() & (label_a.ravel() != 0)
    ndv_a = np.full(h * w, -1.0)
    if valid_a.any():
        ndv_a[valid_a] = _n_dot_v(
            va.normals.reshape(-1, 3)[valid_a], camera_point(va.camera, uv_all[valid_a], z_a[valid_a])
        )
    pool = np.flatnonzero(valid_a & (ndv_a >= min_n_dot_v))
    if pool.size == 0:
        return []
    rng = np.random.default_rng(seed)
    picks = np.sort(rng.choice(pool, size=min(n, pool.size), replace=False))

    if wp_a is not None:
        p_world = wp_a.reshape(-1, 3)[picks].astype(np.float64)
    else:
        p_world = backproject(va.camera, uv_all[picks], z_a[picks])

    hb, wb = vb.shape
    z_b = vb.z
    normals_b = vb.normals
    cam_b = vb.camera
    r_a, _ = _camera_rt(va.camera)
    r_b, _ = _camera_rt(cam_b)
    normals_a_flat = va.normals.reshape(-1, 3)
    out: list[dict] = []
    for i, idx in enumerate(picks):
        pw = p_world[i]
        if not np.all(np.isfinite(pw)):
            continue
        try:
            ub, vbv, db = project_point(cam_b, pw)
        except ValueError:  # behind camera B
            continue
        if not (0.0 <= ub < wb and 0.0 <= vbv < hb):
            continue
        xb, yb = int(ub), int(vbv)
        u32 = int(label_a.ravel()[idx])
        if int(label_b[yb, xb]) != u32:
            continue
        zb = float(z_b[yb, xb])
        if not (np.isfinite(zb) and 0 < zb < vb.bg_value) or abs(zb - db) > z_tol * db:
            continue
        if wp_b is not None and np.linalg.norm(wp_b[yb, xb] - pw) > z_tol * db:
            continue
        p_cam_b = camera_point(cam_b, np.array([[ub, vbv]]), np.array([db]))
        ndv_b = float(_n_dot_v(normals_b[yb, xb][None, :], p_cam_b)[0])
        if ndv_b < min_n_dot_v:
            continue
        n_world_a = r_a @ normals_a_flat[idx].astype(np.float64)
        n_world_b = r_b @ normals_b[yb, xb].astype(np.float64)
        denom = np.linalg.norm(n_world_a) * np.linalg.norm(n_world_b)
        if denom <= 0 or float(n_world_a @ n_world_b) / denom < cos_max:
            continue
        ya, xa = divmod(int(idx), w)
        if not (
            _patch_is_smooth(va.z, va.normals, label_a, u32, xa, ya, radius, z_tol, cos_max, min_patch_fill)
            and _patch_is_smooth(z_b, normals_b, label_b, u32, xb, yb, radius, z_tol, cos_max, min_patch_fill)
        ):
            continue
        out.append(
            {
                "id_m": shared[u32][0],
                "p_world": [float(pw[0]), float(pw[1]), float(pw[2])],
                "uv_a": [float(uv_all[idx, 0]), float(uv_all[idx, 1])],
                "uv_b": [float(ub), float(vbv)],
                "n_dot_v_a": float(ndv_a[idx]),
                "n_dot_v_b": ndv_b,
                "source": source,
            }
        )
    return out


# --------------------------------------------------------------------------- colour at correspondences


def as_rgb8(img: np.ndarray, name: str = "rgb") -> np.ndarray:
    """uint8 passes through; uint16 and float 0..1 are converted to 8-bit gamma RGB (H, W, 3)."""
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[2] < 3:
        raise ValueError(f"{name} must be an RGB array of shape (h, w, 3)")
    a = a[..., :3]
    if a.dtype == np.uint8:
        return a
    if a.dtype == np.uint16:
        return np.clip(np.rint(a.astype(np.float64) / 257.0), 0, 255).astype(np.uint8)
    if np.issubdtype(a.dtype, np.floating):
        return np.clip(np.rint(a.astype(np.float64) * 255.0), 0, 255).astype(np.uint8)
    raise ValueError(f"{name} must be uint8, uint16 or float 0..1, got {a.dtype}")


def _blur_lab(rgb8: np.ndarray, weight: np.ndarray | None, sigma: float) -> np.ndarray:
    """Lab of the sigma-blurred image; with ``weight`` (0/1 float32) the blur is normalised to those pixels."""
    f = rgb8.astype(np.float32)
    if sigma <= 0:
        return srgb_to_lab(f)
    if weight is None:
        return srgb_to_lab(cv2.GaussianBlur(f, (0, 0), sigma))
    num = cv2.GaussianBlur(f * weight[..., None], (0, 0), sigma)
    den = cv2.GaussianBlur(weight, (0, 0), sigma)
    return srgb_to_lab(num / np.maximum(den, 1e-6)[..., None])


def _window_mean(lab: np.ndarray, mask: np.ndarray | None, uv: Sequence[float], radius: int) -> np.ndarray:
    """Mean Lab over the (2r+1)^2 window around the pixel holding ``uv``, restricted to ``mask`` when it is set
    and the window has mask pixels (else the plain window mean)."""
    h, w = lab.shape[:2]
    x, y = int(uv[0]), int(uv[1])
    x0, x1 = max(x - radius, 0), min(x + radius + 1, w)
    y0, y1 = max(y - radius, 0), min(y + radius + 1, h)
    block = lab[y0:y1, x0:x1].reshape(-1, 3)
    if mask is not None:
        m = mask[y0:y1, x0:x1].ravel()
        if m.any():
            block = block[m]
    return block.mean(axis=0)


def delta_e(
    corr: Sequence[Mapping[str, Any]],
    rgb_a: np.ndarray,
    rgb_b: np.ndarray,
    window: int = WINDOW,
    *,
    sigma: float = SIGMA,
    view_a: PacketView | None = None,
    view_b: PacketView | None = None,
    threshold: float = MASK_THRESHOLD,
    return_values: bool = False,
) -> dict:
    """CIEDE2000 at each correspondence between sigma-blurred Lab means over ``window`` x ``window`` pixels.

    ``rgb_a`` / ``rgb_b``: the two views' display images (uint8, uint16 or float 0..1), same size as the packets.
    ``view_a`` / ``view_b``: when both are given the blur and the window mean are isolated to the correspondence's
    own material (no bleed across material borders); otherwise the blur is global.
    Returns {per_material: {m_id: {de00_median, de00_p90, de00_mean, n, dlab_mean}}, overall_p90, overall_median,
    n, window, sigma, isolated, source}; with ``return_values`` each material row also carries ``de00``, the
    per-correspondence values in the order the records of that material appear in ``corr``.
    """
    if window < 1 or window % 2 == 0:
        raise ValueError("window must be a positive odd integer")
    a8, b8 = as_rgb8(rgb_a, "rgb_a"), as_rgb8(rgb_b, "rgb_b")
    radius = window // 2
    isolated = view_a is not None and view_b is not None
    if isolated and (view_a.shape != a8.shape[:2] or view_b.shape != b8.shape[:2]):
        raise ValueError("views and images disagree in size")

    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for c in corr:
        by_id.setdefault(str(c["id_m"]).lower(), []).append(c)

    lab_a_global = lab_b_global = None
    if not isolated:
        lab_a_global = _blur_lab(a8, None, sigma)
        lab_b_global = _blur_lab(b8, None, sigma)

    per_material: dict[str, dict] = {}
    all_de: list[np.ndarray] = []
    sources: set[str] = set()
    for mid, recs in by_id.items():
        if isolated:
            mask_a = view_a.material_mask(mid, threshold)
            mask_b = view_b.material_mask(mid, threshold)
            lab_a = _blur_lab(a8, mask_a.astype(np.float32), sigma)
            lab_b = _blur_lab(b8, mask_b.astype(np.float32), sigma)
        else:
            mask_a = mask_b = None
            lab_a, lab_b = lab_a_global, lab_b_global
        means_a = np.stack([_window_mean(lab_a, mask_a, c["uv_a"], radius) for c in recs])
        means_b = np.stack([_window_mean(lab_b, mask_b, c["uv_b"], radius) for c in recs])
        de = ciede2000(means_a, means_b)
        dlab = (means_a - means_b).mean(axis=0)
        per_material[mid] = {
            "de00_median": float(np.median(de)),
            "de00_p90": float(np.percentile(de, 90)),
            "de00_mean": float(de.mean()),
            "n": int(de.size),
            "dlab_mean": [float(v) for v in dlab],
        }
        if return_values:
            per_material[mid]["de00"] = [float(v) for v in de]
        all_de.append(de)
        sources.update(str(c.get("source", "")) for c in recs)
    pooled = np.concatenate(all_de) if all_de else np.zeros(0)
    return {
        "per_material": per_material,
        "overall_p90": float(np.percentile(pooled, 90)) if pooled.size else None,
        "overall_median": float(np.median(pooled)) if pooled.size else None,
        "n": int(pooled.size),
        "window": int(window),
        "sigma": float(sigma),
        "isolated": bool(isolated),
        "source": sources.pop() if len(sources) == 1 else ("mixed" if sources else None),
    }
