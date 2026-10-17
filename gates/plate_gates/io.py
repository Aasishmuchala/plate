"""Image and video IO for the gates: float sRGB loading, ffprobe/ffmpeg probing and frame grabs, a test-clip writer.

Everything is pathlib + UTF-8, every external call is an argument list (never a shell), frames travel through
pipes (no temp files), and ffprobe output is parsed as JSON (no eval of r_frame_rate). The ffmpeg / ffprobe
binaries come from PLATE_FFMPEG / PLATE_FFPROBE or PATH (find_tool); an env value that does not resolve returns
None and never falls through to PATH, so a misconfigured box fails loudly instead of using the wrong build.

Colour: load_rgb returns display-referred sRGB floats in 0..1 with no ICC conversion (ICC handling is the
colour core's job in plate_pixels); 16-bit files keep their full precision.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np

__all__ = [
    "find_tool",
    "ffmpeg",
    "ffprobe",
    "load_rgb",
    "to_uint8",
    "probe",
    "frame_at",
    "iter_gray_frames",
    "write_clip",
]

_TIFF_SUFFIXES = {".tif", ".tiff"}


def find_tool(name: str, env_var: str) -> Path | None:
    """Locate an external binary: the env override first, then PATH.

    An env value that does not resolve returns None; it never falls through to PATH (no silent substitution).
    """
    override = os.environ.get(env_var, "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        found = shutil.which(override)
        return Path(found) if found else None
    found = shutil.which(name)
    return Path(found) if found else None


def ffmpeg() -> Path | None:
    return find_tool("ffmpeg", "PLATE_FFMPEG")


def ffprobe() -> Path | None:
    return find_tool("ffprobe", "PLATE_FFPROBE")


def _need(tool: Path | None, name: str, env_var: str) -> Path:
    if tool is None:
        raise FileNotFoundError(f"{name} not found: install it or set {env_var}")
    return tool


def to_uint8(rgb: np.ndarray) -> np.ndarray:
    """uint8 / uint16 / float (0..1) image -> uint8 with rounding. uint8 passes through unchanged."""
    a = np.asarray(rgb)
    if a.dtype == np.uint8:
        return a
    if a.dtype == np.uint16:
        return ((a.astype(np.uint32) + 128) // 257).astype(np.uint8)
    return np.clip(np.rint(a.astype(np.float32) * 255.0), 0, 255).astype(np.uint8)


def load_rgb(path: str | Path, width: int | None = None) -> np.ndarray:
    """Load an 8/16-bit PNG / TIFF / JPG (or float TIFF) as float32 (H, W, 3) sRGB in 0..1.

    Gray is replicated to three channels, an alpha channel is dropped. width, when given and different from the
    file's, resizes keeping aspect (INTER_AREA when shrinking, INTER_CUBIC when growing).
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"cannot read {p}")
    if p.suffix.lower() in _TIFF_SUFFIXES:
        import tifffile

        arr = tifffile.imread(str(p))
        arr = np.squeeze(arr)
        if arr.ndim == 3 and arr.shape[0] in (3, 4) and arr.shape[-1] not in (3, 4):
            arr = np.moveaxis(arr, 0, -1)  # planar -> interleaved
    else:
        flags = cv2.IMREAD_UNCHANGED
        bgr = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), flags)
        if bgr is None:
            raise ValueError(f"cannot decode image {p}")
        arr = bgr
        if arr.ndim == 3 and arr.shape[2] >= 3:
            arr = np.ascontiguousarray(arr[..., [2, 1, 0] + list(range(3, arr.shape[2]))])
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    elif arr.ndim == 3 and arr.shape[2] == 1:
        arr = np.repeat(arr, 3, axis=2)
    elif arr.ndim == 3 and arr.shape[2] == 2:  # gray + alpha
        arr = np.repeat(arr[..., :1], 3, axis=2)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError(f"unsupported image layout {arr.shape} in {p}")
    arr = arr[..., :3]
    if arr.dtype == np.uint8:
        out = arr.astype(np.float32) / 255.0
    elif arr.dtype == np.uint16:
        out = arr.astype(np.float32) / 65535.0
    elif np.issubdtype(arr.dtype, np.floating):
        out = np.clip(arr.astype(np.float32), 0.0, 1.0)
    else:
        raise ValueError(f"unsupported pixel type {arr.dtype} in {p}")
    if width and out.shape[1] != int(width):
        h, w = out.shape[:2]
        interp = cv2.INTER_AREA if width < w else cv2.INTER_CUBIC
        out = cv2.resize(out, (int(width), max(int(round(h * width / w)), 1)), interpolation=interp)
    return np.ascontiguousarray(out)


def _frac(text: str | None) -> Fraction | None:
    if not text or text in ("0/0", "N/A"):
        return None
    try:
        f = Fraction(text)  # "24000/1001" or "24"; Fraction parses, it never evaluates
    except (ValueError, ZeroDivisionError):
        return None
    return f if f > 0 else None


def _run_json(cmd: list[str]) -> dict:
    res = subprocess.run(cmd, capture_output=True, check=False)
    if res.returncode != 0:
        raise ValueError(
            f"ffprobe failed ({res.returncode}): {res.stderr.decode('utf-8', 'replace').strip()}"
        )
    return json.loads(res.stdout.decode("utf-8"))


def probe(video: str | Path) -> dict:
    """{fps (Fraction), duration_s, width, height, nb_frames, codec, pix_fmt} of the first video stream.

    Uses ffprobe -print_format json. nb_frames comes from the container when it is there, else from a counted
    decode (-count_frames), else from round(duration * fps).
    """
    v = Path(video)
    if not v.is_file():
        raise FileNotFoundError(f"cannot open video: {v}")
    tool = _need(ffprobe(), "ffprobe", "PLATE_FFPROBE")
    base = [str(tool), "-v", "error", "-select_streams", "v:0", "-print_format", "json"]
    entries = "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration,codec_name,pix_fmt"
    info = _run_json([*base, "-show_entries", f"{entries}:format=duration", str(v)])
    streams = info.get("streams") or []
    if not streams:
        raise ValueError(f"no video stream in {v}")
    st = streams[0]
    fps = _frac(st.get("avg_frame_rate")) or _frac(st.get("r_frame_rate"))
    if fps is None:
        raise ValueError(f"cannot determine the frame rate of {v}")
    dur_text = st.get("duration") or (info.get("format") or {}).get("duration")
    duration = float(dur_text) if dur_text not in (None, "N/A") else None
    nb_text = st.get("nb_frames")
    nb = int(nb_text) if nb_text not in (None, "N/A") else None
    if nb is None:
        counted = _run_json([*base, "-count_frames", "-show_entries", "stream=nb_read_frames", str(v)])
        n = (counted.get("streams") or [{}])[0].get("nb_read_frames")
        nb = int(n) if n not in (None, "N/A") else None
    if nb is None and duration is not None:
        nb = int(round(duration * float(fps)))
    if nb is None:
        raise ValueError(f"cannot determine the frame count of {v}")
    if duration is None:
        duration = float(nb / fps)
    return {
        "fps": fps,
        "duration_s": duration,
        "width": int(st["width"]),
        "height": int(st["height"]),
        "nb_frames": nb,
        "codec": st.get("codec_name"),
        "pix_fmt": st.get("pix_fmt"),
    }


def frame_at(video: str | Path, t_s: float, *, info: dict | None = None) -> np.ndarray:
    """RGB uint8 (H, W, 3) frame at time t_s: the first frame whose timestamp is >= t_s (ffmpeg -ss before -i).

    Decoded straight to a pipe. Pass `info` (a probe result) to skip the extra ffprobe call when grabbing many
    frames from one clip. Raises ValueError when the time lies past the end of the stream.
    """
    v = Path(video)
    info = info or probe(v)
    w, h = info["width"], info["height"]
    tool = _need(ffmpeg(), "ffmpeg", "PLATE_FFMPEG")
    cmd = [str(tool), "-v", "error", "-nostdin", "-noautorotate", "-ss", f"{max(float(t_s), 0.0):.6f}"]
    cmd += ["-i", str(v), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    res = subprocess.run(cmd, capture_output=True, check=False)
    if res.returncode != 0:
        raise ValueError(f"ffmpeg failed on {v}: {res.stderr.decode('utf-8', 'replace').strip()}")
    if len(res.stdout) != w * h * 3:
        raise ValueError(f"no frame at t={t_s:.3f}s in {v} (got {len(res.stdout)} bytes, wanted {w * h * 3})")
    return np.frombuffer(res.stdout, dtype=np.uint8).reshape(h, w, 3).copy()


def iter_gray_frames(video: str | Path, *, width: int | None = None) -> Iterator[np.ndarray]:
    """Yield every frame as a uint8 gray (H, W) array, optionally scaled to `width` (aspect kept, area filter).

    One ffmpeg process streams rawvideo gray through a pipe; memory stays at one frame.
    """
    v = Path(video)
    info = probe(v)
    w, h = info["width"], info["height"]
    vf = "format=gray"
    if width and int(width) != w:
        w2 = int(width)
        h2 = max(int(round(h * w2 / w)) // 2 * 2, 2)
        vf = f"scale={w2}:{h2}:flags=area,format=gray"
        w, h = w2, h2
    tool = _need(ffmpeg(), "ffmpeg", "PLATE_FFMPEG")
    cmd = [str(tool), "-v", "error", "-nostdin", "-noautorotate", "-i", str(v), "-vf", vf]
    cmd += ["-f", "rawvideo", "-pix_fmt", "gray", "-"]
    size = w * h
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(h, w).copy()


def write_clip(
    path: str | Path,
    frames: list[np.ndarray],
    fps: float | Fraction = 24,
    *,
    crf: int = 0,
    pix_fmt: str = "yuv444p",
) -> Path:
    """Write RGB uint8 frames to an H.264 file by piping rawvideo into ffmpeg. crf 0 + yuv444p is lossless
    apart from the RGB->YUV rounding, which keeps landing / flicker goldens deterministic. Returns the path."""
    if not frames:
        raise ValueError("write_clip needs at least one frame")
    h, w = frames[0].shape[:2]
    for f in frames:
        if f.shape != (h, w, 3) or f.dtype != np.uint8:
            raise ValueError("all frames must be RGB uint8 of one size")
    tool = _need(ffmpeg(), "ffmpeg", "PLATE_FFMPEG")
    out = Path(path)
    cmd = [str(tool), "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24"]
    cmd += ["-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-crf", str(int(crf))]
    cmd += ["-pix_fmt", pix_fmt, "-movflags", "+faststart", str(out)]
    res = subprocess.run(
        cmd, input=b"".join(np.ascontiguousarray(f).tobytes() for f in frames), capture_output=True
    )
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed writing {out}: {res.stderr.decode('utf-8', 'replace').strip()}")
    return out
