"""Optical flow backends: DIS on every machine, RAFT on the render box.

Decision 3 of the build plan: RAFT at 960 px wide is the measurement of record for the reprojection-drift and
camera-lock gates and needs CUDA; ``DisFlow`` (OpenCV's Dense Inverse Search, no weights) is the fallback
that runs on the Mac and in CI, and every report states which backend produced its numbers. Both evaluate at
no more than ``EVAL_WIDTH`` pixels wide and return the flow in pixels of the INPUT size, ``b(x + u, y + v)``
matching ``a(x, y)``. The input is never upscaled.
"""

from __future__ import annotations

import importlib
from typing import Any

import cv2
import numpy as np

from .backends import FlowBackend, InputError, RealBackend, _device, _u8, models_dir

EVAL_WIDTH = 960


def eval_size(width: int, height: int, multiple: int = 1) -> tuple[int, int]:
    """(w, h) to evaluate at: at most ``EVAL_WIDTH`` wide, aspect kept, each side a multiple of ``multiple``."""
    scale = min(1.0, EVAL_WIDTH / width)
    ew = max(multiple, int(round(width * scale / multiple)) * multiple)
    eh = max(multiple, int(round(height * scale / multiple)) * multiple)
    return ew, eh


def check_pair(a: np.ndarray, b: np.ndarray) -> None:
    if a.ndim != 3 or a.shape[2] != 3 or a.shape != b.shape:
        raise InputError(f"flow needs two RGB images of one size, got {a.shape} and {b.shape}")


def _resize(rgb: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    if (rgb.shape[1], rgb.shape[0]) == size:
        return rgb
    return cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)


def _to_input_size(flow_hw2: np.ndarray, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    """Flow computed at (eh, ew) -> (u, v) at (height, width), vectors rescaled to input pixels."""
    eh, ew = flow_hw2.shape[:2]
    if (ew, eh) != (width, height):
        flow_hw2 = cv2.resize(flow_hw2, (width, height), interpolation=cv2.INTER_LINEAR)
    u = np.ascontiguousarray(flow_hw2[..., 0] * (width / ew), dtype=np.float32)
    v = np.ascontiguousarray(flow_hw2[..., 1] * (height / eh), dtype=np.float32)
    return u, v


class DisFlow(FlowBackend):
    """OpenCV DIS (medium preset) on greyscale at <= 960 px wide. Real on every machine; no weights."""

    model_id = "opencv-dis"
    needs_weights = False
    is_stub = False
    impl = "dis"

    def flow(self, a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        check_pair(a, b)
        height, width = a.shape[:2]
        size = eval_size(width, height)
        grey = [cv2.cvtColor(_u8(_resize(img, size)), cv2.COLOR_RGB2GRAY) for img in (a, b)]
        dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        return _to_input_size(dis.calc(grey[0], grey[1], None), width, height)


class RaftFlow(RealBackend, FlowBackend):
    """torchvision RAFT-large at <= 960 px wide (sides rounded to a multiple of 8). Render box, CUDA."""

    model_id = "raft"
    packages = ("torch", "torchvision")
    vram_mb = 2500
    WEIGHTS = "raft_large.pth"
    _model: Any = None

    def load(self) -> None:
        if self._model is not None:
            return
        torch = importlib.import_module("torch")
        optical_flow = importlib.import_module("torchvision.models.optical_flow")
        self._device = _device(torch)
        model = optical_flow.raft_large(weights=None, progress=False)
        state = torch.load(
            models_dir() / self.model_id / self.WEIGHTS, map_location=self._device, weights_only=True
        )
        model.load_state_dict(state)
        self._model = model.to(self._device).eval()

    def unload(self) -> None:
        self._model = None

    def flow(self, a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        check_pair(a, b)
        self.load()
        torch = importlib.import_module("torch")
        height, width = a.shape[:2]
        size = eval_size(width, height, multiple=8)
        pair = [
            torch.from_numpy(np.ascontiguousarray(_resize(img, size).transpose(2, 0, 1)))[None].to(
                self._device
            )
            * 2.0
            - 1.0
            for img in (a, b)
        ]
        with torch.no_grad():
            predicted = self._model(pair[0], pair[1])[-1][0]
        return _to_input_size(predicted.permute(1, 2, 0).float().cpu().numpy(), width, height)
