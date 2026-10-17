"""Veo 3.1 on Vertex AI: image-to-video with an optional end frame (route `vertex`, a C1 candidate).

New for the PLATE monorepo (router plan T18). Long-running prediction in two calls:
    POST https://{region}-aiplatform.googleapis.com/v1/projects/{p}/locations/{region}/publishers/google/models/
         veo-3.1-generate-001:predictLongRunning       body {instances: [{prompt, image: {bytesBase64Encoded, mimeType}}],
                                                              parameters: {durationSeconds, aspectRatio, resolution, ...}}
    POST ...:fetchPredictOperation                     body {operationName}      (poll)
Images are read from local files and sent inline, so nothing is uploaded to a public host. The finished video arrives
either inline (`bytesBase64Encoded`, the default) or as a `gcsUri` when a storage URI was requested. Inline videos are
held by the adapter and handed out as `plate-staged://<name>.mp4`; fetch() resolves those, and gs:// URIs through the
GCS JSON API with the same bearer token.

CUSTODY: Vertex terms (zero retention, regional availability) are not confirmed in writing, so the route is declared C1
`pending_written_terms` and the router treats it as D. The default region `asia-south1` is flagged
`availability_unverified` (Veo 3.1 may not be served there); name a region to clear the flag.

Auth is an OAuth2 bearer token (gcloud auth print-access-token): pass `token` (a string or a callable) or set
VERTEX_ACCESS_TOKEN; the project comes from `project` or GOOGLE_CLOUD_PROJECT. Duration is the manifest's 4 to 8 s; the
vendor may restrict it further (4, 6 or 8) and 1080p to 8 s, unverified until the P0 smoke test.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx
from blake3 import blake3

from ..models import EngineManifest, Route
from .atlas_video import (
    InputRef,
    VideoAdapter,
    VideoSubmitError,
    is_video_refusal,
    mime_of,
    refusal_from_job_id,
    refused_job_id,
)
from .base import FetchError, InputError, JobStatus, Staged

__all__ = ["DEFAULT_REGION", "MODEL", "STAGED_SCHEME", "VertexVeo"]

MODEL = "veo-3.1-generate-001"
DEFAULT_REGION = "asia-south1"
STAGED_SCHEME = "plate-staged://"
FLAG_AVAILABILITY = "availability_unverified"


class VertexVeo(VideoAdapter):
    ENGINE_IDS = ("veo-3.1",)
    PROVIDER = "vertex"
    OUTPUT_FPS = 24.0
    ETA_BASE_S = 60.0
    ETA_PER_VIDEO_S = 10.0

    def __init__(
        self,
        manifest: EngineManifest,
        route: Route | str | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        project: str | None = None,
        region: str | None = None,
        token: str | Callable[[], str] | None = None,
        token_env: str = "VERTEX_ACCESS_TOKEN",
        project_env: str = "GOOGLE_CLOUD_PROJECT",
    ) -> None:
        super().__init__(manifest, route, transport)
        self.region = region or DEFAULT_REGION
        self.project = project or os.environ.get(project_env) or None
        self._token = token
        self._token_env = token_env
        self._staged: dict[str, bytes] = {}

    @property
    def flags(self) -> tuple[str, ...]:
        """`availability_unverified` while the region is the unverified default."""
        return (FLAG_AVAILABILITY,) if self.region == DEFAULT_REGION else ()

    # ---- request plumbing -------------------------------------------------------------------------------

    def _bearer(self) -> str:
        value = self._token() if callable(self._token) else self._token
        value = value or os.environ.get(self._token_env)
        if not value:
            raise VideoSubmitError(f"no Vertex access token: pass token= or set {self._token_env}")
        return value

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer()}", "Content-Type": "application/json"}

    def _model_url(self, verb: str) -> str:
        if not self.project:
            raise VideoSubmitError("no Google Cloud project: pass project= or set GOOGLE_CLOUD_PROJECT")
        host = f"https://{self.region}-aiplatform.googleapis.com"
        return f"{host}/v1/projects/{self.project}/locations/{self.region}/publishers/google/models/{MODEL}:{verb}"

    @staticmethod
    def _inline(ref: InputRef | None, what: str) -> dict[str, str] | None:
        if ref is None:
            return None
        if ref.path is None:
            raise InputError(f"{what}: Veo on Vertex takes local files (sent inline), not URLs")
        return {
            "bytesBase64Encoded": base64.b64encode(ref.path.read_bytes()).decode("ascii"),
            "mimeType": mime_of(ref.path),
        }

    def build_body(self, staged: Staged) -> dict[str, Any]:
        parsed = self.parse_inputs(staged.inputs)
        p = self.validate_params(staged.params)
        instance: dict[str, Any] = {
            "prompt": parsed.prompt,
            "image": self._inline(parsed.image, "start_image"),
        }
        if parsed.image_tail is not None:
            instance["lastFrame"] = self._inline(parsed.image_tail, "end_image")
        parameters: dict[str, Any] = {
            "durationSeconds": p["duration"],
            "aspectRatio": p["aspect_ratio"],
            "resolution": str(p["resolution"]).lower(),
            "generateAudio": p["generate_audio"],
            "sampleCount": 1,
        }
        if p.get("negative_prompt"):
            parameters["negativePrompt"] = p["negative_prompt"]
        if "seed" in p:
            parameters["seed"] = p["seed"]
        return {"instances": [instance], "parameters": parameters}

    def validate_params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        params = dict(params)
        if isinstance(params.get("resolution"), str):
            params["resolution"] = (
                params["resolution"].strip().lower()
            )  # Vertex wants 720p / 1080p in lower case
        return super().validate_params(params)

    # ---- the interface ----------------------------------------------------------------------------------

    def submit(self, staged: Staged) -> str:
        body = self.build_body(staged)
        resp = self.http().post(self._model_url("predictLongRunning"), json=body, headers=self._headers())
        data = self._json(resp)
        if resp.status_code // 100 != 2 or not isinstance(data.get("name"), str):
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            text = str(err.get("message", "")) or resp.text[:200]
            message = f"predictLongRunning failed: {resp.status_code} {text}"
            if is_video_refusal(text):
                return refused_job_id(message)
            raise VideoSubmitError(message)
        return data["name"]

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def poll(self, provider_job_id: str) -> JobStatus:
        refusal = refusal_from_job_id(provider_job_id)
        if refusal is not None:
            return JobStatus(state="refused", message=refusal)
        resp = self.http().post(
            self._model_url("fetchPredictOperation"),
            json={"operationName": provider_job_id},
            headers=self._headers(),
        )
        d = self._json(resp)
        if resp.status_code // 100 != 2:
            if resp.status_code == 429 or resp.status_code >= 500:
                return JobStatus(state="running", message=f"transient {resp.status_code}")
            return JobStatus(state="failed", message=f"fetchPredictOperation failed: {resp.status_code}")
        if not d.get("done"):
            return JobStatus(state="running")
        err = d.get("error")
        if isinstance(err, dict):
            text = str(err.get("message", ""))
            state = "refused" if is_video_refusal(text) else "failed"
            return JobStatus(state=state, message=f"operation error {err.get('code', '')}: {text}")
        result = d.get("response") if isinstance(d.get("response"), dict) else {}
        videos = result.get("videos") if isinstance(result.get("videos"), list) else []
        outputs: list[str] = []
        for v in videos:
            if not isinstance(v, dict):
                continue
            if isinstance(v.get("bytesBase64Encoded"), str):
                raw = base64.b64decode(v["bytesBase64Encoded"])
                name = f"veo-{blake3(raw).hexdigest()[:16]}.mp4"
                self._staged[name] = raw
                outputs.append(STAGED_SCHEME + name)
            elif isinstance(v.get("gcsUri"), str):
                outputs.append(v["gcsUri"])
        if outputs:
            return JobStatus(state="done", outputs=outputs, output_fps=self.OUTPUT_FPS)
        filtered = result.get("raiMediaFilteredCount")
        if isinstance(filtered, int) and filtered > 0:
            reasons = result.get("raiMediaFilteredReasons")
            why = "; ".join(str(r) for r in reasons) if isinstance(reasons, list) else "filtered"
            return JobStatus(
                state="refused", message=f"{filtered} video(s) filtered by responsible AI: {why}"
            )
        return JobStatus(state="failed", message="operation finished without a video")

    def fetch(self, url: str) -> bytes:
        """plate-staged:// names come from this adapter's own poll(); gs:// goes through the GCS JSON API with the
        token; any other URL takes the base path (allowlisted host, no credentials)."""
        if url.startswith(STAGED_SCHEME):
            name = url[len(STAGED_SCHEME) :]
            if name not in self._staged:
                raise FetchError(f"{name} is not staged on this adapter; poll the operation again")
            return self._staged[name]
        if url.startswith("gs://"):
            bucket, _, obj = url[len("gs://") :].partition("/")
            if not bucket or not obj:
                raise FetchError(f"malformed gs:// URI {url!r}")
            api = f"https://storage.googleapis.com/storage/v1/b/{quote(bucket, safe='')}/o/{quote(obj, safe='')}"
            resp = self.http().get(
                api, params={"alt": "media"}, headers={"Authorization": f"Bearer {self._bearer()}"}
            )
            if resp.status_code // 100 != 2:
                raise FetchError(f"download failed: {resp.status_code}")
            return resp.content
        return super().fetch(url)
