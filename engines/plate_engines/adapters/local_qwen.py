"""Local Qwen-Image-Edit-2509 adapter (custody C0, router plan T17): a real code path and a deterministic stub.

New for the PLATE monorepo. Nothing leaves the box and no transport is needed. submit() runs the edit
synchronously, stages the PNG and poll() answers done at once.

Real path (render box, extra `gpu`; imported lazily, never exercised on the Mac): the diffusers pipeline
QwenImageEditPlusPipeline from `Qwen/Qwen-Image-Edit-2509` (or the directory in PLATE_QWEN_EDIT_DIR, so a box
without internet runs from disk), weights cast to FP8 with layerwise casting when precision is fp8, the plate
first and then the references as the image list. Depth and edge conditioning images (the packet's Z and the
G-buffer edges, inputs `depth` and `edges`) go to the pipeline as ControlNet control images scaled by
controlnet_depth_scale and controlnet_edge_scale; if the installed pipeline has no control_image argument the
call raises LocalModelUnavailable instead of quietly running unconditioned. The ControlNet wiring is the least
certain part and is listed under verify-on-box.

Without torch, diffusers and CUDA, submit() raises LocalModelUnavailable unless PLATE_STUB_LOCAL=1, in which case
a deterministic stub runs: the input blurred plus noise seeded from the input hashes and parameters, so the
output depends on the input and the pipeline can be tested end to end. A stub job says so in its message and
usage. Masks are refused (the model has no mask channel; PLATE composites inside the mask afterwards).
"""

from __future__ import annotations

import inspect
import io
import os
from collections.abc import Mapping
from typing import Any

from blake3 import blake3

from .base import JobStatus, Quote, Staged
from .image_common import (
    ImageAdapter,
    LocalModelUnavailable,
    Prepared,
    decode_png_8bit,
    encode_png,
    stub_enabled,
)

__all__ = ["LocalModelUnavailable", "QwenImageEdit"]

PIPELINE_ID = "Qwen/Qwen-Image-Edit-2509"
CONTROL_SCALE = {"depth": "controlnet_depth_scale", "edges": "controlnet_edge_scale"}


class QwenImageEdit(ImageAdapter):
    PROVIDER = "local"
    SUPPORTED_ENGINES = ("qwen-image-edit-2509",)
    ETA_S = 90.0
    EXTRA_IMAGE_INPUTS = ("depth", "edges")

    _pipe: Any = None

    # ---- the stub ---------------------------------------------------------------------------------------

    def stub_edit(self, prep: Prepared) -> bytes:
        import cv2
        import numpy as np

        arr = decode_png_8bit(prep.image.data)
        out = cv2.GaussianBlur(arr, (0, 0), 1.5)
        material = prep.digest + repr(sorted(prep.params.items()))
        rng = np.random.default_rng(int.from_bytes(blake3(material.encode("utf-8")).digest()[:8], "little"))
        noise = rng.integers(-8, 9, size=out.shape, dtype=np.int16)
        return encode_png(np.clip(out.astype(np.int16) + noise, 0, 255).astype(np.uint8))

    # ---- the real path ----------------------------------------------------------------------------------

    def _pipeline(self, torch: Any, pipeline_cls: Any, precision: str) -> Any:
        if self._pipe is None:
            source = os.environ.get("PLATE_QWEN_EDIT_DIR") or PIPELINE_ID
            pipe = pipeline_cls.from_pretrained(source, torch_dtype=torch.bfloat16)
            if precision == "fp8":
                pipe.transformer.enable_layerwise_casting(
                    storage_dtype=torch.float8_e4m3fn, compute_dtype=torch.bfloat16
                )
            self._pipe = pipe.to("cuda")
        return self._pipe

    def run_pipeline(self, prep: Prepared) -> bytes:
        try:
            import torch
            from diffusers import QwenImageEditPlusPipeline
            from PIL import Image
        except ImportError as exc:
            raise LocalModelUnavailable(
                "Qwen-Image-Edit-2509 needs the gpu extra (torch, diffusers) on the render box; "
                "set PLATE_STUB_LOCAL=1 for the deterministic stub"
            ) from exc
        if not torch.cuda.is_available():
            raise LocalModelUnavailable("Qwen-Image-Edit-2509 needs a CUDA device; none is available")
        p = prep.params
        pipe = self._pipeline(torch, QwenImageEditPlusPipeline, p["precision"])
        images = [Image.open(io.BytesIO(i.data)).convert("RGB") for i in (prep.image, *prep.refs)]
        kwargs: dict[str, Any] = {
            "image": images,
            "prompt": prep.prompt,
            "negative_prompt": p.get("negative_prompt", " "),
            "true_cfg_scale": p["true_cfg_scale"],
            "num_inference_steps": p["num_inference_steps"],
        }
        if "seed" in p:
            kwargs["generator"] = torch.Generator(device="cuda").manual_seed(int(p["seed"]))
        controls = [(n, prep.extras[n]) for n in ("depth", "edges") if n in prep.extras]
        if controls:
            if "control_image" not in inspect.signature(pipe.__call__).parameters:
                raise LocalModelUnavailable(
                    "the installed diffusers pipeline has no control_image argument; "
                    "depth and edge conditioning cannot be applied"
                )
            kwargs["control_image"] = [Image.open(io.BytesIO(img.data)).convert("RGB") for _, img in controls]
            kwargs["controlnet_conditioning_scale"] = [p[CONTROL_SCALE[n]] for n, _ in controls]
        result = pipe(**kwargs).images[0]
        buf = io.BytesIO()
        result.save(buf, format="PNG")
        return buf.getvalue()

    # ---- the interface ----------------------------------------------------------------------------------

    def quote(self, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Quote:
        self.prepare(inputs, params)
        choice = self.choose_size(inputs, params)
        return self.make_quote(self.estimate(), self.ETA_S, choice.size)

    def submit(self, staged: Staged) -> str:
        prep = self.prepare(staged.inputs, staged.params)
        choice = self.choose_size(staged.inputs, staged.params, staged.size)
        stub = stub_enabled()
        data = self.stub_edit(prep) if stub else self.run_pipeline(prep)
        job_id = self.new_job_id(prep.digest)
        outputs = self.store_outputs(job_id, [(data, "png")])
        status = JobStatus(
            state="done",
            outputs=outputs,
            message="stub: deterministic blur and noise, not a model output" if stub else "",
            usage={"stub": True} if stub else {},
        )
        self.finish(job_id, status, choice)
        return job_id
