"""Runway Dev API video adapter: image_to_video, text_to_video reference mode, uploads, poll, cancel, fetch (T118).

New for the PLATE monorepo (Wave 11, decisions 59 to 70). One adapter, `RunwayVideo`, serves every video engine that
has a `runway` route: Seedance 2.5 image-to-video and reference-to-video, Kling O3 4K, Kling 3.0 Pro, Veo 3.1 and
Gen-4.5. It speaks Runway's REST API directly (no SDK: the SDKs lag the API and auto-retry creates that have no
idempotency key) over the guarded transport, so the route's allowed_hosts and, in the daemon, the Egress transport
(host allowlist, PNG strip, leak scan, outbound log) see every request:

    POST {RUNWAY_BASE}/image_to_video   a start frame, or a start + end pair, as promptImage [{uri, position}]
    POST {RUNWAY_BASE}/text_to_video    Seedance 2.5 reference mode: stills as `references`, the clay as
                                        `referenceVideos`; or the 7-day draft enhance {model, draftTaskId} alone
    POST {RUNWAY_BASE}/uploads          then one multipart POST to the presigned uploadUrl (no Runway headers)
    GET  {RUNWAY_BASE}/tasks/{id}       one look, never twice inside 5 s
    DELETE {RUNWAY_BASE}/tasks/{id}     cancel (204; a 404 is ignored, as the vendor says)

Bodies are built against `runway_schema` (T117): `check_body` runs on every body, so a key outside the model's closed
field allowlist fails a test here, never at the vendor. What a model cannot express is refused or folded, never
dropped (decision 69): a negative prompt with no `negativePrompt` field becomes a final `Avoid:` line, a seed on Kling,
`camera_fixed` on Seedance or elements on Kling 3.0 Pro raise. The ratio always follows the source frame within 2.5 %:
Runway centre-crops an off-ratio frame, which would move a crop pair's landing, so such a frame is refused and the
message points at the Atlas route, which follows the source aspect.

`validate_params` takes a `negative_prompt` on every model, also where the manifest declares none (Seedance 2.5,
Gen-4.5), so the fold is reachable; `check_expressible(params, inputs)` runs the same refusals submit runs on the
params and on which inputs are present, without reading a file, so the daemon can skip a route at the quote that
would refuse at submit (Wave 12, T138).

Money rules (decision 65): exactly one create POST per submit and no retry on a timeout, 429 or 5xx (Runway has no
idempotency key and no list-tasks endpoint), so `idempotent_submit` and `supports_lookup` are False and the worker
ends a lost submit as `submit_state_unknown` for a human. An upload that fails is not retried either (the docs say
start over). Failures map by `failureCode` prefix (decision 66) onto refused / `bad_output:` / `input:` /
`transient:` messages; JobState is unchanged. A task that reads SUCCEEDED with no output URL yet is `running`
(transient) for its first `EMPTY_SUCCESS_LOOKS` looks, then `failed`: the output list can lag the status.

Key hygiene: the key is read from RUNWAYML_API_SECRET at call time and appears in the Authorization header of
api.dev.runwayml.com requests only: never on the upload hop, never on a result download, never in a message.
CUSTODY: grade D on the self-serve Terms of Use (training licence, s4.4); the route says so and custody.md has the row.
"""

from __future__ import annotations

import base64
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from blake3 import blake3

from ..models import EngineManifest, EngineSize, Route
from . import runway_schema
from .atlas_video import (
    InputRef,
    VideoAdapter,
    VideoInputs,
    VideoSubmitError,
    refusal_from_job_id,
    refused_job_id,
)
from .base import FetchError, InputError, JobStatus, ParamError, Quote, Staged
from .runway_schema import KEY_ENV, RUNWAY_BASE, RUNWAY_VERSION, ModelSpec

__all__ = ["MIME_EXT", "RunwayVideo"]

# The input types Runway takes (JPEG, PNG, WebP; MP4, MOV, WebM), by file suffix, so the answer is the same on every
# OS (mimetypes reads the Windows registry). GIF is not supported by the vendor.
_SUFFIX_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}
MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
}
_KEY_SHAPE = re.compile(r"key_[0-9a-fA-F]{16,}")
_BAD_OUTPUT_HINT = " (fix the prompt or inputs, then re-quote)"
_TRANSIENT_HINT = " (wait, then re-quote)"
_NO_REFUND = " (credits are not refunded for SAFETY.INPUT.*)"


@dataclass
class _Plan:
    """Everything a request needs except the media URIs, checked before anything leaves (quote and submit share it)."""

    endpoint: str
    spec: ModelSpec
    fields: dict[str, Any]
    tier: str | None
    ratio: str | None
    seconds: int
    notes: list[str] = field(default_factory=list)


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _range_text(values: tuple[int, ...]) -> str:
    """(3, 4, ..., 15) -> "3 to 15"; (4, 6, 8) -> "4, 6, 8"."""
    ordered = sorted(values)
    if len(ordered) > 2 and ordered == list(range(ordered[0], ordered[-1] + 1)):
        return f"{ordered[0]} to {ordered[-1]}"
    return ", ".join(str(v) for v in ordered)


class RunwayVideo(VideoAdapter):
    """The `runway` route of a video engine. The route's model_slug names the Runway model (decision 60)."""

    ENGINE_IDS = ("seedance-2.5-i2v", "seedance-2.5-r2v", "kling-o3-4k", "kling-v3-pro", "veo-3.1", "gen-4.5")
    PROVIDER = "runway"
    OUTPUT_FPS = 24.0
    POLL_INTERVAL_S = 6.0  # the SDKs' cadence (6 s +/- 1.5 s); the vendor asks for 5 s or more
    MIN_POLL_GAP_S = 5.0
    ETA_BASE_S = 90.0
    ETA_PER_VIDEO_S = 8.0
    EMPTY_SUCCESS_LOOKS = 3  # SUCCEEDED without an output reads as running this many looks, then failed

    idempotent_submit = False  # no idempotency key at the vendor: a lost submit is a human's call
    supports_lookup = False  # no list-tasks endpoint: a lost task id cannot be found again

    def __init__(
        self,
        manifest: EngineManifest,
        route: Route | str | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        key_env: str = KEY_ENV,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(manifest, route, transport)
        if not self.route.model_slug:
            raise ValueError(
                f"{manifest.id} via {self.route.route_id}: the runway route must name its Runway model (model_slug)"
            )
        self.model_id = self.route.model_slug
        self._endpoint = "text_to_video" if manifest.kind == "video_r2v" else "image_to_video"
        try:
            runway_schema.spec_for(self.model_id, self._endpoint)
        except KeyError as exc:
            raise ValueError(f"{manifest.id} via runway: {exc.args[0]}") from None
        self._key_env = key_env
        self._now = now
        self._costs: dict[str, Any] = {}  # task id -> estimatedCost.credits (the most the task may charge)
        self._tiers: dict[str, str | None] = {}  # task id -> resolution tier asked for (output_bits)
        self._notes: dict[str, list[str]] = {}  # task id -> what the route did that the plan did not say
        self._looks: dict[str, tuple[float, JobStatus]] = {}  # task id -> (when, the answer) for the 5 s rule
        self._empty: dict[str, int] = {}  # task id -> looks that read SUCCEEDED without an output
        self._last_notes: list[str] = []  # the notes and tier of the last build_body, filed by submit
        self._last_tier: str | None = None

    # ---- key and headers --------------------------------------------------------------------------------

    def _key(self) -> str:
        key = (os.environ.get(self._key_env) or "").strip()
        if not key:
            raise VideoSubmitError(f"no Runway key: set {self._key_env}")
        return key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key()}",
            "X-Runway-Version": RUNWAY_VERSION,
            "Content-Type": "application/json",
        }

    def _safe(self, text: str) -> str:
        """A vendor message with the key (or anything shaped like a Runway key) blanked out."""
        key = (os.environ.get(self._key_env) or "").strip()
        if key:
            text = text.replace(key, "<redacted>")
        return _KEY_SHAPE.sub("<redacted>", text)

    def _error_text(self, resp: httpx.Response) -> str:
        err = _json(resp).get("error")
        text = str(err) if err else resp.text[:200]
        return self._safe(text.strip())

    # ---- params -----------------------------------------------------------------------------------------

    def validate_params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """The manifest schema, after mapping a resolution spelling onto the manifest's own (1080p / 1080 / 1080P).
        A `negative_prompt` is taken on every model, also where the manifest declares none (Seedance 2.5, Gen-4.5):
        a model with no `negativePrompt` field gets it folded into `promptText` as the final `Avoid:` line (decision
        69), so it is neither refused for want of a field nor dropped."""
        params = dict(params)
        res = params.get("resolution")
        if res is not None and not isinstance(res, bool):
            enum = next((p.enum for p in self.manifest.params if p.name == "resolution"), None) or []
            text = str(res).strip().upper()
            want = text + "P" if text.isdigit() else text
            match = next((e for e in enum if e.upper() == want), None)
            if match is not None:
                params["resolution"] = match
        negative = self._folded_negative(params)
        out = super().validate_params(params)
        if negative:
            out["negative_prompt"] = negative
        return out

    def _folded_negative(self, params: dict[str, Any]) -> str | None:
        """Take a `negative_prompt` the manifest does not declare out of `params` when this model folds it (no
        `negativePrompt` field); the manifest schema checks every other name as before."""
        if "negative_prompt" not in params or any(p.name == "negative_prompt" for p in self.manifest.params):
            return None
        if runway_schema.spec_for(self.model_id, self._endpoint).has_negative_prompt:
            return None  # a model with the field: the manifest has to declare it
        value = params.pop("negative_prompt")
        if value is None:
            return None
        if not isinstance(value, str):
            raise ParamError(
                f"{self.engine_id}: negative_prompt: expected a string, got {type(value).__name__}"
            )
        return value.strip() or None

    def clamp_duration(self, seconds: float) -> int:
        """Ceil into the manifest range, then up to a duration this Runway model accepts (Kling 1 s -> 3 s)."""
        return runway_schema.snap_duration(self.model_id, super().clamp_duration(seconds))

    # ---- the plan: every refusal before any byte leaves --------------------------------------------------

    def _tier(self, spec: ModelSpec, p: Mapping[str, Any]) -> str | None:
        """The ratio tier: the model's only one (Kling), the resolution param's (Seedance, Veo), or None (Gen-4.5)."""
        tiers = [k for k in spec.ratios if k != "all"]
        if not tiers:
            return None
        if len(tiers) == 1:
            res = p.get("resolution")
            if res is not None and str(res).upper() != tiers[0].upper():
                raise ParamError(
                    f"{self.engine_id} via runway: resolution {res!r} is not {self.model_id}'s tier {tiers[0]}; "
                    "pin the atlas route for another resolution"
                )
            return tiers[0]
        res = p.get("resolution")
        if res is None:
            return None
        wanted = str(res).upper()
        if wanted not in (t.upper() for t in tiers):
            raise ParamError(f"{self.engine_id} via runway: resolution {res!r} is not one of {tiers}")
        return next(t for t in tiers if t.upper() == wanted)

    def _legal(self, spec: ModelSpec, tier: str | None) -> tuple[str, ...]:
        if tier is not None:
            for label, ratios in spec.ratios.items():
                if label.upper() == tier.upper():
                    return ratios
        return tuple(r for ratios in spec.ratios.values() for r in ratios)

    def _ratio_param(self) -> tuple[str | None, bool]:
        """(name of the manifest's named-ratio param, whether it offers `adaptive`)."""
        for p in self.manifest.params:
            if p.name in ("ratio", "aspect_ratio") and p.type == "enum":
                return p.name, "adaptive" in (p.enum or [])
        return None, False

    def _ratio(
        self,
        spec: ModelSpec,
        p: Mapping[str, Any],
        dims: tuple[int, int] | None,
        tier: str | None,
        notes: list[str],
        *,
        need: bool,
    ) -> str | None:
        """The ratio string. A named ratio wins only where the manifest's ratio param offers `adaptive` (Seedance,
        as on Atlas); everywhere else the source frame decides (Kling, Veo, Gen-4.5), and with no frame known there
        is no ratio: submit refuses, a quote sizes "auto". A frame no legal ratio fits within 2.5 % is refused
        (Runway would centre-crop it, which moves a crop pair's landing)."""
        model = self.model_id
        param, has_adaptive = self._ratio_param()
        name = p.get(param) if param else None
        named = name if name not in (None, "adaptive") else None
        if named is not None and has_adaptive:
            ratio = runway_schema.named_ratio_for(model, str(named), tier)
            if ratio is None:
                raise InputError(
                    f"{self.engine_id} via runway: no legal ratio for {named} on {model} at {tier}; "
                    f"legal: {', '.join(self._legal(spec, tier))}"
                )
            return ratio
        if dims is not None:
            w, h = dims
            ratio = runway_schema.ratio_for(model, w, h, tier)
            if ratio is None:
                raise InputError(
                    f"{self.engine_id} via runway: no legal ratio for {w}x{h} on {model}; "
                    f"legal: {', '.join(self._legal(spec, tier))}; the atlas route follows the source aspect"
                )
            if named is not None:
                try:
                    asked = runway_schema.named_ratio_for(model, str(named), tier)
                except ValueError:
                    asked = None
                if asked != ratio:
                    notes.append(f"{param} {named} not sent: the runway route follows the frame ({ratio})")
            return ratio
        if need:
            raise InputError(
                f"{self.engine_id} via runway: the ratio comes from the source dimensions; pass w and h on the "
                "image input, a PNG or JPEG file, or the egress size"
            )
        return None

    def check_expressible(self, params: Mapping[str, Any], inputs: Mapping[str, Any] | None = None) -> None:
        """The refusals submit runs for what this Runway model cannot express (decision 69), on the params (as
        `validate_params` returns them) and on which inputs are present: a seed on Kling, `cfg_scale` off the
        default on Kling O3, `camera_fixed` or `return_last_frame` on Seedance, an end frame on a start-only model,
        elements where the model takes none or too many, and a prompt over the cap once a negative prompt is
        folded in. No file is read and nothing leaves: the daemon calls it at the quote, so a route that would
        refuse at submit is skipped before a run exists. Raises ParamError or InputError, as submit would."""
        inputs = inputs or {}
        p = dict(params)
        spec = runway_schema.spec_for(self.model_id, self._endpoint)
        if self._endpoint == "text_to_video":
            tail, elements, keyframes = False, 0, 0
        else:
            tail = any(inputs.get(k) is not None for k in ("image_tail", "end_image"))
            raw = inputs.get("elements")
            elements = len(raw) if isinstance(raw, (list, tuple)) else int(raw is not None)
            keyframes = 1 + tail
        self._refuse_inexpressible(spec, p, tail=tail, elements=elements, keyframes=keyframes)
        prompt = inputs.get("prompt")
        if isinstance(prompt, str):
            try:
                runway_schema.prompt_text(self.model_id, prompt, p.get("negative_prompt"))
            except ValueError as exc:
                raise InputError(f"{self.engine_id} via runway: {exc}") from None

    def _refuse_inexpressible(
        self, spec: ModelSpec, p: Mapping[str, Any], *, tail: bool, elements: int, keyframes: int
    ) -> None:
        eid, model = self.engine_id, self.model_id
        pin = "not expressible on the runway route; pin the atlas route"
        if "seed" in p and not spec.has_seed:
            raise ParamError(f"{eid}: seed is {pin} ({model} has no seed field)")
        if "cfg_scale" in p and not spec.has_cfg_scale:
            default = next((q.default for q in self.manifest.params if q.name == "cfg_scale"), None)
            if p["cfg_scale"] != default:
                raise ParamError(
                    f"{eid}: cfg_scale {p['cfg_scale']} is {pin} ({model} has no cfgScale field)"
                )
        for flag in ("camera_fixed", "return_last_frame"):
            if p.get(flag) is True:
                raise ParamError(f"{eid}: {flag} true is {pin}")
        if tail and spec.keyframes != "first_last":
            raise InputError(f"{eid} via runway: {model} takes a start frame only, no end frame")
        if elements:
            if spec.references != "references_field":
                raise InputError(f"{eid}: elements are {pin} ({model} takes no reference images)")
            room = (spec.references_max or 0) - keyframes
            if elements > room:
                raise InputError(
                    f"{eid} via runway: {elements} elements and {keyframes} keyframe(s); {model} takes "
                    f"at most {spec.references_max} images in all, so at most {room} elements"
                )
        negative = p.get("negative_prompt")
        if spec.has_negative_prompt and negative and len(str(negative)) > 1000:
            raise ParamError(f"{eid}: negative_prompt is {len(str(negative))} characters; the limit is 1000")

    def _plan(
        self, parsed: VideoInputs, p: Mapping[str, Any], dims: tuple[int, int] | None, *, need_ratio: bool
    ) -> _Plan:
        eid, model = self.engine_id, self.model_id
        if self.manifest.kind == "video_r2v":
            endpoint = "text_to_video"
            keyframes = 0
        else:
            endpoint = "image_to_video"
            if parsed.image is None:
                raise InputError(f"{eid} via runway: a start frame is required")
            keyframes = 1 + (parsed.image_tail is not None)
        spec = runway_schema.spec_for(model, endpoint)
        self._refuse_inexpressible(
            spec, p, tail=parsed.image_tail is not None, elements=len(parsed.elements), keyframes=keyframes
        )
        notes: list[str] = []
        tier = self._tier(spec, p)
        ratio = self._ratio(spec, p, dims, tier, notes, need=need_ratio)
        asked = p[self.duration_param()]
        seconds = runway_schema.snap_duration(model, float(asked))
        if seconds != asked:
            notes.append(f"duration {asked} -> {seconds} ({model} accepts {_range_text(spec.durations)})")
        try:
            text = runway_schema.prompt_text(model, parsed.prompt, p.get("negative_prompt"))
        except ValueError as exc:
            raise InputError(f"{eid} via runway: {exc}") from None
        fields: dict[str, Any] = {"model": model}
        if text:
            fields["promptText"] = text
        if ratio is not None:
            fields["ratio"] = ratio
        fields["duration"] = seconds
        if spec.has_audio:
            fields["audio"] = bool(p.get("generate_audio", False))
        if "seed" in p:
            if int(p["seed"]) < 0:
                notes.append(f"seed {p['seed']} (random) not sent: Runway draws a seed when none is given")
            else:
                fields["seed"] = int(p["seed"])
        if spec.has_negative_prompt and p.get("negative_prompt"):
            fields["negativePrompt"] = p["negative_prompt"]
        if spec.has_cfg_scale and "cfg_scale" in p:
            fields["cfgScale"] = p["cfg_scale"]
        if spec.has_draft and tier is not None and tier.upper() == "480P":
            fields["draft"] = True  # Runway's 480p preview is PLATE's 480P tier (decision 68)
        return _Plan(endpoint, spec, fields, tier, ratio, seconds, notes)

    # ---- media ------------------------------------------------------------------------------------------

    def _uri(self, ref: InputRef, mime: str = "image/png") -> str:
        """A URI Runway can read: an https or data: URL as given; a local image inline as a data URI when it fits
        the 5,242,880-character cap, else (and every local video) one ephemeral upload."""
        if ref.url is not None:
            if ref.url.startswith("data:") and len(ref.url) > runway_schema.DATA_URI_MAX_CHARS:
                raise InputError(
                    f"{self.engine_id} via runway: a data: URL of {len(ref.url)} characters is over the "
                    f"{runway_schema.DATA_URI_MAX_CHARS} cap; pass the file instead (it is uploaded)"
                )
            return ref.url
        assert ref.path is not None
        path = Path(ref.path)
        actual = _SUFFIX_MIME.get(path.suffix.lower())
        if actual is None or actual.split("/")[0] != mime.split("/")[0]:
            kinds = sorted(s for s, m in _SUFFIX_MIME.items() if m.split("/")[0] == mime.split("/")[0])
            raise InputError(f"{self.engine_id} via runway: {path.name} is not a {', '.join(kinds)} file")
        if actual.startswith("image/") and runway_schema.data_uri_fits(path.stat().st_size, actual):
            return f"data:{actual};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        return self._upload(path, actual)

    def _upload(self, path: Path, mime: str) -> str:
        """POST /v1/uploads, then the multipart POST of `fields` + `file` to the presigned uploadUrl without the
        Runway headers. One attempt each: a failed upload is never retried (the vendor says start over). An upload
        host outside the route's allowed_hosts raises HostNotAllowed from the transport, naming the host."""
        data = path.read_bytes()
        # Opaque: the bytes' hash names the upload, never the client's file name.
        filename = f"{blake3(data).hexdigest()[:26]}.{MIME_EXT[mime]}"
        resp = self.http().request(
            "POST",
            f"{RUNWAY_BASE}/uploads",
            json={"filename": filename, "type": "ephemeral"},
            headers=self._headers(),
        )
        d = _json(resp)
        upload_url, fields, uri = d.get("uploadUrl"), d.get("fields"), d.get("runwayUri")
        if (
            resp.status_code != 200
            or not isinstance(upload_url, str)
            or not isinstance(fields, dict)
            or not isinstance(uri, str)
        ):
            raise VideoSubmitError(f"runway upload init failed: {resp.status_code} {self._error_text(resp)}")
        if urlsplit(upload_url).scheme != "https":
            raise VideoSubmitError("runway upload init failed: the uploadUrl is not https; nothing was sent")
        if not uri.startswith("runway://"):
            raise VideoSubmitError("runway upload init failed: runwayUri is not a runway:// URI")
        put = self.http().post(
            upload_url,
            data={str(k): str(v) for k, v in fields.items()},
            files={"file": (filename, data, mime)},
        )
        if put.status_code // 100 != 2:
            raise VideoSubmitError(f"runway upload failed: {put.status_code}; not retried (start over)")
        return uri

    # ---- bodies -----------------------------------------------------------------------------------------

    def build_body(
        self, parsed: VideoInputs, p: Mapping[str, Any], dims: tuple[int, int] | None
    ) -> tuple[str, dict[str, Any]]:
        """(endpoint, body). Every refusal runs first; media resolves (and uploads) last; check_body runs on the
        result. The notes of this build are left on `_last_notes` for submit to file under the task id."""
        plan = self._plan(parsed, p, dims, need_ratio=True)
        body: dict[str, Any] = {"model": plan.fields["model"]}
        notes = list(plan.notes)
        if plan.endpoint == "image_to_video":
            assert parsed.image is not None
            frames = [{"uri": self._uri(parsed.image), "position": "first"}]
            if parsed.image_tail is not None:
                frames.append({"uri": self._uri(parsed.image_tail), "position": "last"})
            body["promptImage"] = frames
        body.update((k, v) for k, v in plan.fields.items() if k != "model")
        if plan.endpoint == "image_to_video" and parsed.elements:
            body["references"] = [{"uri": self._uri(e)} for e in parsed.elements]
        if plan.endpoint == "text_to_video":
            if any(r.role == "first_frame" for r in parsed.references):
                notes.append("reference roles are not a Runway field: first_frame sent as a plain reference")
            body["references"] = [{"uri": self._uri(r)} for r in parsed.references]
            if parsed.videos:
                body["referenceVideos"] = [
                    {"type": "video", "uri": self._uri(v, "video/mp4")} for v in parsed.videos
                ]
        runway_schema.check_body(self.model_id, plan.endpoint, body)
        self._last_notes = notes
        self._last_tier = plan.tier
        return plan.endpoint, body

    @staticmethod
    def _draft_task(inputs: Mapping[str, Any]) -> Any:
        return inputs.get("draft_task_id")

    def _enhance_body(self, staged: Staged) -> tuple[str, dict[str, Any]]:
        """The 7-day draft enhance: {model, draftTaskId} alone on text_to_video (anything else is refused)."""
        eid = self.engine_id
        if (
            self.manifest.kind != "video_r2v"
            or "draftTaskId" not in runway_schema.spec_for(self.model_id, "text_to_video").fields
        ):
            raise InputError(f"{eid} via runway: only Seedance 2.5 reference mode enhances a draft")
        task = staged.inputs.get("draft_task_id")
        if not isinstance(task, str) or not task.strip():
            raise InputError(f"{eid} via runway: draft_task_id must be the succeeded draft's task id")
        others = sorted(
            k for k, v in staged.inputs.items() if k != "draft_task_id" and v not in (None, "", [], ())
        )
        if others:
            raise InputError(f"{eid} via runway: a draft enhance sends only the draft task id; drop {others}")
        if self.validate_params(staged.params) != self.validate_params({}):
            raise InputError(
                f"{eid} via runway: a draft enhance takes no params (the draft fixed them); send the defaults"
            )
        body = {"model": self.model_id, "draftTaskId": task.strip()}
        runway_schema.check_body(self.model_id, "text_to_video", body)
        self._last_notes = []
        self._last_tier = "1080P"  # an enhance renders (and bills) at 1080p
        return "text_to_video", body

    # ---- the interface ----------------------------------------------------------------------------------

    def submit(self, staged: Staged) -> str:
        """Exactly one create POST. A refusal at create (a 400 whose reason is SAFETY) comes back as a stateless
        refused job id; every other non-200 raises VideoSubmitError and nothing is retried."""
        headers = self._headers()  # no key, no uploads: fail before anything leaves
        if self._draft_task(staged.inputs) is not None:
            endpoint, body = self._enhance_body(staged)
        else:
            parsed = self.parse_inputs(staged.inputs)
            p = self.validate_params(staged.params)
            endpoint, body = self.build_body(parsed, p, self.source_dims(parsed, staged.size))
        notes, tier = list(self._last_notes), self._last_tier
        resp = self.http().request("POST", f"{RUNWAY_BASE}/{endpoint}", json=body, headers=headers)
        status = resp.status_code
        d = _json(resp)
        if status == 200:
            job = d.get("id")
            if not isinstance(job, str) or not job:
                raise VideoSubmitError("runway 200 without a task id; check the dev portal Request History")
            cost = d.get("estimatedCost")
            if isinstance(cost, dict) and cost.get("credits") is not None:
                self._costs[job] = cost["credits"]
            self._tiers[job] = tier
            self._notes[job] = notes
            return job
        error = self._error_text(resp)
        if status == 400:
            reason = self._safe(str(d.get("reason") or "").strip())
            if reason and runway_schema.classify_failure(reason) == "refused":
                return refused_job_id(f"runway:{reason}: {error}")
            issues = d.get("issues") if isinstance(d.get("issues"), list) else []
            paths = [
                "/".join(str(x) for x in (i.get("path") or []))
                for i in issues
                if isinstance(i, dict) and i.get("path")
            ]
            raise VideoSubmitError(
                "runway 400"
                + (f" {reason}" if reason else "")
                + f": {error}"
                + (f" at {', '.join(paths)}" if paths else "")
            )
        if status == 401:
            raise VideoSubmitError(f"runway 401: the key was refused ({self._key_env})")
        if status == 429:
            raise VideoSubmitError("runway 429: daily generation cap or rate limit; not retried")
        if status >= 500:
            raise VideoSubmitError(
                f"runway {status}: not retried (no idempotency key; check the dev portal Request History "
                "before re-quoting)"
            )
        raise VideoSubmitError(f"runway {status}: {error}")

    def poll(self, provider_job_id: str, resolution: str | None = None) -> JobStatus:
        """One look, and never a second request for the same task inside MIN_POLL_GAP_S (the cached answer instead).
        `resolution` (optional) lets a restarted runner say what it asked for, for output_bits."""
        refusal = refusal_from_job_id(provider_job_id)
        if refusal is not None:
            return JobStatus(state="refused", message=refusal)
        t = self._now()
        last = self._looks.get(provider_job_id)
        if last is not None and t - last[0] < self.MIN_POLL_GAP_S:
            return last[1]
        resp = self.http().request(
            "GET", f"{RUNWAY_BASE}/tasks/{quote(str(provider_job_id), safe='')}", headers=self._headers()
        )
        status = self._status(provider_job_id, resp, resolution)
        self._looks[provider_job_id] = (t, status)
        return status

    def _status(self, job: str, resp: httpx.Response, resolution: str | None) -> JobStatus:
        code = resp.status_code
        if code == 404:
            return JobStatus(state="failed", message="task not found (deleted or cancelled)")
        if code == 429 or code >= 500:
            return JobStatus(state="running", message=f"transient {code}")
        if code == 401:
            return JobStatus(
                state="failed",
                message=f"runway 401 on poll: the key was refused ({self._key_env}); the task may still run",
            )
        if code // 100 != 2:
            return JobStatus(state="failed", message=f"runway {code} on poll: {self._error_text(resp)}")
        d = _json(resp)
        est = d.get("estimatedCost")
        if isinstance(est, dict) and est.get("credits") is not None:
            self._costs[job] = est["credits"]
        state = str(d.get("status") or "").upper()
        if state in ("PENDING", "THROTTLED"):
            msg = (
                "throttled: at the account's concurrency limit, queued at the vendor"
                if state == "THROTTLED"
                else ""
            )
            return JobStatus(state="queued", message=msg)
        if state == "RUNNING":
            progress = d.get("progress")
            msg = f"progress {float(progress):.2f}" if isinstance(progress, (int, float)) else ""
            return JobStatus(state="running", message=msg)
        usage = self._usage(job, d)
        if state == "SUCCEEDED":
            outputs = [str(u) for u in d.get("output") or [] if isinstance(u, str) and u]
            if not outputs:
                # The output list can lag the status: the first looks wait for it, then the task is failed.
                looks = self._empty[job] = self._empty.get(job, 0) + 1
                return JobStatus(
                    state="running" if looks <= self.EMPTY_SUCCESS_LOOKS else "failed",
                    message="transient: runway SUCCEEDED without an output",
                    usage=usage,
                )
            tier = resolution or self._tiers.get(job)
            bits = 10 if self.model_id == "seedance2_5" and tier and str(tier).upper() == "1080P" else None
            return JobStatus(
                state="done", outputs=outputs, output_fps=self.OUTPUT_FPS, output_bits=bits, usage=usage
            )
        if state == "FAILED":
            failure_code = d.get("failureCode")
            code_text = self._safe(str(failure_code)) if failure_code else "none"
            kind = runway_schema.classify_failure(failure_code if isinstance(failure_code, str) else None)
            message = f"{kind}: runway:{code_text}: {self._safe(str(d.get('failure') or ''))}"
            if kind == "refused":
                if code_text.upper().startswith("SAFETY.INPUT."):
                    message += _NO_REFUND
                return JobStatus(state="refused", message=message, usage=usage)
            if kind == "bad_output":
                message += _BAD_OUTPUT_HINT
            elif kind == "transient":
                message += _TRANSIENT_HINT
            return JobStatus(state="failed", message=message, usage=usage)
        if state == "CANCELLED":
            return JobStatus(state="failed", message="cancelled at the vendor", usage=usage)
        return JobStatus(state="running", message=f"transient: unknown task status {state or 'none'!r}")

    def _usage(self, job: str, d: Mapping[str, Any]) -> dict[str, Any]:
        cost = d.get("cost")
        usage: dict[str, Any] = {}
        if isinstance(cost, dict) and cost.get("credits") is not None:
            usage["credits"] = cost["credits"]
            usage["usd"] = round(float(cost["credits"]) * runway_schema.USD_PER_CREDIT, 6)
        if job in self._costs:
            usage["estimated_credits"] = self._costs[job]
        return usage

    def cancel(self, provider_job_id: str) -> None:
        """DELETE /v1/tasks/{id}: 204 is done, a 404 is ignored (already cancelled or deleted)."""
        if refusal_from_job_id(provider_job_id) is not None:
            return  # refused at submit: there is no task at the vendor
        resp = self.http().request(
            "DELETE", f"{RUNWAY_BASE}/tasks/{quote(str(provider_job_id), safe='')}", headers=self._headers()
        )
        if resp.status_code // 100 == 2 or resp.status_code == 404:
            return
        raise VideoSubmitError(f"runway cancel {resp.status_code}: {self._error_text(resp)}")

    def fetch(self, url: str) -> bytes:
        """The base download (allowlisted host, no credentials), https only."""
        if urlsplit(url).scheme != "https":
            raise FetchError(f"runway outputs are https URLs; refusing {url[:60]!r}")
        return super().fetch(url)

    def notes(self, provider_job_id: str) -> list[str]:
        """What this route did that the plan did not say (a duration snap, reference roles), per task id."""
        return list(self._notes.get(provider_job_id, ()))

    # ---- prices and quotes ------------------------------------------------------------------------------

    def price_for(self, params: Mapping[str, Any], seconds: float) -> float:
        """Runway's catalogue (USD 0.01 a credit): per second of the snapped duration, floored per generation."""
        model = self.model_id
        res = params.get("resolution")
        unit = runway_schema.usd_per_second(
            model,
            resolution=str(res) if res is not None else None,
            audio=bool(params.get("generate_audio", False)),
        )
        billed = unit * runway_schema.snap_duration(model, seconds)
        return round(max(billed, runway_schema.minimum_usd(model)), 6)

    def ratio_for(self, params: Mapping[str, Any], dims: tuple[int, int] | None) -> str | None:
        """The Runway ratio this job would ask for, or None when it cannot be told (or the frame fits none)."""
        spec = runway_schema.spec_for(
            self.model_id, "text_to_video" if self.manifest.kind == "video_r2v" else "image_to_video"
        )
        try:
            return self._ratio(spec, params, dims, self._tier(spec, params), [], need=False)
        except (InputError, ParamError):
            return None

    def quote(self, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Quote:
        """Manifest data and the Runway tables only, no request: every refusal submit would raise, raised here."""
        if self._draft_task(inputs) is not None:
            raise InputError(
                f"{self.engine_id} via runway: a draft enhance is billed at 1080p for the draft's own length, which "
                "this adapter does not know; quote it from the draft's run"
            )
        p = self.validate_params(params)
        parsed = self.parse_inputs(inputs)
        plan = self._plan(parsed, p, self.source_dims(parsed), need_ratio=False)
        if plan.ratio is not None:
            w, h = (int(x) for x in plan.ratio.split(":"))
            size = EngineSize(w=w, h=h, label=plan.tier or "")
        else:
            size = EngineSize(w=0, h=0, label="auto")
        price = self.price_for(p, float(p[self.duration_param()]))
        return self.make_quote(price, self.ETA_BASE_S + self.ETA_PER_VIDEO_S * plan.seconds, size)

    def make_quote(self, price_usd: float, eta_s: float, size: EngineSize) -> Quote:
        """As the base, with the price status of the price that applies: the route's own when it carries one."""
        q = super().make_quote(price_usd, eta_s, size)
        spec = self.route.price or self.manifest.price
        return q.model_copy(update={"price_status": spec.status})
