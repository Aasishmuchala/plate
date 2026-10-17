"""Numpy synthetics for the gate tests: nothing is committed but this code (router plan T2).

    from fixtures.synthetic import make_pair, make_depth_scene

make_pair(kind, size=(256, 256), seed=0) -> (ref, test, mask, expected)
    ref, test   uint8 RGB arrays, shape (height, width, 3), display-referred sRGB
    mask        bool array (height, width): the region the kind is about (documented per kind below)
    expected    dict of ground truth the gates must recover; JSON-friendly scalars plus numpy arrays for maps
    size        (width, height), the same order as manifest camera.resolution and the plan's "640x360" notation

    identity            test == ref bit for bit. mask: central rectangle (an edit region that was not edited).
    shift3              test is ref translated by +3 px in x (test[y, x] = ref[y, x - 3], edge pixels repeated),
                        so registering test back onto ref needs dx = -3. mask: pixels valid in both images.
                        expected: shift_px (+3.0, 0.0). shift_image() makes any other shift.
    window_added        ref is the two-plane depth scene render (make_depth_scene), test paints a flat window
                        rectangle on the wall, away from every geometric edge. mask: the rectangle.
                        expected: rect (x0, y0, x1, y1), wall_id, n_invention_flags 1, scene.
                        along_edge=True repaints the box's front face instead: its outline is exactly an
                        existing geometric edge, so n_invention_flags is 0.
    material_shift_de5  three materials with legend ids; the panel material is shifted along b* by exactly
                        CIEDE2000 5.0 per pixel (before the 8-bit rounding). mask: the panel.
                        expected: ids (uint32), legend, shifted_id, de00_map (measured on the 8-bit pair),
                        target_de00 5.0. Every other material is bit-identical.
    texture_crunch      the mask region is over-sharpened and grit-noised (the "synthetic crunch" of the
                        texture gate: Laplacian variance several times the reference). Outside the mask
                        identical. expected: lapvar_ref / lapvar_test / lapvar_ratio and test_soft, the same
                        region blurred (the "soft" failure), with its own lapvar_soft.
    feather_leak        an edit composited into the mask. expected["test_clean"] is the correct composite
                        (feather inside the mask, identical outside by construction); test is the leaky one
                        (a ring of leak_px pixels outside the mask changed). mask: the hard edit mask.

make_depth_scene(size=(256, 256)) -> (z, normals_cam, ids, K, world_from_camera)
    A pinhole camera at (0, 0, 1.5) looking along world +Y, Z up, in the plate-cam-v0 convention (the camera
    looks down its local -Z, +Y up, +X right; matrix columns are the camera axes in world space). Two planes:
    the wall at planar depth 4.0 m and a box whose front face is at 2.5 m (its left side face is visible too).
    z float32 (H, W) planar depth in metres, point sampled (unfiltered, like VRayZDepth); normals_cam float32
    (H, W, 3) unit camera-space normals (+Z toward the viewer); ids uint32 Cryptomatte object ids (wall,
    box); K the 3x3 pixel intrinsics (60 degree horizontal field of view); world_from_camera the 4x4 matrix.
    The return value unpacks to exactly those five and also carries, as attributes: image (uint8 render of the
    same scene, 3x3 supersampled), edges (the analytic edge map: a pixel whose right or lower neighbour is a
    different surface; one pixel thick), labels (0 wall, 1 box front, 2 box side), wall_id / box_id
    (Cryptomatte id strings), wall_mask, box_rect, camera (a dict project_point() accepts) and size.
"""

from __future__ import annotations

import math

import numpy as np

from ._color import ciede2000, lab_to_srgb8, srgb8_to_lab

KINDS = ("identity", "shift3", "window_added", "material_shift_de5", "texture_crunch", "feather_leak")

__all__ = [
    "KINDS",
    "DepthScene",
    "ciede2000",
    "laplacian_variance",
    "make_depth_scene",
    "make_pair",
    "shift_image",
]

HFOV_DEG = 60.0
CAMERA_POSITION = (0.0, 0.0, 1.5)
WALL_DEPTH_M = 4.0
# world-space box: x, z extents of the front face, depth range of the left side face (y), Z up
BOX = {"x0": 0.20, "x1": 1.10, "z0": 1.05, "z1": 2.05, "y_front": 2.5, "y_back": 3.2}
WALL_NAME = "ARCH_wall_N"
BOX_NAME = "ARCH_panel_box"
SHIFT_PX = 3
MIN_SIDE = 48


# --------------------------------------------------------------------------- shared helpers


def _check_size(size) -> tuple[int, int]:
    w, h = (int(size[0]), int(size[1]))
    if min(w, h) < MIN_SIDE:
        raise ValueError(f"size must be at least {MIN_SIDE} px on each side, got {size}")
    return w, h


def _bilinear(g: np.ndarray, h: int, w: int) -> np.ndarray:
    gh, gw = g.shape
    y = np.linspace(0.0, gh - 1.000001, h)
    x = np.linspace(0.0, gw - 1.000001, w)
    y0, x0 = y.astype(int), x.astype(int)
    fy, fx = (y - y0)[:, None], (x - x0)[None, :]
    a, b = g[y0][:, x0], g[y0][:, x0 + 1]
    c, d = g[y0 + 1][:, x0], g[y0 + 1][:, x0 + 1]
    return a * (1 - fx) * (1 - fy) + b * fx * (1 - fy) + c * (1 - fx) * fy + d * fx * fy


def _vnoise(h: int, w: int, rng: np.random.Generator, cell: int) -> np.ndarray:
    """Value noise in [-0.5, 0.5]: a random lattice with `cell` px spacing, bilinearly upsampled."""
    return _bilinear(rng.random((h // cell + 2, w // cell + 2)), h, w) - 0.5


def _to_u8(x: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(x * 255.0), 0, 255).astype(np.uint8)


def _plate(w: int, h: int, rng: np.random.Generator, n_rects: int = 9) -> np.ndarray:
    """A textured, edge-rich RGB plate: sky-to-ground gradient, 3 octaves of value noise and flat rectangles."""
    yy = np.linspace(0.0, 1.0, h)[:, None, None]
    top, bot = np.array([0.58, 0.64, 0.74]), np.array([0.36, 0.34, 0.31])
    img = np.broadcast_to(top * (1 - yy) + bot * yy, (h, w, 3)).copy()
    tex = _vnoise(h, w, rng, 32) * 0.05 + _vnoise(h, w, rng, 9) * 0.04 + _vnoise(h, w, rng, 3) * 0.025
    img += tex[..., None] * np.array([1.0, 0.95, 0.9])
    for _ in range(n_rects):
        rw = int(rng.integers(max(w // 8, 6), max(w // 3, 8)))
        rh = int(rng.integers(max(h // 8, 6), max(h // 3, 8)))
        x0 = int(rng.integers(0, w - rw))
        y0 = int(rng.integers(0, h - rh))
        colour = rng.uniform(0.15, 0.9, 3)
        img[y0 : y0 + rh, x0 : x0 + rw] = colour + _vnoise(rh, rw, rng, 5)[..., None] * 0.06
    return _to_u8(img)


def _rect_mask(w: int, h: int, fx0: float, fy0: float, fx1: float, fy1: float) -> np.ndarray:
    m = np.zeros((h, w), dtype=bool)
    m[int(fy0 * h) : int(fy1 * h), int(fx0 * w) : int(fx1 * w)] = True
    return m


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1), x1 and y1 exclusive."""
    ys, xs = np.nonzero(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _erode(mask: np.ndarray, n: int) -> np.ndarray:
    out = mask.copy()
    for _ in range(n):
        p = np.pad(out, 1, constant_values=False)
        out = p[1:-1, 1:-1] & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    return out


def _box_blur(img: np.ndarray, k: int) -> np.ndarray:
    """Separable k x k mean filter on a float (H, W, C) array, edge pixels repeated."""
    r = k // 2
    out = np.asarray(img, dtype=np.float64)
    for axis in (0, 1):
        pad = [(0, 0)] * out.ndim
        pad[axis] = (r + 1, r)
        c = np.cumsum(np.pad(out, pad, mode="edge"), axis=axis)
        n = out.shape[axis]
        hi = np.take(c, np.arange(k, k + n), axis=axis)
        lo = np.take(c, np.arange(0, n), axis=axis)
        out = (hi - lo) / k
    return out


def _luma(img: np.ndarray) -> np.ndarray:
    return img[..., 0] * 0.2126 + img[..., 1] * 0.7152 + img[..., 2] * 0.0722


def laplacian_variance(img: np.ndarray, mask: np.ndarray | None = None) -> float:
    """Variance of the 4-neighbour Laplacian of the 8-bit luma (the texture gate's lapvar, at native size).
    With a mask only interior pixels whose whole stencil is inside the mask count (mask eroded by one)."""
    y = _luma(np.asarray(img, dtype=np.float64))
    lap = 4 * y[1:-1, 1:-1] - y[:-2, 1:-1] - y[2:, 1:-1] - y[1:-1, :-2] - y[1:-1, 2:]
    if mask is None:
        return float(lap.var())
    sel = _erode(mask, 1)[1:-1, 1:-1]
    return float(lap[sel].var())


def shift_image(img: np.ndarray, dx: int, dy: int = 0) -> np.ndarray:
    """Translate by whole pixels: out[y, x] = img[y - dy, x - dx], edge pixels repeated (exact ground truth)."""
    h, w = img.shape[:2]
    ys = np.clip(np.arange(h) - int(dy), 0, h - 1)
    xs = np.clip(np.arange(w) - int(dx), 0, w - 1)
    return img[ys][:, xs]


def _cid(name: str) -> str:
    import plate_spec

    return plate_spec.cryptomatte_id(name, "object")


def _cid_u32(name: str) -> int:
    return int(_cid(name).split(":")[1], 16)


# --------------------------------------------------------------------------- the depth scene


class DepthScene(tuple):
    """Unpacks to (z, normals_cam, ids, K, world_from_camera); extras are attributes (module docstring)."""

    def __new__(cls, z, normals_cam, ids, K, world_from_camera, **extra):  # noqa: N803
        self = super().__new__(cls, (z, normals_cam, ids, K, world_from_camera))
        self.__dict__.update(extra)
        return self

    z = property(lambda self: self[0])
    normals_cam = property(lambda self: self[1])
    ids = property(lambda self: self[2])
    K = property(lambda self: self[3])
    world_from_camera = property(lambda self: self[4])


def _intrinsics(w: int, h: int) -> tuple[float, float, float]:
    fx = (w / 2.0) / math.tan(math.radians(HFOV_DEG) / 2.0)
    return fx, w / 2.0, h / 2.0


def _cast(u: np.ndarray, v: np.ndarray, fx: float, cx: float, cy: float):
    """Cast rays through render-frame pixel coordinates (u, v). Returns (planar depth, surface label)."""
    cam_z = CAMERA_POSITION[2]
    xn = (u - cx) / fx  # camera +x is world +x, camera +y is world +z, the view axis is world +y
    zn = -(v - cy) / fx
    b = BOX
    t = np.full(u.shape, WALL_DEPTH_M)
    lab = np.zeros(u.shape, dtype=np.uint8)
    xf, zf = b["y_front"] * xn, b["y_front"] * zn + cam_z
    front = (xf >= b["x0"]) & (xf <= b["x1"]) & (zf >= b["z0"]) & (zf <= b["z1"])
    t, lab = np.where(front, b["y_front"], t), np.where(front, 1, lab).astype(np.uint8)
    with np.errstate(divide="ignore", invalid="ignore"):
        ts = np.where(xn > 0, b["x0"] / np.where(xn > 0, xn, 1.0), 1e9)
    zs = ts * zn + cam_z
    side = (ts >= b["y_front"]) & (ts <= b["y_back"]) & (zs >= b["z0"]) & (zs <= b["z1"]) & (ts < t)
    t, lab = np.where(side, ts, t), np.where(side, 2, lab).astype(np.uint8)
    return t, lab


_ALBEDO = np.array([[0.74, 0.72, 0.66], [0.30, 0.42, 0.58], [0.30, 0.42, 0.58]])  # wall, box front, box side
_LIGHT = np.array([0.3, -0.6, 0.5]) / np.linalg.norm([0.3, -0.6, 0.5])
_NORMAL_WORLD = np.array([[0.0, -1.0, 0.0], [0.0, -1.0, 0.0], [-1.0, 0.0, 0.0]])


def _render_image(w: int, h: int, front_albedo=None, ss: int = 3) -> np.ndarray:
    """Lambert render of the scene, ss x ss supersampled in linear light, encoded with a 2.2 gamma."""
    fx, cx, cy = _intrinsics(w, h)
    albedo = _ALBEDO.copy()
    if front_albedo is not None:
        albedo[1] = front_albedo
    acc = np.zeros((h, w, 3))
    offs = (np.arange(ss) + 0.5) / ss
    uu0, vv0 = np.meshgrid(np.arange(w, dtype=np.float64), np.arange(h, dtype=np.float64))
    for oy in offs:
        for ox in offs:
            u, v = uu0 + ox, vv0 + oy
            t, lab = _cast(u, v, fx, cx, cy)
            px = t * (u - cx) / fx  # world x
            pz = t * (-(v - cy) / fx) + CAMERA_POSITION[2]  # world z
            shade = 0.35 + 0.65 * np.clip(_NORMAL_WORLD[lab] @ _LIGHT, 0.0, 1.0)
            tex = 1.0 + 0.04 * np.sin(2.1 * px + 1.3 * pz) * np.sin(1.7 * pz - 0.9 * px)
            acc += albedo[lab] * (shade * tex)[..., None]
    return _to_u8((acc / (ss * ss)) ** (1.0 / 2.2))


def make_depth_scene(size=(256, 256)) -> DepthScene:
    w, h = _check_size(size)
    fx, cx, cy = _intrinsics(w, h)
    u, v = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    t, lab = _cast(u, v, fx, cx, cy)
    z = t.astype(np.float32)
    # camera axes in world space are the matrix columns; camera-space normal = R^T n_world
    r_wc = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
    n_cam = np.array([r_wc.T @ n for n in _NORMAL_WORLD]).astype(np.float32)
    normals = n_cam[lab]
    wall_id, box_id = _cid_u32(WALL_NAME), _cid_u32(BOX_NAME)
    ids = np.where(lab == 0, np.uint32(wall_id), np.uint32(box_id)).astype(np.uint32)
    k = np.array([[fx, 0.0, cx], [0.0, fx, cy], [0.0, 0.0, 1.0]])
    wfc = np.eye(4)
    wfc[:3, :3] = r_wc
    wfc[:3, 3] = CAMERA_POSITION
    edges = np.zeros((h, w), dtype=bool)
    edges[:, :-1] |= lab[:, :-1] != lab[:, 1:]
    edges[:-1, :] |= lab[:-1, :] != lab[1:, :]
    camera = {
        "world_from_camera": [[float(x) for x in row] for row in wfc],
        "intrinsics_px": {"fx": fx, "fy": fx, "principal_point": [cx, cy], "K": k.tolist()},
        "resolution": {"width": w, "height": h, "pixel_aspect": 1.0},
    }
    return DepthScene(
        z,
        normals,
        ids,
        k,
        wfc,
        image=_render_image(w, h),
        edges=edges,
        labels=lab,
        wall_id=_cid(WALL_NAME),
        box_id=_cid(BOX_NAME),
        wall_mask=lab == 0,
        box_rect=_bbox(lab != 0),
        camera=camera,
        size=(w, h),
    )


# --------------------------------------------------------------------------- the pair kinds


def _identity(w: int, h: int, rng: np.random.Generator):
    ref = _plate(w, h, rng)
    mask = _rect_mask(w, h, 0.25, 0.25, 0.75, 0.75)
    expected = {
        "kind": "identity",
        "shift_px": (0.0, 0.0),
        "max_abs_diff": 0,
        "de00_max": 0.0,
        "bbox": _bbox(mask),
    }
    return ref, ref.copy(), mask, expected


def _shift3(w: int, h: int, rng: np.random.Generator):
    ref = _plate(w, h, rng)
    test = shift_image(ref, SHIFT_PX, 0)
    mask = np.ones((h, w), dtype=bool)
    mask[:, :SHIFT_PX] = False
    expected = {
        "kind": "shift3",
        "shift_px": (float(SHIFT_PX), 0.0),
        "registration_dx": -float(SHIFT_PX),
        "registration_dy": 0.0,
    }
    return ref, test, mask, expected


def _window_added(w: int, h: int, along_edge: bool = False):
    scene = make_depth_scene((w, h))
    ref = scene.image
    expected = {"kind": "window_added", "scene": scene, "wall_id": scene.wall_id, "box_id": scene.box_id}
    if along_edge:
        test = _render_image(w, h, front_albedo=np.array([0.62, 0.20, 0.16]))
        mask = scene.labels == 1
        expected.update(rect=_bbox(mask), along_edge=True, n_invention_flags=0)
        return ref, test, mask, expected
    x0, y0, x1, y1 = int(0.08 * w), int(0.10 * h), int(0.32 * w), int(0.38 * h)
    mask = np.zeros((h, w), dtype=bool)
    mask[y0:y1, x0:x1] = True
    pad = np.zeros_like(mask)
    pad[max(y0 - 3, 0) : y1 + 3, max(x0 - 3, 0) : x1 + 3] = True
    if not scene.wall_mask[pad].all() or scene.edges[pad].any():
        raise ValueError(
            f"size {(w, h)} puts the window on a geometric edge; use a less extreme aspect ratio"
        )
    test = ref.copy()
    test[mask] = (165, 198, 226)
    expected.update(rect=(x0, y0, x1, y1), along_edge=False, n_invention_flags=1)
    return ref, test, mask, expected


def _material_shift_de5(w: int, h: int, rng: np.random.Generator):
    names = ("ARCH_wall_N", "HARD_floor", "ARCH_panel_7016")
    base = np.array([[199, 189, 168], [121, 112, 100], [94, 114, 132]], dtype=np.float64)
    ids = np.zeros((h, w), dtype=np.uint32)
    idx = np.zeros((h, w), dtype=np.intp)
    idx[int(0.58 * h) :, :] = 1
    panel = _rect_mask(w, h, 0.52, 0.12, 0.86, 0.50)
    idx[panel] = 2
    for i, n in enumerate(names):
        ids[idx == i] = _cid_u32(n)
    tex = (_vnoise(h, w, rng, 6) * 6.0 + _vnoise(h, w, rng, 2) * 3.0)[..., None]
    ref = np.clip(np.rint(base[idx] + tex), 0, 255).astype(np.uint8)

    lab0 = srgb8_to_lab(ref[panel])
    direction = np.array([0.0, 0.0, 1.0])  # +b*: a warm shift
    lo, hi = np.zeros(len(lab0)), np.full(len(lab0), 60.0)
    for _ in range(48):
        mid = (lo + hi) / 2.0
        below = ciede2000(lab0, lab0 + mid[:, None] * direction) < 5.0
        lo, hi = np.where(below, mid, lo), np.where(below, hi, mid)
    test = ref.copy()
    test[panel] = lab_to_srgb8(lab0 + ((lo + hi) / 2.0)[:, None] * direction)
    de = ciede2000(srgb8_to_lab(ref), srgb8_to_lab(test)).astype(np.float32)
    expected = {
        "kind": "material_shift_de5",
        "ids": ids,
        "legend": {n: _cid(n) for n in names},
        "shifted": names[2],
        "shifted_id": _cid(names[2]),
        "target_de00": 5.0,
        "de00_map": de,
        "de00_p90_shifted": float(np.percentile(de[panel], 90)),
        "de00_max_unshifted": float(de[~panel].max()),
    }
    return ref, test, panel, expected


def _texture_crunch(w: int, h: int, rng: np.random.Generator):
    ref = _plate(w, h, rng)
    mask = _rect_mask(w, h, 0.20, 0.40, 0.75, 0.90)
    f = ref.astype(np.float64)
    f += rng.normal(0.0, 3.0, f.shape) * mask[..., None]  # a gently grained "paving" the AI will rewrite
    ref = np.clip(np.rint(f), 0, 255).astype(np.uint8)
    f = ref.astype(np.float64)
    sharp = f + 2.5 * (f - _box_blur(f, 3)) + rng.normal(0.0, 9.0, f.shape)
    test = np.where(mask[..., None], np.clip(np.rint(sharp), 0, 255), f).astype(np.uint8)
    soft = _box_blur(_box_blur(f, 5), 5)
    test_soft = np.where(mask[..., None], np.clip(np.rint(soft), 0, 255), f).astype(np.uint8)
    lv_ref, lv_test = laplacian_variance(ref, mask), laplacian_variance(test, mask)
    lv_soft = laplacian_variance(test_soft, mask)
    expected = {
        "kind": "texture_crunch",
        "lapvar_ref": lv_ref,
        "lapvar_test": lv_test,
        "lapvar_ratio": lv_test / lv_ref,
        "test_soft": test_soft,
        "lapvar_soft": lv_soft,
        "lapvar_soft_ratio": lv_soft / lv_ref,
    }
    return ref, test, mask, expected


def _feather_leak(w: int, h: int, rng: np.random.Generator, leak_px: int = 1, feather: int = 6):
    ref = _plate(w, h, rng)
    x0, y0, x1, y1 = int(0.30 * w), int(0.30 * h), int(0.70 * w), int(0.70 * h)
    mask = np.zeros((h, w), dtype=bool)
    mask[y0:y1, x0:x1] = True
    xs, ys = np.arange(w)[None, :], np.arange(h)[:, None]
    dx = np.maximum(np.maximum(x0 - xs, xs - (x1 - 1)), 0)
    dy = np.maximum(np.maximum(y0 - ys, ys - (y1 - 1)), 0)
    d_out = np.maximum(dx, dy)  # Chebyshev distance to the mask (0 inside)
    d_in = np.minimum(np.minimum(xs - x0 + 1, x1 - xs), np.minimum(ys - y0 + 1, y1 - ys))
    edit = np.clip(ref.astype(np.int16) + np.array([60, -10, -50]), 0, 255).astype(np.float64)
    f = ref.astype(np.float64)

    def blend(alpha):
        return np.clip(np.rint(f * (1 - alpha[..., None]) + edit * alpha[..., None]), 0, 255).astype(np.uint8)

    a_clean = np.where(mask, np.clip((d_in - 1.0) / float(feather), 0.0, 1.0), 0.0)
    ring = (d_out >= 1) & (d_out <= leak_px)
    a_leak = np.where(mask, 1.0, 0.0)
    a_leak = np.where(ring, 0.5 * (1.0 - (d_out - 1.0) / float(leak_px)), a_leak)
    test = blend(a_leak)
    clean = blend(a_clean)
    expected = {
        "kind": "feather_leak",
        "leak_px": leak_px,
        "feather_px": feather,
        "ring_mask": ring,
        "outside_changed_px": int((np.any(test != ref, axis=-1) & ~mask).sum()),
        "outside_identical": False,
        "test_clean": clean,
        "clean_outside_identical": bool(np.array_equal(clean[~mask], ref[~mask])),
    }
    return ref, test, mask, expected


def make_pair(kind: str, size=(256, 256), seed: int = 0, *, along_edge: bool = False):
    """(ref, test, mask, expected) for one of KINDS; see the module docstring.
    Deterministic in (kind, size, seed). `along_edge` only applies to window_added."""
    w, h = _check_size(size)
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    if along_edge and kind != "window_added":
        raise ValueError("along_edge only applies to the window_added kind")
    rng = np.random.default_rng(seed)
    if kind == "identity":
        return _identity(w, h, rng)
    if kind == "shift3":
        return _shift3(w, h, rng)
    if kind == "window_added":
        return _window_added(w, h, along_edge)
    if kind == "material_shift_de5":
        return _material_shift_de5(w, h, rng)
    if kind == "texture_crunch":
        return _texture_crunch(w, h, rng)
    return _feather_leak(w, h, rng)
