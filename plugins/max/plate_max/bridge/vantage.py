"""Chaos Vantage: the .vrscene handoff and the live-link toggle. No command-line batch.

Stock Vantage 2.0+ removed command-line rendering (-sceneFile/-outputFile exist only in the paid
Developer Edition; MaxGaffer's vantage.py, verified against the Chaos docs 2026-07-16), so PLATE never
drives a vantage_console: per-camera .vrscene exports go into Vantage's in-app batch queue, and the
live link mirrors the scene while the artist works. Seeds: MaxGaffer maxbridge/vantage.py
(start_live_link, export_vrscene) and MaxDirector maxbridge/vantage.py (frame arguments are mandatory:
without startFrame/endFrame vrayExportVRScene writes no animation).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..rt import runtime
from .scene import current_frame, get_camera, rt_attr, set_active_camera, viewport_camera

# MAXScript globals that start the live link on some V-Ray builds (TODO-verify; the action-table
# scan below is the fallback that finds "Initiate a Live-Link to Chaos Vantage" by its text).
LIVE_LINK_GLOBALS = (
    "vantageStartLiveLink()",
    "startVantageLiveLink()",
    "vrayStartVantageLiveLink()",
    "vray_startVantageLiveLink()",
)
EXPORTER = "vrayExportVRScene"  # TODO-verify: keyword names startFrame/endFrame/exportCompressed


def export_vrscene(path: Path | str, camera: Any, f0: int | None = None, f1: int | None = None) -> str | None:
    """Export the scene as a .vrscene seen through camera (a node or a name), frames f0..f1.

    f0 None is the current frame (a still); f1 None is f0. The frame range is always passed:
    vrayExportVRScene without startFrame/endFrame writes no animation. The active viewport camera is
    put back afterwards. Returns the path, or None when the exporter is missing, the camera is
    unknown, or no file appeared."""
    target = Path(path)
    if ":" in target.name:
        raise ValueError(f"{target.name!r}: ':' is not allowed in a file name")
    exporter = rt_attr(EXPORTER)
    if not callable(exporter):
        return None
    node = get_camera(camera)
    if node is None:
        return None
    if f0 is None:
        f0 = current_frame() or 0
    f1 = f0 if f1 is None else f1
    if int(f1) < int(f0):
        raise ValueError(f"frame range {f0}..{f1} runs backwards")
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = viewport_camera()
    if not set_active_camera(node):
        return None
    try:
        try:  # compressed keeps multi-GB interiors manageable (MaxGaffer)
            exporter(str(target), exportCompressed=True, startFrame=int(f0), endFrame=int(f1))
        except Exception:
            try:
                exporter(str(target), startFrame=int(f0), endFrame=int(f1))
            except Exception:
                return None
    finally:
        if previous is not None and previous is not node:
            set_active_camera(previous)
    return str(target) if target.is_file() else None


def start_live_link() -> tuple[bool, str]:
    """Run V-Ray's "Initiate a Live-Link to Chaos Vantage" (a toggle: it also stops an active link;
    port 20701, 20703 on V-Ray 7.3 DR2). (executed, how or why not). Degrades off-Max."""
    try:
        rt = runtime()
    except Exception:
        return False, "pymxs unavailable (not running inside 3ds Max)"
    for expr in LIVE_LINK_GLOBALS:
        try:
            rt.execute(expr)
        except Exception:
            continue
        return True, f"maxscript global {expr}"
    hit = _find_live_link_action()
    if hit is not None:
        action, label = hit
        try:
            action.execute()
            return True, f"actionMan: {label}"
        except Exception as exc:
            return False, f"found action {label!r} but execute failed: {exc}"
    return False, (
        "no live-link entry point found: start it once from the V-Ray menu (Chaos Vantage live link); "
        "the link then mirrors the scene"
    )


def _find_live_link_action() -> tuple[Any, str] | None:
    """Scan actionMan for an action whose text mentions Vantage and live/link (TODO-verify the
    actionMan / getActionTable / getDescriptionText names on Max 2026)."""
    action_man = rt_attr("actionMan")
    try:
        tables = int(action_man.numActionTables)
    except Exception:
        return None
    for t in range(1, tables + 1):
        try:
            table = action_man.getActionTable(t)
            count = int(table.numActions)
        except Exception:
            continue
        for a in range(1, count + 1):
            try:
                action = table.getAction(a)
            except Exception:
                continue
            label = ""
            for getter in ("getDescriptionText", "getButtonText", "getMenuText"):
                try:
                    label = str(getattr(action, getter)())
                except Exception:
                    continue
                if label:
                    break
            low = label.lower()
            if "vantage" in low and ("live" in low or "link" in low):
                return action, label
    return None
