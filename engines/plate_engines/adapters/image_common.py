"""Shared plumbing for the image engine adapters (router plan T17): input parsing, size choice, staging, ledger.

New for the PLATE monorepo. T17's file list names one module per vendor; the parts every one of them needs
(reading an egress PNG, picking the size the vendor is asked for, writing staged outputs, the
output-depends-on-input check) live here once instead of six times. Nothing in this module opens a socket.

An IMAGE input (image, mask, reference_images[i], depth, edges) is the PNG that egress.prepare produced, as raw
bytes or as a mapping:

    {"bytes": <png bytes>,            # or "data", or "path": <file>
     "name": "plate.png", "mime": "image/png",   # optional (name is sanitised: no ':' and no separators)
     "role": "plate" | "ai_layer" | ..., # upscalers refuse role == "plate"
     "kind": "object" | "character" | "style",   # optional reference kind (Gemini per-kind caps)
     "sizemap": <egress.SizeMap or its to_dict()>}  # optional; its engine_size and mode are used as chosen

Sizes: a SizeMap wins (egress already snapped the plate and recorded mode exact | pad | arbitrary), then explicit
vendor parameters, else the adapter snaps the PNG's own dimensions with plate_pixels.sizes.pick_size and records
the mode it got. SizeChoice carries both, and Adapter.size_choice(job_id) returns it for the run record.

Doctrine in code: `image` is a required input of every image engine here (no text-to-image path exists), an
upscaler refuses role == "plate", and a vendor that hands back the same bytes for two different inputs raises
SilentlyIgnoredInput (OutputLedger).
"""

from __future__ import annotations

import base64
import itertools
import os
import re
import struct
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import httpx
from blake3 import blake3

from ..atlas import _atomic_write
from ..models import EngineManifest, EngineSize, Route
from .base import (
    Adapter,
    FetchError,
    InputError,
    JobStatus,
    ParamError,
    PlateUpscaleForbidden,
    Quote,
    SilentlyIgnoredInput,
)

__all__ = [
    "ImageAdapter",
    "ImageData",
    "InputError",
    "LocalModelUnavailable",
    "MissingKey",
    "ModelIdUnknown",
    "OutputLedger",
    "ParamError",
    "Prepared",
    "SilentlyIgnoredInput",
    "SizeChoice",
    "SubmitFailed",
    "UnknownJob",
    "decode_b64",
    "TIER_BOX",
    "decode_png_8bit",
    "encode_png",
    "png_size",
    "read_image",
    "sniff_mime",
    "stub_enabled",
    "tier_dims",
]

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
# Vendor resolution tiers are long-edge boxes: "2K" fits a ratio inside 2048 x 2048 (same rule as plate_pixels.sizes).
TIER_BOX = {"1K": 1024, "2K": 2048, "4K": 4096}


class MissingKey(RuntimeError):
    """The environment holds none of the API key variables this adapter reads."""


class ModelIdUnknown(ValueError):
    """The manifest and the route name no vendor model string yet (router plan: unverified until the P0 smoke test)."""


class SubmitFailed(RuntimeError):
    """The vendor refused or failed a submit for a reason other than moderation (no provider job id exists)."""


class UnknownJob(KeyError):
    """poll() was asked about a job this adapter instance never created."""


class LocalModelUnavailable(RuntimeError):
    """A local engine cannot run here (no gpu extra, no CUDA, no weights) and PLATE_STUB_LOCAL is not 1."""


# ---- image inputs ---------------------------------------------------------------------------------------


def sniff_mime(data: bytes) -> str:
    if data.startswith(PNG_SIGNATURE):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def png_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the IHDR chunk, or None when data is not a PNG."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return (w, h) if w > 0 and h > 0 else None


def decode_b64(text: str, what: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise SubmitFailed(f"{what}: the vendor sent invalid base64") from exc


@dataclass(frozen=True)
class ImageData:
    data: bytes
    mime: str
    name: str
    role: str | None = None
    kind: str | None = None
    width: int | None = None
    height: int | None = None
    sizemap: dict[str, Any] | None = None
    digest: str = ""

    @property
    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


def read_image(value: Any, label: str) -> ImageData:
    """Parse one image input (bytes or the mapping documented in the module docstring)."""
    meta: Mapping[str, Any] = {}
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    elif isinstance(value, Mapping):
        meta = value
        blob = meta.get("bytes", meta.get("data"))
        if blob is not None:
            if not isinstance(blob, (bytes, bytearray, memoryview)):
                raise InputError(f"{label}: 'bytes' must be bytes, got {type(blob).__name__}")
            raw = bytes(blob)
        elif meta.get("path") is not None:
            raw = Path(meta["path"]).read_bytes()
        else:
            raise InputError(f"{label}: an image mapping needs 'bytes' (or 'data' or 'path')")
    else:
        raise InputError(f"{label}: expected PNG bytes or a mapping with 'bytes', got {type(value).__name__}")
    if not raw:
        raise InputError(f"{label}: the image is empty")
    digest = blake3(raw).hexdigest()
    mime = str(meta.get("mime") or sniff_mime(raw))
    name = _SAFE_NAME.sub("_", str(meta.get("name") or f"{digest[:12]}.{_EXT.get(mime, 'bin')}"))
    sm = meta.get("sizemap")
    if sm is not None and not isinstance(sm, Mapping):
        sm = sm.to_dict()
    dims = png_size(raw)
    if dims is None and sm is not None and sm.get("dst"):
        dims = (int(sm["dst"][0]), int(sm["dst"][1]))
    return ImageData(
        data=raw,
        mime=mime,
        name=name,
        role=meta.get("role"),
        kind=meta.get("kind"),
        width=dims[0] if dims else None,
        height=dims[1] if dims else None,
        sizemap=dict(sm) if sm is not None else None,
        digest=digest,
    )


# ---- sizes ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SizeChoice:
    """The size an adapter asks the vendor for and where it came from.

    mode is egress's vocabulary (exact | pad | arbitrary) when a SizeMap or the snapping supplied it, and
    "explicit" when the caller named the size with vendor parameters or a Staged.size. source says which.
    """

    size: EngineSize
    mode: str
    source: Literal["sizemap", "snapped", "native", "params", "staged"]
    ratio: str = ""


def _ratio_for(manifest: EngineManifest, w: int, h: int) -> str:
    """The manifest's named aspect that w x h matches within 0.5%, else ''."""
    if manifest.sizes is None or w < 1 or h < 1:
        return ""
    for a in manifest.sizes.aspects:
        p, q = (int(x) for x in a.split(":"))
        if abs((w / h) / (p / q) - 1.0) <= 0.005:
            return a
    return ""


def stub_enabled() -> bool:
    """PLATE_STUB_LOCAL=1 turns local engines into deterministic stubs so the pipeline is testable without weights."""
    return os.environ.get("PLATE_STUB_LOCAL") == "1"


def decode_png_8bit(data: bytes) -> Any:
    """Decode image bytes to an 8-bit numpy array (BGR order as OpenCV reads it; 16-bit is rounded down to 8)."""
    import cv2
    import numpy as np

    arr = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if arr is None:
        raise InputError("the image cannot be decoded")
    if arr.dtype == np.uint16:
        arr = ((arr.astype(np.uint32) + 128) // 257).astype(np.uint8)
    elif arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    return arr


def encode_png(arr: Any) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".png", arr)
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buf.tobytes()


def tier_dims(tier: str, ratio: str) -> tuple[int, int]:
    """(w, h) of a named aspect fitted inside a tier box, or (0, 0) when either is not a known name."""
    box = TIER_BOX.get(tier)
    if box is None or not ratio:
        return 0, 0
    p, q = (int(x) for x in ratio.split(":"))
    s = min(box / p, box / q)
    return max(1, round(p * s)), max(1, round(q * s))


@dataclass(frozen=True)
class Prepared:
    """Validated inputs and params for one job."""

    image: ImageData
    refs: tuple[ImageData, ...]
    mask: ImageData | None
    extras: Mapping[str, ImageData]  # other image-typed inputs (depth, edges)
    prompt: str
    params: dict[str, Any]
    raw_params: Mapping[str, Any]
    digest: str  # blake3 of every image + the prompt: what "different input" means for the ledger


class OutputLedger:
    """Remembers output hashes per input hash; the same output for two different inputs means the input was ignored."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def note(self, engine_id: str, input_digest: str, output: bytes) -> str:
        out = blake3(output).hexdigest()
        prev = self._seen.setdefault(out, input_digest)
        if prev != input_digest:
            raise SilentlyIgnoredInput(
                f"{engine_id}: identical output {out[:12]} for two different inputs "
                f"({prev[:12]} and {input_digest[:12]}); the vendor ignored the input"
            )
        return out


# ---- the adapter base for image engines -----------------------------------------------------------------


class ImageAdapter(Adapter):
    """Common machinery for image_edit / local_image / upscale engines. Subclasses set the class constants."""

    PROVIDER: str = ""
    SUPPORTED_ENGINES: tuple[str, ...] = ()
    ENV_KEY: tuple[str, ...] = ()
    ETA_S: float = 30.0
    NATIVE_MASK: bool = False
    # Most images (image + references) one request may carry; None = no adapter-level cap beyond the manifest's.
    MAX_IMAGES: int | None = None
    # Image-typed inputs besides image / mask / reference_images that the adapter understands.
    EXTRA_IMAGE_INPUTS: tuple[str, ...] = ()

    def __init__(
        self,
        manifest: EngineManifest,
        route: Route | str | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        staging_dir: Path | str | None = None,
        allow_unverified_sizes: bool = False,
        ledger: OutputLedger | None = None,
    ) -> None:
        super().__init__(manifest, route, transport)
        if self.SUPPORTED_ENGINES and manifest.id not in self.SUPPORTED_ENGINES:
            raise ValueError(
                f"{type(self).__name__} serves {list(self.SUPPORTED_ENGINES)}, not {manifest.id!r}"
            )
        if self.PROVIDER and self.route.provider != self.PROVIDER:
            raise ValueError(
                f"{type(self).__name__} talks to provider {self.PROVIDER!r}; "
                f"route {self.route.route_id!r} of {manifest.id} is provider {self.route.provider!r}"
            )
        self.allow_unverified_sizes = allow_unverified_sizes
        self.ledger = ledger if ledger is not None else OutputLedger()
        self._staging: Path | None = Path(staging_dir) if staging_dir is not None else None
        self._seq = itertools.count(1)
        self._jobs: dict[str, JobStatus] = {}
        self._digests: dict[str, str] = {}
        self._choices: dict[str, SizeChoice] = {}
        self._url_digest: dict[str, str] = {}
        # What a vendor said about a job at submit time (BFL cost / megapixels), echoed into poll()'s usage.
        self._submitted: dict[str, dict[str, Any]] = {}

    # ---- keys, model, staging ---------------------------------------------------------------------------

    def api_key(self) -> str:
        for name in self.ENV_KEY:
            value = os.environ.get(name)
            if value:
                return value
        raise MissingKey(f"{self.engine_id}: set {' or '.join(self.ENV_KEY)} in the environment")

    def model_name(self) -> str:
        """The vendor model string for this route; raises ModelIdUnknown rather than sending a placeholder."""
        name = self.route.model_slug or self.manifest.model_id
        if not name:
            raise ModelIdUnknown(
                f"{self.engine_id} via {self.route_id}: no vendor model id is established yet "
                "(flag unverified_p0); set route.model_slug or manifest model_id after the P0 smoke test"
            )
        return name

    def staging(self) -> Path:
        if self._staging is None:
            self._staging = Path(tempfile.mkdtemp(prefix="plate-adapter-"))
        self._staging.mkdir(parents=True, exist_ok=True)
        return self._staging

    def stage(self, name: str, data: bytes) -> Path:
        """Write data to the staging dir under a Windows-safe name (os.replace with retry)."""
        dest = self.staging() / _SAFE_NAME.sub("_", name)
        _atomic_write(dest, data, time.sleep)
        return dest

    def stage_input(self, img: ImageData) -> Path:
        """A file for an uploader that wants a path (content-addressed, so a re-upload reuses the file)."""
        return self.stage(f"in-{img.digest[:16]}-{img.name}", img.data)

    # ---- inputs and params ------------------------------------------------------------------------------

    def prepare(self, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Prepared:
        """Check inputs against the manifest, validate params with defaults, parse every image."""
        self.check_inputs(inputs)
        declared = {f.name: f for f in self.manifest.inputs}
        if inputs.get("mask") is not None and not self.NATIVE_MASK:
            raise InputError(
                f"{self.engine_id} via {self.route_id} has no mask channel; send the unmasked frame and let "
                "PLATE composite inside the mask afterwards (mask-locked compositing)"
            )
        validated = self.validate_params(params)
        prompt = ""
        if "prompt" in declared:
            prompt = inputs.get("prompt") or ""
            if not isinstance(prompt, str) or not prompt.strip():
                raise InputError(f"{self.engine_id}: prompt must be a non-empty string")
        image = read_image(inputs["image"], "image")
        refs_raw = inputs.get("reference_images")
        refs: list[ImageData] = []
        if refs_raw is not None:
            if not isinstance(refs_raw, (list, tuple)):
                raise InputError("reference_images must be a list of images")
            refs = [read_image(v, f"reference_images[{i}]") for i, v in enumerate(refs_raw)]
        if self.MAX_IMAGES is not None and 1 + len(refs) > self.MAX_IMAGES:
            raise InputError(
                f"{self.engine_id}: {1 + len(refs)} images (image + references) exceed this route's cap "
                f"of {self.MAX_IMAGES}"
            )
        mask = read_image(inputs["mask"], "mask") if inputs.get("mask") is not None else None
        extras = {n: read_image(inputs[n], n) for n in self.EXTRA_IMAGE_INPUTS if inputs.get(n) is not None}
        h = blake3()
        for label, img in (
            [("image", image)]
            + [(f"ref{i}", r) for i, r in enumerate(refs)]
            + ([("mask", mask)] if mask else [])
            + sorted(extras.items())
        ):
            h.update(f"{label}:{img.digest};".encode("ascii"))
        h.update(prompt.encode("utf-8"))
        return Prepared(image, tuple(refs), mask, extras, prompt, validated, dict(params), h.hexdigest())

    # ---- sizes ------------------------------------------------------------------------------------------

    def snap(self, img: ImageData) -> SizeChoice:
        """Snap the image's own dimensions to the engine's legal sizes (plate_pixels.sizes) and record the mode."""
        if img.width is None or img.height is None:
            raise InputError(
                f"{self.engine_id}: cannot read the size of the image (send a PNG or a mapping with a sizemap)"
            )
        from plate_pixels.sizes import (  # lazy: engines import pixels on demand
            NoLegalSize,
            legal_sizes,
            pick_size,
        )

        try:
            legal = legal_sizes(self.manifest, allow_unverified=self.allow_unverified_sizes)
            es, mode = pick_size(img.width, img.height, legal)
        except NoLegalSize:
            return SizeChoice(EngineSize(w=img.width, h=img.height, label="native"), "exact", "native")
        return SizeChoice(
            EngineSize(w=es.width, h=es.height, label=es.label), mode, "snapped", ratio=es.ratio
        )

    def explicit_size(self, raw_params: Mapping[str, Any], snapped: SizeChoice) -> SizeChoice | None:
        """Hook: a size the caller named with vendor parameters, or None. Default: engines name none."""
        return None

    def choose_size(
        self, inputs: Mapping[str, Any], params: Mapping[str, Any], size: EngineSize | None = None
    ) -> SizeChoice:
        """The size to request: Staged.size, else explicit params, else the SizeMap egress made, else a snap."""
        img = read_image(inputs["image"], "image")
        sm = img.sizemap
        if size is not None:
            mode = str(sm.get("mode")) if sm and sm.get("mode") else "explicit"
            return SizeChoice(size, mode, "staged", ratio=_ratio_for(self.manifest, size.w, size.h))
        if sm and sm.get("engine_size"):
            es = sm["engine_size"]
            chosen = SizeChoice(
                EngineSize(
                    w=int(es.get("width", es.get("w", 0))),
                    h=int(es.get("height", es.get("h", 0))),
                    label=str(es.get("label") or ""),
                ),
                str(sm.get("mode") or "exact"),
                "sizemap",
                ratio=str(es.get("ratio") or ""),
            )
        else:
            chosen = self.snap(img)
        return self.explicit_size(params, chosen) or chosen

    def non_default(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """params without the entries that merely equal a manifest default: validate_params fills those in, so on
        the submit path they say nothing about what the caller chose (Staged.size and the SizeMap speak for that)."""
        defaults = {p.name: p.default for p in self.manifest.params if p.default is not None}
        return {k: v for k, v in params.items() if not (k in defaults and defaults[k] == v)}

    # ---- jobs -------------------------------------------------------------------------------------------

    def track(self, job_id: str, digest: str, choice: SizeChoice) -> None:
        """Register a provider-assigned job id (asynchronous vendors) with its input digest and size choice."""
        self._digests[job_id] = digest
        self._choices[job_id] = choice

    def new_job_id(self, digest: str) -> str:
        job_id = f"{self.engine_id}-{self.route_id}-{next(self._seq):04d}-{digest[:8]}"
        self._digests[job_id] = digest
        return job_id

    def finish(self, job_id: str, status: JobStatus, choice: SizeChoice | None = None) -> None:
        self._jobs[job_id] = status
        if choice is not None:
            self._choices[job_id] = choice

    def size_choice(self, job_id: str) -> SizeChoice | None:
        """What size was requested for the job and its SizeMap mode (None for a job this instance never saw)."""
        return self._choices.get(job_id)

    def job_digest(self, job_id: str) -> str:
        return self._digests[job_id]

    def store_outputs(self, job_id: str, blobs: list[tuple[bytes, str]]) -> list[str]:
        """Synchronous vendors: ledger-check every output, stage it, and return file URIs for poll()."""
        digest = self._digests[job_id]
        for data, _ in blobs:
            self.ledger.note(self.engine_id, digest, data)
        urls = []
        for i, (data, ext) in enumerate(blobs):
            urls.append(self.stage(f"{job_id}-{i}.{ext}", data).resolve().as_uri())
        return urls

    def poll(self, provider_job_id: str) -> JobStatus:
        status = self._jobs.get(provider_job_id)
        if status is not None:
            return status
        return self.poll_remote(provider_job_id)

    def poll_remote(self, provider_job_id: str) -> JobStatus:
        """Asynchronous vendors implement this: one look at the provider's job."""
        raise UnknownJob(provider_job_id)

    def note_done(self, job_id: str, status: JobStatus) -> JobStatus:
        """Remember which input produced each output URL so fetch() can run the ledger on the bytes."""
        digest = self._digests.get(job_id)
        if status.state == "done" and digest is not None:
            for url in status.outputs:
                self._url_digest[url] = digest
        return status

    def fetch(self, url: str) -> bytes:
        """Staged files of synchronous vendors, else an allowlisted download; the ledger sees every result."""
        parts = urlsplit(url)
        if parts.scheme == "file":
            path = Path(url2pathname(unquote(parts.path))).resolve()
            root = self.staging().resolve()
            if not path.is_relative_to(root):
                raise FetchError("refusing to read a file outside this adapter's staging directory")
            if not path.is_file():
                raise FetchError(f"staged output is gone: {path.name}")
            return path.read_bytes()
        data = super().fetch(url)
        digest = self._url_digest.get(url)
        if digest is not None:
            self.ledger.note(self.engine_id, digest, data)
        return data

    # ---- helpers for HTTP vendors -----------------------------------------------------------------------

    @staticmethod
    def json_of(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def quote_for(
        self, prep: Prepared, choice: SizeChoice, *, tier: str | None = None, images: int = 1
    ) -> Quote:
        return self.make_quote(self.estimate(tier=tier, images=images), self.ETA_S, choice.size)

    def refuse_upscale_of_plate(self, *images: ImageData | None) -> None:
        for img in images:
            if img is not None and img.role == "plate":
                raise PlateUpscaleForbidden(
                    f"{self.engine_id}: the plate is never upscaled (input is role=plate)"
                )
