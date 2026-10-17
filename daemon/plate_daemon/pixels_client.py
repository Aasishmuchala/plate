"""The daemon's client for the pixels worker (segmentation, text boxes, relative depth).

Two ways to reach the models, chosen per call from the environment:

- HTTP (default): JSON to the worker on `127.0.0.1:<PLATE_PIXELS_PORT or 4611>` with the daemon's own bearer
  token (the worker reads the same `config.json`). `urllib.request` on the literal loopback address, through
  an opener with no proxy handler so an `HTTP_PROXY` in the environment never sees a plate path. A worker that
  is not running raises `PixelsUnavailable` naming `python -m plate_pixels` and `PLATE_PIXELS_INPROC=1`; it is
  never a hang (60 s timeout, and a refused socket answers at once).
- In-process (`PLATE_PIXELS_INPROC=1`): the `plate_pixels.backends` classes are called directly (stubs unless
  `PLATE_PIXELS_REAL=1`), no worker needed. Used by the tests and by a one-box setup.

File PATHS go in and file PATHS come out, as on the worker: `segment` returns the label PNG's path under
`<home>/cache/pixels/`, `ocr` the boxes, `depth` the array (and the EXR path when a worker wrote one). Failures
are normalised so a caller has one mapping: `PixelsUnavailable` (nothing to talk to), `PixelsError(status,
detail)` carrying the worker's own status codes (400 bad input, 401/403 token, 500 a real model missing,
501 not wired, 503 GPU refused, 504 timeout), in-process or not.

`ocr_adapter()` and `depth_fn()` are what `Pipeline(ocr=..., depth_fn=...)` takes: an object with
`.detect(rgb8)` and a callable on `rgb8`, both with a `.name`. They hold no connection, so building them is
free and a missing worker only shows on the first call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

from .config import Settings
from .fsutil import write_bytes_atomic

__all__ = [
    "DEFAULT_PORT",
    "INPROC_ENV",
    "MAX_POINTS",
    "PORT_ENV",
    "TIMEOUT_S",
    "DepthFn",
    "OcrAdapter",
    "PixelsClient",
    "PixelsError",
    "PixelsUnavailable",
]

log = logging.getLogger("plate_daemon.pixels")

INPROC_ENV = "PLATE_PIXELS_INPROC"
PORT_ENV = "PLATE_PIXELS_PORT"
DEFAULT_PORT = 4611  # plate_pixels.server.PORT; not imported so the daemon does not load the worker's app
TIMEOUT_S = 60.0
HEALTH_TIMEOUT_S = 2.0
MAX_POINTS = 64

# No proxy handler at all: the default opener would route a loopback URL through HTTP_PROXY.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class PixelsUnavailable(RuntimeError):
    """There is no worker to talk to (socket refused) and in-process mode is off."""


class PixelsError(RuntimeError):
    """The worker (or the in-process backend) answered with a failure, in the worker's own status codes."""

    def __init__(self, status: int, detail: str, body: dict[str, Any] | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.body = body or {}

    @property
    def model_unavailable(self) -> bool:
        """A real model is missing (the worker's 500 for `ModelUnavailable` carries `real_required`)."""
        return self.status == 500 and "real_required" in self.body


def _failure(status: int, body: Any) -> PixelsError:
    detail = body.get("error") if isinstance(body, dict) else None
    detail = detail if isinstance(detail, str) and detail else f"the pixels worker answered {status}"
    return PixelsError(status, detail, body if isinstance(body, dict) else None)


# ---------------------------------------------------------------------------- the client


class PixelsClient:
    """`PixelsClient(settings)`: segmentation, OCR and depth from the pixels worker, by path."""

    def __init__(
        self,
        settings: Settings,
        *,
        inproc: bool | None = None,
        port: int | None = None,
        backends: Any = None,
        timeout_s: float = TIMEOUT_S,
    ):
        self.settings = settings
        self._inproc = inproc
        self._port = port
        self._backends = backends
        self.timeout_s = timeout_s
        self._lock = threading.RLock()  # in-process models are not re-entrant: one call at a time

    # ------------------------------------------------------------------ how to reach the models

    @property
    def inproc(self) -> bool:
        if self._inproc is not None:
            return self._inproc
        return os.environ.get(INPROC_ENV) == "1"

    @property
    def port(self) -> int:
        if self._port is not None:
            return self._port
        return int(os.environ.get(PORT_ENV) or DEFAULT_PORT)

    @property
    def token(self) -> str:
        return self.settings.token

    @property
    def cache_dir(self) -> Path:
        return self.settings.home / "cache" / "pixels"

    @property
    def backends(self) -> Any:
        """The in-process `plate_pixels.backends.Backends` (built on first use; shared with the Tier C route)."""
        if self._backends is None:
            from plate_pixels.backends import Backends

            self._backends = Backends()
        return self._backends

    def status(self) -> str:
        """`inproc`, `http` (a worker answered `/health`) or `unavailable`."""
        if self.inproc:
            return "inproc"
        return "http" if self.available() else "unavailable"

    def available(self) -> bool:
        """Can a call be answered right now? Always for in-process. Over HTTP: a worker is listening AND takes
        our token. The probe is an `/ocr` request with an empty image name: the worker's guard answers 401/403
        for a wrong token or Host before it validates anything, and 400 for the empty name once it let us in."""
        if self.inproc:
            return True
        try:
            self._call("/ocr", {"image": ""}, timeout_s=HEALTH_TIMEOUT_S)
        except PixelsUnavailable:
            return False
        except PixelsError as e:
            return e.status == 400
        return True

    # ------------------------------------------------------------------ HTTP

    def _call(
        self, path: str, payload: dict[str, Any] | None, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        port = self.port
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        try:
            with _OPENER.open(request, timeout=timeout_s or self.timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8", "replace"))
            except ValueError:
                body = None
            raise _failure(e.code, body) from e
        except TimeoutError as e:
            raise PixelsError(
                504, f"the pixels worker did not answer {path} within {self.timeout_s:.0f} s"
            ) from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise PixelsError(504, f"the pixels worker did not answer {path} in time") from e
            raise self._unavailable(port, e.reason) from e
        except OSError as e:  # ConnectionResetError and friends
            raise self._unavailable(port, e) from e
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise PixelsError(502, f"the pixels worker's answer to {path} is not JSON") from e
        if not isinstance(body, dict):
            raise PixelsError(502, f"the pixels worker's answer to {path} is not a JSON object")
        return body

    @staticmethod
    def _unavailable(port: int, reason: object) -> PixelsUnavailable:
        return PixelsUnavailable(
            f"the pixels worker is not reachable on 127.0.0.1:{port} ({reason}); start it with "
            f"`python -m plate_pixels` or set {INPROC_ENV}=1"
        )

    # ------------------------------------------------------------------ in-process

    def _inproc_call(self, work: Any) -> Any:
        """Run `work()` and say what went wrong in the worker's status codes."""
        from plate_pixels.backends import InputError, ModelUnavailable

        try:
            with self._lock:
                return work()
        except InputError as e:
            raise PixelsError(400, str(e)) from e
        except ModelUnavailable as e:
            raise PixelsError(
                500, str(e), {"real_required": os.environ.get("PLATE_PIXELS_REAL") == "1"}
            ) from e
        except NotImplementedError as e:
            raise PixelsError(501, str(e)) from e
        except ValueError as e:
            raise PixelsError(400, str(e)) from e

    def _cache_name(self, kind: str, source: Path, extra: str, ext: str) -> Path:
        st = source.stat()
        h = hashlib.sha256(
            f"{kind}|{source.resolve()}|{st.st_size}|{st.st_mtime_ns}|{extra}".encode()
        ).hexdigest()
        return self.cache_dir / f"{kind}-{h[:16]}{ext}"

    # ------------------------------------------------------------------ the three calls

    def segment(self, image_path: str | Path, points: list[list[float]] | None = None) -> dict[str, Any]:
        """Label map of the image: `{labels_png_path, segments: [{id, bbox, area}], shape: [h, w], stub, model}`.

        `points` are `[x, y]` prompts (a real SAM returns one mask per point); None or empty segments the whole
        image. The label PNG is 16-bit grey, 0 = no segment."""
        pts = [[float(x), float(y)] for x, y in (points or [])]
        if len(pts) > MAX_POINTS:
            raise PixelsError(400, f"at most {MAX_POINTS} points per request, got {len(pts)}")
        image = Path(image_path)
        if not self.inproc:
            body = self._call("/segment", {"image": str(image), **({"points": pts} if pts else {})})
            return {
                "labels_png_path": body["segments_png"],
                "segments": body.get("segments", []),
                "shape": body["shape"],
                "stub": bool(body.get("stub")),
                "model": body.get("model"),
            }

        def work() -> dict[str, Any]:
            from plate_pixels.backends import read_rgb
            from plate_pixels.tierc import write_label_png

            backend = self.backends.segment
            rgb = read_rgb(image)
            labels, found = backend.segment(rgb, pts or None)
            target = self._cache_name("segments", image, json.dumps(pts), ".png")
            write_label_png(target, labels)
            return {
                "labels_png_path": str(target),
                "segments": found,
                "shape": list(labels.shape),
                "stub": bool(backend.is_stub),
                "model": backend.model_id,
            }

        return self._inproc_call(work)

    def ocr(self, image_path: str | Path) -> dict[str, Any]:
        """Text boxes in the image: `{boxes: [{bbox: [x0, y0, x1, y1], text, conf}], shape: [h, w], stub, model}`."""
        image = Path(image_path)
        if not self.inproc:
            body = self._call("/ocr", {"image": str(image)})
            return {
                "boxes": body.get("boxes", []),
                "shape": body["shape"],
                "stub": bool(body.get("stub")),
                "model": body.get("model"),
            }

        def work() -> dict[str, Any]:
            from plate_pixels.backends import image_shape

            backend = self.backends.ocr
            height, width = image_shape(str(image))
            return {
                "boxes": backend.detect_file(str(image)),
                "shape": [height, width],
                "stub": bool(backend.is_stub),
                "model": backend.model_id,
            }

        return self._inproc_call(work)

    def depth(self, image_path: str | Path, *, load: bool = True) -> dict[str, Any]:
        """Relative depth of the image (never metric): `{depth: float32 (h, w) | None, depth_path: str | None,
        shape, depth_semantics, stub, model}`. In-process there is no file (`depth_path` None); over HTTP the
        worker writes an EXR and `depth` is read back from it unless `load` is False."""
        image = Path(image_path)
        if not self.inproc:
            body = self._call("/depth", {"image": str(image)})
            path = body["depth_exr"]
            return {
                "depth": self._read_depth(path) if load else None,
                "depth_path": path,
                "shape": body["shape"],
                "depth_semantics": body.get("depth_semantics"),
                "stub": bool(body.get("stub")),
                "model": body.get("model"),
            }

        def work() -> dict[str, Any]:
            from plate_pixels.backends import read_rgb

            backend = self.backends.depth
            z = np.ascontiguousarray(backend.infer(read_rgb(image)), dtype=np.float32)
            return {
                "depth": z if load else None,
                "depth_path": None,
                "shape": list(z.shape),
                "depth_semantics": backend.depth_semantics,
                "stub": bool(backend.is_stub),
                "model": backend.model_id,
            }

        return self._inproc_call(work)

    @staticmethod
    def _read_depth(path: str) -> np.ndarray:
        from plate_pixels import exr

        manifest = {
            "files": {"beauty_exr": {"elements": [{"element": "depth", "layer": "", "channels": ["Z"]}]}}
        }
        try:
            return np.ascontiguousarray(
                exr.read_elements(path, manifest, ["depth"])["depth"][..., 0], np.float32
            )
        except (OSError, exr.MissingChannel, ValueError) as e:
            raise PixelsError(502, f"cannot read the worker's depth file {path!r}: {e}") from e

    # ------------------------------------------------------------------ pixels in memory (the gates' side)

    def _array_png(self, rgb8: np.ndarray, kind: str) -> Path:
        """Write a uint8 RGB array as a PNG the worker can read; the caller removes it."""
        import cv2

        ok, buf = cv2.imencode(".png", np.ascontiguousarray(rgb8[..., ::-1]))
        if not ok:
            raise PixelsError(400, "cv2 could not encode the array as PNG")
        path = self.cache_dir / f"{kind}-in-{secrets.token_hex(6)}.png"
        write_bytes_atomic(path, bytes(buf))
        return path

    def ocr_adapter(self, *, strict: bool = False) -> OcrAdapter:
        """What `Pipeline(ocr=...)` takes. With a worker that is not running, `detect` logs one warning and finds
        nothing unless `strict`, in which case it raises `PixelsUnavailable`."""
        return OcrAdapter(self, strict=strict)

    def depth_fn(self) -> DepthFn:
        """What `Pipeline(depth_fn=...)` takes: `rgb8 -> float32 (h, w)` relative depth. Raises
        `PixelsUnavailable` when there is no worker; check `available()` first to leave the gate's depth
        agreement out instead."""
        return DepthFn(self)


def _check_rgb8(rgb8: np.ndarray) -> np.ndarray:
    a = np.asarray(rgb8)
    if a.dtype != np.uint8 or a.ndim != 3 or a.shape[2] != 3:
        raise ValueError(f"expected a uint8 RGB array of shape (h, w, 3), got {a.dtype} {a.shape}")
    return a


class OcrAdapter:
    """`plate_pixels.ocr.OcrBackend`'s `detect(rgb8)` over the pixels client."""

    def __init__(self, client: PixelsClient, *, strict: bool = False):
        self.client = client
        self.strict = strict
        self.unavailable = False  # set once a call found no worker; a later success clears it
        self._warned = False

    @property
    def name(self) -> str:
        return f"pixels-{'inproc' if self.client.inproc else 'http'}"

    def detect(self, rgb8: np.ndarray) -> list[dict[str, Any]]:
        """`[{bbox: (x0, y0, x1, y1), text, conf}]` for the text found in a uint8 RGB array."""
        from plate_pixels.ocr import normalise_box

        a = _check_rgb8(rgb8)
        try:
            if self.client.inproc:
                boxes = self.client._inproc_call(
                    lambda: self.client.backends.ocr.detect(a.astype(np.float32) / 255.0)
                )
            else:
                path = self.client._array_png(a, "ocr")
                try:
                    boxes = self.client.ocr(path)["boxes"]
                finally:
                    path.unlink(missing_ok=True)
        except PixelsUnavailable as e:
            if self.strict:
                raise
            self.unavailable = True
            if not self._warned:
                self._warned = True
                log.warning("OCR text boxes skipped, so the text-lock has no OCR boxes: %s", e)
            return []
        self.unavailable = False
        return [normalise_box(b) for b in boxes]


class DepthFn:
    """A callable `rgb8 -> float32 (h, w)` relative depth, computed by the pixels worker."""

    def __init__(self, client: PixelsClient):
        self.client = client

    @property
    def name(self) -> str:
        return f"pixels-{'inproc' if self.client.inproc else 'http'}-depth"

    def __call__(self, rgb8: np.ndarray) -> np.ndarray:
        a = _check_rgb8(rgb8)
        if self.client.inproc:
            return np.ascontiguousarray(
                self.client._inproc_call(
                    lambda: self.client.backends.depth.infer(a.astype(np.float32) / 255.0)
                ),
                dtype=np.float32,
            )
        path = self.client._array_png(a, "depth")
        result: dict[str, Any] = {}
        try:
            result = self.client.depth(path)
            return result["depth"]
        finally:  # both files are private to this call: the PNG we wrote and the EXR the worker answered with
            path.unlink(missing_ok=True)
            if result.get("depth_path"):
                Path(result["depth_path"]).unlink(missing_ok=True)
