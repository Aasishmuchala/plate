"""Seedance 2.5 on BytePlus and WaveSpeed: the same model on another provider, only after a new quote.

New for the PLATE monorepo (router plan T18, BUILD_PLAN decision 13: "fallback only to the same model elsewhere,
re-quoted, never silent"). Two adapters (BytePlusSeedance, WaveSpeedSeedance) serve the seedance-2.5-i2v and
seedance-2.5-r2v manifests on their `byteplus` and `wavespeed` routes, and Fallback ties one of them to the primary Atlas
adapter. The invariants live in code:

* a Fallback exists only when both adapters drive the same manifest and the same `model_id`, and the fallback route
  names the primary's provider in `same_model_fallback_of`;
* Fallback.requote() prices the job on the fallback adapter and runs requote_needed(old, new); Fallback.submit() refuses
  to send anything without that quote, and without `approved=True` whenever requote_needed said the old approval no
  longer covers the new quote. Switching provider always counts as needing approval.

Request shapes. BytePlus follows the public ModelArk task API (POST .../contents/generations/tasks with a `content`
array of text, image_url and video_url items; GET .../tasks/{id}). WaveSpeed follows the v3 task API (POST
/api/v3/{model path}; GET /api/v3/predictions/{id}/result). The Seedance 2.5 model paths and field names on WaveSpeed
(and the exact ModelArk field set for 2.5) are UNVERIFIED until the P0 smoke test reads a real response; the manifest
routes carry no endpoint (`unknown`), so the URLs below are constants here. Images travel as data: URLs inside the request
(nothing is uploaded to a public host); videos must already be https URLs.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..atlas import failure_text
from ..models import EngineManifest, Route
from .atlas_video import (
    InputRef,
    VideoAdapter,
    VideoInputs,
    VideoSubmitError,
    data_url,
    is_video_refusal,
    refusal_from_job_id,
    refused_job_id,
)
from .base import Adapter, InputError, JobStatus, Quote, Staged

__all__ = [
    "BYTEPLUS_TASKS_URL",
    "WAVESPEED_BASE",
    "BytePlusSeedance",
    "Fallback",
    "FallbackError",
    "FallbackQuote",
    "RequoteRequired",
    "WaveSpeedSeedance",
    "requote_needed",
    "seedance_fallbacks",
]

BYTEPLUS_TASKS_URL = "https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks"
WAVESPEED_BASE = "https://api.wavespeed.ai/api/v3"
# Unverified until the P0 smoke test (the manifest routes carry no endpoint or slug for WaveSpeed).
WAVESPEED_PATHS = {
    "seedance-2.5-i2v": "bytedance/seedance-v2.5/image-to-video",
    "seedance-2.5-r2v": "bytedance/seedance-v2.5/reference-to-video",
}
# A price move of more than 5 percent needs a new approval (router plan T12 quotes.requote_needed uses the same rule).
REQUOTE_PRICE_MOVE = 0.05

# ModelArk words its content-policy errors InputImageSensitiveContentDetected.PrivacyInformation and the like.
_BYTEPLUS_REFUSAL = re.compile(r"(sensitive|privacy)", re.IGNORECASE)


# ---- the fallback invariants ---------------------------------------------------------------------------


class FallbackError(ValueError):
    """The two adapters are not a legal primary / fallback pair."""


class RequoteRequired(RuntimeError):
    """Fallback.submit() was called without a fresh quote, or without approval of one that needs it."""


def requote_needed(old: Quote, new: Quote, *, threshold: float = REQUOTE_PRICE_MOVE) -> bool:
    """True when the earlier approval no longer covers the new quote: a different provider or route, a different model
    string, or a price that moved by more than `threshold` (5 percent)."""
    if (old.engine_id, old.route_id) != (new.engine_id, new.route_id):
        return True
    if old.model_version != new.model_version:
        return True
    if old.price_usd == 0:
        return new.price_usd != 0
    return abs(new.price_usd - old.price_usd) / old.price_usd > threshold


@dataclass(frozen=True)
class FallbackQuote:
    """The fallback adapter's own quote for the job, tied to the Fallback that made it."""

    quote: Quote
    requote_needed: bool
    fallback: Fallback


@dataclass(frozen=True)
class Fallback:
    """The same Seedance model on another provider. Built only for a legal pair; used only after a new quote."""

    primary: Adapter
    adapter: Adapter

    def __post_init__(self) -> None:
        a, b = self.primary, self.adapter
        if a.manifest.id != b.manifest.id:
            raise FallbackError(f"fallback must serve the same engine: {a.manifest.id} vs {b.manifest.id}")
        if not a.manifest.model_id or a.manifest.model_id != b.manifest.model_id:
            raise FallbackError(
                f"fallback must serve the same model_id: {a.manifest.model_id!r} vs {b.manifest.model_id!r}"
            )
        if b.route.provider == a.route.provider:
            raise FallbackError("a fallback runs on a different provider than the primary")
        if b.route.same_model_fallback_of != a.route.provider:
            raise FallbackError(
                f"route {b.route.route_id!r} is not declared a same-model fallback of {a.route.provider!r}"
            )

    @property
    def model_id(self) -> str:
        assert self.adapter.manifest.model_id is not None
        return self.adapter.manifest.model_id

    def requote(
        self,
        inputs: Mapping[str, Any],
        params: Mapping[str, Any],
        old: Quote,
        check: Callable[[Quote, Quote], bool] | None = None,
    ) -> FallbackQuote:
        """Quote the job on the fallback adapter and decide whether `old` (the primary's approved quote) still covers it.
        `check` defaults to this module's requote_needed, looked up at call time (the daemon's rule can be passed in)."""
        new = self.adapter.quote(inputs, params)
        decide = check if check is not None else requote_needed
        return FallbackQuote(quote=new, requote_needed=bool(decide(old, new)), fallback=self)

    def submit(self, staged: Staged, quoted: FallbackQuote, *, approved: bool = False) -> str:
        if quoted.fallback is not self:
            raise RequoteRequired("this quote was made for a different fallback; quote again")
        if quoted.requote_needed and not approved:
            raise RequoteRequired(
                f"{self.adapter.engine_id}: moving from {self.primary.route_id} to {self.adapter.route_id} changes the "
                "quote; a human or an envelope must approve the new one before anything is sent"
            )
        return self.adapter.submit(staged)


# ---- shared Seedance fallback behaviour ----------------------------------------------------------------


class _SeedanceFallback(VideoAdapter):
    ENGINE_IDS = ("seedance-2.5-i2v", "seedance-2.5-r2v")
    OUTPUT_FPS = 24.0
    ETA_BASE_S = 90.0
    ETA_PER_VIDEO_S = 8.0
    KEY_ENV = ""

    def __init__(
        self,
        manifest: EngineManifest,
        route: Route | str | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        key_env: str | None = None,
    ) -> None:
        super().__init__(manifest, route, transport)
        self._key_env = key_env or self.KEY_ENV
        self._resolutions: dict[str, str] = {}

    def key(self) -> str:
        value = os.environ.get(self._key_env)
        if not value:
            raise VideoSubmitError(f"{self._key_env} missing; add it to the environment")
        return value

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key()}", "Content-Type": "application/json"}

    def output_bits(self, resolution: str | None) -> int | None:
        if not resolution:
            return None
        return 10 if str(resolution).upper() == "1080P" else 8

    def media(self, ref: InputRef, what: str, *, video: bool = False) -> str:
        """Images are inlined as data: URLs; a video must already be reachable by the vendor (https)."""
        if video and ref.url is None:
            raise InputError(
                f"{what}: {self.route.provider} takes reference videos as https URLs, not local files"
            )
        return data_url(ref)

    def _post(self, url: str, body: dict[str, Any]) -> httpx.Response:
        return self.http().post(url, json=body, headers=self.headers())

    def _get(self, url: str) -> httpx.Response:
        return self.http().get(url, headers=self.headers())

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _submit_failed(self, resp: httpx.Response, data: dict[str, Any], what: str) -> str:
        """A non-2xx submit: a refusal becomes a refused job id, anything else raises."""
        text = failure_text(data)  # reads message-like fields and the string values of an `error` object
        message = f"{what} failed: {resp.status_code} {text or resp.text[:200]}"
        if is_video_refusal(text) or (resp.status_code == 400 and _BYTEPLUS_REFUSAL.search(text)):
            return refused_job_id(message)
        raise VideoSubmitError(message)


# ---- BytePlus ModelArk ---------------------------------------------------------------------------------


class BytePlusSeedance(_SeedanceFallback):
    """Seedance 2.5 on BytePlus ModelArk (route `byteplus`; key in ARK_API_KEY; resolution goes out lower-case)."""

    PROVIDER = "byteplus"
    KEY_ENV = "ARK_API_KEY"

    def build_body(self, parsed: VideoInputs, p: Mapping[str, Any]) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": parsed.prompt}]
        if parsed.image is not None:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self.media(parsed.image, "image")},
                    "role": "first_frame",
                }
            )
        for i, ref in enumerate(parsed.references):
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self.media(ref, f"reference_images[{i}]")},
                    "role": ref.role or "reference_image",
                }
            )
        for i, ref in enumerate(parsed.videos):
            content.append(
                {
                    "type": "video_url",
                    "video_url": {"url": self.media(ref, f"reference_videos[{i}]", video=True)},
                    "role": "reference_video",
                }
            )
        body: dict[str, Any] = {
            "model": self.model_version,
            "content": content,
            "resolution": str(p["resolution"]).lower(),
            "ratio": p["ratio"],
            "duration": p["duration"],
            "camera_fixed": p["camera_fixed"],
            "generate_audio": p["generate_audio"],
            "return_last_frame": p["return_last_frame"],
        }
        if "seed" in p:
            body["seed"] = p["seed"]
        return body

    def submit(self, staged: Staged) -> str:
        parsed = self.parse_inputs(staged.inputs)
        p = self.validate_params(staged.params)
        resp = self._post(BYTEPLUS_TASKS_URL, self.build_body(parsed, p))
        data = self._json(resp)
        if resp.status_code // 100 != 2 or not data.get("id"):
            return self._submit_failed(resp, data, "ModelArk create task")
        job_id = str(data["id"])
        self._resolutions[job_id] = str(p["resolution"])
        return job_id

    def poll(self, provider_job_id: str) -> JobStatus:
        refusal = refusal_from_job_id(provider_job_id)
        if refusal is not None:
            return JobStatus(state="refused", message=refusal)
        resp = self._get(f"{BYTEPLUS_TASKS_URL}/{provider_job_id}")
        d = self._json(resp)
        status = str(d.get("status", "")).lower()
        if status == "succeeded":
            content = d.get("content") if isinstance(d.get("content"), dict) else {}
            urls = [
                u
                for u in (content.get("video_url"), content.get("last_frame_url"))
                if isinstance(u, str) and u
            ]
            if not urls:
                return JobStatus(
                    state="failed", message=f"task succeeded without a video_url: {str(d)[:200]}"
                )
            fps = d.get("framespersecond")
            resolution = d.get("resolution") or self._resolutions.get(provider_job_id)
            usage = d.get("usage") if isinstance(d.get("usage"), dict) else {}
            return JobStatus(
                state="done",
                outputs=urls,
                output_fps=float(fps) if isinstance(fps, (int, float)) and fps > 0 else self.OUTPUT_FPS,
                output_bits=self.output_bits(resolution),
                usage=dict(usage),
            )
        if status in ("failed", "cancelled", "expired"):
            err = d.get("error") if isinstance(d.get("error"), dict) else {}
            text = " ".join(str(v) for v in err.values() if isinstance(v, str))
            message = f"generation {status}: {text or str(d)[:200]}"
            refused = status == "failed" and (is_video_refusal(text) or _BYTEPLUS_REFUSAL.search(text))
            return JobStatus(state="refused" if refused else "failed", message=message)
        return JobStatus(state="queued" if status == "queued" else "running")


# ---- WaveSpeed -----------------------------------------------------------------------------------------


class WaveSpeedSeedance(_SeedanceFallback):
    """Seedance 2.5 on WaveSpeed (route `wavespeed`; key in WAVESPEED_API_KEY; resolution goes out lower-case)."""

    PROVIDER = "wavespeed"
    KEY_ENV = "WAVESPEED_API_KEY"

    @property
    def model_version(self) -> str:
        return self.route.model_slug or WAVESPEED_PATHS[self.manifest.id]

    def build_body(self, parsed: VideoInputs, p: Mapping[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"prompt": parsed.prompt}
        if parsed.image is not None:
            body["image"] = self.media(parsed.image, "image")
        if parsed.references:
            body["reference_images"] = [
                {"url": self.media(r, f"reference_images[{i}]"), "role": r.role or "reference_image"}
                for i, r in enumerate(parsed.references)
            ]
        if parsed.videos:
            body["reference_videos"] = [
                self.media(v, f"reference_videos[{i}]", video=True) for i, v in enumerate(parsed.videos)
            ]
        body.update(
            {
                "duration": p["duration"],
                "resolution": str(p["resolution"]).lower(),
                "camera_fixed": p["camera_fixed"],
                "generate_audio": p["generate_audio"],
                "return_last_frame": p["return_last_frame"],
            }
        )
        if p["ratio"] != "adaptive":  # adaptive is WaveSpeed's default: follow the image
            body["aspect_ratio"] = p["ratio"]
        if "seed" in p:
            body["seed"] = p["seed"]
        return body

    def submit(self, staged: Staged) -> str:
        parsed = self.parse_inputs(staged.inputs)
        p = self.validate_params(staged.params)
        resp = self._post(f"{WAVESPEED_BASE}/{self.model_version}", self.build_body(parsed, p))
        data = self._json(resp)
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        if resp.status_code // 100 != 2 or not inner.get("id"):
            return self._submit_failed(resp, data, "WaveSpeed create prediction")
        job_id = str(inner["id"])
        self._resolutions[job_id] = str(p["resolution"])
        return job_id

    def poll(self, provider_job_id: str) -> JobStatus:
        refusal = refusal_from_job_id(provider_job_id)
        if refusal is not None:
            return JobStatus(state="refused", message=refusal)
        resp = self._get(f"{WAVESPEED_BASE}/predictions/{provider_job_id}/result")
        data = self._json(resp)
        d = data.get("data") if isinstance(data.get("data"), dict) else {}
        status = str(d.get("status", "")).lower()
        outputs = d.get("outputs")
        urls = (
            [u for u in outputs if isinstance(u, str) and u.startswith("http")]
            if isinstance(outputs, list)
            else []
        )
        if status == "completed":
            if not urls:
                return JobStatus(
                    state="failed", message=f"prediction completed without outputs: {str(d)[:200]}"
                )
            return JobStatus(
                state="done",
                outputs=urls,
                output_fps=self.OUTPUT_FPS,
                output_bits=self.output_bits(self._resolutions.get(provider_job_id)),
            )
        if status in ("failed", "error", "cancelled", "canceled"):
            text = failure_text(d)
            message = f"generation {status}: {text or str(d)[:200]}"
            return JobStatus(state="refused" if is_video_refusal(text) else "failed", message=message)
        return JobStatus(state="queued" if status == "created" else "running")


_FALLBACK_CLASSES: dict[str, type[_SeedanceFallback]] = {
    "byteplus": BytePlusSeedance,
    "wavespeed": WaveSpeedSeedance,
}


def seedance_fallbacks(primary: Adapter, transport: httpx.BaseTransport, **keys: str) -> list[Fallback]:
    """Every same-model fallback the manifest declares for `primary`'s provider, as legal Fallback pairs (active routes
    only). `keys` may rename the key variable per provider, for example byteplus="MY_ARK_KEY"."""
    out: list[Fallback] = []
    for route in primary.manifest.routes:
        if route.same_model_fallback_of != primary.route.provider or route.status != "active":
            continue
        cls = _FALLBACK_CLASSES.get(route.provider)
        if cls is None:
            continue
        adapter = cls(primary.manifest, route, transport, key_env=keys.get(route.provider))
        out.append(Fallback(primary, adapter))
    return out
