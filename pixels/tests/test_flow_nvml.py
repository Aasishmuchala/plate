"""Optical flow (DIS on every machine, RAFT behind the gpu extra) and NVML admission with scheduled windows."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plate_pixels import flow
from plate_pixels.backends import Backends, InputError, ModelUnavailable, StubDepth
from plate_pixels.flow import DisFlow, RaftFlow, check_pair, eval_size
from plate_pixels.nvml import (
    ALLOW_UNMANAGED_ENV,
    FORCE_LOCAL_ENV,
    Admission,
    AdmissionDenied,
    ModelSlot,
    Window,
    parse_hhmm,
    parse_windows,
)

# --------------------------------------------------------------------------- flow


def textured(height: int, width: int, seed: int = 3) -> np.ndarray:
    """A float32 RGB image with detail at every scale (smoothed noise), so a dense flow has something to lock to."""
    rng = np.random.default_rng(seed)
    out = np.empty((height, width, 3), dtype=np.float32)
    for c in range(3):
        fine = rng.random((height, width), dtype=np.float32)
        coarse = cv2.resize(rng.random((height // 8 + 2, width // 8 + 2), dtype=np.float32), (width, height))
        out[..., c] = 0.6 * cv2.GaussianBlur(fine, (0, 0), 1.2) + 0.4 * coarse
    return np.clip(out, 0.0, 1.0)


def shifted(img: np.ndarray, dx: int, dy: int = 0) -> np.ndarray:
    """``b(x + dx, y + dy) = a(x, y)`` with edge replication (np.roll would wrap content across the border)."""
    h, w = img.shape[:2]
    pad = max(abs(dx), abs(dy))
    big = np.pad(img, ((pad, pad), (pad, pad), (0, 0)), mode="edge")
    return np.ascontiguousarray(big[pad - dy : pad - dy + h, pad - dx : pad - dx + w])


def test_dis_recovers_a_two_pixel_shift():
    a = textured(180, 320)
    u, v = DisFlow().flow(a, shifted(a, 2))
    assert u.dtype == v.dtype == np.float32 and u.shape == v.shape == (180, 320)
    assert abs(np.median(np.abs(u)) - 2.0) <= 0.3
    assert np.median(u) > 0  # b(x + u) = a(x): content moved right, u is positive
    assert np.median(np.abs(v)) <= 0.3


def test_dis_reports_vertical_motion_with_its_sign():
    a = textured(180, 320, seed=5)
    u, v = DisFlow().flow(a, shifted(a, 0, -3))
    assert abs(np.median(v) + 3.0) <= 0.4
    assert np.median(np.abs(u)) <= 0.3


def test_dis_rescales_vectors_to_input_pixels_above_960_wide():
    """1280 px wide is evaluated at 960 (scale 0.75); a 4 px shift must still read as 4 px, not 3."""
    a = textured(720, 1280, seed=7)
    u, v = DisFlow().flow(a, shifted(a, 4))
    assert u.shape == (720, 1280)
    assert abs(np.median(np.abs(u)) - 4.0) <= 0.5


def test_dis_of_identical_images_is_zero():
    a = textured(90, 160)
    u, v = DisFlow().flow(a, a.copy())
    assert float(np.abs(u).max()) < 0.05 and float(np.abs(v).max()) < 0.05


def test_dis_is_real_not_a_stub_and_is_licence_clean():
    backend = DisFlow()
    assert backend.is_stub is False and backend.impl == "dis" and backend.model_id == "opencv-dis"
    assert backend.eval_width == 960
    assert Backends().describe()["flow"] == {"model": "opencv-dis", "impl": "dis", "stub": False}


@pytest.mark.parametrize(
    ("size", "multiple", "expected"),
    [
        ((320, 180), 1, (320, 180)),  # never upscaled
        ((960, 540), 1, (960, 540)),
        ((1920, 1080), 1, (960, 540)),
        ((3840, 2160), 1, (960, 540)),
        ((1920, 1080), 8, (960, 544)),
        ((1000, 562), 8, (960, 536)),
        ((50, 30), 8, (48, 32)),
    ],
)
def test_eval_size_caps_at_960_and_never_upscales(size, multiple, expected):
    assert eval_size(*size, multiple=multiple) == expected
    assert eval_size(*size, multiple=multiple)[0] <= max(size[0], 8)


def test_flow_never_grows_the_input():
    """The flow comes back at the input size whatever size it was evaluated at."""
    a = textured(540, 960, seed=9)
    u, _ = DisFlow().flow(a, a)
    assert u.shape == a.shape[:2]


def test_check_pair_demands_two_rgb_images_of_one_size():
    check_pair(np.zeros((4, 6, 3), np.float32), np.zeros((4, 6, 3), np.float32))
    with pytest.raises(InputError):
        check_pair(np.zeros((4, 6, 3), np.float32), np.zeros((4, 7, 3), np.float32))
    with pytest.raises(InputError):
        check_pair(np.zeros((4, 6), np.float32), np.zeros((4, 6), np.float32))
    with pytest.raises(InputError):
        DisFlow().flow(np.zeros((4, 6, 3), np.float32), np.zeros((5, 6, 3), np.float32))


@pytest.mark.skipif(importlib.util.find_spec("torch") is not None, reason="torch is installed (render box)")
def test_raft_without_the_gpu_extra_is_model_unavailable():
    with pytest.raises(ModelUnavailable) as err:
        RaftFlow()
    assert "raft" in str(err.value) and "gpu" in str(err.value)
    assert "torch" not in sys.modules


def test_raft_is_the_real_flow_under_the_real_flag(monkeypatch):
    monkeypatch.setenv("PLATE_PIXELS_REAL", "1")
    assert Backends().describe()["flow"] == {"model": "raft", "impl": "real", "stub": False}
    assert flow.RaftFlow.vram_mb > 0 and flow.DisFlow.vram_mb == 0


# --------------------------------------------------------------------------- nvml: a fake pynvml


class FakeNvml:
    """A pynvml-shaped module that reports ``free_mb`` of free VRAM and counts its calls."""

    def __init__(self, free_mb: int, broken: bool = False):
        self.free_mb = free_mb
        self.broken = broken
        self.queries = 0

    def nvmlDeviceGetHandleByIndex(self, index):  # noqa: N802 - pynvml's names
        self.queries += 1
        if self.broken:
            raise RuntimeError("driver reset")
        return index

    def nvmlDeviceGetMemoryInfo(self, handle):  # noqa: N802
        return SimpleNamespace(free=self.free_mb * 1024 * 1024, total=32768 * 1024 * 1024)


@pytest.fixture
def no_pynvml(monkeypatch):
    """pynvml cannot be imported (the Mac): sys.modules[name] = None makes the import raise."""
    monkeypatch.setitem(sys.modules, "pynvml", None)


def at(hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 10, 17, hh, mm)


def test_headroom_is_none_without_nvml(no_pynvml):
    assert Admission(env={}).headroom_mb() is None


def test_headroom_reads_free_vram_in_mib():
    assert Admission(nvml=FakeNvml(8000), env={}).headroom_mb() == 8000


def test_a_driver_reset_reads_as_unmeasurable_not_a_crash():
    assert Admission(nvml=FakeNvml(8000, broken=True), env={}).headroom_mb() is None


def test_mac_admission_is_refused_unless_unmanaged_is_allowed(no_pynvml):
    ok, reason = Admission(env={}).admit(1000)
    assert ok is False and "no nvml" in reason and ALLOW_UNMANAGED_ENV in reason
    assert Admission(env={ALLOW_UNMANAGED_ENV: "1"}).admit(1000) == (True, "no nvml: unmanaged")
    assert Admission(env={ALLOW_UNMANAGED_ENV: "0"}).admit(1000)[0] is False
    assert Admission(env={ALLOW_UNMANAGED_ENV: "yes"}).admit(1000)[0] is False  # only "1" counts


def test_admit_needs_the_model_plus_the_reserve():
    adm = Admission(nvml=FakeNvml(8000), env={})
    assert adm.admit(4000)[0] is True
    assert adm.admit(5952)[0] is True  # 5952 + the default 2048 reserve == 8000: exactly fits
    ok, reason = adm.admit(5953)
    assert ok is False and "insufficient VRAM" in reason and "8000 MiB free" in reason
    assert adm.admit(7000, reserve_mb=1000)[0] is True
    assert adm.admit(7500, reserve_mb=1000)[0] is False


def test_admit_zero_needs_only_the_reserve():
    assert Admission(nvml=FakeNvml(2048), env={}).admit(0)[0] is True
    assert Admission(nvml=FakeNvml(2047), env={}).admit(0)[0] is False


# --------------------------------------------------------------------------- nvml: windows


def test_parse_hhmm():
    assert parse_hhmm("00:00") == 0 and parse_hhmm("22:30") == 1350 and parse_hhmm("0600") == 360
    assert parse_hhmm("9:05") == 545
    for bad in ("24:00", "12:60", "noon", "", "1:2:3"):
        with pytest.raises(ValueError):
            parse_hhmm(bad)


def test_windows_from_a_string_a_list_dicts_and_tuples():
    expected = [Window("01:00", "06:00"), Window("13:00", "14:00")]
    assert parse_windows("01:00-06:00, 13:00-14:00") == expected
    assert parse_windows(expected) == expected
    assert parse_windows([("01:00", "06:00"), ("13:00", "14:00")]) == expected
    assert parse_windows([{"start_hhmm": "01:00", "end_hhmm": "06:00"}, "13:00-14:00"]) == expected
    assert parse_windows(None) == [] and parse_windows("") == []
    for bad in ("01:00", "25:00-26:00", "a-b"):
        with pytest.raises(ValueError):
            parse_windows(bad)


def test_in_window_is_start_inclusive_end_exclusive():
    adm = Admission(windows="01:00-06:00,13:00-14:00", env={})
    assert adm.in_window(at(1, 0)) and adm.in_window(at(2, 30)) and adm.in_window(at(5, 59))
    assert not adm.in_window(at(6, 0)) and not adm.in_window(at(0, 59))
    assert adm.in_window(at(13, 59)) and not adm.in_window(at(12, 0)) and not adm.in_window(at(14, 0))


def test_a_window_may_wrap_midnight():
    adm = Admission(windows=[Window("22:00", "02:00")], env={})
    assert adm.in_window(at(23, 30)) and adm.in_window(at(0, 0)) and adm.in_window(at(1, 59))
    assert not adm.in_window(at(2, 0)) and not adm.in_window(at(12, 0)) and not adm.in_window(at(21, 59))


def test_an_empty_or_zero_length_window_never_matches():
    assert Admission(env={}).in_window(at(3)) is False
    assert Admission(windows=[Window("03:00", "03:00")], env={}).in_window(at(3)) is False


def test_windows_come_from_the_environment():
    adm = Admission(env={"PLATE_GPU_WINDOWS": "01:00-06:00"})
    assert adm.windows == [Window("01:00", "06:00")] and adm.in_window(at(2))
    assert Admission(windows="08:00-09:00", env={"PLATE_GPU_WINDOWS": "01:00-06:00"}).windows == [
        Window("08:00", "09:00")
    ]


@pytest.mark.parametrize("model", ["qwen-image-edit-2509", "Qwen_Image_Edit_2509", "wan2.2", "wan2.2_vace"])
def test_qwen_and_wan_need_a_window_or_force_local(model):
    adm = Admission(nvml=FakeNvml(30000), windows="01:00-06:00", env={})
    ok, reason = adm.admit(8000, model=model, now=at(12))
    assert (
        ok is False
        and "scheduled GPU window" in reason
        and "01:00-06:00" in reason
        and FORCE_LOCAL_ENV in reason
    )
    assert adm.admit(8000, model=model, now=at(2))[0] is True
    forced = Admission(nvml=FakeNvml(30000), windows="01:00-06:00", env={FORCE_LOCAL_ENV: "1"})
    assert forced.admit(8000, model=model, now=at(12))[0] is True


def test_qwen_with_no_windows_configured_says_so():
    ok, reason = Admission(nvml=FakeNvml(30000), env={}).admit(8000, model="qwen-image-edit-2509", now=at(3))
    assert ok is False and "none configured" in reason


def test_a_window_does_not_waive_the_vram_check():
    adm = Admission(nvml=FakeNvml(4000), windows="01:00-06:00", env={})
    ok, reason = adm.admit(8000, model="wan2.2", now=at(2))
    assert ok is False and "insufficient VRAM" in reason


def test_other_models_ignore_windows():
    adm = Admission(nvml=FakeNvml(8000), windows="01:00-06:00", env={})
    assert adm.admit(1500, model="depth-anything-v2-small", now=at(12))[0] is True
    assert adm.admit(1500, now=at(12))[0] is True


def test_snapshot_reports_what_admission_sees(no_pynvml):
    snap = Admission(windows="01:00-06:00", env={FORCE_LOCAL_ENV: "1"}).snapshot(at(2))
    assert snap == {
        "headroom_mb": None,
        "nvml": False,
        "in_window": True,
        "windows": [{"start_hhmm": "01:00", "end_hhmm": "06:00"}],
        "force_local": True,
        "unmanaged_allowed": False,
    }
    live = Admission(nvml=FakeNvml(1234), env={}).snapshot(at(2))
    assert live["headroom_mb"] == 1234 and live["nvml"] is True and live["in_window"] is False


# --------------------------------------------------------------------------- nvml: ModelSlot


class Recorder:
    model_id = "depth-anything-v2-small"

    def __init__(self):
        self.calls = []

    def load(self):
        self.calls.append("load")

    def unload(self):
        self.calls.append("unload")


def test_model_slot_loads_then_unloads():
    backend = Recorder()
    with ModelSlot(backend) as held:
        assert held is backend and backend.calls == ["load"]
    assert backend.calls == ["load", "unload"]


def test_model_slot_unloads_when_the_body_raises():
    backend = Recorder()
    with pytest.raises(RuntimeError, match="boom"), ModelSlot(backend):
        raise RuntimeError("boom")
    assert backend.calls == ["load", "unload"]


def test_model_slot_flushes_the_cuda_cache_when_torch_is_loaded(monkeypatch):
    flushed = []
    cuda = SimpleNamespace(is_available=lambda: True, empty_cache=lambda: flushed.append(1))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    with ModelSlot(Recorder()):
        assert flushed == []
    assert flushed == [1]


def test_model_slot_skips_the_flush_without_cuda(monkeypatch):
    flushed = []
    cuda = SimpleNamespace(is_available=lambda: False, empty_cache=lambda: flushed.append(1))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    with ModelSlot(Recorder()):
        pass
    assert flushed == []


def test_model_slot_never_imports_torch(monkeypatch):
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    with ModelSlot(Recorder()):
        pass
    assert "torch" not in sys.modules


def test_model_slot_refuses_before_loading_when_admission_says_no():
    backend = Recorder()
    adm = Admission(nvml=FakeNvml(3000), env={})
    with pytest.raises(AdmissionDenied, match="insufficient VRAM"), ModelSlot(backend, adm, required_mb=4000):
        pass
    assert backend.calls == []  # nothing was loaded, nothing needs unloading


def test_model_slot_admits_and_uses_the_backends_model_id_for_windows():
    backend = Recorder()
    backend.model_id = "wan2.2"
    # A window that starts 2 h from now and ends 3 h from now never contains "now" (the old fixed
    # 01:00-06:00 window failed whenever the suite ran in the small hours).
    now = datetime.now()
    start, end = now + timedelta(hours=2), now + timedelta(hours=3)
    adm = Admission(nvml=FakeNvml(30000), windows=f"{start:%H:%M}-{end:%H:%M}", env={})
    assert not adm.in_window(now)
    with (
        pytest.raises(AdmissionDenied, match="scheduled GPU window"),
        ModelSlot(backend, adm, required_mb=8000),
    ):
        pass
    ok = Admission(nvml=FakeNvml(30000), env={FORCE_LOCAL_ENV: "1"})
    with ModelSlot(backend, ok, required_mb=8000):
        assert backend.calls == ["load"]


def test_a_real_backend_is_a_model_slot_citizen():
    """load()/unload() exist on every backend, so a stub or a real model can sit in a slot."""
    with ModelSlot(StubDepth()) as held:
        assert held.model_id == "depth-anything-v2-small"
