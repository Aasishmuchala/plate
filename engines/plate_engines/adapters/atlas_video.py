"""Video engine adapters on Atlas: Seedance 2.5 image-to-video and reference-to-video, Kling O3 4K and v3 Pro.

New for the PLATE monorepo (router plan T18, BUILD_PLAN decision 13). Every adapter here is an Adapter (base.py):
quote from manifest data, submit one job, poll one look, fetch an allowlisted result. They ride the Atlas client
contract in atlas.py (the quirks pinned in T7: `images` arrays, integer `duration`, upper-case `resolution`, pace words
stripped under an `image_tail`, outputs[0] wins, moderation reads as refused) over the guarded transport, so recorded
fixtures and the route's allowed_hosts apply and nothing opens a socket of its own.

This module also holds what the other video adapters (seedance_fallbacks, vertex_veo, local_wan) share: VideoAdapter,
input parsing (InputRef, parse_ref, VideoInputs), duration clamping, frame-size maths, the stateless "refused at submit"
job id, the real-face refusal pattern, and check_output_depends_on_input (the nightly SilentlyIgnoredInput test).

Doctrine in code: every video path starts from a supplied still (no text-to-video), the camera is never invented here
(Kling takes a start and an optional end frame; ratio follows the source dimensions), and nothing upscales a plate.
CUSTODY: uploadMedia returns public Alibaba OSS URLs, so every Atlas route is grade D (the router refuses NDA projects).
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import struct
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from blake3 import blake3

from ..atlas import (
    ATLAS_BASE,
    RESULT_HOSTS,
    VIDEO_POLL_INTERVAL_S,
    VIDEO_POLL_TIMEOUT_S,
    AtlasError,
    AtlasRefused,
    atlas_duration,
    atlas_ratio,
    atlas_resolution,
    classify_prediction,
    failure_text,
    is_refusal,
    unwrap,
)
from ..models import EngineManifest, EngineSize, Route
from .base import (
    Adapter,
    AtlasClient,
    FetchError,
    InputError,
    JobStatus,
    ParamError,
    Quote,
    SilentlyIgnoredInput,
    Staged,
)
from .transport import HttpxAtlasTransport

__all__ = [
    "AtlasVideoAdapter",
    "InputRef",
    "KlingStartEnd",
    "REAL_FACE_PATTERN",
    "SeedanceI2V",
    "SeedanceR2V",
    "VideoAdapter",
    "VideoInputs",
    "VideoSubmitError",
    "VideoTimeout",
    "atlas_video_adapter",
    "check_output_depends_on_input",
    "data_url",
    "frame_size",
    "inputs_digest",
    "is_video_refusal",
    "parse_ref",
    "refusal_from_job_id",
    "refused_job_id",
    "sniff_image_size",
    "timing_hints",
]

# Seedance blocks uploads that show a real person's face; BytePlus words it as ...SensitiveContentDetected.PrivacyInformation.
# Narrow phrases only: a prompt that merely says "face" must never turn a plain failure into a refusal.
REAL_FACE_PATTERN = re.compile(
    r"(real[\s_-]*(?:person|people|human|face)s?|human\s+faces?|face[\s_-]*(?:detect|recogni)\w*"
    r"|privacy[\s_.-]*information|sensitive[\s_-]*content|likeness|celebrit)",
    re.IGNORECASE,
)
REFUSED_ID_PREFIX = "refused-"
_PACE_WORDS = ("slow", "slowly", "gently", "leisurely", "unhurried")  # documented in atlas.strip_pace_words
_SHORT_SIDE = {"480P": 480, "720P": 720, "1080P": 1080, "2K": 1440, "4K": 2160}
_ROLES_R2V = ("first_frame", "reference_image")


class VideoSubmitError(RuntimeError):
    """A video vendor rejected a submit for a reason that is not a refusal (auth, quota, schema, 5xx)."""


class VideoTimeout(TimeoutError):
    """wait() ran out of time before the job reached done, failed or refused."""


# ---- refusals ------------------------------------------------------------------------------------------


def is_video_refusal(text: str) -> bool:
    """Moderation / safety / policy wording (atlas.REFUSAL_PATTERN) or a real-face rejection."""
    return is_refusal(text) or REAL_FACE_PATTERN.search(text) is not None


def refused_job_id(message: str) -> str:
    """A job id that carries its own refusal. A vendor can refuse at submit, where there is no job id to return; the
    adapter hands back this one and poll() answers `refused` from it, with no adapter state to lose on a restart."""
    raw = message.encode("utf-8")[:240]
    return REFUSED_ID_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def refusal_from_job_id(job_id: str) -> str | None:
    """The refusal message inside a refused_job_id, else None (an ordinary vendor job id)."""
    if not job_id.startswith(REFUSED_ID_PREFIX):
        return None
    payload = job_id[len(REFUSED_ID_PREFIX) :]
    try:
        return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8", errors="replace")
    except ValueError:
        return "refused"


# ---- inputs --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InputRef:
    """One media input after egress: an https/data URL already, or a local file that still has to reach the vendor."""

    url: str | None = None
    path: Path | None = None
    role: str | None = None
    name: str | None = None
    size: tuple[int, int] | None = None

    @property
    def label(self) -> str:
        return self.url[:60] if self.url else str(self.path)


def sniff_image_size(path: Path) -> tuple[int, int] | None:
    """(width, height) from a PNG or JPEG header, stdlib only; None for anything else or a damaged header."""
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(24)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                if len(head) < 24 or head[12:16] != b"IHDR":
                    return None
                w, h = struct.unpack(">II", head[16:24])
                return (w, h) if w and h else None
            if head[:2] != b"\xff\xd8":
                return None
            fh.seek(2)
            for _ in range(512):
                lead = fh.read(1)
                if not lead:
                    return None
                if lead != b"\xff":
                    continue
                kind = fh.read(1)
                while kind == b"\xff":  # fill bytes
                    kind = fh.read(1)
                if not kind:
                    return None
                k = kind[0]
                if k in (0x01, 0xD8) or 0xD0 <= k <= 0xD7 or k == 0x00:
                    continue
                seg = fh.read(2)
                if len(seg) < 2:
                    return None
                length = struct.unpack(">H", seg)[0]
                if 0xC0 <= k <= 0xCF and k not in (0xC4, 0xC8, 0xCC):
                    frame = fh.read(5)
                    if len(frame) < 5:
                        return None
                    h, w = struct.unpack(">HH", frame[1:5])
                    return (w, h) if w and h else None
                fh.seek(length - 2, 1)
    except OSError:
        return None
    return None


def parse_ref(value: Any, what: str) -> InputRef:
    """str / Path / {"url"|"path", role?, name?, w?, h?} -> InputRef. Local paths must exist; URLs must be https or data."""
    meta: Mapping[str, Any] = {}
    raw: Any = value
    if isinstance(value, Mapping):
        meta = value
        both = [k for k in ("url", "path") if value.get(k) is not None]
        if len(both) != 1:
            raise InputError(f"{what}: give exactly one of url or path, got {sorted(value)}")
        raw = value[both[0]]
    if isinstance(raw, Path):
        raw = str(raw)
    if not isinstance(raw, str) or not raw.strip():
        raise InputError(f"{what}: expected a file path or an https URL, got {raw!r}")
    raw = raw.strip()
    if raw.startswith("http://"):
        raise InputError(f"{what}: {raw[:60]!r} is plain http; only https URLs are sent")
    size = None
    if "w" in meta and "h" in meta:
        try:
            size = (int(meta["w"]), int(meta["h"]))
        except (TypeError, ValueError):
            raise InputError(f"{what}: w and h must be integers") from None
        if size[0] <= 0 or size[1] <= 0:
            raise InputError(f"{what}: w and h must be positive")
    role = meta.get("role")
    name = meta.get("name")
    if raw.startswith(("https://", "data:")):
        return InputRef(url=raw, role=role, name=name, size=size)
    path = Path(raw)
    if not path.is_file():
        raise InputError(f"{what}: file not found: {path}")
    return InputRef(path=path, role=role, name=name, size=size or sniff_image_size(path))


def data_url(ref: InputRef) -> str:
    """The file inlined as a data: URL (nothing is uploaded anywhere); an existing URL passes through."""
    if ref.url is not None:
        return ref.url
    assert ref.path is not None
    mime = mimetypes.guess_type(ref.path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(ref.path.read_bytes()).decode('ascii')}"


def mime_of(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


@dataclass(frozen=True)
class VideoInputs:
    """The manifest's inputs, parsed and renamed to one vocabulary (Veo's start_image is `image`, and so on)."""

    prompt: str
    image: InputRef | None = None
    image_tail: InputRef | None = None
    references: tuple[InputRef, ...] = ()
    videos: tuple[InputRef, ...] = ()
    elements: tuple[InputRef, ...] = ()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def inputs_digest(inputs: Mapping[str, Any]) -> str:
    """blake3 over the inputs with every local file replaced by its bytes' hash: the key the nightly test compares."""

    def norm(v: Any) -> Any:
        if isinstance(v, Mapping):
            return {str(k): norm(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        if isinstance(v, Path) or (
            isinstance(v, str) and not v.startswith(("https://", "data:")) and Path(v).is_file()
        ):
            return "file:" + blake3(Path(v).read_bytes()).hexdigest()
        return v

    return blake3(
        json.dumps(norm(inputs), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def check_output_depends_on_input(results: Iterable[tuple[str, bytes]]) -> None:
    """results: (input digest, output bytes) pairs. Two different inputs that produced identical output bytes mean the
    vendor ignored the input; raises SilentlyIgnoredInput. The nightly contract job runs this on its canary pairs."""
    seen: dict[str, str] = {}
    for digest, output in results:
        key = blake3(output).hexdigest()
        if key in seen and seen[key] != digest:
            raise SilentlyIgnoredInput(
                f"inputs {seen[key][:12]} and {digest[:12]} produced the same output {key[:12]}: the vendor ignored the input"
            )
        seen.setdefault(key, digest)


def timing_hints(prompt: str, hints: Sequence[tuple[float, str]], duration_s: int) -> str:
    """Append integer-second timing hints ("0s: ...", "6s: ...") to a prompt, in time order. Seedance 2.5 reads
    integer-second timestamps (scout 02; the exact phrasing is unverified until the P0 smoke test). A hint outside
    [0, duration] is an error, never clamped."""
    lines = []
    for t, text in sorted(hints, key=lambda h: h[0]):
        if not 0 <= t <= duration_s:
            raise InputError(f"timing hint at {t} s lies outside the {duration_s} s clip")
        lines.append(f"{int(t + 0.5)}s: {text.strip()}")
    return prompt.rstrip() + ("\n" + "\n".join(lines) if lines else "")


# ---- frame size ----------------------------------------------------------------------------------------


def _even(n: float) -> int:
    return max(2, int(round(n / 2.0)) * 2)


def frame_size(resolution: str | None, ratio: str | None, dims: tuple[int, int] | None) -> EngineSize:
    """The output frame a (resolution, ratio) pair names. `adaptive` follows the source dimensions; with neither
    known the size is "auto" (w = h = 0). The resolution is the short side, as the vendors mean it."""
    short = _SHORT_SIDE.get(str(resolution).upper()) if resolution else None
    aspect: float | None = None
    if ratio and re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", ratio):
        a, b = ratio.split(":")
        aspect = int(a) / int(b)
    elif dims is not None:
        aspect = dims[0] / dims[1]
    if short is None or aspect is None:
        return EngineSize(w=0, h=0, label="auto")
    w, h = (_even(short * aspect), short) if aspect >= 1 else (short, _even(short / aspect))
    return EngineSize(w=w, h=h, label=str(resolution).upper())


# ---- the shared video adapter --------------------------------------------------------------------------


class VideoAdapter(Adapter):
    """What every cloud and local video adapter shares: input parsing, quoting, duration clamping, a polling helper.

    Subclasses set ENGINE_IDS / PROVIDER and implement submit and poll (and build_body or its equivalent). A quote is
    manifest data only; the price tier is the resolution when price.by has it, else `base` on a fallback route, else
    price.usd (the figure the router ranks on).
    """

    ENGINE_IDS: tuple[str, ...] = ()
    PROVIDER: str | None = None
    OUTPUT_FPS: float | None = None
    POLL_INTERVAL_S: float = VIDEO_POLL_INTERVAL_S
    POLL_TIMEOUT_S: float = VIDEO_POLL_TIMEOUT_S
    ETA_BASE_S: float = 60.0
    ETA_PER_VIDEO_S: float = 6.0

    def __init__(
        self,
        manifest: EngineManifest,
        route: Route | str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if self.ENGINE_IDS and manifest.id not in self.ENGINE_IDS:
            raise ValueError(f"{type(self).__name__} drives {list(self.ENGINE_IDS)}, not {manifest.id!r}")
        if self.PROVIDER is not None and route is None:
            route = self.PROVIDER  # pick the named route rather than the manifest's first active one
        super().__init__(manifest, route, transport)
        if self.PROVIDER is not None and self.route.provider != self.PROVIDER:
            raise ValueError(
                f"{type(self).__name__} speaks to {self.PROVIDER!r}, not {self.route.provider!r}"
            )

    # ---- inputs and params ------------------------------------------------------------------------------

    def duration_param(self) -> str:
        names = {p.name for p in self.manifest.params}
        return "duration" if "duration" in names else "duration_s"

    def clamp_duration(self, seconds: float) -> int:
        """A shot length in seconds -> the integer the vendor takes: ceil (the take outlasts the shot), clamped to the
        manifest range (Kling 1 to 15, Seedance 4 to 30, Veo 4 to 8). validate_params still rejects out-of-range ints."""
        p = next(p for p in self.manifest.params if p.name == self.duration_param())
        lo, hi = (int(p.range[0]), int(p.range[1])) if p.range else (1, 1 << 30)
        return atlas_duration(seconds, lo, hi)

    def parse_inputs(self, inputs: Mapping[str, Any]) -> VideoInputs:
        """check_inputs, then InputRefs. Every path starts from a supplied still: an empty image list is text-to-video
        under another name, and PLATE has no such path (doctrine rule 1)."""
        self.check_inputs(inputs)
        prompt = inputs.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise InputError(f"{self.engine_id}: prompt must be a non-empty string")
        declared = {f.name for f in self.manifest.inputs}

        def one(*names: str) -> InputRef | None:
            for n in names:
                if n in declared and inputs.get(n) is not None:
                    return parse_ref(inputs[n], n)
            return None

        image = one("image", "start_image")
        tail = one("image_tail", "end_image")
        refs: list[InputRef] = []
        if "reference_images" in declared:
            refs = [
                parse_ref(v, f"reference_images[{i}]")
                for i, v in enumerate(_as_list(inputs.get("reference_images")))
            ]
            if not refs:
                raise InputError(
                    f"{self.engine_id}: reference_images is empty; text-to-video is not a PLATE path"
                )
            if self.manifest.kind == "video_r2v":
                bad = [r.role for r in refs if (r.role or "reference_image") not in _ROLES_R2V]
                if bad:
                    raise InputError(f"{self.engine_id}: reference role(s) {bad} not in {list(_ROLES_R2V)}")
                if sum(1 for r in refs if r.role == "first_frame") > 1:
                    raise InputError(f"{self.engine_id}: at most one reference may carry role first_frame")
        videos: list[InputRef] = []
        for n in ("reference_videos", "reference_video"):
            if n in declared:
                videos = [parse_ref(v, f"{n}[{i}]") for i, v in enumerate(_as_list(inputs.get(n)))]
        elements: list[InputRef] = []
        if "elements" in declared:
            for i, el in enumerate(_as_list(inputs.get("elements"))):
                if not isinstance(el, Mapping):
                    raise InputError(f"elements[{i}]: expected an object with name and image_url or path")
                src = {
                    ("url" if k == "image_url" else k): v
                    for k, v in el.items()
                    if k in ("image_url", "url", "path")
                }
                name = el.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise InputError(f"elements[{i}]: name is required")
                elements.append(parse_ref({**src, "name": name}, f"elements[{i}]"))
        return VideoInputs(prompt, image, tail, tuple(refs), tuple(videos), tuple(elements))

    def source_dims(self, parsed: VideoInputs, size: EngineSize | None = None) -> tuple[int, int] | None:
        """Source dimensions: the egress size when it names one, else the first frame's own, else a reference's."""
        if size is not None and size.w > 0 and size.h > 0:
            return (size.w, size.h)
        for ref in (parsed.image, *parsed.references):
            if ref is not None and ref.size is not None:
                return ref.size
        return None

    # ---- prices and quotes ------------------------------------------------------------------------------

    def price_for(self, params: Mapping[str, Any], seconds: float) -> float:
        by = self.manifest.price.by
        res = params.get("resolution")
        tier = None
        if res is not None and str(res).upper() in by:
            tier = str(res).upper()
        elif self.route.same_model_fallback_of is not None and "base" in by:
            tier = "base"
        return self.estimate(tier=tier, seconds=seconds)

    def ratio_for(self, params: Mapping[str, Any], dims: tuple[int, int] | None) -> str | None:
        """The ratio this engine will be asked for. Subclasses with a source-derived ratio override."""
        r = params.get("ratio") or params.get("aspect_ratio")
        return str(r) if r else None

    def quote(self, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Quote:
        p = self.validate_params(params)
        parsed = self.parse_inputs(inputs)
        seconds = float(p[self.duration_param()])
        dims = self.source_dims(parsed)
        size = frame_size(p.get("resolution"), self.ratio_for(p, dims), dims)
        return self.make_quote(
            self.price_for(p, seconds), self.ETA_BASE_S + self.ETA_PER_VIDEO_S * seconds, size
        )

    def output_bits(self, resolution: str | None) -> int | None:
        return None

    # ---- polling helper ---------------------------------------------------------------------------------

    def wait(
        self,
        provider_job_id: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        interval_s: float | None = None,
        timeout_s: float | None = None,
    ) -> JobStatus:
        """poll() in a loop at the video cadence (5 s steps, 12 min cap by default): sleep first, then look, until the
        job is done, failed or refused. For canaries and smoke tests; the daemon's runner polls on its own schedule."""
        step = self.POLL_INTERVAL_S if interval_s is None else interval_s
        deadline = now() + (self.POLL_TIMEOUT_S if timeout_s is None else timeout_s)
        while now() < deadline:
            sleep(step)
            status = self.poll(provider_job_id)
            if status.state in ("done", "failed", "refused"):
                return status
        raise VideoTimeout(f"{self.engine_id} job {provider_job_id} did not finish before the deadline")


# ---- Atlas ---------------------------------------------------------------------------------------------


class AtlasVideoAdapter(VideoAdapter):
    """Atlas /model/generateVideo for one engine. Subclasses name the engines and build the request body."""

    PROVIDER = "atlas"

    def __init__(
        self,
        manifest: EngineManifest,
        route: Route | str | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        key_env: str = "ATLASCLOUD_KEY",
    ) -> None:
        super().__init__(manifest, route, transport)
        self._key_env = key_env
        self._atlas_http: HttpxAtlasTransport | None = None
        self._atlas: AtlasClient | None = None
        self._resolutions: dict[str, str] = {}  # job id -> resolution asked for, for JobStatus.output_bits

    # ---- plumbing ---------------------------------------------------------------------------------------

    def client(self) -> AtlasClient:
        if self._atlas is None:
            self._atlas_http = HttpxAtlasTransport(self.guarded_transport())
            self._atlas = AtlasClient(self._atlas_http, key_env=self._key_env)
        return self._atlas

    def close(self) -> None:
        if self._atlas_http is not None:
            self._atlas_http.close()
            self._atlas_http = self._atlas = None
        super().close()

    def _url(self, client: AtlasClient, ref: InputRef | None) -> str | None:
        """A vendor-reachable URL for a ref: an existing https URL as is, a local file through uploadMedia."""
        if ref is None:
            return None
        if ref.url is not None:
            if ref.url.startswith("data:"):
                raise InputError("Atlas takes https URLs or local files, not data: URLs")
            return ref.url
        assert ref.path is not None
        return client.upload(ref.path, mime_of(ref.path))

    # ---- params -----------------------------------------------------------------------------------------

    @staticmethod
    def _normal_resolution(value: Any) -> Any:
        """1080, "1080", "1080p" -> "1080P"; "4k" -> "4K"; anything else is left for the enum check to reject."""
        if isinstance(value, bool):
            return value
        text = str(value).strip()
        if re.fullmatch(r"\d{3,4}\s*[pP]?", text):
            return atlas_resolution(text)
        if re.fullmatch(r"[1-9][kK]", text):
            return text.upper()
        return value

    def validate_params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        params = dict(params)
        if params.get("resolution") is not None:
            params["resolution"] = self._normal_resolution(params["resolution"])
        out = super().validate_params(params)
        negative = out.get("negative_prompt")
        if negative is not None and len(negative.split()) >= 30:
            raise ParamError(
                f"negative_prompt: {len(negative.split())} words; keep it under 30 (an anti-drift list, not an essay)"
            )
        return out

    # ---- the interface ----------------------------------------------------------------------------------

    def build_body(
        self, parsed: VideoInputs, p: Mapping[str, Any], dims: tuple[int, int] | None, client: AtlasClient
    ) -> dict[str, Any]:
        raise NotImplementedError

    def submit(self, staged: Staged) -> str:
        parsed = self.parse_inputs(staged.inputs)
        p = self.validate_params(staged.params)
        client = self.client()
        body = self.build_body(parsed, p, self.source_dims(parsed, staged.size), client)
        try:
            pid = client.generate_video(body)
        except AtlasRefused as exc:
            return refused_job_id(str(exc))
        except AtlasError as exc:
            text = str(exc)
            if "generateVideo failed:" in text and REAL_FACE_PATTERN.search(text):
                return refused_job_id(text)
            raise
        if p.get("resolution"):
            self._resolutions[pid] = str(p["resolution"])
        return pid

    def _look(self, prediction_id: str) -> dict[str, Any]:
        """GET /model/prediction/{id} and unwrap `data.data ?? data`; an unparsable body is `still running`."""
        client = self.client()
        assert self._atlas_http is not None
        resp = self._atlas_http.request(
            "GET",
            f"{ATLAS_BASE}/model/prediction/{quote(str(prediction_id), safe='')}",
            headers={"Authorization": f"Bearer {client.key()}"},
        )
        return unwrap(resp.json if isinstance(resp.json, dict) else {})

    def poll(self, provider_job_id: str, resolution: str | None = None) -> JobStatus:
        """One look. `resolution` (optional) lets a restarted runner say what it asked for, for output_bits."""
        refusal = refusal_from_job_id(provider_job_id)
        if refusal is not None:
            return JobStatus(state="refused", message=refusal)
        d = self._look(provider_job_id)
        pred = classify_prediction(d)
        if pred.state == "done":
            res = resolution or self._resolutions.get(provider_job_id)
            return JobStatus(
                state="done",
                outputs=list(pred.outputs),
                output_fps=self.OUTPUT_FPS,
                output_bits=self.output_bits(res),
            )
        state = pred.state
        if state == "failed" and REAL_FACE_PATTERN.search(failure_text(d)):
            state = "refused"
        return JobStatus(state=state, message=pred.message)

    def fetch(self, url: str) -> bytes:
        """Result download. Besides the route's allowed_hosts, only the hosts atlas.RESULT_HOSTS names (atlas.ts)."""
        host = (urlparse(url).hostname or "").lower()
        if not any(host.endswith(suffix) for suffix in RESULT_HOSTS):
            raise FetchError(f"result host {host!r} is not an Atlas result host")
        return super().fetch(url)


class _SeedanceAtlas(AtlasVideoAdapter):
    """Seedance 2.5 (24 fps; 1080p is 10-bit, 480p and 720p are 8-bit; no 4K)."""

    OUTPUT_FPS = 24.0
    ETA_BASE_S = 90.0
    ETA_PER_VIDEO_S = 8.0

    def output_bits(self, resolution: str | None) -> int | None:
        if not resolution:
            return None
        return 10 if str(resolution).upper() == "1080P" else 8

    def _common(self, p: Mapping[str, Any], parsed: VideoInputs) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model_version,
            "prompt": parsed.prompt,
            "duration": p["duration"],
            "resolution": p["resolution"],
            "ratio": p["ratio"],  # "adaptive" follows the first frame (edits); otherwise the named aspect
            "camera_fixed": p["camera_fixed"],
            "generate_audio": p["generate_audio"],
            "return_last_frame": p["return_last_frame"],
        }
        if "seed" in p:
            body["seed"] = p["seed"]
        return body


class SeedanceI2V(_SeedanceAtlas):
    """seedance-2.5-i2v: `image` is the strict first frame; there are no reference arrays."""

    ENGINE_IDS = ("seedance-2.5-i2v",)

    def build_body(self, parsed, p, dims, client):
        assert parsed.image is not None
        body = self._common(p, parsed)
        body["image"] = self._url(client, parsed.image)
        return body


class SeedanceR2V(_SeedanceAtlas):
    """seedance-2.5-r2v: reference_images [{url, role}] (30 at most; role first_frame locks the frame, reference_image
    is a soft reference), reference_videos [url] (10 at most). Timing hints go inside the prompt (timing_hints)."""

    ENGINE_IDS = ("seedance-2.5-r2v",)

    def build_body(self, parsed, p, dims, client):
        body = self._common(p, parsed)
        body["reference_images"] = [
            {"url": self._url(client, r), "role": r.role or "reference_image"} for r in parsed.references
        ]
        if parsed.videos:
            body["reference_videos"] = [self._url(client, v) for v in parsed.videos]
        return body


class KlingStartEnd(AtlasVideoAdapter):
    """kling-o3-4k and kling-v3-pro: a start frame and an optional end frame (`image_tail`). The client strips pace words
    (slow, gently, leisurely...) from the prompt when an end frame is pinned. `ratio` follows the source dimensions."""

    ENGINE_IDS = ("kling-o3-4k", "kling-v3-pro")
    ETA_BASE_S = 120.0
    ETA_PER_VIDEO_S = 20.0

    def ratio_for(self, params, dims):
        return atlas_ratio(*dims) if dims is not None else None

    def build_body(self, parsed, p, dims, client):
        assert parsed.image is not None
        if dims is None:
            raise InputError(
                f"{self.engine_id}: the ratio comes from the source dimensions; pass w and h on the image "
                "input, a PNG or JPEG file, or the egress size"
            )
        body: dict[str, Any] = {
            "model": self.model_version,
            "image": self._url(client, parsed.image),
            "prompt": parsed.prompt,
            "duration": p["duration"],
            "cfg_scale": p["cfg_scale"],
            "ratio": atlas_ratio(*dims),
        }
        if parsed.image_tail is not None:
            body["image_tail"] = self._url(client, parsed.image_tail)
        if p.get("negative_prompt"):
            body["negative_prompt"] = p["negative_prompt"]
        # The Atlas client only normalises NNNP values, and "4K" is carried by the model slug itself
        # (kwaivgi/kling-video-o3-4k): the quote still sizes the frame 3840x2160, the body names no resolution.
        # UNVERIFIED until the P0 smoke test whether Atlas wants an explicit field for the 4K slug.
        if re.fullmatch(r"[0-9]{3,4}P", str(p.get("resolution") or "")):
            body["resolution"] = p["resolution"]
        if "seed" in p:
            body["seed"] = p["seed"]
        if parsed.elements:
            body["elements"] = [{"image_url": self._url(client, e), "name": e.name} for e in parsed.elements]
        return body


_ATLAS_CLASSES: dict[str, type[AtlasVideoAdapter]] = {
    "seedance-2.5-i2v": SeedanceI2V,
    "seedance-2.5-r2v": SeedanceR2V,
    "kling-o3-4k": KlingStartEnd,
    "kling-v3-pro": KlingStartEnd,
}


def atlas_video_adapter(
    manifest: EngineManifest,
    route: Route | str | None = None,
    transport: httpx.BaseTransport | None = None,
    **kwargs: Any,
) -> AtlasVideoAdapter:
    """The Atlas adapter for a video manifest (ValueError for an engine Atlas does not serve here)."""
    cls = _ATLAS_CLASSES.get(manifest.id)
    if cls is None:
        raise ValueError(f"no Atlas video adapter for {manifest.id!r}; have {sorted(_ATLAS_CLASSES)}")
    return cls(manifest, route, transport, **kwargs)
