"""Ingestion routes that BUILD a packet from a client's files and then register it like any other:

- `POST /projects/{id}/tierc {plate_path, shot_id, consent_record, labels?}`: a client's finished image with no
  3ds Max scene behind it becomes a Tier C packet (PACKET.md section 11). The pixels worker builds the folder
  (`POST /tierc` on 127.0.0.1:4611 with the daemon's own bearer token, or in this process when
  `PLATE_PIXELS_INPROC=1`; `PLATE_PIXELS_PORT` moves the port) and the daemon ingests it. Decision 10: no
  consent record, no packet: `428 {"error": "consent record required"}` before anything is read or built.
  The packet inherits the project's NDA flag. A worker that is not running answers 503, not a hang.
- `POST /projects/{id}/kit {folder, camera, plate_path, shot_id?}`: the client packet kit's output folder
  (EXR with Z, normals and Cryptomatte; camera JSON; legend) plus the client's plate becomes a Tier A packet
  through `plate_max.kit_ingest.kit_to_packet`. plate_max is stdlib-only, so the BLAKE3, the Cryptomatte
  coverage (decoded by `plate_pixels.exr`) and the 16-bit TIFF conversion are passed in from here.

Both write the new packet folder under `<packets dir>/<project_id>/` (so `POST /hash` may hash it), validate
it with `prepare_ingest` and answer 202 like `POST /packets`: `{packet_id, status, events_url, packet_dir,
tier}`; `status` is `ingested` when the daemon ingests synchronously (`PLATE_SYNC_INGEST=1`), else `ingesting`
and the packet's event stream says when it is done. Nothing here leaves the machine.

Refusals made before a pixel is decoded (router plan T137): a `shot_id` is a slug by `fullmatch` (a trailing
newline is not one); a plate whose header declares more than `TIERC_MAX_PIXELS` (100 000 000) is 422
`{"error": "too_many_pixels", "declared": [w, h], "limit": N}` (a 52 KB PNG can declare 4000 x 4000, and a 775 KB
one 16000 x 16000); a kit `camera` that is not 1 to 64 of `[A-Za-z0-9 ._-]`, or whose `<camera>_camera.json`
resolves outside the kit folder, is 422 `bad_camera`.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import __version__
from ..cas import hash_bytes, hash_file
from ..config import check_project_id
from ..ingest import PacketConflict, PacketInvalid, begin_ingest, get_packet, prepare_ingest
from ..projects import Project
from . import API, ApiError, Ctx, ctx, require_token

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

PIXELS_PORT = 4611  # plate_pixels.server.PORT; not imported so the daemon does not load the worker's app
PIXELS_PORT_ENV = "PLATE_PIXELS_PORT"
INPROC_ENV = "PLATE_PIXELS_INPROC"
PIXELS_TIMEOUT = httpx.Timeout(900.0, connect=5.0)
SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")  # always `fullmatch`: `$` would let a trailing "\n" in
CAMERA_NAME = re.compile(r"[A-Za-z0-9 ._-]{1,64}")
TOO_MANY_PIXELS = "too_many_pixels"
TIERC_KINDS = ("depth", "intrinsics", "segment")
MAX_CONSENT_CHARS = 2000


class TierCBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plate_path: str = Field(min_length=1)
    shot_id: str = Field(min_length=1)
    consent_record: str | None = None  # absent or blank is a 428, not a 422: it is the rule, not a typo
    labels: dict[int, dict[str, str]] | None = None


class KitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder: str = Field(min_length=1)
    camera: str = Field(min_length=1)
    plate_path: str = Field(min_length=1)
    shot_id: str | None = None


# ---------------------------------------------------------------------------- shared steps


def project_or_404(c: Ctx, project_id: str) -> Project:
    try:
        check_project_id(project_id)
    except ValueError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    project = c.pipeline.load_project(project_id)
    if project is None:
        raise ApiError(404, {"error": "not_found", "project_id": project_id})
    return project


def check_slug(what: str, value: str) -> str:
    if not SLUG.fullmatch(value):
        raise ApiError(422, {"errors": [f"invalid {what} {value!r}: expected a lower-case slug [a-z0-9_-]"]})
    return value


def existing_file(what: str, raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        raise ApiError(422, {"errors": [f"{what} must be an absolute path, got {raw!r}"]})
    if not p.is_file():
        raise ApiError(422, {"errors": [f"{what}: no such file {raw!r}"]})
    return p


def too_many_pixels(e: Any) -> ApiError:
    """The 422 for a `plate_pixels.imageio.TooManyPixels` (nothing was decoded)."""
    return ApiError(
        422,
        {
            "error": TOO_MANY_PIXELS,
            "detail": str(e),
            "declared": [e.width, e.height],
            "limit": e.limit,
        },
    )


def check_pixel_budget(plate: Path) -> None:
    """422 `too_many_pixels` when the plate's header declares more than `TIERC_MAX_PIXELS`. The header alone is
    read; a file whose header cannot be read passes here and is named by the decoder."""
    from plate_pixels import imageio
    from plate_pixels.tierc import TIERC_MAX_PIXELS

    try:
        imageio.check_pixel_budget(plate, TIERC_MAX_PIXELS)
    except imageio.TooManyPixels as e:
        raise too_many_pixels(e) from e
    except OSError:
        pass  # unreadable: the build says so


def check_camera(folder: Path, camera: str) -> str:
    """A kit camera name is a plain name, and the camera JSON the kit looks for (`<stem>_camera.json`, then
    `<camera>_camera.json`) stays inside the kit folder. 422 `bad_camera` otherwise."""
    from plate_max import kit_ingest

    if not CAMERA_NAME.fullmatch(camera):
        raise ApiError(
            422,
            {
                "error": "bad_camera",
                "detail": f"camera {camera!r} must be 1 to 64 of letters, digits, space, '.', '_' or '-'",
            },
        )
    root = folder.resolve()
    for name in dict.fromkeys((kit_ingest.kit_stem(camera), camera)):
        try:
            inside = (folder / f"{name}_camera.json").resolve().is_relative_to(root)
        except (OSError, ValueError):
            inside = False
        if not inside:
            raise ApiError(
                422,
                {
                    "error": "bad_camera",
                    "detail": f"the camera file for {camera!r} is outside the kit folder",
                },
            )
    return camera


def new_packet_folder(c: Ctx, project_id: str, prefix: str, shot_id: str) -> Path:
    """A folder that does not exist yet, under the first packets dir: `<packets>/<project>/<prefix>-<shot>-<hex>`."""
    folder = c.settings.packets_dirs[0] / project_id / f"{prefix}-{shot_id}-{secrets.token_hex(4)}"
    folder.parent.mkdir(parents=True, exist_ok=True)
    return folder


def register_packet_folder(
    c: Ctx, folder: Path, project_id: str, sender: str, background: BackgroundTasks, tier: str
) -> dict[str, Any]:
    """Validate and ingest a packet folder the daemon just had built, the way `POST /packets` does.
    Returns the 202 body."""
    try:
        prepared = prepare_ingest(folder)
    except PacketInvalid as e:
        raise ApiError(422, {"error": "built_packet_invalid", "errors": e.errors}) from e
    if prepared.project_id != project_id:
        raise ApiError(
            500,
            {
                "error": "packet_project_mismatch",
                "detail": f"the built packet names project {prepared.project_id!r}, not {project_id!r}",
            },
        )
    packet_id = prepared.packet_id
    with c.index.project(project_id) as conn:
        try:
            status, _rec = begin_ingest(
                conn,
                str(folder),
                sender={"name": sender, "version": __version__},
                prepared=prepared,
                now=c.now(),
            )
        except PacketConflict as e:
            raise ApiError(409, {"error": "conflict", "packet_id": packet_id, "detail": str(e)}) from e
    c.index.remember(packet_id, project_id)
    if status != "known":
        if c.settings.sync_ingest:
            c.finish_packet(project_id, packet_id)
        else:
            background.add_task(c.finish_packet, project_id, packet_id)
    with c.index.project(project_id) as conn:
        now_status = get_packet(conn, packet_id, with_blobs=False)["status"]
    return {
        "packet_id": packet_id,
        "status": now_status,
        "tier": tier,
        "packet_dir": str(folder),
        "events_url": f"{API}/packets/{packet_id}/events",
    }


# ---------------------------------------------------------------------------- Tier C


def _pixels_failure(status: int, body: Any) -> ApiError:
    detail = body.get("error") if isinstance(body, dict) else None
    detail = detail if isinstance(detail, str) else f"the pixels worker answered {status}"
    if status == 428:
        return ApiError(428, {"error": "consent record required"})
    if status == 400:
        # plate_pixels.imageio.TooManyPixels, across the process line
        if detail.startswith(f"{TOO_MANY_PIXELS}:"):
            return ApiError(422, {"error": TOO_MANY_PIXELS, "detail": detail})
        return ApiError(422, {"errors": [detail]})
    if status == 409:
        return ApiError(409, {"error": "exists", "detail": detail})
    if status in (401, 403):
        return ApiError(
            502,
            {
                "error": "pixels_auth",
                "detail": "the pixels worker refused the daemon's token: start both with the same PLATE_HOME / "
                "PLATE_TOKEN_FILE",
            },
        )
    code = 503 if status == 503 else 502
    return ApiError(code, {"error": "pixels_failed", "status": status, "detail": detail})


def _tierc_http(c: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    port = int(os.environ.get(PIXELS_PORT_ENV) or PIXELS_PORT)
    try:
        # Loopback only: the URL is built from the literal 127.0.0.1, so this is not vendor egress. One-shot
        # `httpx.post`, and trust_env=False so an HTTP(S)_PROXY in the environment never sees the plate path.
        r = httpx.post(
            f"http://127.0.0.1:{port}/tierc",
            json=args,
            headers={"Authorization": f"Bearer {c.settings.token}"},
            timeout=PIXELS_TIMEOUT,
            trust_env=False,
        )
    except httpx.ConnectError as e:
        raise ApiError(
            503,
            {
                "error": "pixels_unavailable",
                "detail": f"the pixels worker is not reachable on 127.0.0.1:{port} ({e}); start it with "
                f"`python -m plate_pixels` or set {INPROC_ENV}=1",
            },
        ) from e
    except httpx.TimeoutException as e:
        raise ApiError(
            504, {"error": "pixels_timeout", "detail": str(e) or "the pixels worker timed out"}
        ) from e
    except httpx.HTTPError as e:
        raise ApiError(502, {"error": "pixels_failed", "detail": f"{type(e).__name__}: {e}"}) from e
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.status_code != 200 or not isinstance(body, dict):
        raise _pixels_failure(r.status_code, body)
    return body


def _tierc_inproc(request: Request, args: dict[str, Any]) -> dict[str, Any]:
    from plate_pixels.backends import Backends, InputError, ModelUnavailable
    from plate_pixels.imageio import TooManyPixels
    from plate_pixels.tierc import ConsentRequired, TierCError, build_tier_c_packet

    backends = getattr(request.app.state, "pixels_backends", None)
    if backends is None:
        backends = request.app.state.pixels_backends = Backends()
    try:
        packet = build_tier_c_packet(
            args["plate_path"],
            args["out_dir"],
            backends,
            args["project_id"],
            args["shot_id"],
            args["consent_record"],
            labels=args["labels"],
            nda=args["nda"],
        )
    except ConsentRequired as e:
        raise ApiError(428, {"error": "consent record required"}) from e
    except TooManyPixels as e:
        raise too_many_pixels(e) from e
    except FileExistsError as e:
        raise ApiError(409, {"error": "exists", "detail": str(e)}) from e
    except ModelUnavailable as e:
        raise ApiError(503, {"error": "model_unavailable", "detail": str(e)}) from e
    except TierCError as e:
        raise ApiError(500, {"error": "tierc_invalid", "errors": e.errors}) from e
    except (InputError, ValueError) as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8-sig"))
    return {
        "stub": any(backends.get(k).is_stub for k in TIERC_KINDS),
        "packet_dir": str(packet),
        "packet_id": manifest["packet_id"],
        "tier": manifest["tier"],
    }


@router.post("/projects/{project_id}/tierc", status_code=202)
def post_tierc(
    request: Request, project_id: str, body: TierCBody, background: BackgroundTasks
) -> JSONResponse:
    c = ctx(request)
    consent = (body.consent_record or "").strip()
    if not consent:
        raise ApiError(428, {"error": "consent record required"})
    if len(consent) > MAX_CONSENT_CHARS:
        raise ApiError(422, {"errors": [f"consent_record is at most {MAX_CONSENT_CHARS} characters"]})
    project = project_or_404(c, project_id)
    shot_id = check_slug("shot_id", body.shot_id)
    plate = existing_file("plate_path", body.plate_path)
    check_pixel_budget(plate)
    out_dir = new_packet_folder(c, project_id, "tierc", shot_id)
    args = {
        "plate_path": str(plate),
        "out_dir": str(out_dir),
        "project_id": project_id,
        "shot_id": shot_id,
        "consent_record": consent,
        "labels": body.labels,
        "nda": project.nda,
    }
    try:
        if os.environ.get(INPROC_ENV) == "1":
            built = _tierc_inproc(request, args)
        else:
            built = _tierc_http(c, {k: v for k, v in args.items() if v is not None})
        if Path(str(built.get("packet_dir", ""))) != out_dir:
            raise ApiError(
                502,
                {
                    "error": "pixels_failed",
                    "detail": f"the worker built {built.get('packet_dir')!r}, not {str(out_dir)!r}",
                },
            )
        payload = register_packet_folder(c, out_dir, project_id, "plate-daemon/tierc", background, "C")
    except ApiError:
        shutil.rmtree(out_dir, ignore_errors=True)  # a half-built folder under the packets dir helps nobody
        raise
    return JSONResponse({**payload, "stub": bool(built.get("stub"))}, status_code=202)


# ---------------------------------------------------------------------------- the client packet kit


def kit_hasher(source: Any) -> str:
    """BLAKE3 of a `Path` (streamed) or of bytes: what `plate_max.kit_ingest` asks the daemon for."""
    if isinstance(source, bytes | bytearray):
        return hash_bytes(bytes(source))
    return hash_file(Path(source))[0]


def object_pixel_shares(exr_path: Path | str, elements: list[dict[str, Any]]) -> dict[str, float]:
    """`{"o:<8 hex>": share}`: the share of the frame each object id covers at rank 0, from the EXR's own
    Cryptomatte pixels (coverage comes from the render, never from the kit's legend)."""
    import numpy as np

    from plate_pixels import exr as pexr

    manifest = {"files": {"beauty_exr": {"elements": elements}}}
    ids, cov = pexr.read_cryptomatte(exr_path, manifest, "object")
    first, weight = ids[..., 0], cov[..., 0]
    return {
        f"o:{int(v):08x}": float(weight[first == v].sum() / first.size) for v in np.unique(first) if v != 0
    }


def plate_to_tiff16(src: Path | str, dst: Path | str) -> None:
    """Any 8- or 16-bit RGB(A) image to a 16-bit TIFF of the same size (8-bit values are promoted by x257).
    A plate whose header declares more than `TIERC_MAX_PIXELS` is refused (`TooManyPixels`) before it is decoded."""
    import cv2
    import numpy as np
    import tifffile

    from plate_pixels import imageio
    from plate_pixels.tierc import TIERC_MAX_PIXELS

    imageio.check_pixel_budget(src, TIERC_MAX_PIXELS)
    img = cv2.imread(str(src), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"cannot read the plate {str(src)!r}")
    if img.ndim == 2:
        img = np.repeat(img[..., None], 3, axis=2)
    if img.shape[2] not in (3, 4):
        raise ValueError(f"the plate has {img.shape[2]} channels: expected grey, RGB or RGBA")
    img = img[..., [2, 1, 0] + ([3] if img.shape[2] == 4 else [])]  # BGR(A) to RGB(A)
    if img.dtype == np.uint8:
        img = img.astype(np.uint16) * 257
    elif img.dtype != np.uint16:
        raise ValueError(f"the plate is {img.dtype}: only 8- and 16-bit images become a display master")
    tifffile.imwrite(str(dst), np.ascontiguousarray(img), photometric="rgb")


@router.post("/projects/{project_id}/kit", status_code=202)
def post_kit(request: Request, project_id: str, body: KitBody, background: BackgroundTasks) -> JSONResponse:
    c = ctx(request)
    project = project_or_404(c, project_id)
    folder = Path(body.folder)
    if not folder.is_absolute() or not folder.is_dir():
        raise ApiError(
            422, {"errors": [f"folder must be an absolute path to the kit's output, got {body.folder!r}"]}
        )
    plate = existing_file("plate_path", body.plate_path)
    shot_id = check_slug("shot_id", body.shot_id) if body.shot_id else None
    camera = check_camera(folder, body.camera)
    check_pixel_budget(plate)

    from plate_max import kit_ingest  # stdlib-only plugin module: imported when a kit arrives
    from plate_pixels import imageio

    out_dir = new_packet_folder(c, project_id, "kit", shot_id or "shot")
    custody = {"nda": True, "allowed_grades": project.allowed_grades} if project.nda else None
    try:
        result = kit_ingest.convert_kit(
            folder,
            camera,
            plate,
            hasher=kit_hasher,
            pixel_shares=object_pixel_shares,
            plate_to_tiff16=plate_to_tiff16,
            project_id=project_id,
            shot_id=shot_id,
            out_dir=out_dir,
            custody=custody,
        )
    except kit_ingest.KitError as e:
        raise ApiError(422, {"error": "kit_invalid", "errors": [str(e)]}) from e
    except imageio.TooManyPixels as e:
        raise too_many_pixels(e) from e
    except (OSError, ValueError) as e:
        raise ApiError(422, {"error": "kit_invalid", "errors": [f"{type(e).__name__}: {e}"]}) from e
    try:
        payload = register_packet_folder(
            c, result.packet_dir, project_id, "plate-daemon/kit", background, "A"
        )
    except ApiError:
        shutil.rmtree(result.packet_dir, ignore_errors=True)
        raise
    return JSONResponse({**payload, "flags": result.flags, "warnings": result.warnings}, status_code=202)
