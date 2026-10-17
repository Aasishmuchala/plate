"""Scene plumbing shared by the bridge: candidate-name property access, time evaluation, cameras.

Property names drift across V-Ray builds, so every read and write goes through a tuple of
candidates (the first property that exists wins) instead of one hard-coded name. get_prop/set_prop
are MaxGaffer's maxbridge/scene.py helpers, moved onto plate_max's single pymxs door (rt.runtime()).
at_time(frame) evaluates reads at a frame: pymxs.attime inside Max, a runtime-supplied attime in
tests, else the time slider moved and put back.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ..rt import runtime

_MISSING = object()


# --------------------------------------------------------------------------- candidates plumbing


def has_prop(obj: Any, name: str) -> bool:
    """rt.isProperty(obj, #name), False for anything that raises (pymxs does for odd objects)."""
    if obj is None:
        return False
    rt = runtime()
    try:
        return bool(rt.isProperty(obj, rt.Name(name)))
    except Exception:
        return False


def get_prop(obj: Any, names: tuple[str, ...], default: Any = None) -> Any:
    """Value of the first property in names that obj has, else default."""
    for name in names:
        if not has_prop(obj, name):
            continue
        try:
            return getattr(obj, name)
        except Exception:
            continue
    return default


def find_prop(obj: Any, names: tuple[str, ...]) -> tuple[str | None, Any]:
    """(name, value) of the first property in names that obj has, (None, None) when none does."""
    for name in names:
        if not has_prop(obj, name):
            continue
        try:
            return name, getattr(obj, name)
        except Exception:
            continue
    return None, None


def set_prop(obj: Any, names: tuple[str, ...], value: Any) -> str | None:
    """Set the first existing property; returns the name used (None = none existed or all raised)."""
    for name in names:
        if not has_prop(obj, name):
            continue
        try:
            setattr(obj, name, value)
        except Exception:
            continue
        return name
    return None


def rt_attr(name: str, default: Any = None) -> Any:
    """getattr(rt, name, default) that also survives a runtime raising something other than
    AttributeError for an undefined MAXScript global."""
    try:
        return getattr(runtime(), name)
    except Exception:
        return default


def class_name(obj: Any) -> str:
    try:
        return str(runtime().classOf(obj))
    except Exception:
        return ""


# --------------------------------------------------------------------------- time


@contextmanager
def at_time(frame: Any) -> Iterator[None]:
    """Evaluate every pymxs read inside the block at frame (None: the current time, no change).

    Inside Max this is pymxs.attime(frame), the module rt.runtime() already imported (looked up in
    sys.modules so pymxs keeps exactly one import site; TODO-verify that attime rounds a fractional
    frame the way the render does, ONBOX.md step 1). A test runtime may offer its own attime(frame)
    context manager. Otherwise the time slider is moved to frame and put back afterwards."""
    if frame is None:
        yield
        return
    rt = runtime()
    module = sys.modules.get("pymxs")
    if module is not None and getattr(module, "runtime", None) is rt and hasattr(module, "attime"):
        with module.attime(frame):
            yield
        return
    hook = rt_attr("attime")
    if callable(hook):
        with hook(frame):
            yield
        return
    previous = rt_attr("sliderTime", _MISSING)
    rt.sliderTime = frame
    try:
        yield
    finally:
        if previous is not _MISSING:
            try:
                rt.sliderTime = previous
            except Exception:
                pass


def current_frame() -> int | None:
    value = rt_attr("currentTime")
    raw = getattr(value, "frame", value)
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- cameras


def get_camera(camera: Any) -> Any:
    """A camera node from a node or a name (None when the name is unknown)."""
    if camera is None or not isinstance(camera, str):
        return camera
    try:
        return runtime().getNodeByName(camera, exact=True)
    except Exception:
        return None


def viewport_camera() -> Any:
    """The active viewport's camera (rt.viewport.getCamera(), TODO-verify), None for a perspective view."""
    viewport = rt_attr("viewport")
    try:
        return viewport.getCamera()
    except Exception:
        return None


def set_active_camera(camera: Any) -> bool:
    """Put camera in the active viewport (what vrayExportVRScene and the live link export from)."""
    node = get_camera(camera)
    if node is None:
        return False
    viewport = rt_attr("viewport")
    try:
        viewport.setCamera(node)
    except Exception:
        return False
    redraw = rt_attr("redrawViews")
    if callable(redraw):
        try:
            redraw()
        except Exception:
            pass
    return True


def scene_path() -> str:
    """Absolute path of the open .max file, "" when the scene was never saved."""
    folder = str(rt_attr("maxFilePath") or "")
    name = str(rt_attr("maxFileName") or "")
    if not name:
        return ""
    if folder and not folder.endswith(("/", "\\")):
        folder += "\\" if "\\" in folder else "/"
    return folder + name
