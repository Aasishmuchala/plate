"""One packet frame: V-Ray's multilayer EXR (beauty plus every render element) and the
VFB-corrected 16-bit TIFF, rendered through the current V-Ray renderer.

The EXR comes from V-Ray's own raw-image output (output_saveRawFile + output_rawFileName), never from
render(outputfile=): Max's bitmap writer would overwrite V-Ray's file after the render and drop the
Cryptomatte manifest from the EXR header. render() runs with vfb=True so the VFB holds the frame and
its corrections (exposure, white balance, curves, OCIO display) when vfbControl #saveimage writes the
display master (decision 2: what was signed off in the VFB is what ships). Both files are checked on
disk afterwards: the EXR by its magic number, the TIFF by its BitsPerSample tag (stdlib parsers here).

Every renderer property and vfbControl command below is a candidate list from the V-Ray docs and
MaxGaffer's render.py, unverified on a Max box (TODO-verify, ONBOX.md steps 1, 5, 6). The render
settings this module changes are restored afterwards, whatever happens.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..rt import runtime
from .elements import BEAUTY_LAYER, ELEMENT_NAME, current_manager, list_elements
from .scene import get_camera, get_prop, has_prop, rt_attr

# V-Ray renderer output properties: (purpose, candidates, value or a key filled per call). TODO-verify
# every name and every enum value: they are the V-Ray 6/7 MAXScript names as documented, not read
# back from a box yet.
RAW_SAVE = ("output_saveRawFile",)  # TODO-verify: "V-Ray raw image file" checkbox
RAW_FILE = ("output_rawFileName",)  # TODO-verify: its path (.exr selects multichannel OpenEXR)
RAW_PREVIEW = ("output_genpreview",)  # TODO-verify: "Generate preview" (off: no extra bitmap in RAM)
VFB_ON = ("output_on",)  # TODO-verify: "Enable built-in frame buffer" (vfbControl needs the V-Ray VFB)
SPLIT_SAVE = ("output_splitgbuffer",)  # TODO-verify: "Save separate render channels" (off: one file)
# Bit depth of the raw EXR: V-Ray writes one depth per file unless per-channel types exist; data passes
# need 32-bit float, so the file is float whenever only a global switch is found.
EXR_BITS = ("output_rawExrBitsPerChannel", "output_raw_exr_bits")  # TODO-verify: 16 or 32
EXR_HALF_BEAUTY = ("output_rawExrHalfBeauty",)  # TODO-verify: half beauty, float data (if V-Ray has it)
EXR_COMPRESSION = ("output_rawExrCompression", "output_raw_exr_compression")  # TODO-verify enum
EXR_COMPRESSION_ZIP = 3  # TODO-verify: OpenEXR's own ZIP_COMPRESSION is 3
SAVE_IMAGE = ("saveimage", "saveImage")  # TODO-verify: vfbControl #saveimage <path>
SET_SAVE_FORMAT = ("setsaveformat", "setSaveFormat")  # TODO-verify: vfbControl #setsaveformat
TIFF16_FORMAT = ("tif", 16)  # TODO-verify: the #setsaveformat arguments for a 16-bit TIFF
# sysInfo.getMAXMemoryInfo() -> #(PageFaultCount, PeakWorkingSetSize, WorkingSetSize, ...) in bytes.
MEMORY_INFO = ("getMAXMemoryInfo",)  # TODO-verify
PEAK_WORKING_SET_INDEX = 1  # TODO-verify

EXR_MAGIC = b"\x76\x2f\x31\x01"
GIB = 1024**3


class RenderFailed(RuntimeError):
    """render() raised or returned nothing; the message says which call."""


@dataclass
class RenderResult:
    exr: str | None
    tif: str | None
    time_s: float
    peak_rss_gb: float | None
    element_layers: list[str]
    warnings: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)  # property -> value this call set
    render_kwargs: dict[str, Any] = field(default_factory=dict)
    tif_bits: int | None = None

    @property
    def ok(self) -> bool:
        return self.exr is not None and self.tif is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "exr": self.exr,
            "tif": self.tif,
            "time_s": self.time_s,
            "peak_rss_gb": self.peak_rss_gb,
            "element_layers": list(self.element_layers),
            "warnings": list(self.warnings),
            "settings": dict(self.settings),
            "tif_bits": self.tif_bits,
        }


# --------------------------------------------------------------------------- file checks (stdlib)


def is_exr(path: Path | str) -> bool:
    try:
        with Path(path).open("rb") as fh:
            return fh.read(4) == EXR_MAGIC
    except OSError:
        return False


def tiff_bits_per_sample(path: Path | str) -> int | None:
    """BitsPerSample (tag 258) of a TIFF's first image, or None when the file is not a TIFF.
    Handles both byte orders and the SHORT array form (one value per channel)."""
    try:
        data = Path(path).read_bytes()[: 1 << 20]
    except OSError:
        return None
    if data[:4] not in (b"II*\x00", b"MM\x00*"):
        return None
    order = "<" if data[:2] == b"II" else ">"
    try:
        (ifd,) = struct.unpack_from(order + "I", data, 4)
        (count,) = struct.unpack_from(order + "H", data, ifd)
        for i in range(count):
            tag, kind, n, value = struct.unpack_from(order + "HHI4s", data, ifd + 2 + 12 * i)
            if tag != 258:
                continue
            if kind != 3:  # SHORT
                return None
            if n * 2 <= 4:
                return struct.unpack_from(order + "H", value, 0)[0]
            (offset,) = struct.unpack(order + "I", value)
            return struct.unpack_from(order + "H", data, offset)[0]
    except struct.error:
        return None
    return 1  # TIFF 6.0: an image without a BitsPerSample tag is bilevel (1 bit)


# --------------------------------------------------------------------------- settings plumbing


class _Settings:
    """Sets properties by candidate name and puts every one back on restore(). check=False is for
    MAXScript globals on rt itself (renderWidth), which isProperty cannot be asked about."""

    def __init__(self, target: Any, check: bool = True):
        self.target = target
        self.check = check
        self.saved: list[tuple[str, Any]] = []
        self.applied: dict[str, Any] = {}

    def set(self, names: tuple[str, ...], value: Any) -> str | None:
        for name in names:
            if self.check and not has_prop(self.target, name):
                continue
            try:
                before = getattr(self.target, name)
                setattr(self.target, name, value)
            except Exception:
                continue
            self.saved.append((name, before))
            self.applied[name] = value
            return name
        return None

    def restore(self) -> None:
        for name, before in reversed(self.saved):
            try:
                setattr(self.target, name, before)
            except Exception:
                pass
        self.saved.clear()


def _peak_rss_gb() -> float | None:
    info = rt_attr("sysInfo")
    for name in MEMORY_INFO:
        fn = getattr(info, name, None) if info is not None else None
        if not callable(fn):
            continue
        try:
            values = list(fn())
            return round(float(values[PEAK_WORKING_SET_INDEX]) / GIB, 3)
        except Exception:
            continue
    return None


def _vfb(command: tuple[str, ...], *args: Any) -> str | None:
    """rt.vfbControl(#command, *args) for the first command spelling that does not raise."""
    rt = runtime()
    vfb = rt_attr("vfbControl")
    if not callable(vfb):
        return None
    for name in command:
        try:
            vfb(rt.Name(name), *args)
        except Exception:
            continue
        return name
    return None


def element_layers(mgr: Any = None) -> list[str]:
    """RGBA plus the elementname of every enabled render element: the layers the raw EXR will hold."""
    try:
        mgr = current_manager() if mgr is None else mgr
    except Exception:
        return [BEAUTY_LAYER]
    layers = [BEAUTY_LAYER]
    for element in list_elements(mgr):
        if get_prop(element, ("enabled",)) is False:
            continue
        name = get_prop(element, ELEMENT_NAME)
        if name:
            layers.append(str(name))
    return layers


# --------------------------------------------------------------------------- the frame


def render_packet_frame(
    cam: Any,
    out_exr: Path | str,
    out_tif16: Path | str,
    width: int,
    height: int,
    frame: int,
) -> RenderResult:
    """Render camera cam (a node or a name) at frame and width x height through V-Ray: the
    multilayer EXR to out_exr (V-Ray raw output: float data passes, ZIP, one file) and the
    VFB-corrected 16-bit TIFF to out_tif16 (vfbControl #saveimage). Raises RenderFailed when render()
    itself fails; anything softer (a property that could not be set, a TIFF that is not 16-bit, a
    file that did not appear) is a warning and a None path in the result."""
    rt = runtime()
    node = get_camera(cam)
    if node is None:
        raise RenderFailed(f"camera {cam!r} not found")
    exr, tif = Path(out_exr), Path(out_tif16)
    for path in (exr, tif):
        if ":" in path.name:
            raise ValueError(f"{path.name!r}: ':' is not allowed in a file name")
        path.parent.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    try:
        rt.renderSceneDialog.close()  # an open render dialog blocks scripted setting changes
    except Exception:
        pass
    renderers = rt_attr("renderers")
    renderer = getattr(renderers, "current", None)
    vray = _Settings(renderer)
    size = _Settings(rt, check=False)
    started = time.perf_counter()
    try:
        if vray.set(RAW_SAVE, True) is None:
            warnings.append(f"{'/'.join(RAW_SAVE)}: not on this renderer; V-Ray will not write the EXR")
        if vray.set(RAW_FILE, str(exr)) is None:
            warnings.append(f"{'/'.join(RAW_FILE)}: not on this renderer")
        vray.set(RAW_PREVIEW, False)
        vray.set(SPLIT_SAVE, False)
        if vray.set(VFB_ON, True) is None:
            warnings.append(f"{'/'.join(VFB_ON)}: unreadable; vfbControl needs the V-Ray frame buffer")
        if vray.set(EXR_HALF_BEAUTY, True) is not None:
            vray.set(EXR_BITS, 32)
        elif vray.set(EXR_BITS, 32) is not None:
            warnings.append("raw EXR is 32-bit float throughout (no half-beauty switch found)")
        else:
            warnings.append(
                f"{'/'.join(EXR_BITS)}: no bit-depth property; check the EXR channel types (ONBOX.md step 6)"
            )
        if vray.set(EXR_COMPRESSION, EXR_COMPRESSION_ZIP) is None:
            warnings.append(f"{'/'.join(EXR_COMPRESSION)}: no compression property; V-Ray's default applies")
        size.set(("renderWidth",), int(width))
        size.set(("renderHeight",), int(height))
        for stale in (exr, tif):  # a file left by an earlier attempt must not pass for this render
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RenderFailed(f"{stale.name}: cannot replace the previous file ({exc})") from exc

        kwargs = {
            "camera": node,
            "frame": int(frame),
            "outputwidth": int(width),
            "outputheight": int(height),
            "vfb": True,
            "quiet": True,
        }
        try:
            bitmap = rt.render(**kwargs)
        except Exception as exc:
            raise RenderFailed(f"render() raised {type(exc).__name__}: {exc}") from exc
        if bitmap is None:
            raise RenderFailed("render() returned undefined (cancelled, or the renderer failed)")
        elapsed = time.perf_counter() - started
        try:
            rt.close(bitmap)  # free the returned beauty bitmap; the VFB keeps its own copy
        except Exception:
            pass

        if _vfb(SET_SAVE_FORMAT, *TIFF16_FORMAT) is None:
            warnings.append("vfbControl #setsaveformat: not available; the TIFF bit depth is the VFB default")
        saved_with = _vfb(SAVE_IMAGE, str(tif))
        if saved_with is None:
            warnings.append("vfbControl #saveimage: failed; no display TIFF")
    finally:
        vray.restore()
        size.restore()

    exr_path: str | None = str(exr)
    if not exr.is_file():
        warnings.append(f"{exr.name}: V-Ray wrote no EXR (raw image output off or unsupported?)")
        exr_path = None
    elif not is_exr(exr):
        warnings.append(f"{exr.name}: not an OpenEXR file (bad magic number)")
        exr_path = None
    tif_path: str | None = str(tif)
    bits = tiff_bits_per_sample(tif) if tif.is_file() else None
    if not tif.is_file():
        warnings.append(f"{tif.name}: the VFB saved no file")
        tif_path = None
    elif bits is None:
        warnings.append(f"{tif.name}: not a TIFF (the VFB ignored the extension?)")
        tif_path = None
    elif bits != 16:
        warnings.append(f"{tif.name}: {bits}-bit TIFF, the display master must be 16-bit")
        tif_path = None
    return RenderResult(
        exr=exr_path,
        tif=tif_path,
        time_s=round(elapsed, 3),
        peak_rss_gb=_peak_rss_gb(),
        element_layers=element_layers(),
        warnings=warnings,
        settings=dict(vray.applied),
        render_kwargs={k: v for k, v in kwargs.items() if k != "camera"},
        tif_bits=bits,
    )
