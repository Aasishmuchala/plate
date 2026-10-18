"""External files imported as runs (router plan T74, decision 42).

A take made outside PLATE (Higgsfield, Runway, or by hand) comes back as a run of its own, so it is gated,
ledgered, reviewed and exported like any other take:

    import_file(pipeline, project_id, packet_id, path, *, source, node_id=None, cost_usd=0, note=None,
                by="human", approved_still_hash=None, end_still_hash=None,
                motion_packet_id=None) -> Run                           DONE, GATE_FAIL (or FAILED)

What it checks before anything is stored, in this order (a refusal leaves no run row and no CAS object):

- `source` is `higgsfield`, `runway` or `manual` (422 `unknown_source`), `cost_usd` a finite number from 0 to
  `MAX_COST_USD` (1000; 422 `bad_cost`) and `by` an approver (`human`, `auto`, `agent:<envelope_id>`), else 422;
- the path is a regular file (`check_import_file`: a FIFO, a device, a socket or a directory is 422 `not_a_file`
  and is never opened, a missing one 422 `unreadable_image` / `unreadable_video`) of at most `IMPORT_MAX_BYTES`
  (1 GiB; 413 `too_large`);
- the project and the packet exist (404 `not_found`), and so does `node_id` when one is named (404);
- custody: the import's route must be a grade the project and the packet allow (`custody.require_route`). A
  Higgsfield or Runway file is grade D (its plate already went to that vendor), so on an NDA project it is
  `CustodyViolation` (403); a manual file is C0;
- the file decodes as a display-referred image (422 `unreadable_image`; a scene-linear file is refused too: the
  gates score display masters) at exactly the plate's size (422 `size_mismatch`: nothing is resized or
  registered onto the plate here; the gates measure the file as it is). The size is read from the header first
  (`plate_pixels.imghdr`): a mismatch is refused before a pixel is decoded, so a 117 KiB 6000 x 6000 PNG costs the
  daemon no memory, and a header over `IMPORT_MAX_PIXELS` (100 000 000) is 422 `too_many_pixels`. A JPEG's EXIF
  Orientation is applied by the decoder, so a portrait JPEG whose header says landscape is judged upright.

The run. Route `{engine_id: "import:<source>", provider: "external", custody_grade: D | C0, custody_status:
"confirmed", upload_visibility: "unknown" | "local"}`; inputs `{plate, mask, imported: {path_name, source, note,
blake3}, staging}`, where `mask` is what `Pipeline.mask_spec` reads for `node_id` (its nearest upstream Mask
node, else the packet's policy), `imported.blake3` the raw file's key, and `staging` the pipeline's marker with
`node_kind: "Edit", imported: true`, so `POST /runs/{id}/gate/rescore` scores an import again like an Edit take
and a daemon stopped mid-import has `Pipeline.resume` fail the run (no engine speaks `import:*`) instead of
leaving it in flight. A node's snapshot rides in `inputs.subgraph` as a quote's does. The attempt is numbered
by decision 40: 1 + the highest attempt of earlier runs of the same node (or of no node) whose inputs (less
`subgraph` and `router`), params and route are the same, so a second import of one file is attempt 2.

The moves, each published on the bus as `run.state`: QUOTED at the declared price (`cost_usd`,
`price_status: "declared"`), APPROVED by `by` (the budget rules of `quotes.approve`), QUEUED, STAGING,
SUBMITTED (the provider job id is `import-<blake3[:24]>`), RUNNING, FETCHING, NORMALIZING (the raw file is the
run's `result_key` and `normalized.raw_key`; a 16-bit PNG of it with the run's lineage embedded is
`outputs[0]`, its perceptual hash recorded; the editable mask is stored as `normalized.mask_key`), GATING
(`gating.run_gates` with the arguments `Pipeline.gate` passes: the packet's plate, the editable mask, its
G-buffer edges and material Cryptomatte; an import has no ingress report, so the gate measures the
registration itself and a moved camera shows as `camera_drift`), then DONE or GATE_FAIL with the report written
by `gate_reports.Service`. `quoted` (with `cost_usd_quoted`), `approved` and `done` / `gated` (and `failed` or
`cancelled` when it comes to that) are ledgered with `data.imported: true`; `quoted` and `approved` fail
closed (503 `ledger_unavailable`, the run cancelled) like every quote. Nothing leaves the box, so the outbound
log is never written. Refusals are `PipelineError` (its `code` and `status` are the HTTP answer) or
`CustodyViolation`.

A clip (router plan T87). A `.mp4`, `.mov` or `.webm` file is imported the same way, along the same run (route,
custody, quote, approval, moves, ledger), with these differences:

- `delivery.tools.probe_video` reads width, height, fps and frame count (422 `unreadable_video`, 503
  `tool_missing` without ffprobe). The size must equal the plate's, or, when `motion_packet_id` names a motion
  packet of the project, that packet's `motion.sequence_resolution` (422 `size_mismatch`, with `expected`);
  nothing is resized. `approved_still_hash`, `end_still_hash` and `motion_packet_id` apply to a clip only (422
  `not_a_clip` on a still); a still hash names an image in the project's CAS (404 `not_found`, 422
  `unreadable_image`).
- A clip has at least 2 frames (422 `unreadable_video`: a single frame cannot be scored for camera lock or
  flicker), and the container comes from ffprobe's `format_name`, not the extension: a PNG renamed `.mp4` is "a
  still with a clip suffix" (422 `unreadable_video`).
- The raw file goes into the CAS with its extension (`result_key` = `normalized.raw_key`); `outputs[0]` is a copy
  with the run's lineage in the container (`.mp4` and `.mov`; `.webm` has no carrier, so the copy is the raw file
  and `normalized.lineage` is false). A clip has no editable mask and no ingress report: `inputs` carries no
  `mask`, `normalized` no `mask_key`, so `POST /runs/{id}/gate/rescore` answers 409 `not_rescorable`.
- GATING is the video gate assembly a Kling take is scored with (`plate_gates.video_gates.run`): the first frame
  against `approved_still_hash` (default: the plate), the last frame against `end_still_hash` (none: not
  scored), camera lock (as for a clip with no motion packet unless one is named), flicker, output tags and, with
  `motion_packet_id`, reprojection drift against that packet. No shot length is declared, so duration is not
  scored. The report is `gates/<run_id>.<n>.json`; the run's `gate_report` also carries `landing` and `timemap`.
- `normalized = {media: "video", probe, timemap, outputs, raw_key, start_still, end_still, ...}`; the time map is
  the identity at the probed fps (an import is not retimed). Ledger records carry `data.media: "video"`.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from plate_gates import io as pio
from plate_gates import video_gates
from plate_gates.timemap import TimeMap, fps_text
from plate_pixels import imghdr

from . import budgets, canvas_rules, gating
from . import lineage as lineage_mod
from .cas import CAS, CASError, check_key, normalize_ext
from .config import check_project_id
from .custody import require_route
from .db import loads, tx
from .delivery.guard import DeliveryError, ToolMissing
from .delivery.tools import PROBE_TIMEOUT_S, ffprobe_path, probe_video
from .gate_reports import Service as ReportService
from .machine import DuplicateRequest, IllegalTransition, annotate, create_run, get_run, transition
from .models import APPROVED_BY_RE, QUOTE_TTL, Quote, Run, RunCreate
from .pipeline import (
    LEDGER_KINDS,
    STAGING_KEY,
    PacketView,
    Pipeline,
    PipelineError,
    decode_png16,
    encode_png16,
    repeat_canonical,
    subgraph_snapshot,
)
from .quotes import AutoApprovalDenied, EnvelopeRequired, QuoteExpired, QuoteNotFound, insert_quote
from .quotes import approve as approve_one
from .states import Priority, RunState

__all__ = [
    "ENGINE_PREFIX",
    "IMPORT_MAX_BYTES",
    "IMPORT_MAX_PIXELS",
    "MAX_COST_USD",
    "SOURCES",
    "VIDEO_EXTS",
    "check_import_file",
    "import_file",
    "import_route",
    "is_import_run",
    "next_attempt",
]

log = logging.getLogger(__name__)
S = RunState

ENGINE_PREFIX = "import:"
# source -> the custody grade of the file: a vendor's output means the plate already went to that vendor.
SOURCES: dict[str, str] = {"higgsfield": "D", "runway": "D", "manual": "C0"}
VIDEO_EXTS = frozenset({".mp4", ".mov", ".webm"})
ACTOR = "import"
MAX_NOTE_CHARS = 500
JOB_ID_HEX = 24
RAW_FALLBACK_EXT = "bin"
IMPORT_MAX_BYTES = 1 << 30  # a file larger than this is 413 `too_large`
# the declared width x height a still may ask a decoder for (as assets.UPLOAD_MAX_PIXELS)
IMPORT_MAX_PIXELS = 100_000_000
MAX_COST_USD = 1000.0  # a declared cost above this is a typo or a probe, not an import
HEADER_PROBE_BYTES = 1 << 20  # the first MiB of a still holds its header (a JPEG's EXIF and ICC come first)
TEMP_SUFFIX_CHARS = 16
MIN_CLIP_FRAMES = 2  # one frame cannot be scored for camera lock or flicker
# The moves an import ledgers (decision 42): the quote, the approval and the end, whatever it is.
LEDGERED = frozenset({S.QUOTED, S.APPROVED, S.DONE, S.GATE_FAIL, S.FAILED, S.CANCELLED})


def import_route(source: str) -> dict[str, Any]:
    """The route an import of `source` runs on (decision 42). Raises PipelineError 422 for an unknown source."""
    grade = SOURCES.get(source) if isinstance(source, str) else None
    if grade is None:
        raise PipelineError(
            f"unknown import source {source!r}; use one of {', '.join(SOURCES)}", code="unknown_source"
        )
    local = grade == "C0"
    return {
        "engine_id": f"{ENGINE_PREFIX}{source}",
        "provider": "external",
        "region": "local" if local else "external",
        "route_id": f"import-{source}",
        "custody_grade": grade,
        "custody_status": "confirmed",
        "upload_visibility": "local" if local else "unknown",
        "model_version": source,
    }


def is_import_run(run: Run) -> bool:
    return run.route.engine_id.startswith(ENGINE_PREFIX) and isinstance(run.inputs.get("imported"), Mapping)


def next_attempt(
    conn: Any,
    node_id: str | None,
    inputs: Mapping[str, Any],
    params: Mapping[str, Any],
    route: Mapping[str, Any],
) -> int:
    """Decision 40, as `Pipeline.next_attempt`, for a node or for no node (`node_id IS NULL`)."""
    want = repeat_canonical(inputs, params, route)
    best = 0
    for row in conn.execute("SELECT attempt, inputs, params, route FROM runs WHERE node_id IS ?", (node_id,)):
        try:
            same = repeat_canonical(loads(row[1]) or {}, loads(row[2]) or {}, loads(row[3]) or {}) == want
        except (TypeError, ValueError):
            continue
        if same:
            best = max(best, int(row[0] or 0))
    return best + 1


# ---------------------------------------------------------------------------- checks before anything is stored


def _check_args(source: Any, cost_usd: Any, by: Any, note: Any) -> tuple[dict[str, Any], float, str | None]:
    route = import_route(source)
    if isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float)):
        raise PipelineError(f"cost_usd must be a number of USD, got {cost_usd!r}", code="bad_cost")
    price = float(cost_usd)
    if not math.isfinite(price) or price < 0 or price > MAX_COST_USD:
        raise PipelineError(
            f"cost_usd must be a finite amount from 0 to {MAX_COST_USD:g} USD, got {cost_usd!r}",
            code="bad_cost",
        )
    if not isinstance(by, str) or not APPROVED_BY_RE.match(by):
        raise PipelineError(f"by must be human, auto or agent:<envelope_id>, got {by!r}", code="bad_approver")
    if note is not None and not isinstance(note, str):
        raise PipelineError("note must be text", code="bad_note")
    text = (note or "").strip()[:MAX_NOTE_CHARS] or None
    return route, price, text


def check_import_file(path: str | Path) -> int:
    """The size of `path`, which must be a regular file of at most `IMPORT_MAX_BYTES`. A FIFO, a device, a socket
    or a directory is 422 `not_a_file` (opening a FIFO with no writer blocks a worker thread for good; `stat`
    never opens it); a missing or unstat-able file is 422 `unreadable_image` (`unreadable_video` for a clip
    suffix); a larger one is 413 `too_large`."""
    file = Path(path)
    code = "unreadable_video" if file.suffix.lower() in VIDEO_EXTS else "unreadable_image"
    try:
        st = os.stat(file)
    except (OSError, ValueError) as e:  # missing, no permission, a name too long, an embedded NUL
        raise PipelineError(f"cannot read {file.name}: {e}"[:500], code=code) from e
    if not stat.S_ISREG(st.st_mode):
        raise PipelineError(
            f"{file.name} is not a regular file (a directory, FIFO, device or socket cannot be imported)",
            code="not_a_file",
        )
    if st.st_size > IMPORT_MAX_BYTES:
        raise PipelineError(
            f"{file.name} is {st.st_size} bytes; the largest import is {IMPORT_MAX_BYTES} bytes",
            code="too_large",
            status=413,
            size=st.st_size,
            limit=IMPORT_MAX_BYTES,
        )
    return st.st_size


def _packet(pl: Pipeline, project_id: str, packet_id: str) -> PacketView:
    try:
        return pl.packet(project_id, packet_id)
    except PipelineError as e:
        if e.code == "unknown_packet":
            raise PipelineError(str(e), code="not_found", status=404, packet_id=packet_id) from e
        raise


def _size_mismatch(name: str, plate: tuple[int, int], got: tuple[int, int]) -> PipelineError:
    return PipelineError(
        f"{name} is {got[0]} x {got[1]}; the plate is {plate[0]} x {plate[1]} (an import is never resized onto it)",
        code="size_mismatch",
        plate=list(plate),
        file=list(got),
    )


def _read_still(file: Path, plate: tuple[int, int]) -> bytes:
    """The bytes of a still, after its header was compared with the plate: a size that is not the plate's is
    `size_mismatch` and one over `IMPORT_MAX_PIXELS` is `too_many_pixels`, both before a pixel is decoded (a
    117 KiB PNG can declare 6000 x 6000). A JPEG may be the plate's size turned by its EXIF Orientation, so its
    swapped size passes here and the decoder's upright shape is judged afterwards. A file with no readable
    header passes to the decoder."""
    try:
        with file.open("rb") as fh:
            head = fh.read(HEADER_PROBE_BYTES)
        raw: bytes | None = None
        size = imghdr.header_size(head)
        if size is None and len(head) == HEADER_PROBE_BYTES:  # e.g. a TIFF whose first IFD is at the end
            raw = file.read_bytes()
            size = imghdr.header_size(raw)
        if size is not None:
            width, height = size
            if width * height > IMPORT_MAX_PIXELS:
                raise PipelineError(
                    f"{file.name} declares {width} x {height} = {width * height} px; the cap is {IMPORT_MAX_PIXELS}",
                    code="too_many_pixels",
                    declared=[width, height],
                    limit=IMPORT_MAX_PIXELS,
                )
            turned = imghdr.magic(head) == "jpeg" and (height, width) == plate
            if size != plate and not turned:
                raise _size_mismatch(file.name, plate, size)
        return file.read_bytes() if raw is None else raw
    except OSError as e:
        raise PipelineError(f"cannot read {file.name}: {e}"[:500], code="unreadable_image") from e


def _decode(raw: bytes, suffix: str, cache: Path) -> np.ndarray:
    """uint16 RGB (H, W, 3) of a display-referred image file's bytes (8-bit files are promoted by x257, as
    `PacketView.plate16` does). Raises PipelineError 422 `unreadable_image`."""
    from plate_pixels import imageio

    cache.mkdir(parents=True, exist_ok=True)
    # The suffix only picks the decoder (.tif/.tiff or not): keep letters, digits and dots, at most 16 characters,
    # so a 250-character extension is no "File name too long" and a ':' is no Windows alternate stream.
    suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:TEMP_SUFFIX_CHARS]
    # decoded from these bytes, the ones that go into the CAS (never a second read of a file that may change)
    with tempfile.TemporaryDirectory(dir=cache) as tmp:
        p = Path(tmp) / f"import{suffix or '.bin'}"
        try:
            p.write_bytes(raw)
            img = imageio.read(p)
        except Exception as e:  # any decoder or temp-file failure is the same answer: not an image we read
            raise PipelineError(
                f"not a readable image ({type(e).__name__}: {e})"[:500], code="unreadable_image"
            ) from e
    if img.encoding != "srgb":
        raise PipelineError(
            "the file is scene-linear: an import is a display-referred take (PNG, TIFF or JPEG)",
            code="unreadable_image",
        )
    rgb = np.asarray(img.rgb)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise PipelineError(f"expected an RGB image, got shape {rgb.shape}", code="unreadable_image")
    scale = 65535.0 if img.bit_depth >= 16 else 255.0
    arr = np.clip(np.rint(np.nan_to_num(rgb, nan=0.0) * scale), 0, scale).astype(np.uint16)
    return arr if scale == 65535.0 else arr * np.uint16(257)


def _raw_ext(path: Path) -> str:
    try:
        return normalize_ext(path.suffix or RAW_FALLBACK_EXT)
    except CASError:
        return RAW_FALLBACK_EXT


# ---------------------------------------------------------------------------- a clip (T87)


@dataclass
class _Clip:
    """A video import checked before anything is stored: what the gates and `normalized` need."""

    probe: dict[str, Any]
    start: np.ndarray  # RGB uint16, the first-frame reference (the plate, or the approved still)
    end: np.ndarray | None
    start_key: str | None  # the CAS key named by approved_still_hash (None: the plate)
    end_key: str | None
    motion: PacketView | None

    @property
    def fps(self) -> Fraction:
        return Fraction(self.probe["fps"])

    @property
    def size(self) -> tuple[int, int]:
        return int(self.probe["width"]), int(self.probe["height"])


def _frame_rate(stream: Mapping[str, Any]) -> Fraction:
    """The clip's frame rate: the average when ffprobe has one (a webm's r_frame_rate can be the timebase),
    else `probe_video`'s `fps`."""
    try:
        avg = Fraction(str(stream.get("avg_frame_rate") or "0/1"))
    except (ValueError, ZeroDivisionError):
        avg = Fraction(0)
    return avg if avg > 0 else Fraction(stream.get("fps") or 0)


def _is_still_container(format_name: str) -> bool:
    """ffprobe's `format_name` of a still image: `png_pipe`, `jpeg_pipe`, `tiff_pipe`, `webp_pipe`, `bmp_pipe`
    and the `image2` demuxer. A clip's is `mov,mp4,m4a,3gp,3g2,mj2` or `matroska,webm`."""
    return any(t == "image2" or t == "image2pipe" or t.endswith("_pipe") for t in format_name.split(","))


def _check_container(path: Path) -> None:
    """The container is ffprobe's `format_name`, not the file's extension: a still with a clip suffix (a PNG
    renamed `.mp4`) is 422 `unreadable_video`. 503 `tool_missing` without ffprobe."""
    try:
        proc = subprocess.run(
            [
                ffprobe_path(),
                "-v",
                "error",
                "-show_entries",
                "format=format_name",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except ToolMissing as e:
        raise PipelineError(str(e), code="tool_missing", status=503) from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PipelineError(
            f"not a readable video ({type(e).__name__}: {e})"[:500], code="unreadable_video"
        ) from e
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-2:]
        raise PipelineError(
            f"not a readable video: ffprobe could not read {path.name}: {' | '.join(tail)}"[:500],
            code="unreadable_video",
        )
    try:
        format_name = str(
            (json.loads(proc.stdout.decode("utf-8")).get("format") or {}).get("format_name") or ""
        )
    except (ValueError, AttributeError):
        format_name = ""
    if _is_still_container(format_name):
        raise PipelineError(
            f"{path.name} is a still image ({format_name}) with a clip suffix: import it as a still, or give it "
            "a video container",
            code="unreadable_video",
        )


def _probe_clip(path: Path) -> dict[str, Any]:
    """width, height, fps, frames, duration and the codec tags of a clip, as plain JSON data."""
    if not path.is_file():
        raise PipelineError(f"cannot read {path.name}: no such file", code="unreadable_video")
    try:
        stream = probe_video(path)
    except ToolMissing as e:
        raise PipelineError(str(e), code="tool_missing", status=503) from e
    except DeliveryError as e:
        raise PipelineError(f"not a readable video: {e}"[:500], code="unreadable_video") from e
    except (OSError, ValueError) as e:
        raise PipelineError(
            f"not a readable video ({type(e).__name__}: {e})"[:500], code="unreadable_video"
        ) from e
    fps = _frame_rate(stream)
    width, height = stream.get("width"), stream.get("height")
    if fps <= 0 or not isinstance(width, int) or not isinstance(height, int) or width < 1 or height < 1:
        raise PipelineError(f"{path.name} has no usable size or frame rate", code="unreadable_video")
    try:
        frames = int(stream["nb_frames"])
    except (KeyError, TypeError, ValueError):  # webm and some mov files carry no frame count in the header
        try:
            frames = int(pio.probe(path)["nb_frames"])
        except Exception as e:  # ffprobe's own failure modes are all "cannot count the frames"
            raise PipelineError(
                f"cannot count the frames of {path.name}: {e}"[:500], code="unreadable_video"
            ) from e
    if frames < MIN_CLIP_FRAMES:
        raise PipelineError(
            f"a clip has at least {MIN_CLIP_FRAMES} frames; this file has {frames}", code="unreadable_video"
        )
    try:
        duration = float(stream["duration"])
    except (KeyError, TypeError, ValueError):
        duration = float(Fraction(frames) / fps)
    return {
        "codec": stream.get("codec_name"),
        "pix_fmt": stream.get("pix_fmt"),
        "width": width,
        "height": height,
        "fps": fps_text(fps),
        "fps_float": float(fps),
        "frames": frames,
        "duration_s": duration,
        "color_primaries": stream.get("color_primaries"),
        "color_trc": stream.get("color_transfer"),
        "colorspace": stream.get("color_space"),
        "color_range": stream.get("color_range"),
    }


def _still_in_cas(cas: CAS, key: str, what: str) -> np.ndarray:
    """RGB uint16 of the image `key` names in the project's CAS (404 `not_found`, 422 `bad_still_hash` /
    `unreadable_image`)."""
    try:
        check_key(key)
    except CASError as e:
        raise PipelineError(f"{what} must be a blake3 hash: {e}"[:500], code="bad_still_hash") from e
    path = cas.find(key)
    if path is None:
        raise PipelineError(
            f"{what} {key[:12]} is not in the project's CAS", code="not_found", status=404, still_hash=key
        )
    try:
        rgb = pio.load_rgb(path)
    except Exception as e:  # any decoder failure is the same answer
        raise PipelineError(
            f"{what} is not a readable image ({type(e).__name__}: {e})"[:500], code="unreadable_image"
        ) from e
    return np.clip(np.rint(rgb * 65535.0), 0, 65535).astype(np.uint16)


def _motion_view(pl: Pipeline, project_id: str, motion_packet_id: str) -> tuple[PacketView, tuple[int, int]]:
    """The named motion packet and its `motion.sequence_resolution` (width, height)."""
    try:
        mv = pl.packet(project_id, motion_packet_id)
    except PipelineError as e:
        if e.code == "unknown_packet":
            raise PipelineError(str(e), code="not_found", status=404, packet_id=motion_packet_id) from e
        raise
    res = (mv.manifest.get("motion") or {}).get("sequence_resolution")
    try:
        if mv.manifest.get("kind") != "motion" or not isinstance(res, Mapping):
            raise KeyError("sequence_resolution")
        size = (int(res["width"]), int(res["height"]))
    except (KeyError, TypeError, ValueError) as e:
        raise PipelineError(
            f"packet {motion_packet_id} is not a motion packet", code="no_motion_packet"
        ) from e
    return mv, size


def _check_clip(
    pl: Pipeline,
    project_id: str,
    pv: PacketView,
    file: Path,
    cas: CAS,
    approved_still_hash: str | None,
    end_still_hash: str | None,
    motion_packet_id: str | None,
) -> _Clip:
    """Everything a clip must satisfy before a run row or a CAS object exists (see the module docstring)."""
    if not file.is_file():
        raise PipelineError(f"cannot read {file.name}: no such file", code="unreadable_video")
    _check_container(file)
    probe = _probe_clip(file)
    motion, expected = None, None
    if motion_packet_id is not None:
        motion, expected = _motion_view(pl, project_id, motion_packet_id)
    plate16 = pv.plate16()
    plate = (plate16.shape[1], plate16.shape[0])
    want = expected or plate
    got = (probe["width"], probe["height"])
    if got != want:
        extra: dict[str, Any] = {"expected": list(want), "file": list(got)}
        if motion is not None:
            extra.update(sequence=list(want), motion_packet_id=motion_packet_id)
            where = f"the sequence of motion packet {motion_packet_id} is {want[0]} x {want[1]}"
        else:
            extra["plate"] = list(plate)
            where = f"the plate is {want[0]} x {want[1]}"
        raise PipelineError(
            f"{file.name} is {got[0]} x {got[1]}; {where} (an import is never resized onto it)",
            code="size_mismatch",
            **extra,
        )
    start = (
        plate16
        if approved_still_hash is None
        else _still_in_cas(cas, approved_still_hash, "approved_still_hash")
    )
    end = None if end_still_hash is None else _still_in_cas(cas, end_still_hash, "end_still_hash")
    return _Clip(probe, start, end, approved_still_hash, end_still_hash, motion)


def _store_clip(cas: CAS, clip: _Clip, file: Path) -> tuple[str, int]:
    """Copy the raw clip into the CAS with its extension: (its key, its size in bytes). The CAS copy is what is
    gated and delivered, so its probe replaces the one made on the file in place; a file that changed size
    between the check and the copy is refused (409)."""
    try:
        raw_key = cas.put_file(file, _raw_ext(file))
    except (CASError, OSError) as e:  # a symlink, or a file that vanished
        raise PipelineError(f"cannot read {file.name}: {e}"[:500], code="unreadable_video") from e
    stored = cas.find(raw_key)
    if stored is None:
        raise PipelineError(f"{file.name} did not reach the CAS", code="unreadable_video", status=500)
    again = _probe_clip(stored)
    if (again["width"], again["height"]) != clip.size:
        raise PipelineError(
            f"{file.name} changed while it was being imported ({clip.size[0]} x {clip.size[1]}, now "
            f"{again['width']} x {again['height']})",
            code="size_mismatch",
            status=409,
        )
    clip.probe = again
    return raw_key, stored.stat().st_size


def _identity_timemap(probe: Mapping[str, Any]) -> TimeMap:
    """An import is not retimed: the map is the probed fps to itself over the clip's frames."""
    fps = Fraction(probe["fps"])
    return TimeMap(fps, fps, (0, int(probe["frames"]) - 1))


def _clip_with_lineage(pl: Pipeline, run: Run, project_dir: Path, cas: CAS, raw_key: str) -> tuple[str, bool]:
    """The CAS key of a copy of the clip with the run's lineage in the container, else the raw key as it is
    (`.webm` has no carrier; a failed remux keeps the take: lineage is for drop-to-resolve)."""
    src = cas.find(raw_key)
    if src is None or not lineage_mod.supported(src):
        return raw_key, False
    n = max(ReportService(project_dir).versions(run.id), default=0) + 1
    tmp = pl.settings.home / "cache" / f"import_{run.id}_{run.attempt}{src.suffix}"
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, tmp)
        lin = lineage_mod.Lineage(
            project_id=run.project_id,
            node_id=run.node_id or "none",
            run_id=run.id,
            packet_id=run.packet_id,
            engine_id=run.route.engine_id,
            engine_version=str(run.route_dict.get("model_version") or "") or None,
            gate_report=f"gates/{run.id}.{n}.json",
        )
        lineage_mod.embed(tmp, lin)
        return cas.put_file(tmp, src.suffix), True
    except Exception as e:
        log.warning("lineage not embedded in imported clip of run %s: %s", run.id, e)
        return raw_key, False
    finally:
        tmp.unlink(missing_ok=True)


def _normalize_clip(pl: Pipeline, run: Run, cas: CAS, clip: _Clip, project_dir: Path) -> dict[str, Any]:
    """NORMALIZING for a clip: the lineage-tagged copy, the probe and the identity time map."""
    out_key, embedded = _clip_with_lineage(pl, run, project_dir, cas, str(run.result_key))
    return {
        "media": "video",
        "outputs": [out_key],
        "raw_key": run.result_key,
        "probe": dict(clip.probe),
        "timemap": _identity_timemap(clip.probe).to_json(),
        "start_still": clip.start_key or run.inputs.get("plate"),
        "end_still": clip.end_key,
        "motion_packet_id": clip.motion.packet_id if clip.motion is not None else None,
        "ingress": None,
        "composite": None,
        "imported": dict(run.inputs.get("imported") or {}),
        "lineage": embedded,
    }


def _gate_clip(
    pl: Pipeline, run: Run, pv: PacketView, cas: CAS, clip: _Clip, project_dir: Path
) -> tuple[bool, dict[str, Any]]:
    """GATING a clip with the assembly a Kling take is scored with; the report is `gates/<run_id>.<n>.json`.
    Returns (passed, the run's gate_report as the worker stores it)."""
    norm = run.normalized or {}
    out = cas.find((norm.get("outputs") or [""])[0])
    if out is None:
        raise FileNotFoundError("the imported clip is not in the CAS")
    report = video_gates.run(
        out,
        clip.start,
        clip.end,
        clip.motion.root if clip.motion is not None else None,
        None,  # no declared shot: the map is not scored, and a motion packet builds its own
        None,
        engine={"id": run.route.engine_id, "version": str(run.route_dict.get("model_version") or "") or None},
        tier=pv.tier,
    )
    path = Path(ReportService(project_dir).write(run.id, report))
    try:
        ref = path.relative_to(project_dir).as_posix()
    except ValueError:
        ref = path.as_posix()
    passed = report.get("verdict") != "fail"
    stored = dict(report)
    stored["verdict"] = "pass" if passed else "fail"  # what the worker keeps on the run (worker._gating)
    stored["landing"] = (report.get("metrics") or {}).get("landing")
    stored["timemap"] = norm.get("timemap")
    stored["report_path"] = ref
    return passed, stored


# ---------------------------------------------------------------------------- the run's moves


def _moved(
    pl: Pipeline, conn: Any, project_id: str, run_id: str, before: RunState, data: Mapping[str, Any]
) -> Run:
    """Publish the move a run just made from `before`; ledger it with `data` when it is a ledgered state."""
    run = get_run(conn, run_id)
    event = {"kind": "run.state", "run_id": run_id, "from": before.value, "to": run.state.value}
    if run.reason_code:
        event["reason_code"] = run.reason_code
    pl.publish(project_id, run_id, event)
    if run.state in LEDGERED:
        pl.ledger(project_id, LEDGER_KINDS[run.state], run, conn=conn, data=dict(data))
    return run


def _step(
    pl: Pipeline,
    conn: Any,
    project_id: str,
    run_id: str,
    to: RunState,
    data: Mapping[str, Any],
    **fields: Any,
) -> Run:
    before = get_run(conn, run_id).state
    transition(conn, run_id, to, actor=ACTOR, **fields)
    return _moved(pl, conn, project_id, run_id, before, data)


def _stop(
    pl: Pipeline,
    conn: Any,
    project_id: str,
    run_id: str,
    to: RunState,
    code: str,
    reason: str,
    data: Mapping[str, Any],
) -> Run:
    """End the run in FAILED or CANCELLED with a reason code (never raises past a run already terminal)."""
    try:
        return _step(pl, conn, project_id, run_id, to, data, reason=reason[:500], reason_code=code)
    except IllegalTransition:
        return get_run(conn, run_id)


def _with_lineage(pl: Pipeline, run: Run, project_dir: Path, png: bytes) -> tuple[bytes, bool]:
    """The PNG with the run's lineage embedded (the report it will be scored in named), else as given."""
    n = max(ReportService(project_dir).versions(run.id), default=0) + 1
    tmp = pl.settings.home / "cache" / f"import_{run.id}_{run.attempt}.png"
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(png)
        lin = lineage_mod.Lineage(
            project_id=run.project_id,
            node_id=run.node_id or "none",
            run_id=run.id,
            packet_id=run.packet_id,
            engine_id=run.route.engine_id,
            engine_version=str(run.route_dict.get("model_version") or "") or None,
            gate_report=f"gates/{run.id}.{n}.json",
        )
        lineage_mod.embed(tmp, lin)
        return tmp.read_bytes(), True
    except Exception as e:  # lineage is for drop-to-resolve; the take is still the take without it
        log.warning("lineage not embedded in imported run %s: %s", run.id, e)
        return png, False
    finally:
        tmp.unlink(missing_ok=True)


def _record_phash(pl: Pipeline, project_id: str, cas: CAS, key: str) -> None:
    try:
        from .phash import PhashIndex

        path = cas.find(key)
        if path is None:
            raise FileNotFoundError(f"{key[:12]} is not in the CAS")
        with pl.index.project(project_id) as conn:
            PhashIndex(conn, project_id).add_file(path, key)
    except Exception as e:  # a missing phash only weakens drop-to-resolve for re-encoded copies
        log.warning("phash of imported %s in %s not recorded: %s", key[:12], project_id, e)


def _alert(pl: Pipeline, project_id: str, run_id: str) -> Any:
    def alert(decision: budgets.Decision) -> None:
        try:
            budgets.publish_alert(pl.bus, project_id, run_id, decision)
        except Exception as e:  # the bus is for watchers
            log.warning("budget alert for imported run %s not published: %s", run_id, e)

    return alert


def _mask_png(mask: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.where(mask, 255, 0).astype(np.uint8))
    if not ok:
        raise ValueError("cv2 could not encode the mask")
    return bytes(buf)


def _normalize(
    pl: Pipeline, run: Run, pv: PacketView, cas: CAS, image16: np.ndarray, project_dir: Path
) -> dict[str, Any]:
    """NORMALIZING for an import: a lineage-tagged 16-bit PNG copy of the file (no composite: the gates measure
    what the vendor or the artist changed, outside the mask included) and the editable mask it is held to."""
    data, embedded = _with_lineage(pl, run, project_dir, encode_png16(image16))
    out_key = cas.put_bytes(data, "png")
    _record_phash(pl, run.project_id, cas, out_key)
    mask = pl.editable_mask(run, pv, cas)
    return {
        "outputs": [out_key],
        "raw_key": run.result_key,
        "mask_key": cas.put_bytes(_mask_png(mask), "png"),
        "ingress": None,
        "composite": None,
        "textlock_px": None,
        "mask_share": float(mask.mean()),
        "imported": dict(run.inputs.get("imported") or {}),
        "lineage": embedded,
    }


def _gate(pl: Pipeline, run: Run, pv: PacketView, cas: CAS, project_dir: Path) -> tuple[bool, dict[str, Any]]:
    """GATING with the arguments `Pipeline.gate` passes; the report is written as `gates/<run_id>.<n>.json`.
    Returns (passed, the run's gate_report as the worker stores it)."""
    norm = run.normalized or {}
    outputs = norm.get("outputs") or []
    out16 = decode_png16(cas.read_bytes(outputs[0]))
    raw = cv2.imdecode(np.frombuffer(cas.read_bytes(norm["mask_key"]), np.uint8), cv2.IMREAD_GRAYSCALE)
    report = gating.run_gates(
        pv.plate16(),
        out16,
        {"path": pv.root, "manifest": pv.manifest},
        pv.legend,
        pv.tier,
        {"id": run.route.engine_id, "version": str(run.route_dict.get("model_version") or "") or None},
        mask=raw > 127,
        registration=norm.get("ingress"),
        depth_fn=pl.depth_fn,
        edges=pv.edges(),
        crypto_material=pv.crypto("material") if pv.tier == "A" else None,
        extra_metrics={"textlock_px": norm.get("textlock_px"), "output": outputs[0]},
    )
    path = Path(ReportService(project_dir).write(run.id, report))
    try:
        ref = path.relative_to(project_dir).as_posix()
    except ValueError:
        ref = path.as_posix()
    passed = report.verdict != "fail"
    stored = report.model_dump(mode="json")
    stored["verdict"] = "pass" if passed else "fail"  # what the worker keeps on the run (worker._gating)
    stored["score"] = float(gating.score_of(report))
    stored["report_path"] = ref
    return passed, stored


# ---------------------------------------------------------------------------- the entry point


def import_file(
    pipeline: Pipeline,
    project_id: str,
    packet_id: str,
    path: str | Path,
    *,
    source: str,
    node_id: str | None = None,
    cost_usd: float = 0,
    note: str | None = None,
    by: str = "human",
    approved_still_hash: str | None = None,
    end_still_hash: str | None = None,
    motion_packet_id: str | None = None,
) -> Run:
    """Import `path` as a run of `project_id` on `packet_id` (see the module docstring). Returns the run at its
    end: DONE or GATE_FAIL, or FAILED when normalizing or gating raised (the reason is on the run).

    A `.mp4`, `.mov` or `.webm` file is a clip; the last three arguments apply to a clip only: the CAS key of
    the approved still the first frame is held to (default: the plate), the CAS key of an end still for the
    last frame, and a motion packet whose `sequence_resolution` is the size the clip must have and whose camera
    path the clip is checked against."""
    pl = pipeline
    route, price, note = _check_args(source, cost_usd, by, note)
    try:
        check_project_id(project_id)
    except ValueError as e:
        raise PipelineError(str(e), code="bad_project_id") from e
    project = pl.load_project(project_id)
    if project is None:
        raise PipelineError(f"no project {project_id}", code="not_found", status=404, project_id=project_id)
    pv = _packet(pl, project_id, packet_id)
    graph = canvas_rules.as_graph(project.graph)
    if node_id is not None and not any(n.get("id") == node_id for n in graph.nodes):
        raise PipelineError(
            f"project {project_id} has no node {node_id}", code="not_found", status=404, node_id=node_id
        )
    grade = require_route(
        route, pl.allowed_grades(project_id, pv.manifest), what=f"a file imported from {source}"
    )
    file = Path(path)
    check_import_file(file)  # a FIFO would block the read for good; the route has made this check too
    cas = pl.cas_for(project_id)
    is_clip = file.suffix.lower() in VIDEO_EXTS
    if not is_clip and (approved_still_hash or end_still_hash or motion_packet_id):
        raise PipelineError(
            "approved_still_hash, end_still_hash and motion_packet_id apply to a clip "
            f"({', '.join(sorted(VIDEO_EXTS))}), not to {file.name}",
            code="not_a_clip",
        )
    clip: _Clip | None = None
    staging: dict[str, Any] = {"node_kind": "Edit", "imported": True, "region_crop": False}
    if is_clip:
        clip = _check_clip(
            pl, project_id, pv, file, cas, approved_still_hash, end_still_hash, motion_packet_id
        )
        raw_key, raw_len = _store_clip(cas, clip, file)
        imported: dict[str, Any] = {
            "path_name": file.name,
            "source": source,
            "note": note,
            "blake3": raw_key,
            "media": "video",
            "approved_still": clip.start_key,
            "end_still": clip.end_key,
            "motion_packet_id": clip.motion.packet_id if clip.motion is not None else None,
        }
        staging["media"] = "video"
        inputs: dict[str, Any] = {"plate": pv.plate_hash, "imported": imported, STAGING_KEY: staging}
        data: dict[str, Any] = {"imported": True, "source": source, "media": "video"}
    else:
        plate16 = pv.plate16()
        plate_wh = (plate16.shape[1], plate16.shape[0])
        raw = _read_still(file, plate_wh)  # the header is checked before the rest of the file is read
        image16 = _decode(raw, file.suffix.lower(), pl.settings.home / "cache")
        if image16.shape != plate16.shape:
            raise _size_mismatch(file.name, plate_wh, (image16.shape[1], image16.shape[0]))
        mask = pl.mask_spec(graph, node_id or "", pv)
        pl.check_mask_keys(project_id, mask, pv)
        raw_key = cas.put_bytes(raw, _raw_ext(file))
        imported = {"path_name": file.name, "source": source, "note": note, "blake3": raw_key}
        inputs = {"plate": pv.plate_hash, "mask": mask, "imported": imported, STAGING_KEY: staging}
        data = {"imported": True, "source": source}
        raw_len = len(raw)
    if node_id is not None:
        keep = canvas_rules.ancestors(graph, node_id) | {node_id}
        inputs["subgraph"] = subgraph_snapshot(project.graph, keep)
    params: dict[str, Any] = {}
    project_dir = pl.project_dir(project_id)
    now = pl.clock()

    with pl.index.project(project_id) as conn:
        spec = RunCreate(
            project_id=project_id,
            packet_id=packet_id,
            node_id=node_id,
            priority=Priority.INTERACTIVE,
            inputs=inputs,
            params=params,
            route=route,
            attempt=next_attempt(conn, node_id, inputs, params, route),
        )
        run = create_run(conn, spec, now=now)
        pl.index.remember(run.id, project_id)
        rid = run.id
        quote = Quote(
            run_id=rid,
            provider=route["provider"],
            model=route["engine_id"],
            model_version=route["model_version"],
            region=route["region"],
            custody_grade=grade.value,
            price_usd=price,
            eta_s=0.0,
            created_utc=now,
            expires_utc=now + QUOTE_TTL,
            route_id=route["route_id"],
            price_status="declared",
        )
        with tx(conn):
            insert_quote(conn, quote)
            transition(
                conn,
                rid,
                S.QUOTED,
                actor=ACTOR,
                now=now,
                reason=f"imported from {source}: {price:.4f} USD declared",
                quote_id=quote.id,
                quote_json=quote.terms_json(),
            )
        _moved(pl, conn, project_id, rid, S.DRAFT, data)  # fail-closed: 503 and CANCELLED without a record

        try:
            approve_one(
                conn,
                rid,
                by,
                None,
                now,
                policy=pl.policy,
                last_human_interaction_utc=pl.last_human_utc,
                on_alert=_alert(pl, project_id, rid),
            )
        except (AutoApprovalDenied, EnvelopeRequired, QuoteExpired, QuoteNotFound, IllegalTransition) as e:
            _stop(pl, conn, project_id, rid, S.CANCELLED, "approval_refused", str(e), data)
            raise PipelineError(
                f"the import was not approved: {e}"[:500], code="approval_refused", status=409, run_id=rid
            ) from e
        if by == "human":
            pl.touch_human(now)
        _moved(pl, conn, project_id, rid, S.QUOTED, data)  # fail-closed like the quote

        _step(pl, conn, project_id, rid, S.QUEUED, data)
        try:
            _step(pl, conn, project_id, rid, S.STAGING, data)
        except DuplicateRequest as e:
            _stop(pl, conn, project_id, rid, S.CANCELLED, "duplicate_request", str(e), data)
            raise PipelineError(str(e), code="duplicate_request", status=409, run_id=rid) from e
        annotate(conn, rid, expect=S.STAGING, submit_started_utc=now)
        annotate(
            conn,
            rid,
            expect=S.STAGING,
            provider_job_id=f"import-{raw_key[:JOB_ID_HEX]}",
            submitted_utc=now,
        )
        _step(
            pl,
            conn,
            project_id,
            rid,
            S.SUBMITTED,
            data,
            reason=f"imported from {source}; nothing left the box",
        )
        _step(pl, conn, project_id, rid, S.RUNNING, data)
        _step(pl, conn, project_id, rid, S.FETCHING, data, result_url=f"{ENGINE_PREFIX}{raw_key}")
        run = _step(
            pl,
            conn,
            project_id,
            rid,
            S.NORMALIZING,
            data,
            reason=f"{raw_len} bytes of {file.name} into the CAS",
            result_key=raw_key,
        )
        try:
            if clip is not None:
                normalized = _normalize_clip(pl, run, cas, clip, project_dir)
            else:
                normalized = _normalize(pl, run, pv, cas, image16, project_dir)
        except Exception as e:  # recorded as the failure reason, like the worker's normalize_error
            code = e.code if isinstance(e, PipelineError) else "normalize_error"
            return _stop(
                pl, conn, project_id, rid, S.FAILED, code, f"normalize raised {type(e).__name__}: {e}", data
            )
        run = _step(pl, conn, project_id, rid, S.GATING, data, normalized=normalized)
        try:
            if clip is not None:
                passed, report = _gate_clip(pl, run, pv, cas, clip, project_dir)
            else:
                passed, report = _gate(pl, run, pv, cas, project_dir)
        except Exception as e:  # the worker's gate_error
            log.exception("gating imported run %s failed", rid)
            return _stop(
                pl,
                conn,
                project_id,
                rid,
                S.FAILED,
                "gate_error",
                f"gate raised {type(e).__name__}: {e}",
                data,
            )
        if passed:
            return _step(pl, conn, project_id, rid, S.DONE, data, gate_report=report)
        return _step(
            pl,
            conn,
            project_id,
            rid,
            S.GATE_FAIL,
            data,
            reason="gates failed",
            gate_report=report,
            reason_code="gate_fail",
        )
