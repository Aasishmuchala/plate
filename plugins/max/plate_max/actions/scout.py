"""Scout: temporary cameras that cover the scene (MaxDirector's scout poses), rendered as low-res
V-Ray previews and, on request, as full packets.

  bounds    the world bounding box of the visible geometry (cameras and lights excluded)
  poses     scout_poses.scout_poses(bounds, "Z", scene units per metre, eye height, nodes)
  cameras   one free VRayPhysicalCamera per pose, named __PLATE_scout_<N>, aimed with a look-at
            transform and given the pose's focal length (36 mm film)
  previews  rt.render through each camera at 1280 wide (the render's aspect) to scout_<N>.png, with
            V-Ray's raw and split outputs switched off so the artist's output files are untouched
  packets   only with options.packets True: send_packet.run on the scout cameras. A scout pose is a
            new camera angle, so a packet from one is an explicit choice; it is a V-Ray render of the
            real geometry, never a generated view
  cleanup   every temporary camera deleted afterwards unless options.keep

run() never raises; each view is a ScoutResult (camera, ok, preview, packet_id, errors).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import scout_poses as poses_mod
from ..bridge.render import RAW_SAVE, SPLIT_SAVE, _Settings
from ..bridge.scene import get_camera, get_prop, has_prop, rt_attr, set_prop
from ..config import app_data_dir
from ..digest import _METERS_PER_TYPE
from ..rt import runtime
from . import send_packet

SCOUT_PREFIX = "__PLATE_scout_"
PREVIEW_WIDTH = 1280
EYE_M = 1.6
FILM_MM = 36.0  # scout focal lengths are for a 36 mm film gate (MaxDirector)
CAMERA_CLASSES = ("VRayPhysicalCamera", "Physical", "FreeCamera")  # TODO-verify the constructors
FOCAL = ("focal_length", "focal_length_mm")  # TODO-verify (digest.FOCAL_NAMES)
FILM = ("film_width",)  # TODO-verify
SPECIFY_FOV = ("specify_fov",)  # TODO-verify
FOV = ("fov",)
TARGETED = ("targeted",)  # TODO-verify: a free camera's own transform aims it

NO_GEOMETRY = "NO_GEOMETRY"
CAMERA_FAILED = "CAMERA_FAILED"
PREVIEW_FAILED = "PREVIEW_FAILED"
PACKET_FAILED = "PACKET_FAILED"

Progress = Callable[[int, int, str], None]


@dataclass
class ScoutResult:
    view_id: int
    label: str
    camera: str
    ok: bool = False
    code: str | None = None
    preview: str | None = None
    packet_id: str | None = None
    packet_dir: str | None = None
    pose: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    kept: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "view_id": self.view_id,
            "label": self.label,
            "camera": self.camera,
            "ok": self.ok,
            "code": self.code,
            "preview": self.preview,
            "packet_id": self.packet_id,
            "packet_dir": self.packet_dir,
            "pose": self.pose,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "kept": self.kept,
        }


# --------------------------------------------------------------------------- scene reading


def _xyz(point: Any) -> tuple[float, float, float]:
    try:
        return float(point.x), float(point.y), float(point.z)
    except AttributeError:
        a = tuple(point)
        return float(a[0]), float(a[1]), float(a[2])


def node_bbox(node: Any) -> poses_mod.BBox | None:
    """A node's world bounding box: rt.nodeGetBoundingBox(node, matrix3 1) (MaxDirector), else the
    node's .min / .max (world space in MAXScript). TODO-verify both on the box."""
    rt = runtime()
    try:
        identity = rt.matrix3(1)
        pts = rt.nodeGetBoundingBox(node, identity)
        return poses_mod.BBox.coerce((_xyz(pts[0]), _xyz(pts[1])))
    except Exception:
        pass
    lo, hi = get_prop(node, ("min",)), get_prop(node, ("max",))
    if lo is None or hi is None:
        return None
    try:
        return poses_mod.BBox.coerce((_xyz(lo), _xyz(hi)))
    except (TypeError, ValueError, IndexError):
        return None


def _hidden(node: Any) -> bool:
    try:
        return bool(getattr(node, "isHidden", False)) or bool(
            getattr(getattr(node, "layer", None), "ishidden", False)
        )
    except Exception:
        return False


def scene_geometry() -> list[poses_mod.ScoutNode]:
    """Visible geometry nodes with a bounding box (cameras, lights, helpers and our own scout cameras
    left out)."""
    rt = runtime()
    geometry_class = getattr(rt, "GeometryClass", None)
    out: list[poses_mod.ScoutNode] = []
    for node in list(getattr(rt, "objects", [])):
        name = str(getattr(node, "name", ""))
        if name.startswith(SCOUT_PREFIX) or _hidden(node):
            continue
        if geometry_class is not None:
            try:
                if rt.superClassOf(node) != geometry_class:
                    continue
            except Exception:
                pass
        box = node_bbox(node)
        if box is not None:
            out.append(poses_mod.ScoutNode(name, box))
    return out


def meters_to_units() -> float:
    """Scene units per metre (1000 in a millimetre scene); 1.0 when the units are unreadable."""
    units = rt_attr("units")
    system = str(getattr(units, "SystemType", "")).lstrip("#").lower()
    try:
        scale = float(getattr(units, "SystemScale", 1.0) or 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    per = _METERS_PER_TYPE.get(system)
    return 1.0 / (per * scale) if per and scale else 1.0


# --------------------------------------------------------------------------- temporary cameras


def _matrix3(rows: tuple) -> Any:
    rt = runtime()
    point = rt_attr("Point3")
    pts = [point(*r) if callable(point) else tuple(r) for r in rows]
    return rt.matrix3(*pts)


def _delete(node: Any) -> bool:
    try:
        runtime().delete(node)
        return True
    except Exception:
        return False


def _set_focal(cam: Any, focal_mm: float) -> bool:
    """The focal length in mm (the physical control); else the horizontal fov for a 36 mm film, which
    V-Ray only honours with specify_fov on (MaxDirector authoring._set_fov)."""
    for name in FOCAL:
        if has_prop(cam, name) and set_prop(cam, (name,), float(focal_mm)) is not None:
            set_prop(cam, FILM, FILM_MM)
            return True
    set_prop(cam, SPECIFY_FOV, True)
    fov = math.degrees(2.0 * math.atan(FILM_MM / (2.0 * max(float(focal_mm), 1.0))))
    return set_prop(cam, FOV, fov) is not None


def create_scout_camera(name: str, pose: poses_mod.CameraState) -> Any:
    """A free camera named name at pose (look-at transform, focal length). Raises RuntimeError when no
    camera class can be created; an existing node of that name (a scout left over) is deleted first."""
    stale = get_camera(name)
    if stale is not None:
        _delete(stale)
    cam = None
    for cls in CAMERA_CLASSES:
        factory = rt_attr(cls)
        if not callable(factory):
            continue
        try:
            cam = factory()
            break
        except Exception:
            cam = None
    if cam is None:
        raise RuntimeError(f"no camera class could be created ({', '.join(CAMERA_CLASSES)})")
    try:
        cam.name = name
        set_prop(cam, TARGETED, False)
        cam.transform = _matrix3(poses_mod.look_at_rows(pose.pos, pose.look_at, pose.up))
        if not _set_focal(cam, pose.fov_mm):
            raise RuntimeError("the camera's focal length could not be set")
    except Exception:
        _delete(cam)
        raise
    return cam


def _render_preview(cam: Any, path: Path, width: int, height: int) -> bool:
    rt = runtime()
    renderer = getattr(rt_attr("renderers"), "current", None)
    vray = _Settings(renderer)
    vray.set(RAW_SAVE, False)  # a preview must not overwrite the artist's raw / split output files
    vray.set(SPLIT_SAVE, False)
    try:
        bitmap = rt.render(
            camera=cam,
            outputwidth=int(width),
            outputheight=int(height),
            outputfile=str(path),
            vfb=False,
            quiet=True,
        )
        try:
            rt.close(bitmap)
        except Exception:
            pass
    finally:
        vray.restore()
    return path.is_file()


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def preview_folder(options: dict[str, Any]) -> Path:
    given = options.get("preview_dir")
    folder = Path(given) if given else app_data_dir() / "scouts" / _stamp()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# --------------------------------------------------------------------------- the action


def run(
    out_root: Path | str | None = None,
    daemon_base: str | None = None,
    options: Any = None,
) -> list[ScoutResult]:
    """Place the scout cameras, render a preview per view and (options.packets True) send each as a
    packet into out_root. options: progress(done, total, message), preview_dir (default
    <PLATE app data>/scouts/<stamp>), width (1280), eye_m (1.6), views (ids to keep), packets, keep
    (leave the cameras in the scene), plus send_packet.run's options for packets. Never raises."""
    opts = send_packet.normalise_options(options)
    progress = opts.get("progress")
    try:
        geometry = scene_geometry()
    except Exception as exc:
        return [ScoutResult(-1, "scene", "", code=NO_GEOMETRY, errors=[f"{type(exc).__name__}: {exc}"])]
    if not geometry:
        return [
            ScoutResult(-1, "scene", "", code=NO_GEOMETRY, errors=["no visible geometry with a bounding box"])
        ]
    bounds = geometry[0].bbox
    for node in geometry[1:]:
        bounds = bounds.union(node.bbox)
    views = poses_mod.scout_poses(
        bounds, poses_mod.UP_Z, meters_to_units(), float(opts.get("eye_m", EYE_M)), geometry
    )
    wanted = opts.get("views")
    if wanted is not None:
        keep_ids = {int(v) for v in wanted}
        views = [v for v in views if v.id in keep_ids]
    width = int(opts.get("width", PREVIEW_WIDTH))
    rw, rh = rt_attr("renderWidth") or width, rt_attr("renderHeight") or width
    height = max(1, round(width * float(rh) / float(rw)))
    try:
        folder = preview_folder(opts)
    except OSError as exc:
        return [ScoutResult(-1, "scene", "", code=PREVIEW_FAILED, errors=[f"preview folder: {exc}"])]

    results: list[ScoutResult] = []
    created: dict[str, Any] = {}
    total = len(views)
    try:
        for index, view in enumerate(views):
            name = f"{SCOUT_PREFIX}{view.id}"
            result = ScoutResult(view.id, view.label, name, pose=view.pose.as_dict())
            results.append(result)
            send_packet.notify_progress(progress, index, total, f"scout {view.id} ({view.label})")
            try:
                cam = create_scout_camera(name, view.pose)
            except Exception as exc:
                result.code, result.errors = CAMERA_FAILED, [f"camera: {exc}"]
                continue
            created[name] = cam
            path = folder / f"scout_{view.id}.png"
            try:
                rendered = _render_preview(cam, path, width, height)
            except Exception as exc:
                result.code, result.errors = PREVIEW_FAILED, [f"preview: {type(exc).__name__}: {exc}"]
                continue
            if not rendered:
                result.code, result.errors = PREVIEW_FAILED, ["preview: the render wrote no file"]
                continue
            view.thumb_path = str(path)
            result.preview, result.ok = str(path), True
        if opts.get("packets") and out_root is not None:
            ready = [r for r in results if r.ok]
            send_opts = {
                k: v for k, v in opts.items() if k not in ("views", "packets", "keep", "width", "eye_m")
            }
            send_opts.pop("progress", None)
            sent = (
                send_packet.run([r.camera for r in ready], out_root, daemon_base, send_opts) if ready else []
            )
            for result, answer in zip(ready, sent, strict=True):
                result.packet_id, result.packet_dir = answer.packet_id, answer.packet_dir
                result.warnings.extend(answer.warnings)
                if not answer.ok:
                    result.ok, result.code = False, PACKET_FAILED
                    result.errors.extend(f"packet: {e}" for e in answer.errors)
        elif opts.get("packets"):
            for result in results:
                result.warnings.append("packets were asked for without a packets folder (out_root)")
    finally:
        keep = bool(opts.get("keep", False))
        for result in results:
            cam = created.get(result.camera)
            if cam is None:
                continue
            if keep:
                result.kept = True
            elif not _delete(cam):
                result.warnings.append(f"could not delete the temporary camera {result.camera}")
        send_packet.notify_progress(progress, total, total, f"{sum(r.ok for r in results)}/{total} scouts")
    return results
