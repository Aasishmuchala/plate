"""OpenAI Images direct adapter: GPT Image 2.5 through /v1/images/edits (router plan T17).

New for the PLATE monorepo. One synchronous multipart call:
POST {endpoint}/images/edits (Authorization: Bearer) with form fields model, prompt, size, quality,
input_fidelity, output_format, n (plus the optional output_compression, background and moderation) and file
parts image[] (the plate first, then the references, at most 16 in all) and an optional mask. The answer is
data[].b64_json; usage (billed tokens) is recorded on the JobStatus. submit() makes the call and stages the
bytes; poll() then answers done at once.

Sizes: auto, 1024x1024, 1536x1024 and 1024x1536. Anything else is the manifest's `arbitrary` envelope, which
sits behind the unverified_p0 flag (GPT Image 2.5 size caps are not established): it is refused unless the
adapter was built with allow_unverified_sizes=True. Masking is prompt-based on this model ("may not follow the
exact shape"); the mask goes out as the PNG with alpha that OpenAI expects, and PLATE still composites inside
the mask afterwards. Refusal: HTTP 400 with error.code moderation_blocked or content_policy_violation.
Custody: C1 candidate only after zero data retention is held in writing (manifest custody_pending C1).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import EngineSize
from .base import JobStatus, ParamError, Quote, Staged
from .image_common import ImageAdapter, Prepared, SizeChoice, decode_b64

__all__ = ["OpenAIImages"]

REFUSAL_CODES = frozenset({"moderation_blocked", "content_policy_violation"})
UNVERIFIED_FLAG = "unverified_p0"
_OPTIONAL_FIELDS = ("output_compression", "background", "moderation")


class OpenAIImages(ImageAdapter):
    PROVIDER = "openai"
    SUPPORTED_ENGINES = ("gpt-image-2.5",)
    ENV_KEY = ("OPENAI_API_KEY",)
    ETA_S = 60.0
    NATIVE_MASK = True
    MAX_IMAGES = 16

    # ---- sizes ------------------------------------------------------------------------------------------

    def explicit_size(self, raw_params: Mapping[str, Any], snapped: SizeChoice) -> SizeChoice | None:
        size = raw_params.get("size")
        if size is None:
            return None
        if size == "auto":
            return SizeChoice(EngineSize(w=0, h=0, label="auto"), "explicit", "params")
        w, h = (int(x) for x in size.split("x"))
        return SizeChoice(EngineSize(w=w, h=h, label=""), "explicit", "params")

    def wire_size(self, choice: SizeChoice) -> str:
        sz = choice.size
        if sz.label == "auto" or (sz.w, sz.h) == (0, 0):
            return "auto"
        text = f"{sz.w}x{sz.h}"
        sizes = self.manifest.sizes
        if sizes is not None and text in {f"{x.w}x{x.h}" for x in sizes.legal}:
            return text
        arb = sizes.arbitrary if sizes is not None else None
        verified = UNVERIFIED_FLAG not in self.manifest.flags or self.allow_unverified_sizes
        if arb is not None and verified:
            lo, hi = min(sz.w, sz.h), max(sz.w, sz.h)
            if arb.min_side <= lo and hi <= arb.max_side and arb.aspect_min <= sz.w / sz.h <= arb.aspect_max:
                return text
        raise ParamError(
            f"{self.engine_id}: size {text} is not one of the verified sizes "
            f"{sorted(f'{x.w}x{x.h}' for x in (sizes.legal if sizes else []))} or auto; the arbitrary "
            f"envelope is unverified (flag {UNVERIFIED_FLAG}); build the adapter with allow_unverified_sizes=True "
            "once the P0 smoke test confirms it"
        )

    # ---- the request ------------------------------------------------------------------------------------

    def form(self, prep: Prepared, choice: SizeChoice) -> dict[str, str]:
        p = prep.params
        data = {
            "model": self.model_name(),
            "prompt": prep.prompt,
            "size": self.wire_size(choice),
            "quality": str(p["quality"]),
            "input_fidelity": str(p["input_fidelity"]),
            "output_format": str(p["output_format"]),
            "n": str(p.get("n", 1)),
        }
        for name in _OPTIONAL_FIELDS:
            if name in p:
                data[name] = str(p[name])
        return data

    def files(self, prep: Prepared) -> list[tuple[str, tuple[str, bytes, str]]]:
        out = [("image[]", (img.name, img.data, img.mime)) for img in (prep.image, *prep.refs)]
        if prep.mask is not None:
            out.append(("mask", (prep.mask.name, prep.mask.data, prep.mask.mime)))
        return out

    def url(self) -> str:
        return f"{self.route.endpoint.rstrip('/')}/images/edits"

    # ---- the interface ----------------------------------------------------------------------------------

    def quote(self, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Quote:
        prep = self.prepare(inputs, params)
        choice = self.choose_size(inputs, params)
        self.wire_size(choice)  # an unverified size fails the quote, not the run
        tier = f"1K_{prep.params['quality']}"
        return self.make_quote(
            self.estimate(tier=tier, images=prep.params.get("n", 1)), self.ETA_S, choice.size
        )

    def submit(self, staged: Staged) -> str:
        prep = self.prepare(staged.inputs, staged.params)
        # The manifest default size is "auto": it only counts as a caller's choice when it is something else.
        choice = self.choose_size(staged.inputs, self.non_default(staged.params), staged.size)
        form = self.form(prep, choice)
        key = self.api_key()
        job_id = self.new_job_id(prep.digest)
        resp = self.http().post(
            self.url(), data=form, files=self.files(prep), headers={"Authorization": f"Bearer {key}"}
        )
        self.finish(job_id, self._read(job_id, resp, form["output_format"]), choice)
        return job_id

    def _read(self, job_id: str, resp: Any, output_format: str) -> JobStatus:
        data = self.json_of(resp)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        if resp.status_code // 100 != 2:
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            message = (
                f"{resp.status_code} {err.get('code') or err.get('type') or ''}: {err.get('message', '')}"
            )
            if resp.status_code == 400 and err.get("code") in REFUSAL_CODES:
                return JobStatus(state="refused", message=message.strip())
            return JobStatus(state="failed", message=message.strip())
        blobs = [
            (
                decode_b64(item["b64_json"], "data[].b64_json"),
                "jpg" if output_format == "jpeg" else output_format,
            )
            for item in data.get("data") or []
            if isinstance(item, dict) and item.get("b64_json")
        ]
        if not blobs:
            return JobStatus(state="failed", message="openai returned no image data", usage=dict(usage))
        return JobStatus(state="done", outputs=self.store_outputs(job_id, blobs), usage=dict(usage))
