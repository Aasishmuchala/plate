"""Send Packet: render each camera into a tier A Shot Packet and hand it to the daemon.

Before the first camera, the preflight (daemon_gate): GET /api/v0/health and transport.compatible
(the daemon reads this packet version and is this release), then GET /api/v0/session with the token
(PREFLIGHT_TOKEN when the daemon rejects it) and out_root checked against the session's packets_dirs
(PREFLIGHT_OUT_ROOT when it is outside every one: /hash would refuse every file after the render).
Any failure stops every camera at stage "preflight" before anything renders. Batch Cameras runs
send_packet.run once per camera, so the preflight runs again at the start of every camera.

Per camera, in this order (any failure stops that camera, records why, and moves on to the next):

  colour    bridge.colour.detect_colour_pipeline(): an unknown mode stops before anything renders
  lint      policy.lint_scene(): scaled / shifted / hidden camera, no V-Ray, node names with ':'. With
            recipe.level_and_crop (default true, decision 58) a tilt-corrected or vertically shifted
            camera that bridge.camera_level can plan is a SHIFTED_CAMERA warning, not a refusal
  backup    bridge.safety.backup() when options.backup (before the scene is touched)
  preflight every legend record that will be hashed in memory is checked against the payload limit
            (PAYLOAD_TOO_LARGE names the node and the size): nothing oversize reaches the hash step
  elements  bridge.elements.ensure_default_set() + ensure_optin(recipe.extra_elements)
  level     a two-point camera (level-and-crop): camera_level.plan() at the hero frame, then a
            temporary level copy of the camera (camera_level.levelled) is what renders, width x
            (height + 2 ceil(|dy|)), with the original frame as an off-centre safe_frame; the manifest
            camera is the level one under the camera's own name, overscan.note says where it came from
  render    bridge.render.render_packet_frame() into a local staging folder, at the recipe's overscan
            (plan_frame: the dimension that is short of the engine aspect grows, the original frame
            stays centred as safe_frame; a frame that grows wider also widens the film gate or fov
            for the render, so the scale of every pixel is unchanged)
  exr       exr_header: the EXR's size, compression and channels against the element report, the
            Cryptomatte manifests, and rank-0 coverage per ID (the legend's pixel_share)
  packet    beauty.exr and display.tif moved into <out_root>/<packet_id>/ (shutil.move, any volume)
  legend    the visible names of the Cryptomatte object manifest -> legend.build_id_legend, digests
            from the daemon, planar by the class rule or the node's plate_planar property; the policy
            derived from it and narrowed to recipe.editable_classes; policy.lint on the measured coverage
  hash      BLAKE3 of every file and payload from the daemon (POST /api/v0/hash, decision 5b); the
            big ones by path: id_legend.json, and the scene digest written as scene_digest.txt
            (file role x_scene_digest)
  manifest  packet.build_manifest + packet.write_packet (the manifest last)
  post      transport.post_packet (200/202 ok; 409, 422, 426 reported), with a timeout that grows
            with the folder and one re-post after a timeout; then events_url is followed for a few
            seconds and result.ingest says "ingested", "failed: <errors>" (the camera fails) or
            "ingesting"

run() never raises across cameras; every outcome is a SendResult carrying status_code, events_url,
packet_id and ingest. Every pymxs read goes through rt.runtime().
"""

from __future__ import annotations

import math
import os
import platform
import re
import shutil
import struct
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import PACKET_VERSION, autotag, digest, packet, policy, transport
from .. import legend as legend_mod
from ..bridge import camera_level, colour, elements, hashing, render, safety
from ..bridge.scene import current_frame, get_camera, rt_attr, scene_path, set_prop
from ..config import DEFAULT_DAEMON_URL, load_config
from ..exr_header import (
    Coverage,
    CoverageUnsupported,
    CryptoLayer,
    ExrHeader,
    ExrHeaderError,
    cryptomatte_layers,
    layer_channels,
    rank0_coverage,
    read_header,
)
from ..packet_id import new_packet_id
from ..recipes import CameraRecipe, RecipeError, load_recipes, parse_aspect
from ..rt import runtime

BEAUTY_NAME = "beauty.exr"
DISPLAY_NAME = "display.tif"
STAGING_PREFIX = "plate-render-"
FX_TOLERANCE = 1e-6  # relative: a widened overscan or a level render must keep the pixel scale exactly
LEVEL_AND_CROP_DEFAULT = True  # recipe.level_and_crop when nothing sets it (decision 58)
DATA_ELEMENTS_FLOAT = ("zdepth", "cryptomatte_object", "cryptomatte_material", "world_position")
REQUIRED_ELEMENTS = ("beauty", *elements.DEFAULT_ELEMENTS)
OK_STATUS = (200, 202)

# Why a Send stopped before anything rendered (SendResult.code; stage "preflight").
PREFLIGHT_UNREACHABLE = "PREFLIGHT_UNREACHABLE"
PREFLIGHT_VERSION = "PREFLIGHT_VERSION"
PREFLIGHT_TOKEN = "PREFLIGHT_TOKEN"
PREFLIGHT_SESSION = "PREFLIGHT_SESSION"
PREFLIGHT_OUT_ROOT = "PREFLIGHT_OUT_ROOT"
PAYLOAD_TOO_LARGE = hashing.PAYLOAD_TOO_LARGE
INGEST_FAILED = "INGEST_FAILED"  # the daemon took the packet (202) and then failed to ingest it

# maxVersion() -> #(release, api, sdk, product, update, hotfix, build, year, "update text") on 2026
# (TODO-verify the indices on the box, ONBOX.md step 1); vrayVersion() -> #("7.00.03", ...) (TODO-verify).
MAX_VERSION_FN = "maxVersion"  # TODO-verify
VRAY_VERSION_FN = "vrayVersion"  # TODO-verify

Progress = Callable[[int, int, str], None]


class SendAbort(Exception):
    """Stops one camera's Send at a stage, with the reason (and lint problems) for the dock."""

    def __init__(
        self,
        stage: str,
        reason: str,
        problems: list[dict[str, Any]] | None = None,
        code: str | None = None,
    ):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.problems = list(problems or [])
        self.code = code


@dataclass
class SendResult:
    camera: str
    ok: bool = False
    packet_id: str | None = None
    packet_dir: str | None = None
    stage: str = "start"
    errors: list[str] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)  # lint problems {code, node, detail}
    warnings: list[str] = field(default_factory=list)
    status_code: int | None = None  # the daemon's answer to POST /api/v0/packets
    daemon: Any = None  # its body
    code: str | None = None  # PREFLIGHT_*, PAYLOAD_TOO_LARGE or INGEST_FAILED when one stopped it
    events_url: str | None = None  # the packet's event log, followed after the POST
    ingest: str | None = None  # "ingested", "failed: <errors>" or "ingesting" once posted
    backup: str | None = None
    render: dict[str, Any] | None = None
    frame: dict[str, Any] | None = None  # plan_frame(): rendered size, safe frame, overscan
    journal: str | None = None
    batch_summary: str | None = None  # set by batch_cameras

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera": self.camera,
            "ok": self.ok,
            "packet_id": self.packet_id,
            "packet_dir": self.packet_dir,
            "stage": self.stage,
            "errors": list(self.errors),
            "problems": [dict(p) for p in self.problems],
            "warnings": list(self.warnings),
            "status_code": self.status_code,
            "daemon": self.daemon,
            "code": self.code,
            "events_url": self.events_url,
            "ingest": self.ingest,
            "backup": self.backup,
            "render": self.render,
            "frame": self.frame,
            "journal": self.journal,
            "batch_summary": self.batch_summary,
        }


# --------------------------------------------------------------------------- options


def option(options: Any, key: str, default: Any = None) -> Any:
    """options[key] for a dict (the dock's ActionOptions too) or options.key for an object."""
    if options is None:
        return default
    if isinstance(options, dict):
        value = options.get(key, default)
    else:
        value = getattr(options, key, default)
    return default if value is None else value


OPTION_KEYS = (
    "recipes",
    "progress",
    "backup",
    "config",
    "project_id",
    "frame",
    "supersedes",
    "custody",
    "session",
    "staging_dir",
    "working_space",
    "max_unlabelled_share",
    "journal",
    "vfb_corrections_baked",
    "post_timeout_s",
    "ingest_follow_s",
    "level_and_crop",
)


def normalise_options(options: Any) -> dict[str, Any]:
    """A plain dict of the options (a dict, the dock's ActionOptions, or any object with the
    OPTION_KEYS as attributes); unknown dict keys are kept."""
    if options is None:
        return {}
    if isinstance(options, dict):
        return dict(options)
    return {key: getattr(options, key) for key in OPTION_KEYS if getattr(options, key, None) is not None}


def notify_progress(progress: Progress | None, done: int, total: int, message: str) -> None:
    if progress is None:
        return
    try:
        progress(done, total, message)
    except Exception:  # a broken progress display never stops a Send
        pass


# --------------------------------------------------------------------------- frame planning


@dataclass(frozen=True)
class FramePlan:
    """The frame Send renders: width x height, the deliverable safe_frame inside it ({x, y, width,
    height}), the overscan block for the manifest (None without one), widen, the factor the
    horizontal field of view grows by (1.0 unless the frame grew wider), and level, the
    level-and-crop plan when the camera is rendered as a level pinhole."""

    width: int
    height: int
    safe_frame: dict[str, int]
    overscan: dict[str, str] | None
    widen: float = 1.0
    level: camera_level.LevelPlan | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "safe_frame": dict(self.safe_frame),
            "overscan": dict(self.overscan) if self.overscan else None,
            "widen": self.widen,
            "level": self.level.as_dict() if self.level else None,
        }


def overscan_size(width: int, height: int, aspect: str) -> tuple[int, int, int, int]:
    """(render width, render height, x, y): width x height grown to aspect ("16:9") by enlarging the
    dimension that falls short of it, with the original frame centred at (x, y). 1920x804 to 16:9 is
    1920x1080 with the frame at (0, 138). A frame already at the aspect is unchanged."""
    a, b = parse_aspect(aspect)
    width, height = int(width), int(height)
    if width < 1 or height < 1:
        raise ValueError(f"render size {width}x{height} must be positive")
    if width * b > height * a:  # too wide for the aspect: add height
        new_w, new_h = width, max(height, round(width * b / a))
    elif width * b < height * a:  # too tall: add width
        new_w, new_h = max(width, round(height * a / b)), height
    else:
        new_w, new_h = width, height
    return new_w, new_h, (new_w - width) // 2, (new_h - height) // 2


def _ratio(width: int, height: int) -> str:
    g = math.gcd(int(width), int(height)) or 1
    return f"{int(width) // g}:{int(height) // g}"


def plan_frame(
    width: int, height: int, recipe: CameraRecipe, level: camera_level.LevelPlan | None = None
) -> FramePlan:
    """The rendered frame for a recipe over the scene's render size width x height. recipe.safe_frame
    ({x, y, w, h} in scene pixels, default the whole frame) moves with the overscan offset. A partial
    safe frame without an overscan aspect records the frame's own aspect as engine_aspect (the
    manifest needs an overscan block for any partial safe frame, PACKET.md 3.4).

    level (camera_level.plan of the same width x height): the frame to grow is the level render,
    width x level.render_height, with the scene frame at y level.safe_y inside it; the overscan
    block is always written and its note starts with level.note."""
    width, height = int(width), int(height)
    rect = recipe.safe_frame or {"x": 0, "y": 0, "w": width, "h": height}
    if rect["x"] + rect["w"] > width or rect["y"] + rect["h"] > height:
        raise RecipeError(f"safe_frame {rect} lies outside the {width}x{height} render")
    if level is not None and (level.width, level.height) != (width, height):
        raise ValueError(f"level plan is for {level.width}x{level.height}, the render is {width}x{height}")
    base_w, base_h = width, height
    lift = 0
    if level is not None:
        base_h, lift = level.render_height, level.safe_y
    notes: list[str] = [level.note] if level is not None else []
    if recipe.overscan_aspect:
        new_w, new_h, ox, oy = overscan_size(base_w, base_h, recipe.overscan_aspect)
        overscan: dict[str, str] | None = {"engine_aspect": recipe.overscan_aspect}
        if (new_w, new_h) != (base_w, base_h):
            notes.append(
                f"{base_w}x{base_h} frame rendered inside {new_w}x{new_h} ({recipe.overscan_aspect}) "
                "so engines see real context instead of padding"
            )
    else:
        new_w, new_h, ox, oy = base_w, base_h, 0, 0
        whole = rect == {"x": 0, "y": 0, "w": width, "h": height}
        if level is not None:
            overscan = {"engine_aspect": _ratio(base_w, base_h)}
        elif whole:
            overscan = None
        else:
            overscan = {"engine_aspect": _ratio(width, height), "note": "recipe safe frame"}
    if overscan is not None and notes:
        overscan["note"] = "; ".join(notes)
    safe = {"x": rect["x"] + ox, "y": rect["y"] + lift + oy, "width": rect["w"], "height": rect["h"]}
    return FramePlan(new_w, new_h, safe, overscan, widen=new_w / base_w, level=level)


# --------------------------------------------------------------------------- scene state for the render


def _level_plan(
    camera: str, cam0: dict[str, Any], width: int, height: int, frame: Any
) -> camera_level.LevelPlan | None:
    """camera_level.plan() of the camera as it is at the hero frame (lint read the current time). A
    camera that is shifted or tilted there but cannot be planned stops at lint with SHIFTED_CAMERA."""
    if not policy.lens_problems(cam0):
        return None
    level, why = camera_level.assess(cam0, width, height)
    if level is None:
        detail = (
            f"camera {camera} at frame {frame} is shifted or tilted: level-and-crop does not apply ({why})"
        )
        raise SendAbort(
            "lint", f"lint: {detail}", [{"code": policy.SHIFTED_CAMERA, "node": camera, "detail": detail}]
        )
    return level


@contextmanager
def _levelled(
    node: Any, level: camera_level.LevelPlan | None, frame: Any, warnings: list[str]
) -> Iterator[Any]:
    """The node to render: node itself, or for a level plan the temporary level copy of it
    (camera_level.make_level_copy; a LevelError is SendAbort("render", ...)), deleted afterwards."""
    if level is None:
        yield node
        return
    try:
        clone = camera_level.make_level_copy(node, level, frame)
    except camera_level.LevelError as exc:
        raise SendAbort("render", f"level-and-crop: {exc}") from exc
    try:
        yield clone
    finally:
        camera_level.drop_level_copy(clone, warnings)


@contextmanager
def _render_size(width: int, height: int) -> Iterator[None]:
    """rt.renderWidth/Height set to the planned frame (the camera digest reads them), put back after."""
    rt = runtime()
    before = (rt_attr("renderWidth"), rt_attr("renderHeight"))
    if before == (width, height):
        yield
        return
    rt.renderWidth, rt.renderHeight = int(width), int(height)
    try:
        yield
    finally:
        rt.renderWidth, rt.renderHeight = before


def _widen_target(cam0: dict[str, Any]) -> str | None:
    """The camera property that sets the horizontal field of view: the fov when the focal length was
    derived from it (specify_fov, standard cameras), else the film gate the digest read."""
    read_from = cam0.get("read_from") or {}
    if read_from.get("focal_mm") in digest.FOV_NAMES:
        return str(read_from["focal_mm"])
    film = read_from.get("film_gate_mm")
    return str(film) if film else None


@contextmanager
def _widened(node: Any, cam0: dict[str, Any], factor: float) -> Iterator[str | None]:
    """Widen the camera's horizontal field of view by factor for the render (overscan that adds width
    must keep the pixel scale: film' = film * factor, or fov' = 2 atan(tan(fov / 2) * factor)), and
    put the property back afterwards. TODO-verify on the box that V-Ray renders the widened gate."""
    if abs(factor - 1.0) < 1e-12:
        yield None
        return
    name = _widen_target(cam0)
    if name is None:
        raise SendAbort(
            "render", "overscan adds width, but the camera has no readable film gate or fov to widen"
        )
    try:
        old = float(getattr(node, name))
    except Exception as exc:
        raise SendAbort("render", f"overscan: camera {name} unreadable ({exc})") from exc
    if name in digest.FOV_NAMES:
        new = math.degrees(2 * math.atan(math.tan(math.radians(old) / 2) * factor))
    else:
        new = old * factor
    if set_prop(node, (name,), new) is None:
        raise SendAbort("render", f"overscan: could not set the camera's {name} to widen the frame")
    try:
        yield f"{name} {old:.6g} -> {new:.6g}"
    finally:
        set_prop(node, (name,), old)


# --------------------------------------------------------------------------- the EXR


def _tiff_info(path: Path) -> tuple[int, int, int] | None:
    """(width, height, samples per pixel) of a TIFF's first image, None when it is not a TIFF."""
    try:
        with path.open("rb") as fh:
            data = fh.read(1 << 16)
    except OSError:
        return None
    if data[:4] not in (b"II*\x00", b"MM\x00*"):
        return None
    order = "<" if data[:2] == b"II" else ">"
    found: dict[int, int] = {}
    try:
        (ifd,) = struct.unpack_from(order + "I", data, 4)
        (count,) = struct.unpack_from(order + "H", data, ifd)
        for i in range(count):
            tag, kind, _n, raw = struct.unpack_from(order + "HHI4s", data, ifd + 2 + 12 * i)
            if tag in (256, 257, 277):
                found[tag] = struct.unpack_from(order + ("H" if kind == 3 else "I"), raw, 0)[0]
    except struct.error:
        return None
    if 256 not in found or 257 not in found:
        return None
    return found[256], found[257], found.get(277, 1)


def _crypto_channels(header: ExrHeader, layer: str) -> list[str]:
    pattern = re.compile(rf"^{re.escape(layer)}([0-9]{{2}})\.([RGBA])$")
    hits = []
    for ch in header.channels:
        match = pattern.match(ch.name)
        if match:
            hits.append((int(match.group(1)), "RGBA".index(match.group(2)), ch.name))
    return [name for *_, name in sorted(hits)]


def exr_elements(
    header: ExrHeader, reports: list[elements.LayerList]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """(files.beauty_exr.elements, problems, warnings): the element report checked against the
    channels the EXR really holds. Channel names and dtypes come from the header; a default element
    missing from the file, or a data pass that is not 32-bit float, is a problem (the packet would
    lie); an opt-in that is missing is dropped with a warning."""
    names = {ch.name: ch for ch in header.channels}
    groups = layer_channels(header)
    dtypes: dict[str, str] = {}
    chans: dict[str, list[str]] = {}
    ranks: dict[str, int] = {}
    present: set[str] = set()
    problems: list[str] = []
    warnings: list[str] = []
    for report in reports:
        for entry in report.entries:
            kind, layer = entry["element"], entry["layer"]
            if kind == "beauty":
                found = [c for c in ("R", "G", "B", "A") if c in names]
                if not {"R", "G", "B"} <= set(found):
                    found = []
                first = names[found[0]] if found else None
            elif kind in ("cryptomatte_object", "cryptomatte_material"):
                found = _crypto_channels(header, layer)
                first = names[found[0]] if found else None
                ranks[layer] = 2 * len({name[len(layer) : len(layer) + 2] for name in found})
            else:
                members = groups.get(layer, [])
                wanted = [c for c in entry["channels"] if any(m.suffix == c for m in members)]
                extra = [m.suffix for m in members if m.suffix not in wanted]
                found = wanted + extra
                first = members[0] if members else None
            if not found or first is None:
                message = f"{layer} ({kind}) is not in the EXR"
                (problems if kind in REQUIRED_ELEMENTS else warnings).append(message)
                continue
            present.add(layer)
            chans[layer] = found
            dtypes[layer] = first.dtype
            if first.dtype not in ("half", "float"):
                problems.append(f"{layer}: channel type {first.dtype}, the manifest allows half or float")
            elif kind in DATA_ELEMENTS_FLOAT and first.dtype != "float":
                problems.append(
                    f"{layer} ({kind}) is {first.dtype} in the EXR: data passes must be 32-bit float"
                )
    items = elements.manifest_elements(*reports, dtypes=dtypes, channels=chans)
    out = []
    for item in items:
        if item["layer"] not in present:
            continue
        if item["layer"] in ranks:
            item["rank_levels"] = ranks[item["layer"]]
        out.append(item)
    return out, problems, warnings


@dataclass
class ExrFacts:
    header: ExrHeader
    elements: list[dict[str, Any]]
    object_layer: CryptoLayer
    coverage: Coverage
    warnings: list[str]


def read_exr(path: Path, width: int, height: int, reports: list[elements.LayerList]) -> ExrFacts:
    """Everything Send needs from V-Ray's EXR, or SendAbort("exr", why)."""
    try:
        header = read_header(path)
    except ExrHeaderError as exc:
        raise SendAbort("exr", str(exc)) from exc
    box = header.parts[0].data_window
    if box != (0, 0, width - 1, height - 1):
        raise SendAbort("exr", f"the EXR's data window is {box}, the render was {width}x{height}")
    items, problems, warnings = exr_elements(header, reports)
    if problems:
        raise SendAbort("exr", "the EXR does not hold what the manifest would claim: " + "; ".join(problems))
    crypto_layer = next(i["layer"] for i in items if i["element"] == "cryptomatte_object")
    layers = cryptomatte_layers(header)
    object_layer = layers.get(crypto_layer)
    if object_layer is None or not object_layer.ok:
        why = object_layer.problems if object_layer else [f"no cryptomatte/<key>/name {crypto_layer!r}"]
        raise SendAbort("exr", "Cryptomatte object manifest unusable: " + "; ".join(why))
    material_layer = next((i["layer"] for i in items if i["element"] == "cryptomatte_material"), None)
    material = layers.get(material_layer) if material_layer else None
    if material is None or not material.ok:
        warnings.append(f"Cryptomatte material manifest missing or unusable ({material_layer})")
    try:
        coverage = rank0_coverage(path, crypto_layer, header)
    except CoverageUnsupported as exc:
        raise SendAbort("exr", f"cannot measure the legend's pixel shares: {exc}") from exc
    return ExrFacts(header, items, object_layer, coverage, warnings)


# --------------------------------------------------------------------------- legend and policy


def legend_nodes(
    manifest: dict[str, str], coverage: Coverage, scene_nodes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """(nodes for legend.build_id_legend, warnings): every name in the Cryptomatte object manifest
    that covers pixels at rank 0, carrying its scene node's layer, material and tags, and "planar"
    when the node's plate_planar user property sets it (autotag.planar_overrides; otherwise the
    legend's class rule decides). A covered name that is not a scene geometry node (a proxy or
    scatter item) still enters, unlabelled (so locked). A manifest id that is not the Cryptomatte
    hash of its name stops the Send."""
    by_name = {str(n["name"]): n for n in scene_nodes}
    nodes: list[dict[str, Any]] = []
    warnings: list[str] = []
    named: set[str] = set()
    for name, hex_id in manifest.items():
        named.add(hex_id)
        share = coverage.shares.get(hex_id, 0.0)
        if share <= 0.0:
            continue
        expected = legend_mod.cryptomatte_hash(name)
        if expected != hex_id:
            raise SendAbort(
                "legend",
                f"Cryptomatte manifest gives {name!r} id {hex_id}, its MurmurHash3 is {expected}: "
                "check the VRayCryptomatte id type (node name)",
            )
        node = by_name.get(name)
        if node is None:
            warnings.append(f"{name}: in the Cryptomatte manifest but not a scene geometry node (locked)")
            node = {"name": name, "material": None, "layer": None, "tags": []}
        nodes.append({**node, "pixel_share": share})
    unnamed = sum(share for hex_id, share in coverage.shares.items() if hex_id not in named)
    if unnamed > 0:
        warnings.append(f"Cryptomatte ids without a manifest name cover {unnamed:.2%} of the frame")
    overrides = autotag.planar_overrides(n["name"] for n in nodes if n["name"] in by_name)
    for node in nodes:
        if node["name"] in overrides:
            node["planar"] = overrides[node["name"]]
    return nodes, warnings


def payload_preflight(nodes: list[dict[str, Any]]) -> None:
    """SendAbort("preflight", PAYLOAD_TOO_LARGE) when a legend record that will be hashed in memory
    (legend.record_bytes) is over the payload limit: checked before the render, so the hash step
    after it can never refuse one (the scene digest and the legend file go by path, whatever their
    size)."""
    for node in nodes:
        size = len(legend_mod.record_bytes(node))
        try:
            hashing.check_payload(f"legend record of {node['name']}", size)
        except hashing.PayloadTooLarge as exc:
            raise SendAbort("preflight", str(exc), code=PAYLOAD_TOO_LARGE) from exc


def narrow_policy(pol: dict[str, Any], leg: dict[str, Any], classes: list[str] | None) -> dict[str, Any]:
    """The derived policy with editability narrowed to the recipe's classes (never widened: an id
    the rules lock stays locked). A material stays editable only while every object using it is."""
    if classes is None:
        return pol
    allowed = set(classes)
    was = set(pol["editable_ids"])
    objects = [e for e in leg["entries"] if e["kind"] == "object"]
    editable_objects = {e["id"] for e in objects if e["id"] in was and e["class"] in allowed}
    locked: list[str] = []
    editable: list[str] = []
    for e in leg["entries"]:
        if e["kind"] == "object":
            ok = e["id"] in editable_objects
        elif e["kind"] == "material":
            users = [o for o in objects if o.get("material") == e["name"]]
            ok = e["id"] in was and bool(users) and all(o["id"] in editable_objects for o in users)
        else:
            ok = False
        (editable if ok else locked).append(e["id"])
    return {**pol, "locked_ids": locked, "editable_ids": editable}


# --------------------------------------------------------------------------- host facts


def max_version() -> str:
    fn = rt_attr(MAX_VERSION_FN)
    try:
        values = list(fn())
    except Exception:
        return "unknown"
    try:
        if len(values) >= 8 and int(values[7]) > 2000:
            update = int(values[4]) if len(values) > 4 else 0
            return f"{int(values[7])}.{update}"
        return str(int(values[0]) // 1000 + 1998)
    except (TypeError, ValueError):
        return "unknown"


def vray_version(renderer_class: str) -> str:
    fn = rt_attr(VRAY_VERSION_FN)
    try:
        values = fn()
        first = values[0] if isinstance(values, (list, tuple)) else values
        text = str(first).strip()
        if text:
            return text
    except Exception:
        pass
    return renderer_class or "unknown"


def _renderer_class() -> str:
    try:
        return str(runtime().classOf(runtime().renderers.current))
    except Exception:
        return ""


_PROJECT_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _project_id(options: Any) -> str:
    given = option(options, "project_id")
    if given:
        given = str(given)
        # Keep an id the daemon already accepts (underscores included, e.g. "tula_wot"); slugify only
        # free text. slugify maps "_" to "-", which would send the packet to a different project.
        if _PROJECT_ID_RE.fullmatch(given):
            return given
        return packet.slugify(given)
    name = str(rt_attr("maxFileName") or "")
    stem = Path(name).stem if name else ""
    try:
        return packet.slugify(stem) if stem else "untitled"
    except packet.ManifestError:
        return "untitled"


def _supersedes(options: Any, camera: str) -> str | None:
    value = option(options, "supersedes")
    if isinstance(value, dict):
        value = value.get(camera)
    return str(value) if value else None


# --------------------------------------------------------------------------- recipes


def level_and_crop_on(camera: str, recipe: Any, options: Any) -> bool:
    """recipe.level_and_crop (default LEVEL_AND_CROP_DEFAULT, true): the recipe's own field when its
    type has one, else the key on the recipe the caller handed over (options.recipes[camera], a dict
    or an object; CameraRecipe has no such field yet, so a dict keeps it), else
    options.level_and_crop. False keeps the v0.1 refusal of a tilted or shifted camera."""
    given = (option(options, "recipes") or {}).get(camera)
    for source in (recipe, given):
        value = (
            source.get("level_and_crop")
            if isinstance(source, dict)
            else getattr(source, "level_and_crop", None)
        )
        if value is not None:
            return bool(value)
    return bool(option(options, "level_and_crop", LEVEL_AND_CROP_DEFAULT))


def recipe_for(camera: str, options: Any, saved: dict[str, CameraRecipe] | None = None) -> CameraRecipe:
    """options.recipes[camera] (a CameraRecipe or a dict of its fields), else the scene sidecar's,
    else the default recipe. Raises RecipeError for a recipe that must not be used."""
    given = (option(options, "recipes") or {}).get(camera)
    if given is not None:
        return CameraRecipe.coerce(given, camera)
    saved = load_recipes() if saved is None else saved
    return saved.get(camera) or CameraRecipe(camera=camera)


# --------------------------------------------------------------------------- the daemon gate


class Gate(tuple):
    """daemon_gate's answer: unpacks as (ok, reason) exactly as it always has, and carries code (a
    PREFLIGHT_* name, "" when ok) and session (GET /api/v0/session's answer, None when the check
    stopped before it)."""

    code: str
    session: dict[str, Any] | None

    def __new__(cls, ok: bool, reason: str, code: str = "", session: dict[str, Any] | None = None):
        gate = super().__new__(cls, (bool(ok), str(reason)))
        gate.code = code
        gate.session = session
        return gate

    @property
    def ok(self) -> bool:
        return bool(self[0])

    @property
    def reason(self) -> str:
        return str(self[1])


def _inside(path: Path, folder: Path) -> bool:
    """path is folder or below it, both resolved and case-folded as the OS compares them."""
    try:
        child = Path(os.path.normcase(str(path.resolve())))
        parent = Path(os.path.normcase(str(folder.resolve())))
    except (OSError, RuntimeError, ValueError):
        return False
    return child == parent or parent in child.parents


def daemon_gate(config: dict[str, Any], out_root: Path | str | None = None) -> Gate:
    """The preflight: GET /api/v0/health and transport.compatible, then GET /api/v0/session with the
    token, then out_root (when given) against the session's packets_dirs. A failure's reason names
    the version, reachability, token or folder problem and its code is PREFLIGHT_UNREACHABLE,
    PREFLIGHT_VERSION, PREFLIGHT_TOKEN ("daemon rejected the token", or no token at all),
    PREFLIGHT_SESSION or PREFLIGHT_OUT_ROOT ("<out_root> is outside the daemon's packets folders:
    ..."). Never raises for the daemon."""
    url = str(config.get("daemon_url") or DEFAULT_DAEMON_URL)
    try:
        health = transport.handshake(url)
    except transport.TransportError as exc:
        return Gate(
            False, f"daemon unreachable at {url} (version check not possible): {exc}", PREFLIGHT_UNREACHABLE
        )
    ok, reason = transport.compatible(health)
    if not ok:
        problems = [str(p) for p in health.get("problems") or []]
        return Gate(False, f"daemon version check failed: {'; '.join(problems) or reason}", PREFLIGHT_VERSION)
    session_config = {**config, "daemon_url": url}
    try:
        session = transport.session(session_config)
    except transport.Unauthorized as exc:
        return Gate(False, str(exc), PREFLIGHT_TOKEN)
    except transport.DaemonUnreachable as exc:
        return Gate(False, f"daemon unreachable at {url} (session check): {exc}", PREFLIGHT_UNREACHABLE)
    except transport.TransportError as exc:
        return Gate(False, f"daemon session check failed: {exc}", PREFLIGHT_SESSION)
    if out_root is not None:
        listed = session.get("packets_dirs")
        dirs = [str(d) for d in listed if d] if isinstance(listed, list) else []
        if not any(_inside(Path(out_root), Path(d)) for d in dirs):
            shown = ", ".join(dirs) or "(the daemon names none)"
            return Gate(
                False,
                f"{Path(out_root)} is outside the daemon's packets folders: {shown}",
                PREFLIGHT_OUT_ROOT,
                session,
            )
    return Gate(True, "", "", session)


# --------------------------------------------------------------------------- one camera


def _render_block(rr: render.RenderResult, session: str) -> dict[str, Any]:
    block: dict[str, Any] = {
        "session": session,
        "time_s": rr.time_s,
        "denoiser": {"beauty": "unknown"},
    }
    if rr.peak_rss_gb is not None:
        block["peak_rss_gb"] = rr.peak_rss_gb
    return block


def _send_one(
    result: SendResult,
    out_root: Path,
    config: dict[str, Any],
    options: Any,
    saved: dict[str, CameraRecipe],
    step: Callable[[str], None],
) -> None:
    camera = result.camera
    try:
        recipe = recipe_for(camera, options, saved)
    except (RecipeError, TypeError) as exc:
        raise SendAbort("recipe", f"recipe: {exc}") from exc
    node = get_camera(camera)
    if node is None:
        raise SendAbort("lint", f"camera {camera!r} not found in the scene")

    step("colour")
    detected = colour.detect_colour_pipeline(config)
    reason = colour.blocks_send(detected)
    if reason:
        raise SendAbort("colour", f"colour mode: {reason}")
    try:
        colour_block = colour.manifest_colour(
            detected,
            vfb_corrections_baked=bool(option(options, "vfb_corrections_baked", True)),
            working_space=option(options, "working_space"),
        )
    except ValueError as exc:
        raise SendAbort("colour", f"colour: {exc}") from exc

    step("lint")
    level_on = level_and_crop_on(camera, recipe, options)
    report = policy.lint_scene(camera, level_and_crop=level_on)
    if not report.passed:
        raise SendAbort("lint", "lint: " + "; ".join(report), report.problems)
    result.warnings.extend(f"{w['code']}: {w['detail']}" for w in report.warnings)

    if option(options, "backup", False):
        step("backup")
        try:
            result.backup = str(safety.backup())
        except safety.BackupFailed as exc:
            raise SendAbort("backup", str(exc)) from exc
    journal = (
        safety.Journal(scene_path(), result.backup, op="send") if option(options, "journal", True) else None
    )
    if journal is not None:
        result.journal = str(journal.path)
        journal.record("recipe", camera, detail=f"{recipe.engine_id} {recipe.preset}".strip())
    try:
        _render_and_write(
            result, recipe, node, detected, colour_block, out_root, config, options, step, journal, level_on
        )
    except SendAbort as abort:
        if journal is not None:
            journal.record(abort.stage, camera, status="failed", detail=abort.reason)
            journal.finish("failed")
        raise
    except Exception as exc:
        if journal is not None:
            journal.record(result.stage, camera, status="error", detail=f"{type(exc).__name__}: {exc}")
            journal.finish("error")
        raise
    if journal is not None:
        journal.finish("ok" if result.ok else "failed")


def _render_and_write(
    result: SendResult,
    recipe: CameraRecipe,
    node: Any,
    detected: dict[str, Any],
    colour_block: dict[str, Any],
    out_root: Path,
    config: dict[str, Any],
    options: Any,
    step: Callable[[str], None],
    journal: safety.Journal | None,
    level_on: bool = False,
) -> None:
    camera = result.camera

    def note(stage: str, detail: str = "") -> None:
        if journal is not None:
            journal.record(stage, camera, detail=detail)

    step("preflight")
    scene_nodes = digest.scene_nodes()
    payload_preflight(scene_nodes)  # before the scene is touched

    step("elements")
    defaults = elements.ensure_default_set()
    if not defaults.ok:
        raise SendAbort(
            "elements", f"render elements missing: {', '.join(defaults.missing + defaults.unknown)}"
        )
    reports = [defaults]
    if recipe.extra_elements:
        optins = elements.ensure_optin(names=recipe.extra_elements)
        if optins.missing or optins.unknown:
            result.warnings.append(f"opt-in elements not added: {', '.join(optins.missing + optins.unknown)}")
        reports.append(optins)
    for report in reports:
        result.warnings.extend(report.unverified)
    note("elements", ", ".join(layer for report in reports for layer in report))

    scene = digest.scene_digest()  # at the scene's own render size, with the elements in place
    base_w, base_h = int(rt_attr("renderWidth")), int(rt_attr("renderHeight"))
    frame = option(options, "frame")
    if frame is None:
        frame = current_frame()
    cam0 = digest.camera_digest(camera, frame=frame)
    level = _level_plan(camera, cam0, base_w, base_h, frame) if level_on else None
    try:
        plan = plan_frame(base_w, base_h, recipe, level)
    except (RecipeError, ValueError) as exc:
        raise SendAbort("recipe", f"recipe: {exc}") from exc
    result.frame = plan.as_dict()
    if level is not None:
        note("level", level.note)

    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=option(options, "staging_dir")))
    try:
        with (
            _render_size(plan.width, plan.height),
            _levelled(node, level, frame, result.warnings) as render_node,
            _widened(render_node, cam0, plan.widen) as widened,
        ):
            cam = digest.camera_digest(
                str(render_node.name) if level is not None else camera,
                frame=frame,
                recipe=recipe.digest_recipe(plan.safe_frame, plan.overscan),
            )
            if level is not None:
                cam["name"] = camera  # the manifest names the artist's camera, not the temporary copy
            if widened or level is not None:
                fx0 = (cam0.get("intrinsics_px") or {}).get("fx")
                fx1 = (cam.get("intrinsics_px") or {}).get("fx")
                if not fx0 or not fx1 or abs(fx1 - fx0) > FX_TOLERANCE * fx0:
                    what = f"widening the camera ({widened})" if widened else "the level render"
                    raise SendAbort("render", f"overscan: {what} changed the pixel scale {fx0} -> {fx1}")
            if widened:
                result.warnings.append(f"overscan widened the camera for the render: {widened}")
            result.warnings.extend(f"camera: {w}" for w in cam.get("warnings") or [])
            hero = cam.get("hero_frame")
            hero = int(hero) if hero is not None else 0

            step("render")
            try:
                rr = render.render_packet_frame(
                    render_node, staging / BEAUTY_NAME, staging / DISPLAY_NAME, plan.width, plan.height, hero
                )
            except render.RenderFailed as exc:
                raise SendAbort("render", f"render: {exc}") from exc
        result.render = rr.as_dict()
        result.warnings.extend(rr.warnings)
        if not rr.ok:
            raise SendAbort("render", "render: " + ("; ".join(rr.warnings) or "no EXR or TIFF written"))
        note("render", f"{plan.width}x{plan.height} frame {hero} in {rr.time_s}s")

        step("exr")
        exr_path, tif_path = Path(str(rr.exr)), Path(str(rr.tif))
        facts = read_exr(exr_path, plan.width, plan.height, reports)
        result.warnings.extend(facts.warnings)
        tif = _tiff_info(tif_path)
        if tif is None or (tif[0], tif[1]) != (plan.width, plan.height) or tif[2] not in (3, 4):
            raise SendAbort("exr", f"display TIFF is {tif}, expected {plan.width}x{plan.height} RGB(A)")

        step("packet")
        packet_id = new_packet_id()
        root = Path(out_root) / packet_id
        root.mkdir(parents=True, exist_ok=False)
        shutil.move(str(exr_path), str(root / BEAUTY_NAME))
        shutil.move(str(tif_path), str(root / DISPLAY_NAME))
        result.packet_id, result.packet_dir = packet_id, str(root)
        note("packet", str(root))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    step("legend")
    nodes, warnings = legend_nodes(facts.object_layer.manifest or {}, facts.coverage, scene_nodes)
    result.warnings.extend(warnings)
    try:
        leg = hashing.with_daemon_digests(
            lambda fn: legend_mod.build_id_legend(nodes, packet_id, digest_fn=fn), config
        )
    except legend_mod.LegendError as exc:
        raise SendAbort("legend", f"legend: {exc}") from exc
    pol = narrow_policy(policy.derive_policy(leg), leg, recipe.editable_classes)
    share_limit = float(option(options, "max_unlabelled_share", policy.DEFAULT_MAX_UNLABELLED_SHARE))
    coverage = {e["id"]: e["pixel_share"] for e in leg["entries"] if e["kind"] == "object"}
    lint = policy.lint(pol, coverage, share_limit)
    if not lint.passed:
        raise SendAbort("legend", "lint: " + "; ".join(lint), lint.problems)

    step("hash")
    hashes = hashing.hash_packet_files(root, config, rels=[BEAUTY_NAME, DISPLAY_NAME])
    hashes.update(hashing.sidecar_hashes(config, legend=leg, folder=root))
    scene_hash = hashing.hash_scene(scene, config, folder=root, hashes=hashes)

    step("manifest")
    header = facts.header
    beauty_extra: dict[str, Any] = {
        "width": plan.width,
        "height": plan.height,
        "compression": header.compression_name,
        "elements": facts.elements,
    }
    if header.multipart:
        beauty_extra["parts"] = "multi"
    files = {
        "beauty_exr": packet.file_ref_from_hashes(BEAUTY_NAME, hashes, "exr", **beauty_extra),
        "display_tiff16": packet.file_ref_from_hashes(
            DISPLAY_NAME,
            hashes,
            "tiff",
            width=plan.width,
            height=plan.height,
            bit_depth=16,
            channels="RGBA" if tif[2] == 4 else "RGB",
        ),
        "id_legend": packet.file_ref_from_hashes("id_legend.json", hashes, "json"),
        hashing.SCENE_DIGEST_ROLE: hashing.scene_digest_ref(hashes),
    }
    render_block = _render_block(rr, str(option(options, "session", "interactive")))
    renderer_class = _renderer_class()
    try:
        camera_block = packet.build_camera_block(cam)
        view_payload = packet.view_hash_payload(
            camera_block, leg, colour_block, render_block, digest.scene_globals()
        )
        manifest = packet.build_manifest(
            cam,
            scene,
            files,
            leg,
            pol,
            _project_id(options),
            packet.slugify(camera),
            {"version": max_version(), "python": platform.python_version()},
            {"name": "vray", "version": vray_version(renderer_class)},
            colour_block,
            render_block,
            scene_hash=scene_hash,
            view_hash=hashing.hash_view(view_payload, config, folder=root),
            supersedes=_supersedes(options, camera),
            custody=option(options, "custody"),
            max_unlabelled_share=share_limit,
        )
        packet.write_packet(root, manifest, leg, hashes=hashes)
    except packet.ManifestError as exc:
        raise SendAbort("manifest", f"manifest: {exc}") from exc
    note("manifest", packet_id)

    step("post")
    answer = transport.post_packet(root, config, timeout_s=option(options, "post_timeout_s"))
    status, body = answer["status_code"], answer["body"]
    result.status_code, result.daemon = status, body
    if answer.get("reposted"):
        result.warnings.append(f"the POST timed out; the re-post of the same folder answered HTTP {status}")
    if status in OK_STATUS:
        note("post", f"HTTP {status}")
        step("ingest")
        follow_ingest(result, body, config, options)
        result.ok = True
        result.stage = "done"
        return
    if status == 426:
        raise SendAbort("post", f"daemon refused packet version {PACKET_VERSION} (HTTP 426): {body}")
    if status == 409:
        raise SendAbort("post", f"daemon already holds a different packet under this id (HTTP 409): {body}")
    raise SendAbort("post", f"daemon rejected the packet (HTTP {status}): {body}")


def follow_ingest(result: SendResult, body: Any, config: dict[str, Any], options: Any) -> None:
    """After a 200/202: result.events_url (the answer's, else the packet's event route) followed for
    options.ingest_follow_s (default transport.FOLLOW_WINDOW_S) into result.ingest. An ingest the
    daemon failed is SendAbort("ingest", INGEST_FAILED); one still running is not a failure."""
    events_url = body.get("events_url") if isinstance(body, dict) else None
    result.events_url = str(events_url or transport.events_url_for(str(result.packet_id)))
    window = float(option(options, "ingest_follow_s", transport.FOLLOW_WINDOW_S))
    state = transport.follow_ingest(result.events_url, config, window_s=window)
    result.ingest = str(state["ingest"])
    if state.get("problem"):
        result.warnings.append(f"ingest not followed ({result.events_url}): {state['problem']}")
    if result.ingest.startswith("failed"):
        raise SendAbort("ingest", f"ingest: {result.ingest}", code=INGEST_FAILED)


# --------------------------------------------------------------------------- entry point


def run(
    camera_names: Any,
    out_root: Path | str,
    daemon_base: str | None = None,
    options: Any = None,
) -> list[SendResult]:
    """Send every camera in camera_names (names, or recipes carrying the camera) as a packet folder
    under out_root (the daemon's packets folder: /hash refuses paths outside it). daemon_base is the
    daemon URL (default: config.json's). options: recipes {camera: recipe}, progress(done, total,
    message), backup, config (token, daemon_url), project_id, frame, supersedes (an id or {camera: id}),
    custody, session, staging_dir, working_space, max_unlabelled_share, journal. Returns one SendResult
    per camera; never raises across cameras."""
    opts = normalise_options(options)
    recipes: dict[str, Any] = dict(opts.get("recipes") or {})
    refused: dict[str, str] = {}
    names: list[str] = []
    subjects = (
        [camera_names] if isinstance(camera_names, (str, CameraRecipe, dict)) else list(camera_names or [])
    )
    for subject in subjects:
        if isinstance(subject, str):
            names.append(subject)
            continue
        try:
            rec = CameraRecipe.coerce(subject)
        except (RecipeError, TypeError) as exc:
            label = subject.get("camera") if isinstance(subject, dict) else getattr(subject, "camera", None)
            names.append(str(label or "?"))
            refused[names[-1]] = str(exc)
            continue
        names.append(rec.camera)
        recipes[rec.camera] = subject if isinstance(subject, dict) else rec  # a dict keeps level_and_crop
    opts["recipes"] = recipes
    options = opts

    progress = option(options, "progress")
    config = dict(option(options, "config") or load_config())
    if daemon_base:
        config["daemon_url"] = str(daemon_base)
    config.setdefault("daemon_url", DEFAULT_DAEMON_URL)
    total = len(names)
    results: list[SendResult] = []
    if not names:
        return results

    notify_progress(progress, 0, total, "checking the daemon")
    gate = daemon_gate(config, out_root)
    gate_ok, gate_reason = gate
    saved: dict[str, CameraRecipe] = {}
    try:
        saved = load_recipes()
    except Exception:
        saved = {}
    for index, camera in enumerate(names):
        result = SendResult(camera=camera)
        results.append(result)
        if not gate_ok:
            result.stage, result.code, result.errors = "preflight", gate.code, [gate_reason]
            notify_progress(progress, index + 1, total, f"{camera}: {gate_reason}")
            continue

        def step(
            stage: str, _camera: str = camera, _index: int = index, _result: SendResult = result
        ) -> None:
            _result.stage = stage
            notify_progress(progress, _index, total, f"{_camera}: {stage}")

        try:
            if camera in refused:
                raise SendAbort("recipe", f"recipe: {refused[camera]}")
            _send_one(result, Path(out_root), config, options, saved, step)
        except SendAbort as abort:
            result.ok = False
            result.stage = abort.stage
            result.code = abort.code or result.code
            result.errors.append(abort.reason)
            result.problems.extend(abort.problems)
        except transport.TransportError as exc:  # hashing or posting: the daemon said no or went away
            result.ok = False
            result.errors.append(f"{result.stage}: daemon: {exc}")
        except hashing.PayloadTooLarge as exc:  # the preflight stops these: a last net
            result.ok = False
            result.code = PAYLOAD_TOO_LARGE
            result.errors.append(f"{result.stage}: {exc}")
        except Exception as exc:  # anything unexpected stops this camera, never the batch
            result.ok = False
            result.errors.append(f"{result.stage}: {type(exc).__name__}: {exc}")
        if result.packet_dir and not (Path(result.packet_dir) / "manifest.json").is_file():
            result.warnings.append(
                f"the render is kept in {result.packet_dir} for inspection; it has no manifest, so the "
                "daemon never ingests it (delete it when done)"
            )
        notify_progress(
            progress,
            index + 1,
            total,
            f"{camera}: {'sent ' + str(result.packet_id) if result.ok else 'failed at ' + result.stage}",
        )
    return results
