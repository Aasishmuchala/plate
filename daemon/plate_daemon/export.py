"""Export a run's result out of the CAS, enforcing doctrine 6 ("gates can block delivery").

DONE exports. GATE_FAIL is kept and flagged and exports only with `override=True` and a written reason of
at least 10 non-blank characters; the override is appended to the run's append-only event log as
`actor="human", reason="override: <reason>"` before the bytes are written. Every other state raises
`NotExportable`. The blob is re-verified against its BLAKE3 key first and written atomically; an existing
destination file is never overwritten.

Two entry points share those rules:

- `export_run(conn, run_id, dest, ...)` copies the stored bytes to one file (the seed's byte copy: no route
  calls it any more, and it neither decodes nor crops).
- `export(run, fmt, out_dir, override=None, ...)` is what `plate export` calls: it decodes the run's gated
  composite (the 16-bit PNG in the CAS), writes it as `tiff16`, `png16` or `exr` with `plate_pixels.imageio`
  (an EXR is the sRGB display master decoded to linear half float in the packet's working primaries), embeds
  the run's lineage in the file (`lineage.embed`: the file name is not the only thing that says where it came from) and
  writes `<name>.gate.json` beside it (the stored gate report plus an `export` block naming the files). The
  sibling is shared by the formats of one take (tiff16 and exr of the same run and name): a second export adds
  its file to the block; a report that belongs to another run blocks the export. The image is written under a
  temp name and moved into place last; a lineage failure leaves nothing behind.

Framing (router plan T114). When the run's packet declares `camera.safe_frame` (an overscan render, a
level-and-crop packet, room_a's 2.39:1 band in a 16:9 frame), the safe-frame region is what leaves by default:
a pixel crop, nothing resampled. `full_frame=True` keeps the whole take (for re-framing later). The `export`
block of the sidecar says which: `crop: {x, y, width, height}` (take pixels), or `full_frame: true` (plus
`safe_frame` when one was placed on the take); a packet with no safe frame adds neither, exactly as before.
`take_framing` places the rectangle: on a take at the render resolution it is the declared rectangle; a take
that is already the safe-frame size, or any take of a packet whose safe frame is its whole render, is kept
whole; a take of the render's aspect at another size (an engine's 4K clip of a 1080p plate) gets the rectangle
scaled and rounded inward, so no overscan pixel survives. A take of another aspect on an overscan packet (the
rectangle cannot be located), or a registered packet whose manifest cannot be read, raises `SafeFrameError`
unless the whole take was asked for. `cutdown._region` validates the rectangle (inside the take, not empty)
and, for a 4:2:0 encode (`even=True`), snaps it inward to even coordinates. One sidecar describes one framing:
a second format of the same take must be framed the same way or take another name.

Colour tags. A tiff16 and a png16 are sRGB-encoded (that is what the gates scored) and carry the sRGB ICC
profile. An EXR carries `chromaticities` (the primaries of its pixels), the ASWF `colorInteropID` (which OIIO
reads as `oiio:ColorSpace`) and `plate:colourspace` naming the space, all three from the packet's declared
`colour.working_space`: `lin_rec709` (the default; also what a `display_only` Tier C packet gets, since the
display image is decoded with the sRGB EOTF) or `acescg` (the Rec.709 pixels are converted to AP1 with a
Bradford-adapted matrix first, so the tags always describe the pixels in the file). `delivery.frames` reads the
same tags back and converts to Rec.709 before it encodes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import lineage
from .cas import CAS
from .fsutil import replace_with_retry, temp_sibling, write_bytes_atomic, write_json_atomic
from .machine import append_note, get_run, run_events
from .models import Run
from .states import RunState
from .timeutil import iso, utcnow

MIN_OVERRIDE_REASON = 10
# `plate export --format`: the file extension each format is written with.
FORMATS: dict[str, str] = {"tiff16": ".tif", "png16": ".png", "exr": ".exr"}
OVERRIDE_PREFIX = "override: "
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
REC709_CHROMATICITIES = (0.64, 0.33, 0.30, 0.60, 0.15, 0.06, 0.3127, 0.3290)
AP1_CHROMATICITIES = (0.713, 0.293, 0.165, 0.830, 0.128, 0.044, 0.32168, 0.33767)
DEFAULT_WORKING_SPACE = "lin_rec709"
# packet colour.working_space -> (name written as plate:colourspace, ASWF colorInteropID, chromaticities)
WORKING_SPACES: dict[str, tuple[str, str, tuple[float, ...]]] = {
    "lin_rec709": ("lin_rec709", "lin_rec709_scene", REC709_CHROMATICITIES),
    "acescg": ("ACEScg", "lin_ap1_scene", AP1_CHROMATICITIES),
}
# interop id -> chromaticities, for an EXR that names its space but carries no chromaticities
INTEROP_CHROMATICITIES: dict[str, tuple[float, ...]] = {
    iid: chroma for _, iid, chroma in WORKING_SPACES.values()
}


class NotExportable(Exception):
    pass


class SafeFrameError(NotExportable):
    """The packet's safe frame cannot be placed on the take (another aspect, a rectangle outside it), or the
    packet is registered but its manifest cannot be read. `full_frame=True` exports the whole take instead.
    `code` is `SAFE_FRAME_MISMATCH` or `PACKET_UNREADABLE`."""

    def __init__(self, message: str, code: str = "SAFE_FRAME_MISMATCH"):
        super().__init__(message)
        self.code = code


def export_run(
    conn: sqlite3.Connection,
    run_id: str,
    dest: Path,
    *,
    override: bool = False,
    reason: str | None = None,
    cas: CAS | None = None,
) -> Path:
    if cas is None:  # no global CAS (router plan T5): the caller passes the run's project CAS
        raise TypeError("pass the project's CAS (CAS(project_dir / 'assets')); there is no global CAS")
    dest = Path(dest)
    run = get_run(conn, run_id)
    if run.state is RunState.GATE_FAIL:
        if not override:
            raise NotExportable(
                f"run {run_id} failed its gates; export needs override=True and a written reason"
            )
        if reason is None or len(reason.strip()) < MIN_OVERRIDE_REASON:
            raise NotExportable(
                f"a GATE_FAIL override needs a reason of at least {MIN_OVERRIDE_REASON} characters"
            )
    elif run.state is not RunState.DONE:
        raise NotExportable(
            f"run {run_id} is {run.state.value}; only DONE (or GATE_FAIL with override) exports"
        )
    if not run.result_key:
        raise NotExportable(f"run {run_id} has no result blob")
    if not cas.verify(run.result_key):
        raise NotExportable(f"result blob {run.result_key} is missing or fails its BLAKE3 check")
    if dest.exists():
        raise NotExportable(f"{dest} already exists; exports never overwrite")
    if run.state is RunState.GATE_FAIL:
        append_note(conn, run_id, kind="override", actor="human", reason=f"override: {reason}")
    write_bytes_atomic(dest, cas.read_bytes(run.result_key))
    return dest


# ---------------------------------------------------------------------------- the safe frame (T114)

Rect = tuple[int, int, int, int]  # (x, y, width, height) in take pixels, origin top-left
# PACKET.md rule 4: an overscan frame matches its engine aspect within 1%; a take of the render's aspect
# within the same tolerance is the render at another size.
ASPECT_TOLERANCE = 0.01


def _rect_dict(rect: Rect) -> dict[str, int]:
    x, y, w, h = rect
    return {"x": x, "y": y, "width": w, "height": h}


@dataclass(frozen=True)
class Framing:
    """What part of the take leaves. `crop` is the rectangle written, in take pixels; `full_frame` says the
    whole take was kept; `safe_frame` is the deliverable rectangle placed on the take when it was kept whole."""

    crop: Rect | None = None
    full_frame: bool = False
    safe_frame: Rect | None = None

    def cuts(self, width: int, height: int) -> bool:
        """True when the crop removes pixels (a crop that is the whole take changes nothing)."""
        return self.crop is not None and self.crop != (0, 0, width, height)

    def record(self) -> dict[str, Any]:
        """The sidecar and ledger form: `{crop: {x, y, width, height}}`, `{full_frame: true}` (plus
        `safe_frame` when one was placed), or `{}` for a packet that declares no safe frame."""
        if self.full_frame:
            out: dict[str, Any] = {"full_frame": True}
            if self.safe_frame is not None:
                out["safe_frame"] = _rect_dict(self.safe_frame)
            return out
        return {"crop": _rect_dict(self.crop)} if self.crop is not None else {}


def _framing_of(block: Mapping[str, Any]) -> dict[str, Any]:
    """The framing keys of an `export` block (what `Framing.record()` wrote there)."""
    return {k: block[k] for k in ("crop", "full_frame", "safe_frame") if k in block}


def declared_safe_frame(manifest: Mapping[str, Any] | None) -> tuple[Rect, tuple[int, int] | None] | None:
    """(the packet's `camera.safe_frame`, its `camera.resolution` as (width, height) or None), or None when
    the manifest declares no safe frame (no camera block, or one without `safe_frame`)."""
    camera = manifest.get("camera") if isinstance(manifest, Mapping) else None
    sf = camera.get("safe_frame") if isinstance(camera, Mapping) else None
    if not isinstance(sf, Mapping):
        return None
    try:
        x, y, w, h = (int(sf[k]) for k in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError) as e:
        raise SafeFrameError(f"the packet's safe_frame {dict(sf)} is not x, y, width and height") from e
    res = camera.get("resolution")
    try:
        size = (int(res["width"]), int(res["height"])) if isinstance(res, Mapping) else None
    except (KeyError, TypeError, ValueError):
        size = None
    return (x, y, w, h), size


def _declared(
    manifest: Mapping[str, Any] | None, *, lenient: bool
) -> tuple[Rect, tuple[int, int] | None] | None:
    """`declared_safe_frame`, or None for a malformed one when `lenient` (the whole take is kept anyway)."""
    try:
        return declared_safe_frame(manifest)
    except SafeFrameError:
        if lenient:
            return None
        raise


def place_safe_frame(rect: Rect, resolution: tuple[int, int] | None, width: int, height: int) -> Rect:
    """`rect` (render pixels) on a `width` x `height` take; see the module docstring. Raises `SafeFrameError`
    when the render has overscan and the take is neither the render, nor the safe frame, nor the render's
    aspect at another size."""
    x, y, w, h = rect
    rw, rh = resolution or (width, height)
    if (width, height) == (rw, rh):
        return rect
    if rect == (0, 0, rw, rh) or (width, height) == (w, h):
        return 0, 0, width, height  # no overscan to remove, or the take already is the deliverable
    if rw <= 0 or rh <= 0 or abs((width * rh) / (height * rw) - 1.0) > ASPECT_TOLERANCE:
        raise SafeFrameError(
            f"the take is {width} x {height} and the packet renders {rw} x {rh} with the safe frame "
            f"{_rect_dict(rect)}; the rectangle cannot be placed on a take of another aspect. Export it with "
            "full_frame to keep the whole take"
        )
    # the same rectangle at the take's scale, rounded inward on every edge: no overscan pixel survives
    x0, y0 = -(-x * width // rw), -(-y * height // rh)
    x1, y1 = (x + w) * width // rw, (y + h) * height // rh
    return x0, y0, x1 - x0, y1 - y0


def framing(
    manifest: Mapping[str, Any] | None,
    width: int,
    height: int,
    *,
    full_frame: bool = False,
    even: bool = False,
) -> Framing:
    """The `Framing` of a `width` x `height` take of the packet `manifest`. `even` snaps the crop inward to
    even coordinates (a 4:2:0 encode). With `full_frame` nothing raises: a rectangle that cannot be placed is
    left out."""
    from .cutdown import CutdownError, _region

    declared = _declared(manifest, lenient=full_frame)
    if declared is None:
        return Framing(full_frame=full_frame)
    rect, resolution = declared
    try:
        box = place_safe_frame(rect, resolution, width, height)
        x0, y0, x1, y1 = _region(width, height, box)  # cutdown's validation: inside the take, not empty
    except (SafeFrameError, CutdownError) as e:
        if full_frame:
            return Framing(full_frame=True)
        if isinstance(e, SafeFrameError):
            raise
        raise SafeFrameError(f"the packet's safe frame on the {width} x {height} take: {e}") from e
    if full_frame:
        return Framing(full_frame=True, safe_frame=box)
    return Framing(crop=(x0, y0, x1 - x0, y1 - y0) if even else box)


def packet_manifest(conn: sqlite3.Connection, packet_id: str | None) -> dict[str, Any] | None:
    """The run's packet manifest, or None when the run names no packet or the index does not know it. Raises
    `SafeFrameError` (`PACKET_UNREADABLE`) when the packet is registered but its manifest cannot be read: its
    safe frame is then unknown, and a delivery must not guess it."""
    if not packet_id:
        return None
    try:
        row = conn.execute("SELECT path FROM packets WHERE packet_id = ?", (packet_id,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    path = Path(row[0]) / "manifest.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise SafeFrameError(
            f"packet {packet_id}'s manifest cannot be read ({type(e).__name__}), so its safe frame is "
            "unknown; reconnect the packet folder, or export with full_frame to keep the whole take",
            code="PACKET_UNREADABLE",
        ) from e
    return doc if isinstance(doc, dict) else None


def take_framing(
    conn: sqlite3.Connection,
    packet_id: str | None,
    size: tuple[int, int] | Callable[[], tuple[int, int]],
    *,
    full_frame: bool = False,
    even: bool = False,
) -> Framing:
    """`framing` for a run's take: the manifest comes from the index (`packet_manifest`), and `size` (width,
    height), or a callable giving it, is only asked for when the packet declares a safe frame."""
    try:
        manifest = packet_manifest(conn, packet_id)
    except SafeFrameError:
        if full_frame:
            return Framing(full_frame=True)
        raise
    if _declared(manifest, lenient=full_frame) is None:
        return Framing(full_frame=full_frame)
    width, height = size() if callable(size) else size
    return framing(manifest, width, height, full_frame=full_frame, even=even)


# ---------------------------------------------------------------------------- plate export (still formats)


@dataclass(frozen=True)
class Exported:
    """What `export` wrote: the image, its `.gate.json` sibling and the facts the CLI prints. `framing`
    says which part of the take was written; `framing.record()` is what the sidecar and the ledger carry."""

    run_id: str
    fmt: str
    path: Path
    report_path: Path
    overridden: bool
    override_reason: str | None
    lineage_embedded: bool
    report_ref: str | None
    framing: Framing = Framing()


def recorded_override(conn: sqlite3.Connection, run_id: str) -> str | None:
    """The reason of the run's latest recorded override (`POST /runs/{id}/override` or an earlier export), else None."""
    reason = None
    for ev in run_events(conn, run_id):
        if ev.kind == "override":
            text = ev.reason or ""
            reason = text[len(OVERRIDE_PREFIX) :] if text.startswith(OVERRIDE_PREFIX) else text
    return reason


def _earlier_report(report_dest: Path, run_id: str) -> dict[str, Any] | None:
    """The `export` block of a `.gate.json` this run already wrote beside another format of the same take (the
    sibling is shared and kept), else None. A report that belongs to anything else blocks the export."""
    if not report_dest.exists():
        return None
    try:
        block = json.loads(report_dest.read_text(encoding="utf-8-sig")).get("export")
    except (OSError, ValueError, AttributeError):
        block = None
    if not isinstance(block, dict) or block.get("run_id") != run_id:
        raise NotExportable(
            f"{report_dest} already exists and is not run {run_id}'s report; exports never overwrite"
        )
    return block


def _decode16(data: bytes, what: str) -> np.ndarray:
    """uint16 RGB (H, W, 3) from image bytes (8-bit sources are promoted by x257)."""
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise NotExportable(f"{what} is not a decodable image; only image runs export with `plate export`")
    if img.ndim == 2:
        img = np.repeat(img[..., None], 3, axis=2)
    img = np.ascontiguousarray(img[..., :3][..., ::-1])
    return img.astype(np.uint16) * 257 if img.dtype == np.uint8 else img.astype(np.uint16)


def _xyz_from_rgb(chroma: Sequence[float]) -> np.ndarray:
    """3x3 matrix taking linear RGB with these chromaticities (rx, ry, gx, gy, bx, by, wx, wy) to XYZ."""
    rx, ry, gx, gy, bx, by, wx, wy = (float(v) for v in chroma)
    prim = np.array(
        [
            [rx / ry, gx / gy, bx / by],
            [1.0, 1.0, 1.0],
            [(1 - rx - ry) / ry, (1 - gx - gy) / gy, (1 - bx - by) / by],
        ]
    )
    white = np.array([wx / wy, 1.0, (1 - wx - wy) / wy])
    return prim * np.linalg.solve(prim, white)


_BRADFORD = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])


def primaries_matrix(src: Sequence[float], dst: Sequence[float]) -> np.ndarray:
    """3x3 matrix taking linear RGB in the `src` primaries to linear RGB in the `dst` primaries (Bradford
    chromatic adaptation between the two white points; the identity when the chromaticities are equal)."""
    if np.allclose(src, dst, atol=1e-6):
        return np.eye(3)
    src_xyz, dst_xyz = _xyz_from_rgb(src), _xyz_from_rgb(dst)
    src_white, dst_white = src_xyz.sum(axis=1), dst_xyz.sum(axis=1)
    gain = (_BRADFORD @ dst_white) / (_BRADFORD @ src_white)
    adapt = np.linalg.inv(_BRADFORD) @ np.diag(gain) @ _BRADFORD
    return np.linalg.inv(dst_xyz) @ adapt @ src_xyz


def packet_working_space(conn: sqlite3.Connection, packet_id: str | None) -> str:
    """The packet's declared `colour.working_space` when it is one an EXR can be tagged with, else the default
    (`display_only` Tier C packets, a packet that is not in the index, a manifest that cannot be read)."""
    if not packet_id:
        return DEFAULT_WORKING_SPACE
    try:
        row = conn.execute("SELECT path FROM packets WHERE packet_id = ?", (packet_id,)).fetchone()
        manifest = json.loads((Path(row[0]) / "manifest.json").read_text(encoding="utf-8-sig"))
        space = manifest["colour"]["working_space"]
    except (sqlite3.Error, TypeError, KeyError, OSError, ValueError):
        return DEFAULT_WORKING_SPACE
    return space if space in WORKING_SPACES else DEFAULT_WORKING_SPACE


def _write_exr(path: Path, rgb16: np.ndarray, working_space: str = DEFAULT_WORKING_SPACE) -> None:
    """The sRGB display image as scene-linear half float: the EOTF decode of what the gates scored, in the
    primaries of `working_space` and tagged with them (chromaticities, colorInteropID, plate:colourspace)."""
    from plate_pixels import exr as pexr
    from plate_pixels.ocio import srgb_eotf

    if working_space not in WORKING_SPACES:
        raise ValueError(f"working space {working_space!r}: use one of {', '.join(WORKING_SPACES)}")
    name, interop_id, chroma = WORKING_SPACES[working_space]
    lin = srgb_eotf(rgb16.astype(np.float64) / 65535.0)
    lin = (lin @ primaries_matrix(REC709_CHROMATICITIES, chroma).T).astype(np.float32)
    pexr.write_multilayer(
        path,
        {"RGBA": {"R": lin[..., 0], "G": lin[..., 1], "B": lin[..., 2]}},
        dtypes={"RGBA": "half"},
        header_attrs={
            "chromaticities": chroma,
            "colorInteropID": interop_id,
            "plate:colourspace": name,
            "plate:colour": f"scene-linear {name}, the sRGB EOTF applied to the gated display composite",
        },
    )


def _write_image(path: Path, fmt: str, rgb16: np.ndarray, working_space: str = DEFAULT_WORKING_SPACE) -> None:
    from plate_pixels import icc, imageio

    if fmt == "tiff16":
        imageio.write_tiff16(path, rgb16, icc=icc.SRGB_ICC)  # the pixels are sRGB-encoded: say so
    elif fmt == "png16":
        imageio.write_png16(path, rgb16)
    else:
        _write_exr(path, rgb16, working_space)


def _report_for(run: Run, project_dir: Path | None) -> tuple[dict[str, Any], str | None]:
    """(the run's gate report, its `gates/<run>.<n>.json` reference). Stored reports win over the run's summary."""
    if project_dir is not None:
        from .gate_reports import Service

        stored = Service(project_dir).latest(run.id)
        if stored is not None:
            return dict(stored.report), f"gates/{stored.path.name}"
    return dict(run.gate_report or {}), run.gate_report_path


def export(
    run: Run,
    fmt: str,
    out_dir: Path | str,
    override: str | None = None,
    *,
    conn: sqlite3.Connection,
    cas: CAS,
    project_dir: Path | str | None = None,
    name: str | None = None,
    working_space: str | None = None,
    full_frame: bool = False,
) -> Exported:
    """Write the run's result to `out_dir/<name>.<ext>` (+ `<name>.gate.json`); see the module docstring.

    `override` is the written reason (10 characters at least) that lets a GATE_FAIL run out; a reason recorded
    earlier on the run counts when none is given. `name` defaults to the run id. Raises `NotExportable` for a
    run that may not leave (GATE_FAIL without a reason, any state but DONE and GATE_FAIL, no result blob, a
    blob that fails its hash, a destination that exists, a safe frame that cannot be placed: `SafeFrameError`)
    and `ValueError` for an unknown format, name or working space. `working_space` (`lin_rec709` | `acescg`)
    tags an EXR; the default is the packet's declared one. The packet's safe frame is cropped out unless
    `full_frame`."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; use one of {', '.join(FORMATS)}")
    stem = name or run.id
    if not NAME_RE.match(stem):
        raise ValueError(
            f"export name {stem!r}: letters, digits, '.', '_' and '-' only (it is a Windows file name)"
        )
    if working_space is not None and working_space not in WORKING_SPACES:
        raise ValueError(f"working space {working_space!r}: use one of {', '.join(WORKING_SPACES)}")
    out_dir = Path(out_dir)
    project_dir = Path(project_dir) if project_dir is not None else None
    reason = override.strip() if override is not None else None
    reason = reason or None
    if run.state is RunState.GATE_FAIL:
        reason = reason or recorded_override(conn, run.id)
        if reason is None:
            raise NotExportable(
                f"run {run.id} failed its gates; export needs a written override reason of at least "
                f"{MIN_OVERRIDE_REASON} characters"
            )
        if len(reason) < MIN_OVERRIDE_REASON:
            raise NotExportable(
                f"a GATE_FAIL override needs a reason of at least {MIN_OVERRIDE_REASON} characters"
            )
    elif run.state is not RunState.DONE:
        raise NotExportable(
            f"run {run.id} is {run.state.value}; only DONE (or GATE_FAIL with override) exports"
        )
    key = run.outputs[0] if run.outputs else run.result_key
    if not key:
        raise NotExportable(f"run {run.id} has no result blob")
    if not cas.verify(key):
        raise NotExportable(f"result blob {key} is missing or fails its BLAKE3 check")
    dest = out_dir / f"{stem}{FORMATS[fmt]}"
    report_dest = out_dir / f"{stem}.gate.json"
    if dest.exists():
        raise NotExportable(f"{dest} already exists; exports never overwrite")
    earlier = _earlier_report(report_dest, run.id)
    rgb16 = _decode16(cas.read_bytes(key), f"result blob {key[:12]}")
    height, width = rgb16.shape[:2]
    frame = take_framing(conn, run.packet_id, (width, height), full_frame=full_frame)
    if earlier is not None and _framing_of(earlier) != frame.record():
        was, now = _framing_of(earlier) or "the whole take", frame.record() or "the whole take"
        raise NotExportable(
            f"{report_dest.name} describes run {run.id} framed as {was}; this export is {now}. "
            "One sidecar is one framing: use another name"
        )
    if frame.crop is not None and frame.cuts(width, height):
        x, y, w, h = frame.crop
        rgb16 = np.ascontiguousarray(rgb16[y : y + h, x : x + w])  # a pixel crop: nothing is resampled
    report, report_ref = _report_for(run, project_dir)
    lin = lineage.Lineage(
        project_id=run.project_id,
        node_id=run.node_id or "none",
        run_id=run.id,
        packet_id=run.packet_id,
        engine_id=run.route.engine_id,
        engine_version=str(run.route_dict.get("model_version") or "") or None,
        gate_report=report_ref,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    # the temp name keeps the real extension: the writers and `lineage.embed` pick their codec by it
    image_tmp = temp_sibling(dest).with_suffix(dest.suffix)
    try:
        _write_image(image_tmp, fmt, rgb16, working_space or packet_working_space(conn, run.packet_id))
        try:
            lineage.embed(image_tmp, lin)
        except (lineage.LineageError, OSError) as e:
            raise NotExportable(
                f"could not embed lineage in the {fmt} file ({e}); nothing was exported"
            ) from e
        doc = dict(report)
        doc["export"] = {
            "run_id": run.id,
            "files": [*((earlier or {}).get("files") or []), dest.name],
            "state": run.state.value,
            "override_reason": reason if run.state is RunState.GATE_FAIL else None,
            "gate_report": report_ref,
            "exported_utc": (earlier or {}).get("exported_utc") or iso(utcnow()),
            **frame.record(),
        }
        if run.state is RunState.GATE_FAIL and recorded_override(conn, run.id) is None:
            append_note(conn, run.id, kind="override", actor="human", reason=f"{OVERRIDE_PREFIX}{reason}")
        write_json_atomic(report_dest, doc, indent=1)
        try:
            replace_with_retry(image_tmp, dest)
        except OSError:
            if earlier is None:  # a shared sibling written for another format stays
                report_dest.unlink(missing_ok=True)
            raise
    finally:
        image_tmp.unlink(missing_ok=True)
    return Exported(
        run_id=run.id,
        fmt=fmt,
        path=dest,
        report_path=report_dest,
        overridden=run.state is RunState.GATE_FAIL,
        override_reason=reason if run.state is RunState.GATE_FAIL else None,
        lineage_embedded=True,
        report_ref=report_ref,
        framing=frame,
    )
