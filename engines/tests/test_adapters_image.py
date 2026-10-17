"""Contract tests for the image engine adapters (router plan T17).

Gemini (Nano Banana 2.1 and Pro), OpenAI Images (GPT Image 2.5), Atlas Seedream 5 Pro, BFL FLUX 3, the local
Qwen-Image-Edit stub, Magnific Precision v2 and the local Real-ESRGAN stub. Nothing here reaches a vendor:
conftest.py blocks sockets, every cloud adapter is built on a FixtureTransport that replays the committed
recordings under engines/plate_engines/contract/<vendor>/, and an unrecorded request raises UnrecordedRequest.

The recordings are synthetic (authored from each vendor's documented request and response shapes) and are written
by the same code the tests replay, so a request the adapter stops making the same way fails twice: the fixture no
longer matches, and test_committed_recordings_match_a_fresh_recording sees the drift. To re-record after an
intended change, run with PLATE_RECORD_CONTRACT=1. Inputs are hand-encoded PNGs (stored deflate blocks, no zlib
level), so request body hashes are identical on Windows and macOS.

What each adapter must show: (a) its request is accepted by the recorded schema, (b) the output depends on the
input (and SilentlyIgnoredInput fires if a vendor ignores it), (c) refusals map to state refused, (d) size
snapping records the SizeMap mode, (e) no request leaves without a transport.
"""

from __future__ import annotations

import base64
import json
import os
import re
import struct
import sys
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import numpy as np
import pytest

from plate_engines.adapters.atlas_image import AtlasImage
from plate_engines.adapters.base import (
    FetchError,
    InputError,
    MissingTransport,
    ParamError,
    PlateUpscaleForbidden,
    SilentlyIgnoredInput,
    Staged,
)
from plate_engines.adapters.bfl import BFLImage
from plate_engines.adapters.gemini import GeminiImage
from plate_engines.adapters.image_common import (
    LocalModelUnavailable,
    MissingKey,
    ModelIdUnknown,
    SubmitFailed,
    UnknownJob,
    decode_png_8bit,
    png_size,
)
from plate_engines.adapters.local_qwen import QwenImageEdit
from plate_engines.adapters.openai_images import OpenAIImages
from plate_engines.adapters.transport import (
    CONTRACT_DIR,
    FixtureTransport,
    HostNotAllowed,
    body_key,
    write_recording,
)
from plate_engines.adapters.upscale import MagnificPrecisionV2, RealESRGAN
from plate_engines.atlas import AtlasError, AtlasRefused
from plate_engines.models import EngineSize

KEYS = {
    "GEMINI_API_KEY": "gem-key",
    "OPENAI_API_KEY": "oai-key",
    "ATLASCLOUD_KEY": "atl-key",
    "BFL_API_KEY": "bfl-key",
    "MAGNIFIC_API_KEY": "mag-key",
}
PROMPT = "relight to late golden hour, keep every edge and every material"
RECORD = os.environ.get("PLATE_RECORD_CONTRACT") == "1"
ATLAS = "https://api.atlascloud.ai/api/v1"

# ---- deterministic PNGs ---------------------------------------------------------------------------------

_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def synth_png(seed: int, w: int = 64, h: int = 36) -> bytes:
    """A w x h RGB PNG that is a pure function of seed. Stored deflate blocks, so no compressor ever varies."""
    x = np.arange(w, dtype=np.int64)[None, :]
    y = np.arange(h, dtype=np.int64)[:, None]
    pix = np.zeros((h, w, 3), dtype=np.uint8)
    pix[..., 0] = (x * 7 + seed * 53) % 256
    pix[..., 1] = (y * 11 + seed * 29) % 256
    pix[..., 2] = (x * y + seed * 97) % 256
    raw = b"".join(b"\x00" + pix[r].tobytes() for r in range(h))
    stream, pos = b"\x78\x01", 0
    while True:
        block = raw[pos : pos + 65535]
        pos += len(block)
        final = pos >= len(raw)
        stream += bytes([1 if final else 0]) + struct.pack("<HH", len(block), len(block) ^ 0xFFFF) + block
        if final:
            break
    stream += struct.pack(">I", zlib.adler32(raw))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return _SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", stream) + _chunk(b"IEND", b"")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def out_png(n: int) -> bytes:
    """A distinct vendor 'result' image per n (32 x 18)."""
    return synth_png(100 + n, 32, 18)


SM_GEMINI = {"mode": "exact", "engine_size": {"width": 1024, "height": 576, "label": "1K", "ratio": "16:9"}}
SM_OPENAI_PAD = {"mode": "pad", "engine_size": {"width": 1536, "height": 1024, "label": "", "ratio": ""}}


def img(seed: int, *, role: str = "plate", **extra: Any) -> dict[str, Any]:
    return {"bytes": synth_png(seed), "name": f"plate_{seed}.png", "role": role, **extra}


# ---- recording a fixture set -----------------------------------------------------------------------------


class Recorder(httpx.MockTransport):
    """Answers each request from a script of (recording name, response) and writes it as a recording.

    A response may be a list: that is a polling recording (served in order), and the first entry is what the
    caller sees during recording. JSON bodies up to 2000 bytes are stored readable; anything bigger (base64
    images, multipart) is stored as its body key.
    """

    def __init__(self, dest: Path, script: Sequence[tuple[str, Any]]) -> None:
        self.dest = dest
        self._script = list(script)
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if not self._script:
            raise AssertionError(f"unscripted request while recording: {request.method} {request.url}")
        name, spec = self._script.pop(0)
        content_type = request.headers.get("content-type", "")
        body = request.content
        kwargs: dict[str, Any] = {}
        if body:
            if "json" in content_type and len(body) < 2000:
                kwargs["body"] = json.loads(body)
            else:
                kwargs["body_hash"] = body_key(content_type, body)
        specs = spec if isinstance(spec, list) else [spec]
        path = write_recording(
            self.dest,
            name,
            method=request.method,
            url=str(request.url),
            responses=specs if isinstance(spec, list) else None,
            response=None if isinstance(spec, list) else spec,
            **kwargs,
        )
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc = {"note": "synthetic: authored from the vendor's documented shapes, not captured", **doc}
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        return FixtureTransport._build(specs[0])

    def done(self) -> None:
        assert not self._script, f"recording ended with unused script entries {[n for n, _ in self._script]}"


def staged(adapter: Any, inputs: dict[str, Any], params: dict[str, Any] | None = None, size: Any = None):
    """What the worker hands to submit(): inputs as egress left them and params after validate_params."""
    return Staged(inputs=inputs, params=adapter.validate_params(params or {}), size=size)


# ---- Gemini: cases, responses, recording -------------------------------------------------------------------


def gem_ok(n: int, text: str = "ok") -> dict[str, Any]:
    parts = [{"text": text}, {"inlineData": {"mimeType": "image/png", "data": b64(out_png(n))}}]
    return {
        "status": 200,
        "json": {
            "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 1290,
                "candidatesTokenCount": 1120,
                "totalTokenCount": 2410,
            },
        },
    }


GEMINI_CASES: dict[str, dict[str, Any]] = {
    "edit_a": {"inputs": {"image": img(1, sizemap=SM_GEMINI), "prompt": PROMPT}, "params": {}},
    "edit_b": {"inputs": {"image": img(2, sizemap=SM_GEMINI), "prompt": PROMPT}, "params": {}},
    "edit_refs": {
        "inputs": {
            "image": img(1, sizemap=SM_GEMINI),
            "prompt": PROMPT,
            "reference_images": [
                img(11, role="ai_layer", kind="object"),
                img(12, role="ai_layer", kind="character"),
                img(13, role="ai_layer", kind="style"),
            ],
        },
        "params": {"seed": 7, "candidate_count": 2},
    },
    "refused_block": {"inputs": {"image": img(3, sizemap=SM_GEMINI), "prompt": PROMPT}, "params": {}},
    "refused_finish": {"inputs": {"image": img(4, sizemap=SM_GEMINI), "prompt": PROMPT}, "params": {}},
    "error_500": {"inputs": {"image": img(5, sizemap=SM_GEMINI), "prompt": PROMPT}, "params": {}},
}
GEMINI_RESPONSES: dict[str, Any] = {
    "edit_a": gem_ok(1),
    "edit_b": gem_ok(2),
    "edit_refs": gem_ok(3, text="three references used"),
    "refused_block": {
        "status": 200,
        "json": {
            "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"},
            "usageMetadata": {"promptTokenCount": 9},
        },
    },
    "refused_finish": {
        "status": 200,
        "json": {"candidates": [{"finishReason": "SAFETY", "safetyRatings": [{"category": "x"}]}]},
    },
    "error_500": {
        "status": 500,
        "json": {"error": {"code": 500, "message": "Internal error encountered.", "status": "INTERNAL"}},
    },
}
GEMINI_MODEL = "gemini-3-pro-image"


def record_gemini(manifests, dest: Path, tmp: Path) -> None:
    rec = Recorder(dest, [(n, GEMINI_RESPONSES[n]) for n in GEMINI_CASES])
    adapter = GeminiImage(manifests["nano-banana-pro"], None, rec, staging_dir=tmp)
    for case in GEMINI_CASES.values():
        adapter.submit(staged(adapter, case["inputs"], case["params"]))
    rec.done()


# ---- OpenAI -----------------------------------------------------------------------------------------------

OPENAI_PROMPT = PROMPT


def oai_ok(n: int) -> dict[str, Any]:
    return {
        "status": 200,
        "json": {
            "created": 1789000000,
            "data": [{"b64_json": b64(out_png(n))}],
            "usage": {
                "input_tokens": 1300,
                "output_tokens": 1056,
                "total_tokens": 2356,
                "input_tokens_details": {"text_tokens": 40, "image_tokens": 1260},
            },
        },
    }


def oai_err(status: int, code: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "json": {"error": {"code": code, "message": message, "type": "image_generation_user_error"}},
    }


OPENAI_CASES: dict[str, dict[str, Any]] = {
    "edit_a": {"inputs": {"image": img(1, sizemap=SM_OPENAI_PAD), "prompt": OPENAI_PROMPT}, "params": {}},
    "edit_b": {"inputs": {"image": img(2, sizemap=SM_OPENAI_PAD), "prompt": OPENAI_PROMPT}, "params": {}},
    "edit_refs_mask": {
        "inputs": {
            "image": img(1, sizemap=SM_OPENAI_PAD),
            "prompt": "replace the sky only",
            "reference_images": [img(21, role="ai_layer")],
            "mask": img(22, role="mask"),
        },
        "params": {"quality": "high"},
    },
    "blocked_moderation": {
        "inputs": {"image": img(4, sizemap=SM_OPENAI_PAD), "prompt": PROMPT},
        "params": {},
    },
    "blocked_policy": {"inputs": {"image": img(5, sizemap=SM_OPENAI_PAD), "prompt": PROMPT}, "params": {}},
    "bad_request": {"inputs": {"image": img(6, sizemap=SM_OPENAI_PAD), "prompt": PROMPT}, "params": {}},
    "rate_limited": {"inputs": {"image": img(7, sizemap=SM_OPENAI_PAD), "prompt": PROMPT}, "params": {}},
}
OPENAI_RESPONSES: dict[str, Any] = {
    "edit_a": oai_ok(1),
    "edit_b": oai_ok(2),
    "edit_refs_mask": oai_ok(3),
    "blocked_moderation": oai_err(
        400, "moderation_blocked", "Your request was rejected by the safety system."
    ),
    "blocked_policy": oai_err(400, "content_policy_violation", "The prompt violates the content policy."),
    "bad_request": oai_err(400, "invalid_value", "Invalid value for 'size'."),
    "rate_limited": oai_err(429, "rate_limit_exceeded", "Slow down."),
}


def record_openai(manifests, dest: Path, tmp: Path) -> None:
    rec = Recorder(dest, [(n, OPENAI_RESPONSES[n]) for n in OPENAI_CASES])
    adapter = OpenAIImages(manifests["gpt-image-2.5"], None, rec, staging_dir=tmp)
    for case in OPENAI_CASES.values():
        adapter.submit(staged(adapter, case["inputs"], case["params"]))
    rec.done()


# ---- Atlas Seedream 5 Pro -----------------------------------------------------------------------------------

ATLAS_SLUG = "bytedance/seedream-5-pro/edit"  # null in the manifest until the P0 smoke test; recorded here
SM_ATLAS = {"mode": "exact", "engine_size": {"width": 1024, "height": 576, "label": "1K", "ratio": "16:9"}}


def atlas_up(name: str) -> dict[str, Any]:
    return {
        "status": 200,
        "json": {
            "code": 200,
            "data": {"download_url": f"https://atlas-media.oss-ap-southeast-1.aliyuncs.com/up/{name}.png"},
        },
    }


def atlas_poll(n: int) -> list[dict[str, Any]]:
    return [
        {"status": 200, "json": {"data": {"id": f"pred-{n}", "status": "processing"}}},
        {
            "status": 200,
            "json": {
                "data": {
                    "id": f"pred-{n}",
                    "status": "completed",
                    "outputs": [f"https://cdn.aliyuncs.com/out/{n}.png"],
                }
            },
        },
    ]


# name -> (image seed, prediction id)
ATLAS_CASES = {"a": 1, "b": 2}


def atlas_inputs(seed: int) -> dict[str, Any]:
    return {"image": img(seed, sizemap=SM_ATLAS), "prompt": PROMPT}


def record_atlas_image(manifests, dest: Path, tmp: Path) -> None:
    script: list[tuple[str, Any]] = []
    for tag in ATLAS_CASES:
        script += [
            (f"upload_{tag}", atlas_up(tag)),
            (f"generate_{tag}", {"status": 200, "json": {"code": 200, "data": {"id": f"pred-{tag}"}}}),
            (f"prediction_{tag}", atlas_poll(tag)),
            (f"download_{tag}", {"status": 200, "bytes_b64": b64(out_png(1 if tag == "a" else 2))}),
        ]
    # a moderated submit (HTTP 400 at generateImage) and a prediction that fails on policy
    script += [
        ("upload_refused", atlas_up("refused")),
        (
            "generate_refused",
            {"status": 400, "json": {"code": 400, "message": "request blocked by content moderation"}},
        ),
        ("upload_failed", atlas_up("failed")),
        ("generate_failed", {"status": 200, "json": {"code": 200, "data": {"id": "pred-failed"}}}),
        (
            "prediction_failed",
            {
                "status": 200,
                "json": {
                    "data": {"id": "pred-failed", "status": "failed", "message": "safety policy violation"}
                },
            },
        ),
        ("upload_boom", atlas_up("boom")),
        ("generate_boom", {"status": 500, "json": {"code": 500, "message": "upstream exploded"}}),
    ]
    rec = Recorder(dest, script)
    adapter = AtlasImage(_with_model(manifests["seedream-5-pro"], ATLAS_SLUG), None, rec, staging_dir=tmp)
    for tag, seed in ATLAS_CASES.items():
        job = adapter.submit(staged(adapter, atlas_inputs(seed)))
        adapter.poll(job)
        adapter.fetch(f"https://cdn.aliyuncs.com/out/{tag}.png")
    for seed in (3, 4):  # refused at submit, failed at poll
        job = adapter.submit(staged(adapter, atlas_inputs(seed)))
        if seed == 4:
            adapter.poll(job)
    with pytest.raises(AtlasError):
        adapter.submit(staged(adapter, atlas_inputs(5)))
    rec.done()


# ---- BFL FLUX 3 ----------------------------------------------------------------------------------------------

BOX = {"x": 0.25, "y": 0.4, "w": 0.3, "h": 0.5}


def bfl_submit(tag: str) -> dict[str, Any]:
    return {
        "status": 200,
        "json": {
            "id": f"bfl-{tag}",
            "polling_url": f"https://api.elsewhere.example/v1/get_result?id=bfl-{tag}",
            "cost": 4.0,
            "input_mp": 0.0,
            "output_mp": 0.0,
        },
    }


def bfl_inputs(seed: int, **extra: Any) -> dict[str, Any]:
    return {"image": img(seed), "prompt": PROMPT, **extra}


def record_bfl(manifests, dest: Path, tmp: Path) -> None:
    def ready(n: str) -> dict[str, Any]:
        return {"status": "Ready", "result": {"sample": f"https://delivery-eu.bfl.ai/results/{n}.png"}}

    script: list[tuple[str, Any]] = [
        ("submit_a", bfl_submit("a")),
        ("poll_a", [{"status": 200, "json": {"status": "Pending"}}, {"status": 200, "json": ready("a")}]),
        ("download_a", {"status": 200, "bytes_b64": b64(out_png(1))}),
        ("submit_b", bfl_submit("b")),
        ("poll_b", [{"status": 200, "json": {"status": "Pending"}}, {"status": 200, "json": ready("b")}]),
        ("download_b", {"status": 200, "bytes_b64": b64(out_png(2))}),
        ("submit_box", bfl_submit("box")),
        ("submit_moderated", bfl_submit("mod")),
        ("poll_moderated", {"status": 200, "json": {"status": "Content Moderated"}}),
        ("submit_request_moderated", bfl_submit("rmod")),
        ("poll_request_moderated", {"status": 200, "json": {"status": "Request Moderated"}}),
        ("submit_error", bfl_submit("err")),
        ("poll_error", {"status": 200, "json": {"status": "Error", "details": {"msg": "worker crashed"}}}),
        (
            "submit_blocked",
            {"status": 422, "json": {"detail": "Your input was blocked by our moderation filter"}},
        ),
        ("submit_down", {"status": 503, "json": {"detail": "service unavailable"}}),
    ]
    rec = Recorder(dest, script)
    adapter = BFLImage(manifests["flux-3-image"], None, rec, staging_dir=tmp)
    for tag, seed in (("a", 1), ("b", 2)):
        job = adapter.submit(staged(adapter, bfl_inputs(seed)))
        adapter.poll(job)
        adapter.fetch(f"https://delivery-eu.bfl.ai/results/{tag}.png")
    adapter.submit(staged(adapter, bfl_inputs(3, box=BOX)))
    for seed in (4, 5, 6):
        adapter.poll(adapter.submit(staged(adapter, bfl_inputs(seed))))
    adapter.submit(staged(adapter, bfl_inputs(7)))  # blocked at submit
    with pytest.raises(SubmitFailed):
        adapter.submit(staged(adapter, bfl_inputs(8)))
    rec.done()


# ---- Magnific (recorded into tmp_path: the upscaler has no committed folder in T17's file list) -------------


def mag_task(status: str, generated: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": 200,
        "json": {"data": {"task_id": "mag-1", "status": status, "generated": generated or []}},
    }


MAG_BASE = "https://api.freepik.com/v1/ai/image-upscaler/precision"


def mag_inputs(seed: int, role: str = "ai_layer") -> dict[str, Any]:
    return {"image": img(seed, role=role)}


def record_magnific(manifests, dest: Path, tmp: Path) -> None:
    def submit(tag: str) -> dict[str, Any]:
        return {
            "status": 200,
            "json": {"data": {"task_id": f"mag-{tag}", "status": "CREATED", "generated": []}},
        }

    def poll(tag: str, n: int) -> list[dict[str, Any]]:
        return [
            {
                "status": 200,
                "json": {"data": {"task_id": f"mag-{tag}", "status": "CREATED", "generated": []}},
            },
            {
                "status": 200,
                "json": {"data": {"task_id": f"mag-{tag}", "status": "IN_PROGRESS", "generated": []}},
            },
            {
                "status": 200,
                "json": {
                    "data": {
                        "task_id": f"mag-{tag}",
                        "status": "COMPLETED",
                        "generated": [f"https://ai-statics.freepik.com/up/{n}.png"],
                    }
                },
            },
        ]

    script: list[tuple[str, Any]] = [
        ("submit_a", submit("a")),
        ("poll_a", poll("a", 1)),
        ("download_a", {"status": 200, "bytes_b64": b64(out_png(1))}),
        ("submit_b", submit("b")),
        ("poll_b", poll("b", 2)),
        ("download_b", {"status": 200, "bytes_b64": b64(out_png(2))}),
        ("submit_failed", submit("f")),
        (
            "poll_failed",
            {"status": 200, "json": {"data": {"task_id": "mag-f", "status": "FAILED", "generated": []}}},
        ),
        ("submit_blocked", {"status": 400, "json": {"message": "image rejected by the safety filter"}}),
    ]
    rec = Recorder(dest, script)
    adapter = MagnificPrecisionV2(manifests["magnific-precision-v2"], None, rec, staging_dir=tmp)
    for seed in (1, 2):
        job = adapter.submit(staged(adapter, mag_inputs(seed), {"scale": 2, "sharpen": 10}))
        adapter.poll(job)
        adapter.fetch(f"https://ai-statics.freepik.com/up/{seed}.png")
    adapter.poll(adapter.submit(staged(adapter, mag_inputs(3))))
    adapter.submit(staged(adapter, mag_inputs(4)))
    rec.done()


RECORDERS = {
    "gemini": record_gemini,
    "openai": record_openai,
    "atlas_image": record_atlas_image,
    "bfl": record_bfl,
}


def _with_model(manifest, model_id: str):
    return manifest.model_copy(update={"model_id": model_id})


# ---- fixtures -----------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    for name in ("FREEPIK_API_KEY", "GOOGLE_API_KEY", "PLATE_STUB_LOCAL", "PLATE_LIVE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session", autouse=True)
def _rerecord_when_asked(manifests, tmp_path_factory):
    """PLATE_RECORD_CONTRACT=1 rewrites the committed recordings from the code above before any test runs."""
    if RECORD:
        with pytest.MonkeyPatch.context() as mp:
            for name, value in KEYS.items():
                mp.setenv(name, value)
            for vendor, record in RECORDERS.items():
                dest = CONTRACT_DIR / vendor
                for old in dest.glob("*.json") if dest.is_dir() else []:
                    old.unlink()
                record(manifests, dest, tmp_path_factory.mktemp(f"rec-{vendor}"))


@pytest.fixture
def stage_dir(tmp_path):
    return tmp_path / "staging"


@pytest.fixture
def gemini_replay():
    return FixtureTransport(CONTRACT_DIR / "gemini")


@pytest.fixture
def openai_replay():
    return FixtureTransport(CONTRACT_DIR / "openai")


@pytest.fixture
def atlas_replay():
    return FixtureTransport(CONTRACT_DIR / "atlas_image")


@pytest.fixture
def bfl_replay():
    return FixtureTransport(CONTRACT_DIR / "bfl")


@pytest.fixture
def magnific_replay(manifests, tmp_path):
    dest = tmp_path / "magnific-recordings"
    record_magnific(manifests, dest, tmp_path / "rec-staging")
    return FixtureTransport(dest)


def gemini(manifests, transport, stage_dir, engine="nano-banana-pro", **kw) -> GeminiImage:
    return GeminiImage(manifests[engine], None, transport, staging_dir=stage_dir, **kw)


def openai(manifests, transport, stage_dir, **kw) -> OpenAIImages:
    return OpenAIImages(manifests["gpt-image-2.5"], None, transport, staging_dir=stage_dir, **kw)


def atlas(manifests, transport, stage_dir, **kw) -> AtlasImage:
    return AtlasImage(
        _with_model(manifests["seedream-5-pro"], ATLAS_SLUG), None, transport, staging_dir=stage_dir, **kw
    )


def bfl(manifests, transport, stage_dir, **kw) -> BFLImage:
    return BFLImage(manifests["flux-3-image"], None, transport, staging_dir=stage_dir, **kw)


def magnific(manifests, transport, stage_dir, **kw) -> MagnificPrecisionV2:
    return MagnificPrecisionV2(
        manifests["magnific-precision-v2"], None, transport, staging_dir=stage_dir, **kw
    )


def run(adapter, case: dict[str, Any]) -> str:
    return adapter.submit(staged(adapter, case["inputs"], case["params"]))


def result(adapter, job_id: str) -> bytes:
    status = adapter.poll(job_id)
    assert status.state == "done", status
    return adapter.fetch(status.outputs[0])


def parse_multipart(content_type: str, body: bytes) -> list[tuple[str, str | None, bytes]]:
    boundary = re.search(r"boundary=([^;\s]+)", content_type).group(1).strip('"')
    out = []
    for part in body.split(b"--" + boundary.encode("ascii"))[1:]:
        if part.startswith(b"--"):
            break
        head, _, payload = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
        payload = payload[:-2] if payload.endswith(b"\r\n") else payload
        text = head.decode("utf-8")
        filename = re.search(r'filename="([^"]*)"', text)
        out.append(
            (re.search(r'name="([^"]*)"', text).group(1), filename.group(1) if filename else None, payload)
        )
    return out


# =======================================================================================================
# the recordings themselves
# =======================================================================================================


@pytest.mark.parametrize("vendor", sorted(RECORDERS))
def test_committed_recordings_match_a_fresh_recording(vendor, manifests, tmp_path):
    """A request an adapter stops making the same way shows up as drift here, before it shows up on the box."""
    fresh = tmp_path / "fresh"
    RECORDERS[vendor](manifests, fresh, tmp_path / "stage")
    committed = CONTRACT_DIR / vendor
    assert committed.is_dir(), f"no committed recordings at {committed}; run with PLATE_RECORD_CONTRACT=1"
    assert sorted(p.name for p in committed.glob("*.json")) == sorted(p.name for p in fresh.glob("*.json"))
    for p in sorted(fresh.glob("*.json")):
        assert (committed / p.name).read_bytes() == p.read_bytes(), f"{vendor}/{p.name} drifted"


def test_recordings_are_utf8_lf_and_portable():
    for vendor in RECORDERS:
        for p in (CONTRACT_DIR / vendor).glob("*.json"):
            raw = p.read_bytes()
            assert b"\r\n" not in raw and raw.endswith(b"\n")
            assert ":" not in p.name
            json.loads(raw.decode("utf-8"))


def test_synthetic_pngs_are_deterministic_and_decodable():
    assert synth_png(1) == synth_png(1) != synth_png(2)
    assert png_size(synth_png(1)) == (64, 36)
    assert decode_png_8bit(synth_png(1)).shape == (36, 64, 3)
    assert png_size(out_png(1)) == (32, 18)


# =======================================================================================================
# Gemini
# =======================================================================================================


def test_gemini_request_is_the_documented_schema(manifests, gemini_replay, stage_dir):
    adapter = gemini(manifests, gemini_replay, stage_dir)
    run(adapter, GEMINI_CASES["edit_a"])
    (call,) = gemini_replay.served
    assert (call.method, call.host, call.path) == (
        "POST",
        "generativelanguage.googleapis.com",
        f"/v1beta/models/{GEMINI_MODEL}:generateContent",
    )
    assert call.headers["x-goog-api-key"] == KEYS["GEMINI_API_KEY"]
    body = json.loads(call.body)
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"text": PROMPT}
    assert parts[1] == {"inline_data": {"mime_type": "image/png", "data": b64(synth_png(1))}}
    assert len(parts) == 2
    assert body["generationConfig"] == {
        "responseModalities": ["IMAGE"],
        "imageConfig": {"aspectRatio": "16:9", "imageSize": "1K"},
    }


def test_gemini_references_follow_the_plate_and_params_ride_in_generation_config(
    manifests, gemini_replay, stage_dir
):
    adapter = gemini(manifests, gemini_replay, stage_dir)
    job = run(adapter, GEMINI_CASES["edit_refs"])
    body = json.loads(gemini_replay.served[0].body)
    parts = body["contents"][0]["parts"]
    assert [p.get("inline_data", {}).get("data") for p in parts[1:]] == [
        b64(synth_png(s)) for s in (1, 11, 12, 13)
    ]
    assert body["generationConfig"]["seed"] == 7 and body["generationConfig"]["candidateCount"] == 2
    status = adapter.poll(job)
    assert status.state == "done" and status.message == "three references used"
    assert status.usage["totalTokenCount"] == 2410  # billed tokens are kept for the ledger


def test_gemini_output_depends_on_input(manifests, gemini_replay, stage_dir):
    adapter = gemini(manifests, gemini_replay, stage_dir)
    a = result(adapter, run(adapter, GEMINI_CASES["edit_a"]))
    b = result(adapter, run(adapter, GEMINI_CASES["edit_b"]))
    assert a == out_png(1) and b == out_png(2) and a != b


def test_gemini_sync_call_polls_done_at_once_and_stages_the_bytes(manifests, gemini_replay, stage_dir):
    adapter = gemini(manifests, gemini_replay, stage_dir)
    job = run(adapter, GEMINI_CASES["edit_a"])
    first, second = adapter.poll(job), adapter.poll(job)
    assert first == second and first.state == "done" and len(first.outputs) == 1
    staged_file = Path(urlsplit(first.outputs[0]).path)
    assert ":" not in staged_file.name and staged_file.name.startswith(job)
    assert adapter.fetch(first.outputs[0]) == out_png(1)
    with pytest.raises(UnknownJob):
        adapter.poll("no-such-job")


def test_gemini_fetch_refuses_files_outside_the_staging_dir(manifests, gemini_replay, stage_dir, tmp_path):
    adapter = gemini(manifests, gemini_replay, stage_dir)
    run(adapter, GEMINI_CASES["edit_a"])
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"x")
    with pytest.raises(FetchError, match="outside"):
        adapter.fetch(outside.resolve().as_uri())


@pytest.mark.parametrize(
    ("case", "why"), [("refused_block", "PROHIBITED_CONTENT"), ("refused_finish", "SAFETY")]
)
def test_gemini_refusals_map_to_refused(manifests, gemini_replay, stage_dir, case, why):
    adapter = gemini(manifests, gemini_replay, stage_dir)
    status = adapter.poll(run(adapter, GEMINI_CASES[case]))
    assert status.state == "refused" and why in status.message and status.outputs == []


def test_gemini_http_error_is_failed_not_refused(manifests, gemini_replay, stage_dir):
    adapter = gemini(manifests, gemini_replay, stage_dir)
    status = adapter.poll(run(adapter, GEMINI_CASES["error_500"]))
    assert status.state == "failed" and "INTERNAL" in status.message


def test_gemini_records_the_sizemap_mode_and_the_size_asked_for(manifests, gemini_replay, stage_dir):
    adapter = gemini(manifests, gemini_replay, stage_dir)
    job = run(adapter, GEMINI_CASES["edit_a"])
    choice = adapter.size_choice(job)
    assert (choice.mode, choice.source) == ("exact", "sizemap")
    assert (choice.size.w, choice.size.h, choice.size.label) == (1024, 576, "1K")


def test_gemini_without_a_sizemap_snaps_and_records_the_mode(manifests, stage_dir):
    odd = {
        "image": {"bytes": synth_png(1, 50, 40), "name": "odd.png", "role": "plate"},
        "prompt": PROMPT,
    }  # 5:4
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=gem_ok(9)["json"])

    adapter = gemini(manifests, httpx.MockTransport(handler), stage_dir)
    job = adapter.submit(staged(adapter, odd))
    assert seen[0]["generationConfig"]["imageConfig"] == {"aspectRatio": "5:4", "imageSize": "1K"}
    assert adapter.size_choice(job).mode == "exact" and adapter.size_choice(job).source == "snapped"


def test_gemini_with_a_real_egress_sizemap(manifests, stage_dir):
    """egress.prepare's (png, SizeMap) is the input: the SizeMap object itself rides on the image mapping."""
    prepare = pytest.importorskip("plate_pixels.egress").prepare

    plate = (np.linspace(0, 65535, 640 * 360 * 3).reshape(360, 640, 3)).astype(np.uint16)
    png, sm = prepare(plate, None, manifests["nano-banana-pro"])
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=gem_ok(9)["json"])

    adapter = gemini(manifests, httpx.MockTransport(handler), stage_dir)
    job = adapter.submit(staged(adapter, {"image": {"bytes": png, "sizemap": sm}, "prompt": PROMPT}))
    assert adapter.size_choice(job).mode == sm.mode
    cfg = sent[0]["generationConfig"]["imageConfig"]
    assert cfg["aspectRatio"] == sm.engine_size["ratio"] and cfg["imageSize"] == sm.engine_size["label"]


def test_gemini_staged_size_wins_and_is_recorded(manifests, stage_dir):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=gem_ok(9)["json"])

    adapter = gemini(manifests, httpx.MockTransport(handler), stage_dir)
    size = EngineSize(w=2048, h=1152, label="2K")
    job = adapter.submit(staged(adapter, {"image": img(1), "prompt": PROMPT}, size=size))
    assert adapter.size_choice(job).size == size and adapter.size_choice(job).source == "staged"
    assert sent[0]["generationConfig"]["imageConfig"]["imageSize"] == "2K"


def test_gemini_quote_uses_manifest_prices_and_makes_no_request(manifests, stage_dir):
    def boom(request):
        raise AssertionError("quote() must not send anything")

    adapter = gemini(manifests, httpx.MockTransport(boom), stage_dir)
    q = adapter.quote({"image": img(1), "prompt": PROMPT}, {"image_size": "4K"})
    assert q.price_usd == pytest.approx(0.24) and q.price_status == "unconfirmed"
    assert q.custody_grade == "D"  # pending_written_terms counts as D
    assert q.model_version == GEMINI_MODEL and (q.size.w, q.size.h) == (4096, 2304)
    q2 = adapter.quote({"image": img(1), "prompt": PROMPT}, {"image_size": "1K", "candidate_count": 3})
    assert q2.price_usd == pytest.approx(
        0.24 * 3
    )  # 1K is unpriced for Pro: falls back to the manifest figure


def test_gemini_reference_caps(manifests, stage_dir):
    adapter = gemini(manifests, httpx.MockTransport(lambda r: httpx.Response(500)), stage_dir)
    four_style = [img(30 + i, role="ai_layer", kind="style") for i in range(4)]
    with pytest.raises(InputError, match="style"):
        adapter.quote({"image": img(1), "prompt": PROMPT, "reference_images": four_style}, {})
    flash = gemini(
        manifests, httpx.MockTransport(lambda r: httpx.Response(500)), stage_dir, "nano-banana-2.1"
    )
    five_people = [img(40 + i, role="ai_layer", kind="character") for i in range(5)]
    with pytest.raises(InputError, match="character"):
        flash.quote({"image": img(1), "prompt": PROMPT, "reference_images": five_people}, {})
    fourteen = [img(50 + i, role="ai_layer") for i in range(14)]
    with pytest.raises(InputError):  # the manifest caps references at 13 (14 images with the plate)
        adapter.quote({"image": img(1), "prompt": PROMPT, "reference_images": fourteen}, {})


def test_gemini_nb21_has_no_model_id_yet(manifests, stage_dir):
    adapter = gemini(
        manifests, httpx.MockTransport(lambda r: httpx.Response(500)), stage_dir, "nano-banana-2.1"
    )
    with pytest.raises(ModelIdUnknown, match="unverified_p0"):
        adapter.submit(staged(adapter, {"image": img(1), "prompt": PROMPT}))
    ready = GeminiImage(
        _with_model(manifests["nano-banana-2.1"], "gemini-nb21-test"),
        None,
        httpx.MockTransport(lambda r: httpx.Response(200, json=gem_ok(9)["json"])),
        staging_dir=stage_dir,
    )
    assert ready.poll(ready.submit(staged(ready, {"image": img(1), "prompt": PROMPT}))).state == "done"


def test_gemini_ignoring_vendor_raises_silently_ignored_input(manifests, stage_dir):
    deaf = httpx.MockTransport(
        lambda r: httpx.Response(200, json=gem_ok(9)["json"])
    )  # same image for any input
    adapter = gemini(manifests, deaf, stage_dir)
    adapter.submit(staged(adapter, {"image": img(1), "prompt": PROMPT}))
    adapter.submit(staged(adapter, {"image": img(1), "prompt": PROMPT}))  # same input twice is fine
    with pytest.raises(SilentlyIgnoredInput, match="ignored the input"):
        adapter.submit(staged(adapter, {"image": img(2), "prompt": PROMPT}))


# =======================================================================================================
# OpenAI Images
# =======================================================================================================


def test_openai_request_is_the_documented_multipart(manifests, openai_replay, stage_dir):
    adapter = openai(manifests, openai_replay, stage_dir)
    run(adapter, OPENAI_CASES["edit_a"])
    (call,) = openai_replay.served
    assert (call.method, call.host, call.path) == ("POST", "api.openai.com", "/v1/images/edits")
    assert call.headers["authorization"] == f"Bearer {KEYS['OPENAI_API_KEY']}"
    parts = parse_multipart(call.headers["content-type"], call.body)
    fields = {n: p.decode("utf-8") for n, f, p in parts if f is None}
    assert fields == {
        "model": "gpt-image-2.5",
        "prompt": OPENAI_PROMPT,
        "size": "1536x1024",
        "quality": "medium",
        "input_fidelity": "high",
        "output_format": "png",
        "n": "1",
    }
    files = [(n, f, p) for n, f, p in parts if f is not None]
    assert files == [("image[]", "plate_1.png", synth_png(1))]


def test_openai_references_and_mask_go_as_files(manifests, openai_replay, stage_dir):
    adapter = openai(manifests, openai_replay, stage_dir)
    run(adapter, OPENAI_CASES["edit_refs_mask"])
    parts = parse_multipart(openai_replay.served[0].headers["content-type"], openai_replay.served[0].body)
    files = [(n, p) for n, f, p in parts if f is not None]
    assert files == [("image[]", synth_png(1)), ("image[]", synth_png(21)), ("mask", synth_png(22))]
    assert dict((n, p.decode()) for n, f, p in parts if f is None)["quality"] == "high"


def test_openai_output_depends_on_input_and_usage_is_recorded(manifests, openai_replay, stage_dir):
    adapter = openai(manifests, openai_replay, stage_dir)
    ja, jb = run(adapter, OPENAI_CASES["edit_a"]), run(adapter, OPENAI_CASES["edit_b"])
    assert result(adapter, ja) == out_png(1) and result(adapter, jb) == out_png(2)
    assert adapter.poll(ja).usage["total_tokens"] == 2356 and adapter.poll(ja).usage["output_tokens"] == 1056


@pytest.mark.parametrize(
    ("case", "state"),
    [
        ("blocked_moderation", "refused"),
        ("blocked_policy", "refused"),
        ("bad_request", "failed"),  # a 400 that is not moderation is a failure
        ("rate_limited", "failed"),
    ],
)
def test_openai_error_mapping(manifests, openai_replay, stage_dir, case, state):
    adapter = openai(manifests, openai_replay, stage_dir)
    status = adapter.poll(run(adapter, OPENAI_CASES[case]))
    assert status.state == state and status.outputs == []


def test_openai_size_mode_comes_from_the_sizemap(manifests, openai_replay, stage_dir):
    adapter = openai(manifests, openai_replay, stage_dir)
    job = run(adapter, OPENAI_CASES["edit_a"])
    assert adapter.size_choice(job).mode == "pad" and adapter.size_choice(job).source == "sizemap"


def test_openai_snaps_a_frame_to_a_legal_size_and_records_pad(manifests, stage_dir):
    sent = []

    def handler(request):
        sent.append(parse_multipart(request.headers["content-type"], request.content))
        return httpx.Response(200, json=oai_ok(9)["json"])

    adapter = openai(manifests, httpx.MockTransport(handler), stage_dir)
    job = adapter.submit(staged(adapter, {"image": {"bytes": synth_png(1, 64, 36)}, "prompt": PROMPT}))
    assert {n: p for n, f, p in sent[0] if f is None}["size"] == b"1536x1024"
    assert adapter.size_choice(job).mode == "pad" and adapter.size_choice(job).source == "snapped"


def test_openai_arbitrary_sizes_stay_off_until_p0_verifies_them(manifests, stage_dir):
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json=oai_ok(9)["json"]))
    inputs = {"image": img(1), "prompt": PROMPT}
    arbitrary = EngineSize(w=1280, h=1024, label="")
    from_egress = {
        "image": img(1, sizemap={"mode": "arbitrary", "engine_size": {"width": 1280, "height": 1024}}),
        "prompt": PROMPT,
    }
    adapter = openai(manifests, ok, stage_dir)
    with pytest.raises(ParamError, match="unverified_p0"):
        adapter.quote(from_egress, {})
    with pytest.raises(ParamError, match="unverified_p0"):
        adapter.submit(staged(adapter, inputs, size=arbitrary))
    allowed = openai(manifests, ok, stage_dir, allow_unverified_sizes=True)
    assert allowed.quote(from_egress, {}).size == arbitrary
    assert allowed.size_choice(allowed.submit(staged(allowed, inputs, size=arbitrary))).size == arbitrary
    with pytest.raises(ParamError):  # the manifest enum itself rejects size strings it never listed
        adapter.validate_params({"size": "999x999"})
    outside = EngineSize(
        w=2048, h=1024, label=""
    )  # beyond the envelope even when unverified sizes are allowed
    with pytest.raises(ParamError):
        allowed.submit(staged(allowed, inputs, size=outside))


def test_openai_quote_uses_the_quality_tier(manifests, stage_dir):
    adapter = openai(manifests, httpx.MockTransport(lambda r: httpx.Response(500)), stage_dir)
    inputs = {"image": img(1), "prompt": PROMPT}
    assert adapter.quote(inputs, {"quality": "high"}).price_usd == pytest.approx(0.16)
    assert adapter.quote(inputs, {"quality": "medium"}).price_usd == pytest.approx(0.07)
    assert adapter.quote(inputs, {"quality": "medium", "n": 2}).price_usd == pytest.approx(0.14)
    assert adapter.quote(inputs, {}).custody_grade == "D"  # C1 candidate until ZDR is held in writing


def test_openai_image_cap_is_sixteen_and_unknown_params_are_rejected(manifests, stage_dir):
    adapter = openai(manifests, httpx.MockTransport(lambda r: httpx.Response(500)), stage_dir)
    refs = [img(60 + i, role="ai_layer") for i in range(16)]
    with pytest.raises(InputError):
        adapter.quote({"image": img(1), "prompt": PROMPT, "reference_images": refs}, {})
    with pytest.raises(ParamError, match="unknown parameter"):
        adapter.quote({"image": img(1), "prompt": PROMPT}, {"qualty": "high"})


def test_openai_ignoring_vendor_raises(manifests, stage_dir):
    deaf = httpx.MockTransport(lambda r: httpx.Response(200, json=oai_ok(9)["json"]))
    adapter = openai(manifests, deaf, stage_dir)
    adapter.submit(staged(adapter, {"image": img(1, sizemap=SM_OPENAI_PAD), "prompt": PROMPT}))
    with pytest.raises(SilentlyIgnoredInput):
        adapter.submit(staged(adapter, {"image": img(2, sizemap=SM_OPENAI_PAD), "prompt": PROMPT}))


# =======================================================================================================
# Atlas Seedream 5 Pro
# =======================================================================================================


def test_atlas_image_upload_then_generate_then_poll(manifests, atlas_replay, stage_dir):
    adapter = atlas(manifests, atlas_replay, stage_dir)
    job = adapter.submit(staged(adapter, atlas_inputs(1)))
    assert job == "pred-a"
    upload, generate = atlas_replay.served
    assert (upload.method, upload.path) == ("POST", "/api/v1/model/uploadMedia")
    (name, filename, payload) = parse_multipart(upload.headers["content-type"], upload.body)[0]
    assert (
        (name, payload) == ("file", synth_png(1)) and filename.endswith("plate_1.png") and ":" not in filename
    )
    assert generate.path == "/api/v1/model/generateImage"
    body = json.loads(generate.body)
    assert body == {
        "model": ATLAS_SLUG,
        "prompt": PROMPT,
        "images": ["https://atlas-media.oss-ap-southeast-1.aliyuncs.com/up/a.png"],
        "aspect_ratio": "16:9",
        "resolution": "1K",
    }
    assert (
        upload.headers["authorization"]
        == generate.headers["authorization"]
        == f"Bearer {KEYS['ATLASCLOUD_KEY']}"
    )
    assert adapter.poll(job).state == "running"
    done = adapter.poll(job)
    assert done.state == "done" and done.outputs == ["https://cdn.aliyuncs.com/out/a.png"]


def test_atlas_image_output_depends_on_input(manifests, atlas_replay, stage_dir):
    adapter = atlas(manifests, atlas_replay, stage_dir)
    outputs = {}
    for tag, seed in ATLAS_CASES.items():
        job = adapter.submit(staged(adapter, atlas_inputs(seed)))
        adapter.poll(job)
        outputs[tag] = adapter.fetch(adapter.poll(job).outputs[0])
    assert outputs == {"a": out_png(1), "b": out_png(2)}
    # the download carries no credential: the result URL is a third-party host
    downloads = [c for c in atlas_replay.served if c.method == "GET" and c.host.endswith("aliyuncs.com")]
    assert downloads and all("authorization" not in c.headers for c in downloads)


def test_atlas_image_moderated_submit_is_a_refused_job(manifests, atlas_replay, stage_dir):
    adapter = atlas(manifests, atlas_replay, stage_dir)
    job = adapter.submit(staged(adapter, atlas_inputs(3)))
    status = adapter.poll(job)
    assert status.state == "refused" and "moderation" in status.message


def test_atlas_image_policy_failure_at_poll_is_refused(manifests, atlas_replay, stage_dir):
    adapter = atlas(manifests, atlas_replay, stage_dir)
    status = adapter.poll(adapter.submit(staged(adapter, atlas_inputs(4))))
    assert status.state == "refused" and "policy" in status.message


def test_atlas_image_other_submit_failures_raise(manifests, atlas_replay, stage_dir):
    adapter = atlas(manifests, atlas_replay, stage_dir)
    with pytest.raises(AtlasError) as exc:
        adapter.submit(staged(adapter, atlas_inputs(5)))
    assert not isinstance(exc.value, AtlasRefused)


def test_atlas_image_is_grade_d_and_its_slug_is_unverified(manifests, stage_dir):
    ok = httpx.MockTransport(lambda r: httpx.Response(500))
    adapter = AtlasImage(manifests["seedream-5-pro"], None, ok, staging_dir=stage_dir)
    q = adapter.quote({"image": img(1, sizemap=SM_ATLAS), "prompt": PROMPT}, {})
    assert q.custody_grade == "D" and q.price_usd == pytest.approx(0.12) and q.size.label == "1K"
    with pytest.raises(ModelIdUnknown):
        adapter.submit(staged(adapter, atlas_inputs(1)))


def test_atlas_image_carries_only_what_atlas_has_fields_for(manifests, stage_dir):
    adapter = atlas(manifests, httpx.MockTransport(lambda r: httpx.Response(500)), stage_dir)
    with pytest.raises(InputError, match="mask"):
        adapter.quote({"image": img(1), "prompt": PROMPT, "mask": img(2, role="mask")}, {})
    with pytest.raises(ParamError, match="unknown parameter"):
        adapter.quote({"image": img(1), "prompt": PROMPT}, {"guidance": 4})


def test_atlas_image_never_follows_a_result_url_off_the_allowlist(manifests, atlas_replay, stage_dir):
    adapter = atlas(manifests, atlas_replay, stage_dir)
    with pytest.raises(HostNotAllowed):
        adapter.fetch("https://evil.example.com/out/a.png")
    assert not [c for c in atlas_replay.served if c.host == "evil.example.com"]


# =======================================================================================================
# BFL FLUX 3
# =======================================================================================================


def test_bfl_submit_and_poll_by_id(manifests, bfl_replay, stage_dir):
    adapter = bfl(manifests, bfl_replay, stage_dir)
    job = adapter.submit(staged(adapter, bfl_inputs(1)))
    assert job == "bfl-a"
    submit = bfl_replay.served[0]
    assert (submit.method, submit.host, submit.path) == ("POST", "api.bfl.ai", "/v1/flux-3-image")
    assert submit.headers["x-key"] == KEYS["BFL_API_KEY"]
    body = json.loads(submit.body)
    assert body["input_image"] == b64(synth_png(1)) and body["prompt"] == PROMPT
    assert body["output_format"] == "png" and "box" not in body
    assert adapter.poll(job).state == "running"
    done = adapter.poll(job)
    assert done.state == "done" and done.outputs == ["https://delivery-eu.bfl.ai/results/a.png"]
    assert done.usage["cost"] == 4.0
    polled = [c for c in bfl_replay.served if c.path == "/v1/get_result"]
    assert len(polled) == 2 and all(
        c.host == "api.bfl.ai" for c in polled
    )  # polling_url (another host) ignored


def test_bfl_output_depends_on_input(manifests, bfl_replay, stage_dir):
    adapter = bfl(manifests, bfl_replay, stage_dir)
    outputs = []
    for seed in (1, 2):
        job = adapter.submit(staged(adapter, bfl_inputs(seed)))
        adapter.poll(job)
        outputs.append(adapter.fetch(adapter.poll(job).outputs[0]))
    assert outputs == [out_png(1), out_png(2)]
    downloads = [c for c in bfl_replay.served if c.host == "delivery-eu.bfl.ai"]
    assert len(downloads) == 2 and all(
        "x-key" not in c.headers for c in downloads
    )  # the key never follows a URL


def test_bfl_box_is_passed_through(manifests, bfl_replay, stage_dir):
    adapter = bfl(manifests, bfl_replay, stage_dir)
    adapter.submit(staged(adapter, bfl_inputs(3, box=BOX)))
    assert json.loads(bfl_replay.served[0].body)["box"] == BOX


@pytest.mark.parametrize(("seed", "message"), [(4, "Content Moderated"), (5, "Request Moderated")])
def test_bfl_moderation_statuses_are_refused(manifests, bfl_replay, stage_dir, seed, message):
    adapter = bfl(manifests, bfl_replay, stage_dir)
    status = adapter.poll(adapter.submit(staged(adapter, bfl_inputs(seed))))
    assert status.state == "refused" and message in status.message


def test_bfl_error_status_is_failed(manifests, bfl_replay, stage_dir):
    adapter = bfl(manifests, bfl_replay, stage_dir)
    status = adapter.poll(adapter.submit(staged(adapter, bfl_inputs(6))))
    assert status.state == "failed" and "worker crashed" in status.message


def test_bfl_moderated_submit_is_refused_and_other_submit_failures_raise(manifests, bfl_replay, stage_dir):
    adapter = bfl(manifests, bfl_replay, stage_dir)
    status = adapter.poll(adapter.submit(staged(adapter, bfl_inputs(7))))
    assert status.state == "refused" and "moderation" in status.message
    with pytest.raises(SubmitFailed, match="503"):
        adapter.submit(staged(adapter, bfl_inputs(8)))


def test_bfl_reference_cap_and_no_mask(manifests, stage_dir):
    adapter = bfl(manifests, httpx.MockTransport(lambda r: httpx.Response(500)), stage_dir)
    four = [img(70 + i, role="ai_layer") for i in range(4)]
    with pytest.raises(InputError, match="cap"):
        adapter.quote({"image": img(1), "prompt": PROMPT, "reference_images": four}, {})
    with pytest.raises(InputError, match="mask"):
        adapter.quote({"image": img(1), "prompt": PROMPT, "mask": img(2, role="mask")}, {})
    assert adapter.quote({"image": img(1), "prompt": PROMPT}, {}).custody_grade == "D"


def test_bfl_ignoring_vendor_is_caught_when_the_result_is_fetched(manifests, stage_dir):
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"id": f"job-{len(posts)}"})
        if request.url.path == "/v1/get_result":
            return httpx.Response(
                200, json={"status": "Ready", "result": {"sample": "https://delivery-eu.bfl.ai/same.png"}}
            )
        return httpx.Response(200, content=out_png(9))

    adapter = bfl(manifests, httpx.MockTransport(handler), stage_dir)
    first = adapter.submit(staged(adapter, bfl_inputs(1)))
    adapter.fetch(adapter.poll(first).outputs[0])
    second = adapter.submit(staged(adapter, bfl_inputs(2)))
    with pytest.raises(SilentlyIgnoredInput):
        adapter.fetch(adapter.poll(second).outputs[0])


# =======================================================================================================
# local Qwen-Image-Edit (stub)
# =======================================================================================================


@pytest.fixture
def no_gpu_libs(monkeypatch):
    for name in ("torch", "diffusers", "realesrgan", "basicsr"):
        monkeypatch.setitem(sys.modules, name, None)  # `import name` raises ImportError


def qwen(manifests, stage_dir) -> QwenImageEdit:
    return QwenImageEdit(
        manifests["qwen-image-edit-2509"], None, None, staging_dir=stage_dir
    )  # no transport needed


def test_qwen_refuses_to_run_without_the_gpu_stack_or_the_stub_flag(manifests, stage_dir, no_gpu_libs):
    adapter = qwen(manifests, stage_dir)
    with pytest.raises(LocalModelUnavailable, match="PLATE_STUB_LOCAL"):
        adapter.submit(staged(adapter, {"image": img(1), "prompt": PROMPT}))


def test_qwen_stub_is_deterministic_and_depends_on_the_input(manifests, stage_dir, monkeypatch):
    monkeypatch.setenv("PLATE_STUB_LOCAL", "1")
    a1, a2 = qwen(manifests, stage_dir), qwen(manifests, stage_dir / "second")
    ja = a1.submit(staged(a1, {"image": img(1), "prompt": PROMPT}))
    out1 = result(a1, ja)
    out2 = result(a2, a2.submit(staged(a2, {"image": img(1), "prompt": PROMPT})))
    assert out1 == out2  # same input, same stub output on any instance
    out_b = result(a1, a1.submit(staged(a1, {"image": img(2), "prompt": PROMPT})))
    assert out_b != out1
    assert png_size(out1) == (64, 36)
    assert out1 != synth_png(1)  # it is an edit of the input, not the input
    status = a1.poll(ja)
    assert status.usage == {"stub": True} and "stub" in status.message


def test_qwen_stub_reacts_to_parameters_and_accepts_depth_and_edges(manifests, stage_dir, monkeypatch):
    monkeypatch.setenv("PLATE_STUB_LOCAL", "1")
    adapter = qwen(manifests, stage_dir)
    base = {
        "image": img(1),
        "prompt": PROMPT,
        "depth": img(8, role="gbuffer"),
        "edges": img(9, role="gbuffer"),
    }
    a = result(adapter, adapter.submit(staged(adapter, base, {"seed": 1})))
    b = result(adapter, adapter.submit(staged(adapter, base, {"seed": 2})))
    assert a != b
    with pytest.raises(InputError, match="mask"):
        adapter.quote({**base, "mask": img(3, role="mask")}, {})


def test_qwen_is_local_c0_free_and_needs_no_transport(manifests, stage_dir):
    adapter = qwen(manifests, stage_dir)
    q = adapter.quote({"image": img(1), "prompt": PROMPT}, {})
    assert q.custody_grade == "C0" and q.price_usd == 0 and q.price_status == "confirmed"
    with pytest.raises(MissingTransport):
        adapter.http()  # a local route makes no HTTP calls at all


# =======================================================================================================
# upscalers: Magnific Precision v2 and Real-ESRGAN
# =======================================================================================================


def test_magnific_request_is_the_documented_json(manifests, magnific_replay, stage_dir):
    adapter = magnific(manifests, magnific_replay, stage_dir)
    job = adapter.submit(staged(adapter, mag_inputs(1), {"scale": 2, "sharpen": 10}))
    assert job == "mag-a"
    call = magnific_replay.served[0]
    assert (call.method, call.host, call.path) == (
        "POST",
        "api.freepik.com",
        "/v1/ai/image-upscaler/precision",
    )
    assert call.headers["x-freepik-api-key"] == KEYS["MAGNIFIC_API_KEY"]
    assert json.loads(call.body) == {"image": b64(synth_png(1)), "scale_factor": 2, "sharpen": 10}


def test_magnific_poll_lifecycle_and_output_depends_on_input(manifests, magnific_replay, stage_dir):
    adapter = magnific(manifests, magnific_replay, stage_dir)
    outputs = {}
    for tag, seed in (("a", 1), ("b", 2)):
        job = adapter.submit(staged(adapter, mag_inputs(seed), {"scale": 2, "sharpen": 10}))
        states = [adapter.poll(job).state for _ in range(3)]
        assert states == ["queued", "running", "done"]
        outputs[tag] = adapter.fetch(adapter.poll(job).outputs[0])
    assert outputs == {"a": out_png(1), "b": out_png(2)}


def test_magnific_failed_task_and_refused_submit(manifests, magnific_replay, stage_dir):
    adapter = magnific(manifests, magnific_replay, stage_dir)
    assert adapter.poll(adapter.submit(staged(adapter, mag_inputs(3)))).state == "failed"
    blocked = adapter.poll(adapter.submit(staged(adapter, mag_inputs(4))))
    assert blocked.state == "refused" and "safety" in blocked.message


def test_magnific_never_upscales_the_plate(manifests, stage_dir):
    def boom(request):
        raise AssertionError("a plate must never reach the vendor")

    adapter = magnific(manifests, httpx.MockTransport(boom), stage_dir)
    with pytest.raises(PlateUpscaleForbidden, match="plate"):
        adapter.quote(mag_inputs(1, role="plate"), {})
    with pytest.raises(PlateUpscaleForbidden):
        adapter.submit(staged(adapter, mag_inputs(1, role="plate")))


def test_magnific_quote_size_is_the_upscaled_size_and_scale_is_bounded(manifests, stage_dir):
    adapter = magnific(manifests, httpx.MockTransport(lambda r: httpx.Response(500)), stage_dir)
    q = adapter.quote(mag_inputs(1), {"scale": 4})
    assert (q.size.w, q.size.h) == (256, 144) and q.price_usd == pytest.approx(0.32)
    assert q.custody_grade == "D" and q.price_status == "unconfirmed"
    for bad in (1, 5):
        with pytest.raises(ParamError):
            adapter.quote(mag_inputs(1), {"scale": bad})
    with pytest.raises(InputError, match="image"):  # no text-to-image path
        adapter.quote({}, {})


def test_magnific_network_host_stays_inside_the_route_allowlist(manifests, stage_dir):
    adapter = magnific(manifests, httpx.MockTransport(lambda r: httpx.Response(500)), stage_dir)
    with pytest.raises(HostNotAllowed):
        adapter.fetch("https://cdn.example.net/up.png")


def test_realesrgan_stub_scales_and_never_touches_the_plate(manifests, stage_dir, monkeypatch, no_gpu_libs):
    adapter = RealESRGAN(manifests["real-esrgan"], None, None, staging_dir=stage_dir)
    with pytest.raises(LocalModelUnavailable, match="PLATE_STUB_LOCAL"):
        adapter.submit(staged(adapter, mag_inputs(1)))
    monkeypatch.setenv("PLATE_STUB_LOCAL", "1")
    job = adapter.submit(staged(adapter, mag_inputs(1), {"scale": 2}))
    out = result(adapter, job)
    assert png_size(out) == (128, 72)
    assert adapter.size_choice(job).size == EngineSize(w=128, h=72, label="2x")
    assert adapter.poll(job).usage == {"stub": True}
    assert result(adapter, adapter.submit(staged(adapter, mag_inputs(2), {"scale": 2}))) != out
    with pytest.raises(PlateUpscaleForbidden):
        adapter.submit(staged(adapter, mag_inputs(1, role="plate")))
    q = adapter.quote(mag_inputs(1), {})
    assert (q.size.w, q.size.h) == (256, 144) and q.price_usd == 0 and q.custody_grade == "C0"


def test_atlas_image_uploads_every_reference_and_keeps_the_order(manifests, stage_dir):
    uploads, bodies = [], []

    def handler(request):
        if request.url.path.endswith("/uploadMedia"):
            (_, filename, payload) = parse_multipart(request.headers["content-type"], request.content)[0]
            uploads.append((filename, payload))
            return httpx.Response(
                200, json={"data": {"download_url": f"https://m.oss.aliyuncs.com/{len(uploads)}.png"}}
            )
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"id": "pred-refs"}})

    adapter = atlas(manifests, httpx.MockTransport(handler), stage_dir)
    refs = [img(81, role="ai_layer"), img(82, role="ai_layer")]
    adapter.submit(staged(adapter, {**atlas_inputs(1), "reference_images": refs}))
    assert [p for _, p in uploads] == [synth_png(1), synth_png(81), synth_png(82)]
    assert bodies[0]["images"] == [f"https://m.oss.aliyuncs.com/{n}.png" for n in (1, 2, 3)]


def test_gemini_passes_the_whole_vendor_schema_through(manifests, stage_dir):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=gem_ok(9)["json"])

    flash = GeminiImage(
        _with_model(manifests["nano-banana-2.1"], "gemini-nb21-test"),
        None,
        httpx.MockTransport(handler),
        staging_dir=stage_dir,
    )
    flash.submit(
        staged(
            flash,
            {"image": img(1), "prompt": PROMPT},
            {
                "aspect_ratio": "21:9",
                "image_size": "2K",
                "temperature": 0.4,
                "thinking_level": "high",
                "system_prompt": "be exact",
            },
        )
    )
    body = sent[0]
    assert body["generationConfig"]["imageConfig"] == {"aspectRatio": "21:9", "imageSize": "2K"}
    assert body["generationConfig"]["temperature"] == 0.4
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}
    assert body["systemInstruction"] == {"parts": [{"text": "be exact"}]}
    with pytest.raises(ParamError):  # thinking_level exists on 2.1 only
        gemini(manifests, httpx.MockTransport(lambda r: httpx.Response(500)), stage_dir).validate_params(
            {"thinking_level": "high"}
        )


def test_gemini_candidate_count_gives_several_outputs(manifests, stage_dir):
    two = gem_ok(1)["json"]["candidates"] + gem_ok(2)["json"]["candidates"]
    adapter = gemini(
        manifests, httpx.MockTransport(lambda r: httpx.Response(200, json={"candidates": two})), stage_dir
    )
    status = adapter.poll(
        adapter.submit(staged(adapter, {"image": img(1), "prompt": PROMPT}, {"candidate_count": 2}))
    )
    assert status.state == "done" and len(status.outputs) == 2
    assert [adapter.fetch(u) for u in status.outputs] == [out_png(1), out_png(2)]


# ---- image inputs ------------------------------------------------------------------------------------------


def test_read_image_accepts_bytes_mapping_path_and_a_sizemap_object(tmp_path):
    from plate_engines.adapters.image_common import read_image

    png = synth_png(1)
    by_bytes = read_image(png, "image")
    assert (by_bytes.width, by_bytes.height, by_bytes.mime) == (64, 36, "image/png") and by_bytes.role is None
    path = tmp_path / "in.png"
    path.write_bytes(png)
    by_path = read_image({"path": path, "role": "ai_layer"}, "image")
    assert by_path.data == png and by_path.digest == by_bytes.digest and by_path.role == "ai_layer"

    class FakeSizeMap:
        def to_dict(self):
            return {"mode": "pad", "dst": [640, 360], "engine_size": {"width": 1536, "height": 1024}}

    mapped = read_image({"bytes": b"not a png at all", "sizemap": FakeSizeMap()}, "image")
    assert (mapped.width, mapped.height) == (640, 360) and mapped.sizemap[
        "mode"
    ] == "pad"  # dims from the SizeMap


def test_read_image_names_are_windows_safe_and_bad_inputs_are_refused():
    from plate_engines.adapters.image_common import read_image

    assert read_image({"bytes": synth_png(1), "name": 'a:b/c"d.png'}, "image").name == "a_b_c_d.png"
    assert ":" not in read_image(synth_png(1), "image").name
    for bad in (b"", {"role": "plate"}, 12, {"bytes": "text"}):
        with pytest.raises(InputError):
            read_image(bad, "image")


# =======================================================================================================
# every adapter
# =======================================================================================================

CLOUD = [
    pytest.param(GeminiImage, "nano-banana-pro", id="gemini"),
    pytest.param(OpenAIImages, "gpt-image-2.5", id="openai"),
    pytest.param(AtlasImage, "seedream-5-pro", id="atlas"),
    pytest.param(BFLImage, "flux-3-image", id="bfl"),
    pytest.param(MagnificPrecisionV2, "magnific-precision-v2", id="magnific"),
]
ALL_IMAGE_ENGINES = [
    "nano-banana-2.1",
    "nano-banana-pro",
    "gpt-image-2.5",
    "seedream-5-pro",
    "flux-3-image",
    "qwen-image-edit-2509",
    "magnific-precision-v2",
    "real-esrgan",
]


@pytest.mark.parametrize(("cls", "engine"), CLOUD)
def test_no_request_leaves_without_a_transport(manifests, cls, engine):
    with pytest.raises(MissingTransport):
        cls(manifests[engine])


@pytest.mark.parametrize(("cls", "engine"), CLOUD)
def test_a_missing_key_fails_before_anything_is_sent(manifests, stage_dir, monkeypatch, cls, engine):
    for name in KEYS:
        monkeypatch.delenv(name)

    def boom(request):
        raise AssertionError("nothing may be sent without a key")

    adapter = cls(
        _with_model(manifests[engine], "slug-for-the-test"),
        None,
        httpx.MockTransport(boom),
        staging_dir=stage_dir,
    )
    inputs = (
        {"image": img(1, role="ai_layer")}
        if engine == "magnific-precision-v2"
        else {"image": img(1), "prompt": PROMPT}
    )
    with pytest.raises((MissingKey, AtlasError)):
        adapter.submit(staged(adapter, inputs))


@pytest.mark.parametrize("engine", ALL_IMAGE_ENGINES)
def test_there_is_no_text_to_image_path(manifests, engine):
    m = manifests[engine]
    assert any(f.name == "image" and f.required for f in m.inputs)
    assert "text_to_image" not in m.allowed_tasks


@pytest.mark.parametrize(
    ("cls", "engine", "inputs"),
    [
        (GeminiImage, "nano-banana-pro", {"prompt": PROMPT}),
        (OpenAIImages, "gpt-image-2.5", {"prompt": PROMPT}),
        (AtlasImage, "seedream-5-pro", {"prompt": PROMPT}),
        (BFLImage, "flux-3-image", {"prompt": PROMPT}),
    ],
    ids=["gemini", "openai", "atlas", "bfl"],
)
def test_a_prompt_alone_is_refused_by_every_generator(manifests, stage_dir, cls, engine, inputs):
    adapter = cls(
        manifests[engine], None, httpx.MockTransport(lambda r: httpx.Response(500)), staging_dir=stage_dir
    )
    with pytest.raises(InputError, match="image"):
        adapter.quote(inputs, {})


def test_an_adapter_serves_only_its_own_engines(manifests):
    ok = httpx.MockTransport(lambda r: httpx.Response(500))
    with pytest.raises(ValueError, match="serves"):
        GeminiImage(manifests["gpt-image-2.5"], None, ok)
    with pytest.raises(ValueError, match="provider"):
        GeminiImage(
            manifests["nano-banana-2.1"], "atlas", ok
        )  # the atlas route of NB 2.1 is not Gemini's API


def test_an_empty_prompt_is_refused(manifests, stage_dir):
    adapter = gemini(manifests, httpx.MockTransport(lambda r: httpx.Response(500)), stage_dir)
    with pytest.raises(InputError, match="prompt"):
        adapter.quote({"image": img(1), "prompt": "   "}, {})


def test_the_ledger_is_shared_when_given_so_the_nightly_job_can_compare_runs(manifests, stage_dir):
    from plate_engines.adapters.image_common import OutputLedger

    ledger = OutputLedger()
    deaf = httpx.MockTransport(lambda r: httpx.Response(200, json=gem_ok(9)["json"]))
    one = gemini(manifests, deaf, stage_dir / "one", ledger=ledger)
    two = gemini(manifests, deaf, stage_dir / "two", ledger=ledger)
    one.submit(staged(one, {"image": img(1), "prompt": PROMPT}))
    with pytest.raises(SilentlyIgnoredInput):
        two.submit(staged(two, {"image": img(2), "prompt": PROMPT}))
