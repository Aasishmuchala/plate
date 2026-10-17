"""Upscalers for AI-made layers only: Magnific Precision v2 (Freepik API) and local Real-ESRGAN (router plan T17).

New for the PLATE monorepo. Doctrine rule 5 lives here as code: the plate is never upscaled. Both adapters refuse
an input tagged role == "plate" with PlateUpscaleForbidden (Adapter.check_inputs, from the manifest's
plate_upscale_forbidden flag, and once more by name below), and `image` is the only required input, so nothing
here can start from a prompt. Each Quote.size is the size the vendor will send back (input x scale), not the
size of the input.

MagnificPrecisionV2 (custody D: terms and upload behaviour unestablished). POST {endpoint}/ai/image-upscaler/precision
with header x-freepik-api-key and a JSON body {"image": <base64>, "scale_factor", "sharpen", "smart_grain",
"ultra_detail"}; the answer is {"data": {"task_id", "status", "generated": []}}; poll() looks once at
GET {endpoint}/ai/image-upscaler/precision/{task_id} (CREATED and IN_PROGRESS are not done, COMPLETED carries
data.generated, FAILED fails). The manifest route has endpoint "unknown", so the default base is
https://api.freepik.com/v1 until the P0 smoke test sets one. UNVERIFIED (manifest flag unverified_p0): Magnific's
own documentation now shows api.magnific.com, header x-magnific-api-key and /v1/ai/image-upscaler-precision-v2
with scale_factor 2 to 16; the class constants below are the only places that change, and the route's
allowed_hosts must gain .magnific.com in the manifest first.

RealESRGAN (custody C0, extra `gpu`, imported lazily, never exercised on the Mac): the `realesrgan` package with
RRDBNet weights read from the directory in PLATE_REALESRGAN_DIR (<model_name>.pth; nothing is downloaded). Without
the package, CUDA or the weights, submit() raises LocalModelUnavailable unless PLATE_STUB_LOCAL=1, in which case a
deterministic stub runs: bicubic resize plus noise seeded from the input hash and parameters, so the pipeline is
testable. A stub job says so in its message and usage.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from blake3 import blake3

from ..atlas import REFUSAL_PATTERN
from ..models import EngineSize
from .base import InputError, JobStatus, PlateUpscaleForbidden, Quote, Staged
from .image_common import (
    ImageAdapter,
    LocalModelUnavailable,
    Prepared,
    SizeChoice,
    SubmitFailed,
    decode_png_8bit,
    encode_png,
    stub_enabled,
)

__all__ = ["LocalModelUnavailable", "MagnificPrecisionV2", "PlateUpscaleForbidden", "RealESRGAN"]

# Param name in the manifest -> field name on the wire (only ones that differ are listed).
_MAGNIFIC_WIRE = {"scale": "scale_factor"}
_MAGNIFIC_FIELDS = ("scale", "sharpen", "smart_grain", "ultra_detail")


def _upscaled(prep: Prepared, scale: int) -> SizeChoice:
    """The size the upscaler returns: the input's dimensions times scale (mode exact: no padding is involved)."""
    img = prep.image
    if img.width is None or img.height is None:
        raise InputError("cannot read the size of the image (send a PNG)")
    mode = str((img.sizemap or {}).get("mode") or "exact")
    return SizeChoice(
        EngineSize(w=img.width * scale, h=img.height * scale, label=f"{scale}x"), mode, "native"
    )


class MagnificPrecisionV2(ImageAdapter):
    PROVIDER = "magnific"
    SUPPORTED_ENGINES = ("magnific-precision-v2",)
    ENV_KEY = ("MAGNIFIC_API_KEY", "FREEPIK_API_KEY")
    ETA_S = 45.0
    # UNVERIFIED (see the module docstring): the one place the vendor's address and key header are named.
    KEY_HEADER = "x-freepik-api-key"
    DEFAULT_BASE = "https://api.freepik.com/v1"
    PATH = "/ai/image-upscaler/precision"

    def base_url(self) -> str:
        endpoint = self.route.endpoint
        return (endpoint if endpoint.startswith("https://") else self.DEFAULT_BASE).rstrip("/")

    def url(self) -> str:
        return self.base_url() + self.PATH

    def body(self, prep: Prepared) -> dict[str, Any]:
        body: dict[str, Any] = {"image": prep.image.b64}
        for name in _MAGNIFIC_FIELDS:
            if name in prep.params:
                body[_MAGNIFIC_WIRE.get(name, name)] = prep.params[name]
        return body

    # ---- the interface ----------------------------------------------------------------------------------

    def quote(self, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Quote:
        prep = self.prepare(inputs, params)
        self.refuse_upscale_of_plate(prep.image)
        choice = _upscaled(prep, int(prep.params["scale"]))
        return self.make_quote(self.estimate(), self.ETA_S, choice.size)

    def submit(self, staged: Staged) -> str:
        prep = self.prepare(staged.inputs, staged.params)
        self.refuse_upscale_of_plate(prep.image)
        choice = _upscaled(prep, int(prep.params["scale"]))
        body = self.body(prep)
        key = self.api_key()
        resp = self.http().post(self.url(), json=body, headers={self.KEY_HEADER: key})
        data = self.json_of(resp)
        task = data.get("data") if isinstance(data.get("data"), dict) else data
        if resp.status_code // 100 != 2 or not task.get("task_id"):
            text = f"{resp.status_code} {resp.text[:300]}"
            if resp.status_code // 100 == 4 and REFUSAL_PATTERN.search(text):
                job_id = self.new_job_id(prep.digest)
                self.finish(job_id, JobStatus(state="refused", message=f"magnific refused: {text}"), choice)
                return job_id
            raise SubmitFailed(f"magnific submit failed: {text}")
        job_id = str(task["task_id"])
        self.track(job_id, prep.digest, choice)
        return job_id

    def poll_remote(self, provider_job_id: str) -> JobStatus:
        resp = self.http().get(f"{self.url()}/{provider_job_id}", headers={self.KEY_HEADER: self.api_key()})
        data = self.json_of(resp)
        if resp.status_code // 100 != 2:
            return JobStatus(
                state="failed", message=f"magnific poll failed: {resp.status_code} {resp.text[:200]}"
            )
        task = data.get("data") if isinstance(data.get("data"), dict) else data
        status = str(task.get("status") or "").upper()
        if status == "COMPLETED":
            urls = [u for u in task.get("generated") or [] if isinstance(u, str) and u.startswith("http")]
            if not urls:
                return JobStatus(
                    state="failed", message="magnific: status COMPLETED but data.generated is empty"
                )
            return self.note_done(provider_job_id, JobStatus(state="done", outputs=urls))
        if status in ("FAILED", "ERROR"):
            return JobStatus(state="failed", message=f"magnific {status}: {str(task)[:200]}")
        return JobStatus(state="queued" if status == "CREATED" else "running")


class RealESRGAN(ImageAdapter):
    PROVIDER = "local"
    SUPPORTED_ENGINES = ("real-esrgan",)
    ETA_S = 20.0

    _upsampler: Any = None

    # ---- the stub ---------------------------------------------------------------------------------------

    def stub_upscale(self, prep: Prepared) -> bytes:
        import cv2
        import numpy as np

        arr = decode_png_8bit(prep.image.data)
        scale = int(prep.params["scale"])
        h, w = arr.shape[:2]
        out = cv2.resize(arr, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
        material = prep.digest + repr(sorted(prep.params.items()))
        rng = np.random.default_rng(int.from_bytes(blake3(material.encode("utf-8")).digest()[:8], "little"))
        noise = rng.integers(-3, 4, size=out.shape, dtype=np.int16)
        return encode_png(np.clip(out.astype(np.int16) + noise, 0, 255).astype(np.uint8))

    # ---- the real path ----------------------------------------------------------------------------------

    def _build(self, model_name: str, tile: int) -> Any:
        try:
            import torch
            from basicsr.archs.rrdbnet_arch import RRDBNet
            from realesrgan import RealESRGANer
        except ImportError as exc:
            raise LocalModelUnavailable(
                "Real-ESRGAN needs the gpu extra (torch, basicsr, realesrgan) on the render box; "
                "set PLATE_STUB_LOCAL=1 for the deterministic stub"
            ) from exc
        if not torch.cuda.is_available():
            raise LocalModelUnavailable("Real-ESRGAN needs a CUDA device; none is available")
        weights_dir = os.environ.get("PLATE_REALESRGAN_DIR")
        weights = Path(weights_dir) / f"{model_name}.pth" if weights_dir else None
        if weights is None or not weights.is_file():
            raise LocalModelUnavailable(
                f"Real-ESRGAN weights {model_name}.pth not found: put them in PLATE_REALESRGAN_DIR "
                "(PLATE never downloads weights)"
            )
        net_scale = 2 if model_name.endswith("x2plus") else 4
        model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=net_scale)
        return RealESRGANer(
            scale=net_scale,
            model_path=str(weights),
            model=model,
            tile=tile,
            tile_pad=10,
            pre_pad=0,
            half=True,
        )

    def run_pipeline(self, prep: Prepared) -> bytes:
        p = prep.params
        if self._upsampler is None:
            self._upsampler = self._build(str(p["model_name"]), int(p["tile"]))
        out, _ = self._upsampler.enhance(decode_png_8bit(prep.image.data), outscale=int(p["scale"]))
        return encode_png(out)

    # ---- the interface ----------------------------------------------------------------------------------

    def quote(self, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Quote:
        prep = self.prepare(inputs, params)
        self.refuse_upscale_of_plate(prep.image)
        choice = _upscaled(prep, int(prep.params["scale"]))
        return self.make_quote(self.estimate(), self.ETA_S, choice.size)

    def submit(self, staged: Staged) -> str:
        prep = self.prepare(staged.inputs, staged.params)
        self.refuse_upscale_of_plate(prep.image)
        choice = _upscaled(prep, int(prep.params["scale"]))
        stub = stub_enabled()
        data = self.stub_upscale(prep) if stub else self.run_pipeline(prep)
        job_id = self.new_job_id(prep.digest)
        outputs = self.store_outputs(job_id, [(data, "png")])
        status = JobStatus(
            state="done",
            outputs=outputs,
            message="stub: deterministic bicubic resize and noise, not a model output" if stub else "",
            usage={"stub": True} if stub else {},
        )
        self.finish(job_id, status, choice)
        return job_id
