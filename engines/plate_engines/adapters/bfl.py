"""Black Forest Labs adapter: FLUX 3 Image through api.bfl.ai (router plan T17).

New for the PLATE monorepo. POST {endpoint}/{model} (key header x-key) with

    {"prompt", "input_image": <base64 PNG>, "input_image_2".."input_image_4": <base64 PNG> (references),
     "box": <placement, passed through as given>, "output_format", "seed", "safety_tolerance", "prompt_upsampling"}

answers {"id", "polling_url", ...}; the adapter polls GET {endpoint}/get_result?id=<id> itself (the polling_url
in the answer is never followed: it could point off the route's allowlist) and reads result.sample, an http URL
on .bfl.ai. Status Ready is done, Pending is running, Error and Task not found are failed, and Content Moderated
or Request Moderated is refused. A 4xx at submit whose text reads like moderation is a refused job; any other
submit failure raises SubmitFailed. There is no mask channel (mask is refused), and references beyond three
are an InputError. FLUX 3's request fields are unverified (manifest flag unverified_p0). Route custody is grade D.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..atlas import REFUSAL_PATTERN
from .base import InputError, JobStatus, Quote, Staged
from .image_common import ImageAdapter, Prepared, SubmitFailed

__all__ = ["BFLImage"]

MAX_REFERENCES = 3  # input_image_2 .. input_image_4
MODERATED = frozenset({"Content Moderated", "Request Moderated"})


class BFLImage(ImageAdapter):
    PROVIDER = "bfl"
    SUPPORTED_ENGINES = ("flux-3-image",)
    ENV_KEY = ("BFL_API_KEY",)
    ETA_S = 30.0

    def body(self, prep: Prepared, box: Any = None) -> dict[str, Any]:
        if len(prep.refs) > MAX_REFERENCES:
            raise InputError(
                f"{self.engine_id}: {len(prep.refs)} reference images, the cap is {MAX_REFERENCES}"
            )
        body: dict[str, Any] = {"prompt": prep.prompt, "input_image": prep.image.b64}
        for i, ref in enumerate(prep.refs, start=2):
            body[f"input_image_{i}"] = ref.b64
        if box is not None:
            body["box"] = box
        for name, value in prep.params.items():
            body[name] = value
        return body

    def url(self) -> str:
        return f"{self.route.endpoint.rstrip('/')}/{self.model_name()}"

    # ---- the interface ----------------------------------------------------------------------------------

    def quote(self, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Quote:
        prep = self.prepare(inputs, params)
        self.body(prep)  # reference cap
        choice = self.choose_size(inputs, params)
        return self.make_quote(self.estimate(), self.ETA_S, choice.size)

    def submit(self, staged: Staged) -> str:
        prep = self.prepare(staged.inputs, staged.params)
        choice = self.choose_size(staged.inputs, staged.params, staged.size)
        body = self.body(prep, staged.inputs.get("box"))
        url = self.url()
        key = self.api_key()
        resp = self.http().post(url, json=body, headers={"x-key": key})
        data = self.json_of(resp)
        if resp.status_code // 100 != 2 or not data.get("id"):
            text = f"{resp.status_code} {resp.text[:300]}"
            if resp.status_code // 100 == 4 and REFUSAL_PATTERN.search(text):
                job_id = self.new_job_id(prep.digest)
                self.finish(job_id, JobStatus(state="refused", message=f"bfl refused: {text}"), choice)
                return job_id
            raise SubmitFailed(f"bfl submit failed: {text}")
        job_id = str(data["id"])
        self.track(job_id, prep.digest, choice)
        self._submitted[job_id] = {k: data[k] for k in ("cost", "input_mp", "output_mp") if k in data}
        return job_id

    def poll_remote(self, provider_job_id: str) -> JobStatus:
        url = f"{self.route.endpoint.rstrip('/')}/get_result"
        resp = self.http().get(url, params={"id": provider_job_id}, headers={"x-key": self.api_key()})
        data = self.json_of(resp)
        if resp.status_code // 100 != 2:
            return JobStatus(state="failed", message=f"bfl poll failed: {resp.status_code} {resp.text[:200]}")
        status = str(data.get("status") or "")
        usage = dict(self._submitted.get(provider_job_id, {}))
        if status == "Ready":
            sample = (data.get("result") or {}).get("sample")
            if isinstance(sample, str) and sample.startswith("http"):
                return self.note_done(provider_job_id, JobStatus(state="done", outputs=[sample], usage=usage))
            return JobStatus(state="failed", message="bfl: status Ready but result.sample is missing")
        if status in MODERATED:
            return JobStatus(state="refused", message=f"bfl refused: {status}", usage=usage)
        if status in ("Error", "Task not found"):
            return JobStatus(state="failed", message=f"bfl {status}: {str(data.get('details') or '')[:200]}")
        message = "" if status == "Pending" else f"unrecognised status {status!r}"
        return JobStatus(state="running", message=message)
