"""Upscalers for AI-made layers only: Magnific Precision v2 and local Real-ESRGAN (router plan T17, T100).

New for the PLATE monorepo. Doctrine rule 5 lives here as code: the plate is never upscaled. Both adapters refuse
an input tagged role == "plate" with PlateUpscaleForbidden (Adapter.check_inputs, from the manifest's
plate_upscale_forbidden flag, and once more by name below), and `image` is the only required input, so nothing
here can start from a prompt. Each Quote.size is the size the vendor will send back (input x scale), not the
size of the input. Both quote() and submit() refuse an input over the manifest's `limits.max_input_mp`, or an
output (input x scale) over `limits.max_output_mp`, with InputError naming the limit and the size, before
anything is sent (decision 55; the daemon's ingress hook in plate_daemon.layer_upscale picks a scale that fits).

MagnificPrecisionV2 (custody D: terms and upload behaviour unestablished). Magnific's own documentation moved the
API from the Freepik host (decision 55, open question 24): POST {base}/v1/ai/image-upscaler-precision-v2 with header
x-magnific-api-key and a JSON body {"image": <base64>, "scale_factor", "sharpen", "smart_grain", "ultra_detail"};
the answer is {"data": {"task_id", "status", "generated": []}}; poll() looks once at GET {that URL}/{task_id}
(CREATED and IN_PROGRESS are not done, COMPLETED carries data.generated, FAILED fails). The base is the route's
https endpoint (the manifest names https://api.magnific.com), else DEFAULT_BASE. UNVERIFIED (manifest flag
unverified_p0) until the P0 smoke test: the host, the header, scale_factor 2 to 16 (manifest `# UNCONFIRMED`) and
the 6 MP in / 50 MP out limits; the class constants below are the only places the address and header are named.
The route's allowed_hosts keep .freepik.com beside .magnific.com because generated files may still be served from
Freepik's CDN. MAGNIFIC_API_KEY is read first, FREEPIK_API_KEY still works.

RealESRGAN (custody C0, extra `gpu`, imported lazily, never exercised on the Mac): the `realesrgan` package with
RRDBNet weights read from the directory in PLATE_REALESRGAN_DIR (<model_name>.pth; nothing is downloaded). Without
the package, CUDA or the weights, submit() raises LocalModelUnavailable unless PLATE_STUB_LOCAL=1, in which case a
deterministic stub runs: a Lanczos resample (cv2.INTER_LANCZOS4) at the input's own bit depth, which is what
ingress would have done to the layer anyway, so the pipeline is testable and a stub take is never worse than no
upscaler. A stub job says so in its message and usage ({"stub": True}).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..atlas import REFUSAL_PATTERN
from ..models import EngineManifest, EngineSize
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

__all__ = [
    "LocalModelUnavailable",
    "MagnificPrecisionV2",
    "PlateUpscaleForbidden",
    "RealESRGAN",
    "check_limits",
]

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


def check_limits(manifest: EngineManifest, size: tuple[int, int], scale: int) -> None:
    """InputError when `size` (w, h) is over the manifest's `limits.max_input_mp`, or `size` x `scale` is over its
    `limits.max_output_mp`; the message names the limit and the size. No limits (None) means nothing to check."""
    limits = manifest.limits
    if limits is None:
        return
    w, h = size
    for what, (pw, ph), cap, name in (
        ("input", (w, h), limits.max_input_mp, "max_input_mp"),
        ("output", (w * scale, h * scale), limits.max_output_mp, "max_output_mp"),
    ):
        mp = pw * ph / 1e6
        if cap is not None and mp > cap:
            raise InputError(
                f"{manifest.id}: the {what} {'is' if what == 'input' else 'would be'} {pw} x {ph} ({mp:.2f} MP), "
                f"over limits.{name} of {cap:g} MP"
                + (f" (input {w} x {h} at scale {scale})" if what == "output" else "")
            )


def _limits(manifest: EngineManifest, prep: Prepared) -> None:
    check_limits(manifest, (prep.image.width or 0, prep.image.height or 0), int(prep.params["scale"]))


class MagnificPrecisionV2(ImageAdapter):
    PROVIDER = "magnific"
    SUPPORTED_ENGINES = ("magnific-precision-v2",)
    ENV_KEY = ("MAGNIFIC_API_KEY", "FREEPIK_API_KEY")
    ETA_S = 45.0
    # UNVERIFIED (see the module docstring): the one place the vendor's address and key header are named.
    KEY_HEADER = "x-magnific-api-key"
    DEFAULT_BASE = "https://api.magnific.com"
    PATH = "/v1/ai/image-upscaler-precision-v2"

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
        _limits(self.manifest, prep)
        return self.make_quote(self.estimate(), self.ETA_S, choice.size)

    def submit(self, staged: Staged) -> str:
        prep = self.prepare(staged.inputs, staged.params)
        self.refuse_upscale_of_plate(prep.image)
        choice = _upscaled(prep, int(prep.params["scale"]))
        _limits(self.manifest, prep)
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
        """The deterministic stand-in: a Lanczos resample by `scale` at the input's own bit depth (8 or 16 bit;
        alpha dropped, grey kept grey). Different inputs give different outputs; the same input the same bytes."""
        import cv2
        import numpy as np

        arr = cv2.imdecode(np.frombuffer(prep.image.data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if arr is None:
            raise InputError("the image cannot be decoded")
        if arr.ndim == 3 and arr.shape[2] == 4:
            arr = arr[..., :3]
        if arr.dtype not in (np.uint8, np.uint16):
            arr = np.clip(arr, 0, 255).astype(np.uint8)
        scale = int(prep.params["scale"])
        h, w = arr.shape[:2]
        out = cv2.resize(np.ascontiguousarray(arr), (w * scale, h * scale), interpolation=cv2.INTER_LANCZOS4)
        return encode_png(out)

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
        _limits(self.manifest, prep)
        return self.make_quote(self.estimate(), self.ETA_S, choice.size)

    def submit(self, staged: Staged) -> str:
        prep = self.prepare(staged.inputs, staged.params)
        self.refuse_upscale_of_plate(prep.image)
        choice = _upscaled(prep, int(prep.params["scale"]))
        _limits(self.manifest, prep)
        stub = stub_enabled()
        data = self.stub_upscale(prep) if stub else self.run_pipeline(prep)
        job_id = self.new_job_id(prep.digest)
        outputs = self.store_outputs(job_id, [(data, "png")])
        status = JobStatus(
            state="done",
            outputs=outputs,
            message="stub: deterministic Lanczos resample, not a model output" if stub else "",
            usage={"stub": True} if stub else {},
        )
        self.finish(job_id, status, choice)
        return job_id
