"""Receive: show an image the daemon returned inside Max, without touching the scene.

Two places it can go:

  vfb_history          vfbControl #loadimage <path> puts it in the V-Ray frame buffer, then
                       #historysave keeps it in the VFB history next to the original render, so the
                       artist A/Bs it with V-Ray's own compare tools
  viewport_background  the active viewport's background image (backgroundImageFileName, then the
                       display switch and a redraw), for checking it against the scene's geometry

Display only: no node, material, light or render setting changes, so no backup or undo record is
needed. Every vfbControl command and viewport name below is a candidate list, unverified on a Max
box (TODO-verify, ONBOX.md step 1). Stdlib only; every pymxs touch goes through rt.runtime().
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..bridge.scene import rt_attr
from ..rt import runtime

MODES = ("vfb_history", "viewport_background")
LOAD_IMAGE = ("loadimage", "loadImage")  # TODO-verify: vfbControl #loadimage <path>
HISTORY_SAVE = ("historysave", "historySave")  # TODO-verify: vfbControl #historysave (current image)
BACKGROUND_FILE = ("backgroundImageFileName",)  # TODO-verify: the viewport background image global
BACKGROUND_SHOW = ("DispBkgImage", "dispBkgImage")  # TODO-verify: viewport.DispBkgImage = true
IMAGE_SUFFIXES = (".exr", ".tif", ".tiff", ".png", ".jpg", ".jpeg")


def _vfb(names: tuple[str, ...], *args: Any) -> str | None:
    """rt.vfbControl(#name, *args) for the first spelling that does not raise; None when none works."""
    vfb = rt_attr("vfbControl")
    if not callable(vfb):
        return None
    rt = runtime()
    for name in names:
        try:
            vfb(rt.Name(name), *args)
        except Exception:
            continue
        return name
    return None


def _load_vfb_history(path: str) -> tuple[bool, str]:
    if not callable(rt_attr("vfbControl")):
        return False, "vfbControl is not available: Receive into the VFB needs V-Ray's frame buffer"
    loaded = _vfb(LOAD_IMAGE, path)
    if loaded is None:
        return False, f"vfbControl #{LOAD_IMAGE[0]} failed for {Path(path).name}"
    saved = _vfb(HISTORY_SAVE)
    if saved is None:
        return True, f"loaded {Path(path).name} into the VFB (history save unavailable: compare it now)"
    return True, f"loaded {Path(path).name} into the VFB history"


def _set_global(names: tuple[str, ...], value: Any) -> str | None:
    """Set the first MAXScript global in names that takes the value (read back to confirm)."""
    rt = runtime()
    for name in names:
        try:
            setattr(rt, name, value)
            if str(getattr(rt, name)) == str(value):
                return name
        except Exception:
            continue
    return None


def _load_viewport_background(path: str) -> tuple[bool, str]:
    if _set_global(BACKGROUND_FILE, path) is None:
        return False, f"could not set the viewport background ({'/'.join(BACKGROUND_FILE)})"
    shown = False
    viewport = rt_attr("viewport")
    for name in BACKGROUND_SHOW:
        try:
            setattr(viewport, name, True)
        except Exception:
            continue
        shown = True
        break
    redraw = rt_attr("redrawViews")
    if callable(redraw):
        try:
            redraw()
        except Exception:
            pass
    if not shown:
        return True, f"set {Path(path).name} as the viewport background (turn on Show Background to see it)"
    return True, f"{Path(path).name} is the viewport background"


def load_result(path: Path | str, mode: str = "vfb_history") -> tuple[bool, str]:
    """(ok, message) after loading the image at path into mode ('vfb_history' or
    'viewport_background'). Never raises: a missing file, an unknown mode or a host without the
    command is (False, why)."""
    if mode not in MODES:
        return False, f"unknown receive mode {mode!r}: use {' or '.join(MODES)}"
    target = Path(str(path))
    if not target.is_file():
        return False, f"{target.name or path}: no such file"
    if target.suffix.lower() not in IMAGE_SUFFIXES:
        return False, f"{target.name}: not an image Max can show ({', '.join(IMAGE_SUFFIXES)})"
    try:
        runtime()
    except Exception:
        return False, "pymxs unavailable (not running inside 3ds Max)"
    absolute = str(target.resolve())
    try:
        if mode == "vfb_history":
            return _load_vfb_history(absolute)
        return _load_viewport_background(absolute)
    except Exception as exc:  # the dock shows it; nothing in the scene was changed
        return False, f"receive failed: {type(exc).__name__}: {exc}"
