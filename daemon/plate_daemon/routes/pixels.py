"""Pixel-level routes the Mask editor calls: `POST /pixels/idpick|segment|ocr` (router plan T62).

- `idpick {packet_id, x, y, kind?: "object"|"material"}` is answered entirely in the daemon, from the packet's
  own Cryptomatte: the id at the pixel is the rank-0 id when its coverage is at least 0.5, else the rank with
  the highest coverage there. The answer is `{id: "o:<8hex>"|"m:<8hex>", name, class, kind, editable, locked,
  coverage, depth_m}`: `name` and `class` come from the packet's id legend, `editable` / `locked` from its
  policy, `depth_m` from the Z element at the pixel times `units.meters_per_unit` (null on a background pixel;
  a Tier C packet has no Cryptomatte and answers 422 `{"error": "no_cryptomatte"}`). A pixel nothing covers
  answers with `id: null`.
- `segment {packet_id, points?: [[x, y], ...]}` runs the pixels worker's segmenter on the packet's display
  master and stores the 16-bit label PNG in the project CAS as `labels_hash`. The clicked segments are the
  labels under the points (`selected`, label 0 is no segment and a point off the plate selects nothing); their
  union is an 8-bit grey 0/255 PNG at the plate's size, stored as `mask_hash`, which is what a Mask node's
  `samHash` names (router plan T82). `{mask_hash, labels_hash, segments, selected, shape, stub}`; with no
  points, or none on a segment, `mask_hash` is null. `stub` is true when the answer came from a stub backend,
  which is never a measurement.
- `ocr {packet_id}` runs the worker's text detector on the display master: `{boxes: [{bbox: [x0, y0, x1, y1],
  text, conf}], shape: [h, w], stub}`.

Nothing here leaves the machine: the worker is on loopback (`plate_daemon.pixels_client`). An unknown packet is
404; a worker that is not running is 503 `{"error": "pixels_unavailable"}` (set `PLATE_PIXELS_INPROC=1` to run
the models in this process).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..pipeline import PacketView, PipelineError
from ..pixels_client import MAX_POINTS, PixelsClient, PixelsError, PixelsUnavailable
from . import API, ApiError, Ctx, ctx, packet_project, pipeline_error, require_token

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

PICK_COVERAGE = 0.5  # rank 0 wins outright from this coverage up
NO_SURFACE = 1.0e9  # renderers write ~1e10 where nothing was hit: at or above this is not a depth
Z_ELEMENT = "zdepth"


class IdpickBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    packet_id: str = Field(min_length=1)
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    kind: Literal["object", "material"] = "object"


class SegmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    packet_id: str = Field(min_length=1)
    points: list[tuple[float, float]] | None = Field(default=None, max_length=MAX_POINTS)

    @field_validator("points")
    @classmethod
    def finite(cls, points: list[tuple[float, float]] | None) -> list[tuple[float, float]] | None:
        if points and not all(math.isfinite(v) for point in points for v in point):
            raise ValueError("points must be finite numbers")
        return points


class OcrBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    packet_id: str = Field(min_length=1)


# ---------------------------------------------------------------------------- shared steps


def pixels_client(request: Request) -> PixelsClient:
    """The app's client (`app.state.pixels`, which `create_app` may set), else one built from the settings.
    In-process models are shared with the Tier C route through `app.state.pixels_backends`."""
    state = request.app.state
    client = getattr(state, "pixels", None)
    if client is None:
        client = state.pixels = PixelsClient(state.settings, backends=getattr(state, "pixels_backends", None))
    if client.inproc and getattr(state, "pixels_backends", None) is None:
        state.pixels_backends = client.backends
    return client


def packet_view(c: Ctx, packet_id: str) -> tuple[str, PacketView]:
    """(project id, the packet as the pipeline reads it); 404 for an unknown packet, 409 when not ingested."""
    project_id = packet_project(c, packet_id)
    try:
        return project_id, c.pipeline.packet(project_id, packet_id)
    except PipelineError as e:
        raise pipeline_error(e) from e
    except (OSError, ValueError) as e:  # the share went away, or the folder was edited, after the ingest
        raise ApiError(
            409, {"error": "packet_unreadable", "packet_id": packet_id, "detail": f"{type(e).__name__}: {e}"}
        ) from e


def plate_path(pv: PacketView) -> Path:
    try:
        path = pv.root / pv.files[pv.plate_role]["path"]
    except PipelineError as e:
        raise pipeline_error(e) from e
    if not path.is_file():
        raise ApiError(
            409,
            {"error": "packet_unreadable", "packet_id": pv.packet_id, "detail": f"{path.name} is missing"},
        )
    return path


def plate_size(pv: PacketView) -> tuple[int, int]:
    """(H, W) of the packet's display master: the manifest's own numbers, else the decoded plate's shape."""
    entry = pv.files.get(pv.plate_role) or {}
    w, h = entry.get("width"), entry.get("height")
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        return h, w
    shape = pv.plate16().shape
    return int(shape[0]), int(shape[1])


def pixels_failure(e: PixelsError) -> ApiError:
    """The worker's status codes as the daemon's: 400 is the caller's input, 401/403 a token mismatch."""
    if e.status == 400:
        return ApiError(422, {"errors": [e.detail]})
    if e.status in (401, 403):
        return ApiError(
            502,
            {
                "error": "pixels_auth",
                "detail": "the pixels worker refused the daemon's token: start both with the same PLATE_HOME / "
                "PLATE_TOKEN_FILE",
            },
        )
    if e.model_unavailable:
        return ApiError(503, {"error": "model_unavailable", "detail": e.detail})
    if e.status == 504:
        return ApiError(504, {"error": "pixels_timeout", "detail": e.detail})
    code = 503 if e.status == 503 else 502
    return ApiError(code, {"error": "pixels_failed", "status": e.status, "detail": e.detail})


def pixels_unavailable(e: PixelsUnavailable) -> ApiError:
    return ApiError(503, {"error": "pixels_unavailable", "detail": str(e)})


# ---------------------------------------------------------------------------- idpick


def pick_rank(ids: np.ndarray, coverage: np.ndarray) -> int | None:
    """Index of the rank that names the pixel, or None when nothing covers it.

    `ids` and `coverage` are the pixel's (levels,) vectors. Rank 0 wins at coverage >= 0.5 (the dominant id);
    below that the rank with the highest coverage does, so an edge pixel names its larger contributor even
    when a renderer did not sort its ranks. An unused rank (id 0 or no coverage) never wins."""
    live = (np.asarray(ids) != 0) & (np.asarray(coverage) > 0)
    if not live.any():
        return None
    if live[0] and coverage[0] >= PICK_COVERAGE:
        return 0
    return int(np.argmax(np.where(live, coverage, -np.inf)))


def legend_entry(pv: PacketView, cid: str) -> dict[str, Any] | None:
    for entry in (pv.legend or {}).get("entries") or []:
        if isinstance(entry, dict) and entry.get("id") == cid:
            return entry
    return None


def z_plane(pv: PacketView) -> np.ndarray | None:
    """The packet's Z element as float32 (H, W), read once per loaded packet; None when it has none."""
    cache = pv._cache  # PacketView's own decode cache: the packet is immutable, so the plane is too
    if "pixels_z" not in cache:
        plane = None
        exr = pv.files.get("beauty_exr")
        if exr is not None:
            from plate_pixels import exr as pexr

            try:
                plane = pexr.read_elements(pv.root / exr["path"], pv.manifest, [Z_ELEMENT])[Z_ELEMENT][..., 0]
            except (pexr.MissingChannel, OSError, ValueError):
                plane = None
        cache["pixels_z"] = plane
    return cache["pixels_z"]


def depth_m_at(pv: PacketView, x: int, y: int) -> float | None:
    z = z_plane(pv)
    if z is None:
        return None
    value = float(z[y, x])
    if not math.isfinite(value) or value <= 0.0 or value >= NO_SURFACE:
        return None
    mpu = float(((pv.manifest.get("units") or {}).get("meters_per_unit")) or 1.0)
    return value * mpu


@router.post("/pixels/idpick")
def post_idpick(request: Request, body: IdpickBody) -> dict[str, Any]:
    c = ctx(request)
    _project_id, pv = packet_view(c, body.packet_id)
    try:
        pair = pv.crypto(body.kind)
    except (OSError, ValueError, LookupError) as e:
        raise ApiError(
            409,
            {"error": "packet_unreadable", "packet_id": body.packet_id, "detail": f"{type(e).__name__}: {e}"},
        ) from e
    if pair is None:
        raise ApiError(422, {"error": "no_cryptomatte"})
    ranks, coverage = pair
    height, width = ranks.shape[:2]
    x, y = math.floor(body.x), math.floor(body.y)
    if not (0 <= x < width and 0 <= y < height):
        raise ApiError(
            422,
            {
                "error": "out_of_range",
                "errors": [f"pixel ({body.x:g}, {body.y:g}) is outside the {width}x{height} plate"],
                "shape": [height, width],
            },
        )
    prefix = "o" if body.kind == "object" else "m"
    rank = pick_rank(ranks[y, x], coverage[y, x])
    if rank is None:
        return {
            "id": None,
            "name": None,
            "class": None,
            "kind": body.kind,
            "editable": False,
            "locked": False,
            "coverage": 0.0,
            "depth_m": depth_m_at(pv, x, y),
        }
    cid = f"{prefix}:{int(ranks[y, x, rank]):08x}"
    entry = legend_entry(pv, cid)
    return {
        "id": cid,
        "name": entry.get("name") if entry else None,
        "class": entry.get("class") if entry else None,
        "kind": body.kind,
        "editable": cid in pv.editable_ids(),
        "locked": cid in pv.locked_ids(),
        "coverage": float(coverage[y, x, rank]),
        "depth_m": depth_m_at(pv, x, y),
    }


# ---------------------------------------------------------------------------- segment and ocr


def decode_labels(png: bytes) -> np.ndarray | None:
    """The worker's label map as a 2-D integer array (16-bit grey PNG); None when it is not one."""
    try:
        labels = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED) if png else None
    except cv2.error:
        return None
    if labels is None or labels.ndim != 2 or labels.dtype not in (np.uint8, np.uint16):
        return None
    return labels


def selected_labels(labels: np.ndarray, points: list[tuple[float, float]]) -> list[int]:
    """The segment ids under the points (pixel = floor of the point), ascending; 0 and off-plate points skipped."""
    height, width = labels.shape[:2]
    out: set[int] = set()
    for x, y in points:
        px, py = math.floor(x), math.floor(y)
        if 0 <= px < width and 0 <= py < height:
            value = int(labels[py, px])
            if value:
                out.add(value)
    return sorted(out)


def at_plate_size(labels: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """The label map on the plate's (H, W) pixel grid. A worker that answered at another size (a real model's own
    input size) is brought to the plate nearest-neighbour, so points and the mask land where the quote checks."""
    if labels.shape[:2] == size:
        return labels
    return cv2.resize(labels, (size[1], size[0]), interpolation=cv2.INTER_NEAREST)


def selection_mask_png(labels: np.ndarray, selected: list[int]) -> bytes:
    """8-bit grey PNG of the label map's size, 255 where the label is one of `selected`, else 0."""
    mask = np.where(np.isin(labels, np.asarray(selected, dtype=labels.dtype)), 255, 0).astype(np.uint8)
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(mask))
    if not ok:
        raise ApiError(502, {"error": "pixels_failed", "detail": "cv2 could not encode the segment mask"})
    return bytes(buf)


@router.post("/pixels/segment")
def post_segment(request: Request, body: SegmentBody) -> dict[str, Any]:
    c = ctx(request)
    project_id, pv = packet_view(c, body.packet_id)
    plate = plate_path(pv)
    points = [[x, y] for x, y in body.points] if body.points else None
    try:
        result = pixels_client(request).segment(plate, points)
        png = Path(result["labels_png_path"]).read_bytes()
    except PixelsUnavailable as e:
        raise pixels_unavailable(e) from e
    except PixelsError as e:
        raise pixels_failure(e) from e
    except OSError as e:
        raise ApiError(502, {"error": "pixels_failed", "detail": f"cannot read the label map: {e}"}) from e
    labels = decode_labels(png)
    if labels is None:
        raise ApiError(502, {"error": "pixels_failed", "detail": "the label map is not a grey PNG"})
    cas = c.cas_for(project_id)
    labels_hash = cas.put_bytes(png, "png")
    mask_hash, selected = None, []
    if body.points:
        try:
            size = plate_size(pv)
        except PipelineError as e:
            raise pipeline_error(e) from e
        except (OSError, ValueError) as e:
            raise ApiError(
                409,
                {
                    "error": "packet_unreadable",
                    "packet_id": body.packet_id,
                    "detail": f"{type(e).__name__}: {e}",
                },
            ) from e
        on_plate = at_plate_size(labels, size)
        selected = selected_labels(on_plate, body.points)
        if selected:
            mask_hash = cas.put_bytes(selection_mask_png(on_plate, selected), "png")
    return {
        "mask_hash": mask_hash,
        "labels_hash": labels_hash,
        "segments": result["segments"],
        "selected": selected,
        "shape": result["shape"],
        "stub": result["stub"],
    }


@router.post("/pixels/ocr")
def post_ocr(request: Request, body: OcrBody) -> dict[str, Any]:
    from plate_pixels.ocr import normalise_box

    c = ctx(request)
    _project_id, pv = packet_view(c, body.packet_id)
    plate = plate_path(pv)
    try:
        result = pixels_client(request).ocr(plate)
    except PixelsUnavailable as e:
        raise pixels_unavailable(e) from e
    except PixelsError as e:
        raise pixels_failure(e) from e
    try:
        boxes = [normalise_box(b) for b in result["boxes"]]
    except (ValueError, TypeError, AttributeError) as e:
        raise ApiError(
            502, {"error": "pixels_failed", "detail": f"the worker's boxes are malformed: {e}"}
        ) from e
    return {
        "boxes": [{**b, "bbox": list(b["bbox"])} for b in boxes],
        "shape": result["shape"],
        "stub": result["stub"],
    }
