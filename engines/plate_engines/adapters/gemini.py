"""Gemini direct adapter: Nano Banana 2.1 and Nano Banana Pro through generateContent (router plan T17).

New for the PLATE monorepo. One synchronous call:
POST {endpoint}/v1beta/models/{model}:generateContent with header x-goog-api-key and

    {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type", "data"}}, ...]}],
     "generationConfig": {"responseModalities": ["IMAGE"],
                          "imageConfig": {"aspectRatio": "16:9", "imageSize": "2K"}, ...}}

The plate comes first, then the references (at most 14 images in all; Pro also caps 6 objects, 5 characters and
3 style references, Flash 10 objects and 4 characters, enforced when a reference carries kind). imageSize is
upper-case K. An edit's output follows the input size. Images come back in
candidates[].content.parts[].inlineData.data. Gemini has no mask channel, so a mask is refused (PLATE composites
inside the mask afterwards). submit() makes the call and stages the bytes; poll() then answers done at once.

Refusals: promptFeedback.blockReason, or a finishReason that is SAFETY or PROHIBITED_CONTENT (and the image-model
and blocklist variants of those) with no image, map to state refused. Any other non-image answer is failed.
This route is a C1 candidate only (pending written terms, so effective grade D today): see the manifests.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import EngineSize
from .base import InputError, JobStatus, Quote, Staged
from .image_common import TIER_BOX, ImageAdapter, Prepared, SizeChoice, decode_b64, tier_dims

__all__ = ["GeminiImage"]

REFUSAL_FINISH = frozenset(
    {"SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}
)
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class GeminiImage(ImageAdapter):
    PROVIDER = "gemini"
    SUPPORTED_ENGINES = ("nano-banana-2.1", "nano-banana-pro")
    ENV_KEY = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    ETA_S = 30.0
    MAX_IMAGES = 14
    # Per-kind reference caps (scout 02, RESEARCH-model-docs.md). Only enforced for references that carry a kind.
    KIND_CAPS: Mapping[str, Mapping[str, int]] = {
        "nano-banana-pro": {"object": 6, "character": 5, "style": 3},
        "nano-banana-2.1": {"object": 10, "character": 4},
    }

    # ---- sizes ------------------------------------------------------------------------------------------

    def explicit_size(self, raw_params: Mapping[str, Any], snapped: SizeChoice) -> SizeChoice | None:
        ratio = raw_params.get("aspect_ratio")
        tier = raw_params.get("image_size")
        if ratio is None and tier is None:
            return None
        ratio = ratio or snapped.ratio
        tier = tier or (snapped.size.label if snapped.size.label in TIER_BOX else "")
        w, h = tier_dims(tier, ratio)
        return SizeChoice(EngineSize(w=w, h=h, label=tier or "auto"), "explicit", "params", ratio=ratio)

    @staticmethod
    def image_config(choice: SizeChoice, params: Mapping[str, Any]) -> dict[str, str]:
        ratio = params.get("aspect_ratio") or choice.ratio
        tier = params.get("image_size") or choice.size.label
        cfg: dict[str, str] = {}
        if ratio:
            cfg["aspectRatio"] = ratio
        if tier in TIER_BOX:
            cfg["imageSize"] = tier
        return cfg

    # ---- the request ------------------------------------------------------------------------------------

    def _check_kinds(self, prep: Prepared) -> None:
        caps = self.KIND_CAPS.get(self.engine_id, {})
        counts: dict[str, int] = {}
        for ref in prep.refs:
            if ref.kind is not None:
                counts[ref.kind] = counts.get(ref.kind, 0) + 1
        for kind, n in counts.items():
            if kind in caps and n > caps[kind]:
                raise InputError(f"{self.engine_id}: {n} {kind} references exceed the cap of {caps[kind]}")

    def build_body(self, prep: Prepared, choice: SizeChoice) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [{"text": prep.prompt}]
        for img in (prep.image, *prep.refs):
            parts.append({"inline_data": {"mime_type": img.mime, "data": img.b64}})
        p = prep.params
        gen: dict[str, Any] = {"responseModalities": ["IMAGE"]}
        cfg = self.image_config(choice, p)
        if cfg:
            gen["imageConfig"] = cfg
        if "seed" in p:
            gen["seed"] = p["seed"]
        if "temperature" in p:
            gen["temperature"] = p["temperature"]
        if p.get("candidate_count", 1) != 1:
            gen["candidateCount"] = p["candidate_count"]
        if "thinking_level" in p:
            gen["thinkingConfig"] = {"thinkingLevel": p["thinking_level"]}
        body: dict[str, Any] = {"contents": [{"parts": parts}], "generationConfig": gen}
        if p.get("system_prompt"):
            body["systemInstruction"] = {"parts": [{"text": p["system_prompt"]}]}
        return body

    def url(self) -> str:
        return f"{self.route.endpoint.rstrip('/')}/v1beta/models/{self.model_name()}:generateContent"

    # ---- the interface ----------------------------------------------------------------------------------

    def quote(self, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Quote:
        prep = self.prepare(inputs, params)
        self._check_kinds(prep)
        choice = self.choose_size(inputs, params)
        tier = prep.params.get("image_size") or choice.size.label
        return self.make_quote(
            self.estimate(tier=tier, images=prep.params.get("candidate_count", 1)), self.ETA_S, choice.size
        )

    def submit(self, staged: Staged) -> str:
        prep = self.prepare(staged.inputs, staged.params)
        self._check_kinds(prep)
        choice = self.choose_size(staged.inputs, staged.params, staged.size)
        body = self.build_body(prep, choice)
        url = self.url()  # raises ModelIdUnknown before a job exists
        key = self.api_key()
        job_id = self.new_job_id(prep.digest)
        resp = self.http().post(url, json=body, headers={"x-goog-api-key": key})
        self.finish(job_id, self._read(job_id, resp), choice)
        return job_id

    def _read(self, job_id: str, resp: Any) -> JobStatus:
        data = self.json_of(resp)
        if resp.status_code // 100 != 2:
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            return JobStatus(
                state="failed",
                message=f"{resp.status_code} {err.get('status', '')}: {err.get('message', '')}".strip(),
            )
        usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
        feedback = data.get("promptFeedback") if isinstance(data.get("promptFeedback"), dict) else {}
        blobs: list[tuple[bytes, str]] = []
        texts: list[str] = []
        finish: list[str] = []
        for cand in data.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            if cand.get("finishReason"):
                finish.append(str(cand["finishReason"]))
            for part in (cand.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if isinstance(inline, dict) and inline.get("data"):
                    mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                    blobs.append((decode_b64(inline["data"], "inlineData.data"), _EXT.get(mime, "png")))
                elif isinstance(part.get("text"), str):
                    texts.append(part["text"])
        if blobs:
            return JobStatus(
                state="done",
                outputs=self.store_outputs(job_id, blobs),
                usage=dict(usage),
                message=" ".join(texts)[:300],
            )
        block = feedback.get("blockReason")
        if block or REFUSAL_FINISH & set(finish):
            reason = block or ", ".join(sorted(REFUSAL_FINISH & set(finish)))
            return JobStatus(state="refused", message=f"gemini refused: {reason}", usage=dict(usage))
        why = ", ".join(finish) or "no candidates"
        return JobStatus(
            state="failed",
            message=f"gemini returned no image ({why}): {' '.join(texts)[:200]}",
            usage=dict(usage),
        )
