"""GPU admission: may this model load now, and how much VRAM is left for it?

The render box shares one RTX 5090 with Max, V-Ray and Vantage. A model that does not fit must be refused
before ``load()``, not discovered as an out-of-memory crash in the middle of a render. ``Admission`` asks NVML
for the free memory (``pynvml``, installed only with the ``gpu`` extra and imported lazily; on the Mac it is
absent) and applies two more rules:

- With no NVML the headroom cannot be measured, so admission is refused unless ``PLATE_ALLOW_UNMANAGED_GPU=1``
  says the operator accepts an unmanaged GPU.
- The heavy generative models (Qwen-Image-Edit, Wan 2.2) only run inside a scheduled window (``Window``,
  from ``PLATE_GPU_WINDOWS`` such as ``01:00-06:00,13:00-14:00``) or with ``PLATE_FORCE_LOCAL=1``.

``ModelSlot`` is the context manager that holds a model on the GPU: ``load()`` on enter, ``unload()`` and a
CUDA cache flush on exit. It never imports torch itself; it flushes only when a backend already loaded it.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .licences import canonical
from .licences import get as licence_get

__all__ = [
    "ALLOW_UNMANAGED_ENV",
    "FORCE_LOCAL_ENV",
    "WINDOWED_MODELS",
    "WINDOWS_ENV",
    "Admission",
    "AdmissionDenied",
    "ModelSlot",
    "Window",
    "parse_hhmm",
    "parse_windows",
]

ALLOW_UNMANAGED_ENV = "PLATE_ALLOW_UNMANAGED_GPU"
FORCE_LOCAL_ENV = "PLATE_FORCE_LOCAL"
WINDOWS_ENV = "PLATE_GPU_WINDOWS"
DEFAULT_RESERVE_MB = 2048
WINDOWED_MODELS = frozenset({"qwen-image-edit-2509", "wan2.2"})
_HHMM = re.compile(r"^([01]?\d|2[0-3]):?([0-5]\d)$")
_MB = 1024 * 1024


class AdmissionDenied(RuntimeError):
    """The GPU cannot take this model now (HTTP 503 from the pixels worker)."""


def parse_hhmm(value: str) -> int:
    """``'22:00'`` or ``'2200'`` -> minutes since midnight."""
    match = _HHMM.match(str(value).strip())
    if not match:
        raise ValueError(f"invalid time {value!r}: expected HH:MM or HHMM")
    return int(match.group(1)) * 60 + int(match.group(2))


@dataclass(frozen=True)
class Window:
    """A daily GPU window, ``start_hhmm`` inclusive to ``end_hhmm`` exclusive; start > end wraps midnight."""

    start_hhmm: str
    end_hhmm: str

    def contains(self, minute_of_day: int) -> bool:
        start, end = parse_hhmm(self.start_hhmm), parse_hhmm(self.end_hhmm)
        if start == end:
            return False
        if start < end:
            return start <= minute_of_day < end
        return minute_of_day >= start or minute_of_day < end

    def __str__(self) -> str:
        return f"{self.start_hhmm}-{self.end_hhmm}"


def parse_windows(spec: str | Iterable[Any] | None) -> list[Window]:
    """Windows from ``'01:00-06:00,13:00-14:00'`` or an iterable of ``Window``, ``(start, end)`` or
    ``{'start_hhmm', 'end_hhmm'}`` items (the settings file shape). Invalid entries raise ``ValueError``."""
    if not spec:
        return []
    if isinstance(spec, str):
        items: list[Any] = [part.strip() for part in spec.split(",") if part.strip()]
    else:
        items = list(spec)
    out = []
    for item in items:
        if isinstance(item, Window):
            win = item
        elif isinstance(item, str):
            start, sep, end = item.partition("-")
            if not sep:
                raise ValueError(f"invalid window {item!r}: expected HH:MM-HH:MM")
            win = Window(start.strip(), end.strip())
        elif isinstance(item, Mapping):
            win = Window(str(item["start_hhmm"]), str(item["end_hhmm"]))
        else:
            win = Window(str(item[0]), str(item[1]))
        parse_hhmm(win.start_hhmm)
        parse_hhmm(win.end_hhmm)
        out.append(win)
    return out


def _registry_name(model: str) -> str:
    """The registry name behind a spelling (``wan2.2_vace`` -> ``wan2.2``); an unknown name is just canonicalised."""
    entry = licence_get(model)
    return entry.name if entry is not None else canonical(model)


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip() == "1"


class Admission:
    """Decides whether a model of a given size may load now. ``nvml`` injects a pynvml-shaped module (tests)."""

    def __init__(
        self,
        windows: Iterable[Any] | str | None = None,
        nvml: Any | None = None,
        env: Mapping[str, str] | None = None,
        device_index: int = 0,
    ) -> None:
        self.env = os.environ if env is None else env
        self.windows = parse_windows(windows if windows is not None else self.env.get(WINDOWS_ENV))
        self.device_index = device_index
        self._nvml = nvml
        self._nvml_probed = nvml is not None

    # -- NVML ----------------------------------------------------------------

    def _module(self) -> Any | None:
        """The pynvml module once initialised, else ``None`` (not installed, no driver, no GPU)."""
        if not self._nvml_probed:
            self._nvml_probed = True
            try:
                module = importlib.import_module("pynvml")
                module.nvmlInit()
            except Exception:  # noqa: BLE001 - ImportError, or NVMLError when there is no NVIDIA driver
                module = None
            self._nvml = module
        return self._nvml

    def headroom_mb(self) -> int | None:
        """Free VRAM in MiB on the device, or ``None`` when NVML is unavailable (the Mac)."""
        module = self._module()
        if module is None:
            return None
        try:
            handle = module.nvmlDeviceGetHandleByIndex(self.device_index)
            return int(module.nvmlDeviceGetMemoryInfo(handle).free) // _MB
        except Exception:  # noqa: BLE001 - a driver reset mid-query must read as "unmeasurable", not crash
            return None

    # -- windows -------------------------------------------------------------

    def in_window(self, now: datetime | None = None) -> bool:
        """True when ``now`` (default: local time) is inside any scheduled window. No windows -> False."""
        now = now or datetime.now()
        minute = now.hour * 60 + now.minute
        return any(win.contains(minute) for win in self.windows)

    # -- the decision ----------------------------------------------------------

    def admit(
        self,
        required_mb: int,
        reserve_mb: int = DEFAULT_RESERVE_MB,
        model: str | None = None,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """``(ok, reason)``: may a model needing ``required_mb`` load while leaving ``reserve_mb`` free?"""
        if model is not None and _registry_name(model) in WINDOWED_MODELS:
            if not self.in_window(now) and not _env_flag(self.env, FORCE_LOCAL_ENV):
                scheduled = ", ".join(str(w) for w in self.windows) or "none configured"
                return False, (
                    f"{model} runs only inside a scheduled GPU window ({scheduled}); "
                    f"set {FORCE_LOCAL_ENV}=1 to run it now"
                )
        free = self.headroom_mb()
        if free is None:
            if _env_flag(self.env, ALLOW_UNMANAGED_ENV):
                return True, "no nvml: unmanaged"
            return False, (
                f"no nvml: GPU headroom cannot be measured; set {ALLOW_UNMANAGED_ENV}=1 to run unmanaged"
            )
        need = int(required_mb) + int(reserve_mb)
        if free >= need:
            return True, f"ok: {free} MiB free, {required_mb} MiB needed plus {reserve_mb} MiB reserve"
        return (
            False,
            f"insufficient VRAM: {free} MiB free, {required_mb} MiB needed plus {reserve_mb} MiB reserve",
        )

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """What ``/admission`` reports besides the verdict."""
        return {
            "headroom_mb": self.headroom_mb(),
            "nvml": self._module() is not None,
            "in_window": self.in_window(now),
            "windows": [{"start_hhmm": w.start_hhmm, "end_hhmm": w.end_hhmm} for w in self.windows],
            "force_local": _env_flag(self.env, FORCE_LOCAL_ENV),
            "unmanaged_allowed": _env_flag(self.env, ALLOW_UNMANAGED_ENV),
        }


class ModelSlot:
    """Hold one backend on the GPU: ``backend.load()`` on enter, ``backend.unload()`` and a cache flush on exit.

    With an ``admission`` the slot first asks ``admit(required_mb, reserve_mb, model)`` and raises
    ``AdmissionDenied`` (before anything is loaded) when it says no.
    """

    def __init__(
        self,
        backend: Any,
        admission: Admission | None = None,
        required_mb: int = 0,
        reserve_mb: int = DEFAULT_RESERVE_MB,
        model: str | None = None,
    ) -> None:
        self.backend = backend
        self.admission = admission
        self.required_mb = required_mb
        self.reserve_mb = reserve_mb
        self.model = model or getattr(backend, "model_id", None)

    def __enter__(self) -> Any:
        if self.admission is not None:
            ok, reason = self.admission.admit(self.required_mb, self.reserve_mb, self.model)
            if not ok:
                raise AdmissionDenied(reason)
        self.backend.load()
        return self.backend

    def __exit__(self, *exc_info: object) -> None:
        try:
            self.backend.unload()
        finally:
            self.empty_cache()

    @staticmethod
    def empty_cache() -> None:
        """``torch.cuda.empty_cache()`` when a backend already imported torch; never imports it here."""
        torch = sys.modules.get("torch")
        cuda = getattr(torch, "cuda", None)
        if cuda is not None and cuda.is_available():
            cuda.empty_cache()
