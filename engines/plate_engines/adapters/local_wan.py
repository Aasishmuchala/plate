"""Wan 2.2 VACE on the render box: local previs video (route `local`, custody C0, nothing leaves the machine).

New for the PLATE monorepo (router plan T18). Two paths behind one adapter:

* the real path (extra `gpu`): diffusers' WanVACEPipeline from the Wan2.2-VACE weights (repo id and package names are
  recorded in REPO_ID / PIPELINE / WAN_PACKAGE and are UNVERIFIED until the box day), conditioned on the reference stills.
  It needs torch, diffusers and a CUDA device; without them submit() raises LocalModelUnavailable. Reference-video
  conditioning is not wired yet (verify-on-box). It cannot share the 5090 with Vantage (BUILD_PLAN decision 11): it runs in
  scheduled windows.
* the stub (PLATE_STUB_LOCAL=1): ffmpeg turns the first reference still into a 24 fps mp4 with a slow zoom, so the whole
  pipeline (quote, run, fetch, gates) is testable on a Mac with no GPU. It is deterministic and its pixels depend on
  the input still.

Every output is PREVIS, always: the mp4 carries a PREVIS title/comment tag, a `<job>.previs.json` sidecar says
`previs: true`, and JobStatus.usage repeats it. Wan clips are never delivered as final motion (BUILD_PLAN: clay motion
is labelled PREVIS; final motion is rendered or crop-pair Kling from approved stills).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from blake3 import blake3

from ..models import EngineManifest, EngineSize, Route
from .atlas_video import VideoAdapter, VideoInputs, inputs_digest
from .base import FetchError, InputError, JobStatus, Quote, Staged

__all__ = ["PIPELINE", "REPO_ID", "STAGED_SCHEME", "LocalModelUnavailable", "WanVace"]

REPO_ID = "alibaba-pai/Wan2.2-VACE-Fun-A14B"  # unverified until the box day
PIPELINE = "diffusers.WanVACEPipeline"
WAN_PACKAGE = "wan"  # the reference implementation's package name, for a non-diffusers install
STAGED_SCHEME = "plate-staged://"
DEFAULT_SIZE = (832, 480)  # Wan's native 480p
MAX_STUB_LONG_SIDE = 1280
PREVIS_TAG = "PREVIS"


class LocalModelUnavailable(RuntimeError):
    """The local model cannot run here: no GPU stack, no CUDA device, or the stub switch is off."""


def stub_enabled() -> bool:
    return os.environ.get("PLATE_STUB_LOCAL") == "1"


def _round_to(n: float, multiple: int) -> int:
    return max(multiple, int(round(n / multiple)) * multiple)


def _replace_with_retry(src: Path, dest: Path, tries: int = 5, delay_s: float = 0.05) -> None:
    """os.replace with backoff: on Windows an antivirus scan or a viewer can hold the destination for a moment."""
    for attempt in range(tries):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(delay_s)
            delay_s *= 2


class WanVace(VideoAdapter):
    ENGINE_IDS = ("wan-2.2-vace",)
    PROVIDER = "local"
    OUTPUT_FPS = 24.0
    previs = True  # ALWAYS

    def __init__(
        self,
        manifest: EngineManifest,
        route: Route | str | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        staging_dir: Path | str | None = None,
    ) -> None:
        super().__init__(manifest, route, transport)
        self._staging_dir = Path(staging_dir) if staging_dir is not None else None

    @property
    def staging_dir(self) -> Path:
        """Where clips and sidecars land. Pass staging_dir= to pin it; otherwise a temp folder is made on first use."""
        if self._staging_dir is None:
            self._staging_dir = Path(tempfile.mkdtemp(prefix="plate-wan-"))
        return self._staging_dir

    # ---- sizes and quotes -------------------------------------------------------------------------------

    def output_size(self, parsed: VideoInputs, size: EngineSize | None = None) -> tuple[int, int]:
        """The frame size of the clip: the egress size, else the first still's own, fitted to Wan (multiples of 16)."""
        dims = self.source_dims(parsed, size) or DEFAULT_SIZE
        scale = min(1.0, MAX_STUB_LONG_SIDE / max(dims))
        return (_round_to(dims[0] * scale, 16), _round_to(dims[1] * scale, 16))

    def quote(self, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Quote:
        p = self.validate_params(params)
        parsed = self.parse_inputs(inputs)
        seconds = float(p["duration_s"])
        w, h = self.output_size(parsed)
        eta = 120.0 + 60.0 * seconds  # a guess for a 14B model on one 5090; the box day replaces it
        return self.make_quote(self.estimate(seconds=seconds), eta, EngineSize(w=w, h=h, label=f"{h}p"))

    # ---- the interface ----------------------------------------------------------------------------------

    def _job_id(self, staged: Staged, p: Mapping[str, Any]) -> str:
        key = {
            "inputs": inputs_digest(staged.inputs),
            "params": dict(sorted(p.items())),
            "stub": stub_enabled(),
        }
        return "wan-" + blake3(json.dumps(key, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]

    def _paths(self, job_id: str) -> tuple[Path, Path]:
        return self.staging_dir / f"{job_id}.mp4", self.staging_dir / f"{job_id}.previs.json"

    def submit(self, staged: Staged) -> str:
        parsed = self.parse_inputs(staged.inputs)
        p = self.validate_params(staged.params)
        if not stub_enabled():
            self._require_runtime()
        job_id = self._job_id(staged, p)
        video, sidecar = self._paths(job_id)
        if video.is_file() and sidecar.is_file():
            return job_id  # same inputs, same params: the clip is already there
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        w, h = self.output_size(parsed, staged.size)
        part = video.with_name(video.name + ".part")
        try:
            if stub_enabled():
                self._run_stub(parsed, p, w, h, part)
            else:
                self._run_gpu(parsed, p, w, h, part)
            _replace_with_retry(part, video)
        finally:
            part.unlink(missing_ok=True)
        meta = {
            "previs": True,
            "engine": self.engine_id,
            "stub": stub_enabled(),
            "model": REPO_ID,
            "job_id": job_id,
            "fps": int(p["fps"]),
            "duration_s": float(p["duration_s"]),
            "width": w,
            "height": h,
            "seed": p.get("seed"),
            "input_digest": inputs_digest(staged.inputs),
        }
        sidecar.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        return job_id

    def poll(self, provider_job_id: str) -> JobStatus:
        video, sidecar = self._paths(provider_job_id)
        if not (video.is_file() and sidecar.is_file()):
            return JobStatus(state="failed", message=f"no staged clip for {provider_job_id}")
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        return JobStatus(
            state="done",
            outputs=[STAGED_SCHEME + video.name],
            message=f"{PREVIS_TAG}: not a final deliverable",
            output_fps=float(meta["fps"]),
            usage={"previs": True, "stub": bool(meta["stub"])},
        )

    def fetch(self, url: str) -> bytes:
        """Staged clips only, confined to the staging directory; a local route never makes an HTTP call."""
        if not url.startswith(STAGED_SCHEME):
            raise FetchError(f"{self.engine_id} is local: expected a {STAGED_SCHEME} URL, got {url[:60]!r}")
        name = url[len(STAGED_SCHEME) :]
        if not name or "/" in name or "\\" in name or ":" in name or not name.endswith((".mp4", ".json")):
            raise FetchError(f"staged name {name!r} must be a plain file name")
        path = self.staging_dir / name
        if not path.is_file():
            raise FetchError(f"{name} is not in the staging directory")
        return path.read_bytes()

    # ---- the stub ---------------------------------------------------------------------------------------

    def _run_stub(self, parsed: VideoInputs, p: Mapping[str, Any], w: int, h: int, out: Path) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise LocalModelUnavailable("the previs stub needs ffmpeg on PATH")
        still = parsed.references[0].path
        if still is None:
            raise InputError(f"{self.engine_id}: the stub reads a local still, not a URL")
        fps = int(p["fps"])
        frames = max(1, round(float(p["duration_s"]) * fps))
        zoom = f"zoompan=z='min(1+0.0008*on,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps={fps}"
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(still),
            "-vf", f"{zoom},format=yuv420p",
            "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "1", "-an",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1",
            "-metadata", f"title={PREVIS_TAG}",
            "-metadata", f"comment={PREVIS_TAG} (PLATE wan-2.2-vace stub): not a final deliverable",
            "-f", "mp4", str(out),
        ]  # fmt: skip
        done = subprocess.run(cmd, capture_output=True, check=False)
        if done.returncode != 0 or not out.is_file():
            tail = done.stderr.decode("utf-8", errors="replace").strip()[-300:]
            raise LocalModelUnavailable(f"ffmpeg could not write the previs stub: {tail}")

    # ---- the real path ----------------------------------------------------------------------------------

    @staticmethod
    def _require_runtime() -> tuple[Any, Any]:
        try:
            import diffusers
            import torch
        except ImportError as exc:
            raise LocalModelUnavailable(
                "Wan 2.2 VACE needs the `gpu` extra (torch, diffusers) and a CUDA device; "
                "set PLATE_STUB_LOCAL=1 for the previs stub"
            ) from exc
        if not torch.cuda.is_available():
            raise LocalModelUnavailable(
                "Wan 2.2 VACE needs a CUDA device; set PLATE_STUB_LOCAL=1 for the previs stub"
            )
        return torch, diffusers

    def _run_gpu(self, parsed: VideoInputs, p: Mapping[str, Any], w: int, h: int, out: Path) -> None:
        """Unexercised on this Mac (verify-on-box): diffusers WanVACEPipeline over the reference stills."""
        if parsed.videos:
            raise LocalModelUnavailable("reference_video conditioning is not wired in the GPU path yet")
        torch, diffusers = self._require_runtime()
        from PIL import Image

        fps = int(p["fps"])
        num_frames = 4 * round(float(p["duration_s"]) * fps / 4) + 1  # Wan wants 4k + 1 frames
        refs = [Image.open(r.path).convert("RGB") for r in parsed.references if r.path is not None]
        generator = torch.Generator("cuda").manual_seed(int(p["seed"])) if "seed" in p else None
        pipe = diffusers.WanVACEPipeline.from_pretrained(REPO_ID, torch_dtype=torch.bfloat16)
        pipe.to("cuda")
        try:
            result = pipe(
                prompt=parsed.prompt,
                reference_images=refs,
                height=h,
                width=w,
                num_frames=num_frames,
                num_inference_steps=int(p["num_inference_steps"]),
                guidance_scale=float(p["guidance_scale"]),
                generator=generator,
            )
            diffusers.utils.export_to_video(result.frames[0], str(out), fps=fps)
        finally:
            del pipe
            torch.cuda.empty_cache()  # VRAM must be given back for Vantage (decision 11)
