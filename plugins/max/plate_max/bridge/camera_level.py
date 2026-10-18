"""Level-and-crop (decision 58): a two-point-perspective V-Ray camera sent as a level pinhole.

V-Ray's vertical tilt correction (auto_vertical_tilt, or a vertical_tilt value: 2-point perspective)
keeps the image plane vertical while the camera is pitched, and a vertical lens shift slides the image
across the film. Neither is a v0.1 pinhole (spec/PACKET.md 3.3), but both are a crop of one: the level
camera, rendered taller with its principal point at the centre. The level camera keeps the position,
the yaw and the roll and zeroes the pitch (a shifted camera keeps its whole pose: its image plane is
already square to its axis, only the principal point moved). The original frame is the crop of the
level render whose principal point sits dy_px below the crop's centre:

  auto or manual vertical tilt   dy_px = fy * tan(pitch)      pitch > 0 looks up
  manual vertical shift s        dy_px = s * height           V-Ray's unit for s is UNVERIFIED: read
                                                              as a fraction of the frame height (open
                                                              question 14, a box measurement)

The render is width x (height + 2 * ceil(|dy_px|)) and the packet's safe_frame is {x 0, y ceil(|dy_px|)
- dy_px rounded to a whole pixel, width, height}: a positive dy (looking up, or the view raised by a
shift) puts the frame at the top of the level render. The sign is settled by tests/test_camera_level.py
(the identity vector), not by reading V-Ray. Rounding y costs at most 0.5 px (LevelPlan.residual_px).
The width and the film gate width do not change, so fx does not either: the film gate height grows with
the frame (film_height_scale = render_height / height). Every gate then runs on a v0.1 packet.

plan() is pure geometry on a digest.camera_digest(). levelled() is the scene side: a temporary copy of
the camera (rt.copy, named __PLATE_level_<camera>), posed level with its tilt and vertical shift off,
checked through its own digest, rendered, and deleted afterwards, so the artist's camera is never
touched and a crash mid-render leaves an extra camera, not a changed one. Every MAXScript call here is
TODO-verify (plugins/max/ONBOX.md step 4); a manual vertical_tilt value is read as the full correction
of the camera's pitch (what Guess vertical tilt sets), also unverified.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .. import digest
from ..rt import runtime
from .scene import at_time, get_camera, has_prop, rt_attr, set_prop

LEVEL_PREFIX = "__PLATE_level_"
TILT = "tilt"
SHIFT = "shift"
MAX_PITCH_DEG = 45.0  # past this a two-point frame is mostly stretch, and the render over twice the size
ROLL_TOLERANCE_PX = 0.25  # how far keeping the roll in a dy-only crop may move the image, at most
POSE_TOLERANCE = 1e-6  # the copy's pose against the plan: rotation entries; positions scale with |t|
# V-Ray's unit for vertical_shift: a fraction of the frame height until measured (TODO-verify, open
# question 14; ONBOX.md step 4 measures where the principal point moves for a shift of 0.1).
SHIFT_UNIT = "fraction of the frame height"
TARGETED = ("targeted",)  # TODO-verify: off makes the copy a free camera aimed by its own transform
TILT_V_NAMES = tuple(name for name, label in digest.TILT_NAMES if label == "vertical")


class LevelError(RuntimeError):
    """The level camera could not be made or did not take; the message says why."""


@dataclass(frozen=True)
class LevelPlan:
    """How a two-point camera is rendered as a level pinhole: the level pose, the taller render and
    where the original frame sits in it. mode is TILT or SHIFT; source the property that made it one
    (auto_vertical_tilt, vertical_tilt or vertical_shift)."""

    camera: str
    mode: str
    source: str
    pitch_deg: float
    shift: float
    tilt_value: float
    fx: float
    fy: float
    dy_px: float
    pad_px: int
    width: int
    height: int
    render_height: int
    safe_y: int
    residual_px: float
    world_from_camera: list[list[float]]

    @property
    def film_height_scale(self) -> float:
        """The film gate height of the level render over the original's (the width stays)."""
        return self.render_height / self.height

    @property
    def safe_frame(self) -> dict[str, int]:
        """The original frame inside the level render (render pixels, origin top-left)."""
        return {"x": 0, "y": self.safe_y, "width": self.width, "height": self.height}

    @property
    def max_rows(self) -> list[tuple[float, float, float]]:
        """The level pose as Max Matrix3 rows (local X, Y, Z axes in world, then the position)."""
        m = self.world_from_camera
        return [(float(m[0][j]), float(m[1][j]), float(m[2][j])) for j in range(4)]

    @property
    def note(self) -> str:
        """The manifest's overscan.note: where the packet came from (decision 58)."""
        if self.mode == SHIFT:
            head = (
                f"level-and-crop from a vertically shifted camera: shift {self.shift:g} (read as a "
                f"{SHIFT_UNIT}; V-Ray's unit is unverified, open question 14), dy {self.dy_px:.2f} px"
            )
        else:
            head = f"level-and-crop from a tilt-corrected camera: pitch {self.pitch_deg:.3f} deg, dy {self.dy_px:.2f} px"
            if self.source == "vertical_tilt":
                head += f" (vertical_tilt {self.tilt_value:g} read as the full correction of the pitch, unverified)"
        return f"{head}; rendered level at {self.width}x{self.render_height}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera": self.camera,
            "mode": self.mode,
            "source": self.source,
            "pitch_deg": self.pitch_deg,
            "shift": self.shift,
            "dy_px": self.dy_px,
            "pad_px": self.pad_px,
            "width": self.width,
            "height": self.height,
            "render_height": self.render_height,
            "safe_frame": self.safe_frame,
            "residual_px": self.residual_px,
            "film_height_scale": self.film_height_scale,
            "world_from_camera": [list(row) for row in self.world_from_camera],
            "note": self.note,
        }


# --------------------------------------------------------------------------- small vector maths


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _unit(v: tuple[float, float, float]) -> tuple[float, float, float] | None:
    n = math.sqrt(sum(c * c for c in v))
    return None if n < 1e-12 else (v[0] / n, v[1] / n, v[2] / n)


def _dot(a: Any, b: Any) -> float:
    return sum(float(x) * float(y) for x, y in zip(a, b, strict=True))


def _affine(rows: Any) -> list[list[float]]:
    """Max Matrix3 rows (4 of 3) as a 4x4 row-vector affine matrix: [x y z 1] . M."""
    return [[float(v) for v in rows[i]] + [0.0] for i in range(3)] + [[float(v) for v in rows[3]] + [1.0]]


def _mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _inverse(m: list[list[float]]) -> list[list[float]]:
    """The inverse of a row-vector affine 4x4 (any invertible 3x3 part: a node may carry scale)."""
    a = [row[:3] for row in m[:3]]
    det = (
        a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
        - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
        + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
    )
    if abs(det) < 1e-15:
        raise LevelError("the camera's transform is singular")
    inv = [[0.0] * 3 for _ in range(3)]
    for i in range(3):  # the adjugate over the determinant, by cyclic cofactors
        for j in range(3):
            j1, j2, i1, i2 = (j + 1) % 3, (j + 2) % 3, (i + 1) % 3, (i + 2) % 3
            inv[i][j] = (a[j1][i1] * a[j2][i2] - a[j1][i2] * a[j2][i1]) / det
    t = m[3][:3]
    it = [-sum(t[k] * inv[k][j] for k in range(3)) for j in range(3)]
    return [inv[0] + [0.0], inv[1] + [0.0], inv[2] + [0.0], it + [1.0]]


def _rows_of(matrix: Any) -> list[tuple[float, float, float]]:
    """A Matrix3's row1..row4 (Point3-like, or 3-sequences) as tuples."""
    out = []
    for i in (1, 2, 3, 4):
        p = getattr(matrix, f"row{i}")
        try:
            out.append((float(p.x), float(p.y), float(p.z)))
        except AttributeError:
            a = tuple(p)
            out.append((float(a[0]), float(a[1]), float(a[2])))
    return out


# --------------------------------------------------------------------------- the plan (pure)


def _level_pose(m: Any) -> tuple[list[list[float]], float, float] | None:
    """(level world_from_camera, pitch in degrees, drift) for a plate-cam-v0 world_from_camera, or
    None looking straight up or down. The level camera keeps the position, the heading (yaw) and the
    roll about the view axis; drift is how far keeping the roll in a dy-only crop moves the aim point,
    as a fraction of the focal length in pixels."""
    right = (float(m[0][0]), float(m[1][0]), float(m[2][0]))
    back = (float(m[0][2]), float(m[1][2]), float(m[2][2]))
    pos = (float(m[0][3]), float(m[1][3]), float(m[2][3]))
    fwd = (-back[0], -back[1], -back[2])
    pitch = math.asin(max(-1.0, min(1.0, fwd[2])))
    heading = _unit((fwd[0], fwd[1], 0.0))
    if heading is None:
        return None
    r0 = (heading[1], -heading[0], 0.0)  # forward x world up: the unrolled right, the same level or pitched
    u0 = (  # the unrolled up of the pitched camera: r0 x forward
        r0[1] * fwd[2] - r0[2] * fwd[1],
        r0[2] * fwd[0] - r0[0] * fwd[2],
        r0[0] * fwd[1] - r0[1] * fwd[0],
    )
    roll = math.atan2(_dot(right, u0), _dot(right, r0))
    c, s = math.cos(roll), math.sin(roll)
    r1 = (c * r0[0], c * r0[1], s)  # level: the unrolled up is world +Z
    u1 = (-s * r0[0], -s * r0[1], c)
    b1 = (-heading[0], -heading[1], 0.0)
    level = [[r1[i], u1[i], b1[i], pos[i]] for i in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
    # a rolled camera's aim point leaves the vertical: |sin(roll)| tan(pitch) across, (1 - cos) tan down
    drift = abs(math.tan(pitch)) * max(abs(s), 1.0 - c)
    return level, math.degrees(pitch), drift


def assess(cam: dict[str, Any], width: int, height: int) -> tuple[LevelPlan | None, str]:
    """(plan, "") for a camera level-and-crop can send at width x height (the scene's render size);
    (None, "") for a plain pinhole; (None, why) for a camera that stays refused (distortion, a
    horizontal shift or tilt, tilt and shift together, no pose or lens, too steep, rolled)."""
    tilt = cam.get("tilt") or {}
    shift = cam.get("lens_shift") or {}
    distortion = cam.get("distortion") or {}
    v_tilt, h_tilt, auto = _num(tilt.get("vertical")), _num(tilt.get("horizontal")), bool(tilt.get("auto"))
    v_shift, h_shift = _num(shift.get("vertical")), _num(shift.get("horizontal"))
    tilted, shifted = auto or v_tilt != 0.0, v_shift != 0.0
    distorted = bool(distortion.get("amount")) or bool(distortion.get("type"))
    if distorted:
        return None, "lens distortion stays refused: level-and-crop needs a pinhole"
    if h_shift or h_tilt:
        return None, "a horizontal shift or tilt is not levelled (vertical two-point cameras only)"
    if not tilted and not shifted:
        return None, ""
    if tilted and shifted:
        return None, "vertical tilt and vertical shift together are not levelled (unmeasured on V-Ray)"
    m = cam.get("world_from_camera")
    if m is None:
        return None, "the camera pose is unreadable"
    problem = digest.rotation_error(m)
    if problem:
        return None, f"the camera node is not a rotation ({problem})"
    width, height = int(width), int(height)
    focal, film = _num(cam.get("focal_mm")), _num(cam.get("film_gate_mm"))
    zoom = _num(cam.get("zoom_factor") or 1.0)
    if width < 1 or height < 1 or min(focal, film, zoom) <= 0:
        return None, "the focal length, film gate or render size is unknown"
    k = digest.intrinsics(focal, film, zoom, width, height, _num(cam.get("pixel_aspect") or 1.0))
    fx, fy = float(k["intrinsics_px"]["fx"]), float(k["intrinsics_px"]["fy"])
    name = str(cam.get("name"))

    if tilted:
        posed = _level_pose(m)
        if posed is None:
            return None, "the camera looks straight up or down"
        level, pitch_deg, drift = posed
        if abs(pitch_deg) > MAX_PITCH_DEG:
            return None, f"pitch {pitch_deg:.1f} deg is past the {MAX_PITCH_DEG:g} deg level-and-crop limit"
        if max(fx, fy) * drift > ROLL_TOLERANCE_PX:
            return None, "the camera is rolled: level-and-crop moves the frame vertically only"
        dy = fy * math.tan(math.radians(pitch_deg))
        mode, source = TILT, ("auto_vertical_tilt" if auto else "vertical_tilt")
    else:
        level = [[float(v) for v in row] for row in m[:3]] + [[0.0, 0.0, 0.0, 1.0]]
        fwd_z = -float(m[2][2])
        pitch_deg = math.degrees(math.asin(max(-1.0, min(1.0, fwd_z))))
        dy = v_shift * height
        mode, source = SHIFT, "vertical_shift"

    pad = int(math.ceil(abs(dy)))
    exact = pad - dy
    safe_y = max(0, min(2 * pad, int(math.floor(exact + 0.5))))
    return (
        LevelPlan(
            camera=name,
            mode=mode,
            source=source,
            pitch_deg=pitch_deg,
            shift=v_shift,
            tilt_value=v_tilt,
            fx=fx,
            fy=fy,
            dy_px=dy,
            pad_px=pad,
            width=width,
            height=height,
            render_height=height + 2 * pad,
            safe_y=safe_y,
            residual_px=safe_y - exact,
            world_from_camera=level,
        ),
        "",
    )


def plan(cam_digest: dict[str, Any], width: int, height: int) -> LevelPlan | None:
    """The level-and-crop plan of a camera_digest() at width x height, None when the camera is a plain
    pinhole or one that stays refused (assess() says why)."""
    return assess(cam_digest, width, height)[0]


def level_camera(level: LevelPlan) -> dict[str, Any]:
    """The level render as plate_spec.project_point reads a camera: the level pose, the principal point
    at the centre of the width x render_height frame."""
    cx, cy = level.width / 2, level.render_height / 2
    return {
        "world_from_camera": [list(row) for row in level.world_from_camera],
        "intrinsics_px": {"fx": level.fx, "fy": level.fy, "principal_point": [cx, cy]},
    }


def original_camera(level: LevelPlan) -> dict[str, Any]:
    """The two-point camera itself as a pinhole of its own width x height frame: the level pose with
    the principal point dy_px below the centre (what the box check reprojects against)."""
    cx, cy = level.width / 2, level.height / 2 + level.dy_px
    return {
        "world_from_camera": [list(row) for row in level.world_from_camera],
        "intrinsics_px": {"fx": level.fx, "fy": level.fy, "principal_point": [cx, cy]},
    }


# --------------------------------------------------------------------------- the scene side (pymxs)


def _delete(node: Any) -> bool:
    remover = rt_attr("delete")
    if node is None or not callable(remover):
        return False
    try:
        remover(node)
        return True
    except Exception:
        return False


def _matrix3(rows: list[tuple[float, float, float]]) -> Any:
    rt = runtime()
    point = rt_attr("Point3")
    pts = [point(*r) if callable(point) else tuple(r) for r in rows]
    return rt.matrix3(*pts)


def _set_all(node: Any, names: tuple[str, ...], value: Any) -> list[str]:
    """Set every candidate the node has (digest reads every shift candidate, so one left over would
    still read as shifted). Returns the names set."""
    done = []
    for name in names:
        if has_prop(node, name) and set_prop(node, (name,), value) is not None:
            done.append(name)
    return done


def _lens_left(cam: dict[str, Any]) -> list[str]:
    tilt = cam.get("tilt") or {}
    shift = cam.get("lens_shift") or {}
    left = []
    if tilt.get("vertical") or tilt.get("horizontal") or tilt.get("auto"):
        left.append("tilt")
    if shift.get("vertical") or shift.get("horizontal"):
        left.append("lens shift")
    distortion = cam.get("distortion") or {}
    if distortion.get("amount") or distortion.get("type"):
        left.append("distortion")
    return left


def check_level(name: str, level: LevelPlan, frame: Any = None) -> str:
    """Nothing ("") when the node name reads, at frame, as the plan's level pinhole (pose and lens); else why."""
    cam = digest.camera_digest(name, frame=frame)
    left = _lens_left(cam)
    if left:
        return f"the level camera still reads {', '.join(left)}: its V-Ray properties could not be turned off"
    m = cam.get("world_from_camera")
    if m is None:
        return "the level camera's pose is unreadable"
    want = level.world_from_camera
    rot = max(abs(float(m[i][j]) - want[i][j]) for i in range(3) for j in range(3))
    scale = max(1.0, max(abs(want[i][3]) for i in range(3)))
    pos = max(abs(float(m[i][3]) - want[i][3]) for i in range(3)) / scale
    if rot > POSE_TOLERANCE or pos > POSE_TOLERANCE:
        return (
            f"the level camera's pose did not take at frame {frame} (rotation off by {rot:.2e}, position "
            f"by {pos:.2e} relative): an animated or constrained camera? Send it from a static copy"
        )
    return ""


def make_level_copy(node: Any, level: LevelPlan, frame: Any = None) -> Any:
    """A temporary copy of node posed as level's camera: rt.copy (TODO-verify for a targeted camera),
    named LEVEL_PREFIX + the camera, targeted off, vertical tilt and auto tilt off (TILT) or every
    vertical shift candidate zero (SHIFT), its transform set so its objecttransform is the level pose
    at frame (transform' = transform . objecttransform^-1 . level, so a moved pivot is kept), then
    checked through its own digest. Raises LevelError; a copy that failed is deleted again."""
    name = LEVEL_PREFIX + str(getattr(node, "name", level.camera))
    stale = get_camera(name)
    if stale is not None:
        _delete(stale)
    copier = rt_attr("copy")
    if not callable(copier):
        raise LevelError("this Max has no copy(): the level camera cannot be made")
    try:
        clone = copier(node)
    except Exception as exc:
        raise LevelError(f"copying the camera failed ({type(exc).__name__}: {exc})") from exc
    if clone is None:
        raise LevelError("copying the camera returned nothing")
    try:
        clone.name = name
        set_prop(clone, TARGETED, False)
        if level.mode == TILT:
            _set_all(clone, TILT_V_NAMES, 0.0)
            _set_all(clone, digest.TILT_AUTO_NAMES, False)
            with at_time(frame):
                objtm = _affine(_rows_of(clone.objecttransform))
                nodetm = _affine(_rows_of(clone.transform))
            target = _mul(_mul(nodetm, _inverse(objtm)), _affine(level.max_rows))
            clone.transform = _matrix3([tuple(row[:3]) for row in target])
        else:
            _set_all(clone, digest.SHIFT_V_NAMES, 0.0)
        problem = check_level(name, level, frame)
        if problem:
            raise LevelError(problem)
    except LevelError:
        _delete(clone)
        raise
    except Exception as exc:
        _delete(clone)
        raise LevelError(f"posing the level camera failed ({type(exc).__name__}: {exc})") from exc
    return clone


def drop_level_copy(clone: Any, warnings: list[str] | None = None) -> bool:
    """Delete the temporary level camera; one that cannot be deleted is a line in warnings."""
    name = str(getattr(clone, "name", ""))
    if _delete(clone):
        return True
    if warnings is not None:
        warnings.append(f"the temporary level camera {name} could not be deleted: delete it by hand")
    return False


@contextmanager
def levelled(
    node: Any, level: LevelPlan | None, frame: Any = None, warnings: list[str] | None = None
) -> Iterator[Any]:
    """The camera node to render: node itself without a plan, else make_level_copy(node, level, frame)
    (LevelError on entry when it cannot be made), deleted when the block ends."""
    if level is None:
        yield node
        return
    clone = make_level_copy(node, level, frame)
    try:
        yield clone
    finally:
        drop_level_copy(clone, warnings)
