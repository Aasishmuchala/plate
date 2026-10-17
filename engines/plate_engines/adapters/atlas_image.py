"""Atlas image adapter: Seedream 5 Pro through AtlasClient (router plan T17; quirks pinned in T7).

New for the PLATE monorepo. Upload every image with /model/uploadMedia (multipart `file`), then
/model/generateImage with `images` (always an array of the uploaded URLs, the plate first), `prompt`, `model`,
`aspect_ratio` and `resolution` (upper-case K), then poll /model/prediction/{id}: one look per poll(), no sleeping.
The cadence the worker should use is atlas.IMAGE_POLL_INTERVAL_S (3 s) up to IMAGE_POLL_TIMEOUT_S (120 s).
A result is an http URL on .atlascloud.ai or .aliyuncs.com; fetch() downloads it through the route allowlist
without credentials.

CUSTODY: uploadMedia returns public Alibaba OSS URLs, so this route is grade D and the router refuses it for NDA
projects; nothing here softens that. Atlas has no mask channel (mask is refused) and no vendor parameter beyond
aspect_ratio, resolution and seed: any other parameter is a ParamError rather than silently dropped.
A moderation answer at submit time becomes a refused job (poll returns it); every other submit failure raises
atlas.AtlasError. The Atlas slug for Seedream 5 Pro is unverified (null in the manifest): ModelIdUnknown until the
P0 smoke test sets route.model_slug.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from ..atlas import AtlasClient, AtlasRefused
from ..models import EngineManifest, EngineSize, Route
from .base import JobStatus, ParamError, Quote, Staged
from .image_common import (
    TIER_BOX,
    ImageAdapter,
    OutputLedger,
    Prepared,
    SizeChoice,
    tier_dims,
)

__all__ = ["AtlasImage", "AtlasSeedream"]

# Manifest parameter -> generateImage body field. A parameter missing here cannot be carried by Atlas.
PARAM_MAP = {
    "aspect_ratio": "aspect_ratio",
    "resolution": "resolution",
    "image_size": "resolution",
    "seed": "seed",
}


class AtlasImage(ImageAdapter):
    PROVIDER = "atlas"
    SUPPORTED_ENGINES = ("seedream-5-pro",)
    ETA_S = 30.0

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
        super().__init__(
            manifest,
            route,
            transport,
            staging_dir=staging_dir,
            allow_unverified_sizes=allow_unverified_sizes,
            ledger=ledger,
        )
        self._atlas: AtlasClient | None = None

    def client(self) -> AtlasClient:
        if self._atlas is None:
            self._atlas = self.atlas_client()
        return self._atlas

    # ---- sizes ------------------------------------------------------------------------------------------

    def explicit_size(self, raw_params: Mapping[str, Any], snapped: SizeChoice) -> SizeChoice | None:
        ratio = raw_params.get("aspect_ratio")
        tier = raw_params.get("resolution") or raw_params.get("image_size")
        if ratio is None and tier is None:
            return None
        ratio = ratio or snapped.ratio
        tier = tier or (snapped.size.label if snapped.size.label in TIER_BOX else "")
        w, h = tier_dims(tier, ratio)
        return SizeChoice(EngineSize(w=w, h=h, label=tier or "auto"), "explicit", "params", ratio=ratio)

    # ---- the request ------------------------------------------------------------------------------------

    def wire_fields(self, prep: Prepared, choice: SizeChoice) -> dict[str, Any]:
        unsendable = sorted(set(prep.params) - set(PARAM_MAP))
        if unsendable:
            raise ParamError(f"{self.engine_id} via atlas cannot carry parameter(s) {unsendable}")
        fields: dict[str, Any] = {}
        ratio = prep.params.get("aspect_ratio") or choice.ratio
        if ratio:
            fields["aspect_ratio"] = ratio
        tier = choice.size.label if choice.size.label in TIER_BOX else None
        tier = tier or prep.params.get("resolution") or prep.params.get("image_size")
        if tier:
            fields["resolution"] = tier
        if "seed" in prep.params:
            fields["seed"] = prep.params["seed"]
        return fields

    # ---- the interface ----------------------------------------------------------------------------------

    def quote(self, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Quote:
        prep = self.prepare(inputs, params)
        choice = self.choose_size(inputs, params)
        self.wire_fields(prep, choice)
        tier = choice.size.label if choice.size.label in TIER_BOX else prep.params.get("resolution")
        return self.make_quote(self.estimate(tier=tier), self.ETA_S, choice.size)

    def submit(self, staged: Staged) -> str:
        prep = self.prepare(staged.inputs, staged.params)
        choice = self.choose_size(staged.inputs, self.non_default(staged.params), staged.size)
        fields = self.wire_fields(prep, choice)
        model = self.model_name()
        client = self.client()
        urls = [client.upload(self.stage_input(img), img.mime) for img in (prep.image, *prep.refs)]
        body = {"model": model, "prompt": prep.prompt, "images": urls, **fields}
        try:
            prediction_id = client.generate_image(body)
        except AtlasRefused as exc:
            job_id = self.new_job_id(prep.digest)
            self.finish(job_id, JobStatus(state="refused", message=str(exc)), choice)
            return job_id
        self.track(prediction_id, prep.digest, choice)
        return prediction_id

    def poll_remote(self, provider_job_id: str) -> JobStatus:
        pred = self.client().poll_once(provider_job_id)
        status = JobStatus(state=pred.state, outputs=list(pred.outputs), message=pred.message)
        return self.note_done(provider_job_id, status)


AtlasSeedream = AtlasImage
