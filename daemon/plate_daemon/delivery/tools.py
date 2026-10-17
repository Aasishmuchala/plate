"""ffmpeg / ffprobe plumbing for delivery: tool lookup, one runner, one probe.

Both tools come from `PLATE_FFMPEG` / `PLATE_FFPROBE` (a file, or a folder holding the tool) or from PATH.
Commands are argument lists, never strings. ffmpeg's stderr goes to a temp file, not a pipe, so a chatty
encoder cannot deadlock a process that is also being fed raw frames on stdin; a watchdog kills a run that
overshoots its timeout (a blocked stdin write would otherwise never return).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterable
from fractions import Fraction
from pathlib import Path
from typing import Any

from .guard import DeliveryError, ToolMissing

__all__ = ["ffmpeg_path", "ffprobe_path", "find_tool", "probe_video", "run_ffmpeg", "to_fraction"]

DEFAULT_TIMEOUT_S = 3600
PROBE_TIMEOUT_S = 60

# Broadcast rates that are exactly n*1000/1001; a float like 23.976 snaps to the rational.
_NTSC = {Fraction(n * 1000, 1001) for n in (24, 30, 48, 60, 120)}


def find_tool(name: str, env_var: str) -> str | None:
    """`$env_var` (a file, or a folder holding the tool) wins; otherwise PATH. None when absent."""
    override = os.environ.get(env_var)
    if override:
        p = Path(override)
        if p.is_dir():
            p = p / (name + (".exe" if os.name == "nt" else ""))
        return str(p) if p.is_file() else None
    return shutil.which(name)


def ffmpeg_path() -> str:
    found = find_tool("ffmpeg", "PLATE_FFMPEG")
    if found is None:
        raise ToolMissing("ffmpeg not found: put it on PATH or set PLATE_FFMPEG")
    return found


def ffprobe_path() -> str:
    found = find_tool("ffprobe", "PLATE_FFPROBE")
    if found is None:
        raise ToolMissing("ffprobe not found: put it on PATH or set PLATE_FFPROBE")
    return found


def to_fraction(fps: Any) -> Fraction:
    """A frame rate as an exact Fraction. Floats within 0.01 of an NTSC rate (23.976, 29.97, 59.94) snap to
    n*1000/1001; any other float is taken as the nearest simple rational. Strings: '24', '24000/1001', '23.976'."""
    if isinstance(fps, bool):
        raise DeliveryError(f"{fps!r} is not a frame rate")
    if isinstance(fps, Fraction):
        value = fps
    elif isinstance(fps, int):
        value = Fraction(fps)
    elif isinstance(fps, (float, str)):
        try:
            value = Fraction(fps) if isinstance(fps, str) else Fraction(fps).limit_denominator(100000)
        except (ValueError, ZeroDivisionError) as e:
            raise DeliveryError(f"{fps!r} is not a frame rate") from e
    else:
        raise DeliveryError(f"{fps!r} is not a frame rate")
    if value <= 0 or value > 1000:
        raise DeliveryError(f"frame rate {value} is out of range")
    for ntsc in _NTSC:
        if abs(float(value - ntsc)) < 0.01 and value != ntsc and value.denominator != 1001:
            return ntsc
    return value


def run_ffmpeg(
    args: list[str],
    *,
    frames: Iterable[bytes] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> None:
    """Run `ffmpeg <args>`; `frames` (raw video chunks) are written to its stdin when given. Raises
    DeliveryError with the tail of ffmpeg's own message on failure or timeout."""
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *args]
    timed_out = threading.Event()
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if frames is not None else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
        )

        def _kill() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout_s, _kill)
        timer.start()
        try:
            if frames is not None:
                assert proc.stdin is not None
                try:
                    for chunk in frames:
                        proc.stdin.write(chunk)
                except (BrokenPipeError, OSError):
                    pass  # ffmpeg exited early; its stderr says why
                finally:
                    try:
                        proc.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
            code = proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if timed_out.is_set():
            raise DeliveryError(f"ffmpeg timed out after {timeout_s:g} s")
        if code != 0:
            err.seek(0)
            tail = err.read().decode("utf-8", "replace").strip().splitlines()[-4:]
            raise DeliveryError(f"ffmpeg failed ({code}): {' | '.join(tail) or 'no message'}")


def probe_video(path: str | os.PathLike[str]) -> dict[str, Any]:
    """The first video stream of `path` as ffprobe reports it (codec_name, pix_fmt, color_*, r_frame_rate,
    width, height, profile, codec_tag_string, nb_frames), plus `fps` as a Fraction."""
    cmd = [
        ffprobe_path(),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as e:
        raise DeliveryError(f"ffprobe timed out on {Path(path).name}") from e
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-2:]
        raise DeliveryError(f"ffprobe could not read {Path(path).name}: {' | '.join(tail)}")
    streams = json.loads(proc.stdout.decode("utf-8")).get("streams") or []
    if not streams:
        raise DeliveryError(f"{Path(path).name} has no video stream")
    stream: dict[str, Any] = streams[0]
    try:
        stream["fps"] = Fraction(stream.get("r_frame_rate", "0/1"))
    except (ValueError, ZeroDivisionError):
        stream["fps"] = Fraction(0)
    return stream
