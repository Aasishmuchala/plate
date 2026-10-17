"""Light: MaxGaffer's reference-image lighting match for one camera, then a new packet that supersedes
the old one, plus the Vantage live-link preview.

MaxGaffer is a separate install inside the same Max session; this module only wraps its public api
(maxgaffer/api.py) when that is importable:

  match(camera, reference_path, locks, deep)  api.match_camera(camera, reference_path=..., log=...,
                                              should_cancel=..., locks=..., sweep=True, deep=...)
  apply(camera)                               api.apply_camera_state(camera): the saved best state back
  rerender_packet(camera, out_root, ...)      send_packet.run([camera], ..., supersedes=<old packet>)
  vantage_preview(camera)                     the camera into the viewport, then
                                              bridge.vantage.start_live_link() (a toggle in V-Ray)
  run(cameras, out_root, ...)                 match, apply and re-render per camera (the dock's Light)

Without MaxGaffer every call reports MAXGAFFER_MISSING (as data, never raised) and available() says
so, so the dock keeps the Light button disabled. maxgaffer is looked up with importlib at call time,
never at import time: plate_max stays importable, and standard-library only, without it.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..bridge import vantage
from ..bridge.scene import get_camera, set_active_camera
from ..packet_id import PACKET_ID_RE
from . import send_packet

MAXGAFFER_MODULE = "maxgaffer.api"
MAXGAFFER_MISSING = "MAXGAFFER_MISSING"
MAXGAFFER_FAILED = "MAXGAFFER_FAILED"
NO_REFERENCE = "NO_REFERENCE"
NO_SAVED_STATE = "NO_SAVED_STATE"
RERENDER_FAILED = "RERENDER_FAILED"
CAMERA_MISSING = "CAMERA_MISSING"
LIVE_LINK_FAILED = "LIVE_LINK_FAILED"

LIGHT_ONLY_OPTIONS = ("references", "reference_path", "locks", "deep", "rerender", "log", "should_cancel")

Log = Callable[[str], None]


def _no_log(message: str) -> None:
    return None


def _never() -> bool:
    return False


def maxgaffer_api() -> Any:
    """MaxGaffer's api module, or None when MaxGaffer is not installed in this Max."""
    try:
        return importlib.import_module(MAXGAFFER_MODULE)
    except ImportError:
        return None
    except Exception:  # a broken MaxGaffer install is as good as none for the dock
        return None


def available() -> tuple[bool, str]:
    """(True, "") when MaxGaffer's api imports and has match_camera and apply_camera_state, else
    (False, why): the dock disables Light on False."""
    api = maxgaffer_api()
    if api is None:
        return (
            False,
            f"{MAXGAFFER_MISSING}: MaxGaffer is not installed in this 3ds Max (no {MAXGAFFER_MODULE})",
        )
    missing = [n for n in ("match_camera", "apply_camera_state") if not callable(getattr(api, n, None))]
    if missing:
        return False, f"{MAXGAFFER_MISSING}: {MAXGAFFER_MODULE} has no {', '.join(missing)}"
    return True, ""


def _missing(camera: str, why: str | None = None) -> dict[str, Any]:
    reason = why or available()[1] or f"{MAXGAFFER_MISSING}: MaxGaffer is not installed"
    return {"ok": False, "camera": camera, "code": MAXGAFFER_MISSING, "error": reason}


def match(
    camera_name: str,
    reference_path: str,
    locks: Any = None,
    deep: bool = False,
    log: Log | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """MaxGaffer's full match loop for camera_name against reference_path: {"ok": True, "camera",
    "score", "stop_reason", "iterations", "state", "run_dir", "renders", "polish_gain",
    "ceiling_converged"} (api.match_camera's answer), or {"ok": False, "code", "error"}."""
    ok, why = available()
    if not ok:
        return _missing(camera_name, why)
    if reference_path and not Path(str(reference_path)).is_file():
        return {
            "ok": False,
            "camera": camera_name,
            "code": NO_REFERENCE,
            "error": f"no file {reference_path}",
        }
    api = maxgaffer_api()
    try:
        answer = api.match_camera(
            camera_name,
            reference_path=str(reference_path or ""),
            log=log or _no_log,
            should_cancel=should_cancel or _never,
            locks=set(locks) if locks else None,
            sweep=True,
            deep=bool(deep),
        )
    except Exception as exc:
        return {
            "ok": False,
            "camera": camera_name,
            "code": MAXGAFFER_FAILED,
            "error": f"{type(exc).__name__}: {exc}",
        }
    out = dict(answer) if isinstance(answer, dict) else {"answer": answer}
    return {"ok": True, "camera": camera_name, **out}


def apply(camera_name: str) -> dict[str, Any]:
    """api.apply_camera_state(camera_name): {"ok": True, "camera", "warnings"}, or {"ok": False, "code",
    "error"} (NO_SAVED_STATE when MaxGaffer has no state for the camera yet)."""
    ok, why = available()
    if not ok:
        return _missing(camera_name, why)
    try:
        warnings = maxgaffer_api().apply_camera_state(camera_name)
    except RuntimeError as exc:
        return {"ok": False, "camera": camera_name, "code": NO_SAVED_STATE, "error": str(exc)}
    except Exception as exc:
        return {
            "ok": False,
            "camera": camera_name,
            "code": MAXGAFFER_FAILED,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"ok": True, "camera": camera_name, "warnings": [str(w) for w in warnings or []]}


# --------------------------------------------------------------------------- the new packet


def latest_packet(out_root: Path | str, camera: str) -> str | None:
    """The newest still packet of camera under out_root (packet ids are time-sortable ULIDs), or None."""
    root = Path(out_root)
    if not root.is_dir():
        return None
    best: str | None = None
    for manifest_path in root.glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict) or manifest.get("kind") != "still":
            continue
        if (manifest.get("camera") or {}).get("name") != camera:
            continue
        pid = str(manifest.get("packet_id"))
        if PACKET_ID_RE.match(pid) and (best is None or pid > best):
            best = pid
    return best


def rerender_packet(
    camera: str,
    out_root: Path | str,
    daemon_base: str | None = None,
    options: Any = None,
    supersedes: str | None = None,
) -> send_packet.SendResult:
    """Send camera again through send_packet.run with supersedes = the packet it replaces (given, else
    the newest still of this camera under out_root): the relit plate becomes a new packet and the
    daemon carries approved layers over where IDs are unchanged (decision 17)."""
    opts = send_packet.normalise_options(options)
    old = supersedes or opts.get("supersedes") or latest_packet(out_root, camera)
    if isinstance(old, dict):
        old = old.get(camera)
    if old:
        opts["supersedes"] = {camera: str(old)}
    else:
        opts.pop("supersedes", None)
    [result] = send_packet.run([camera], out_root, daemon_base, opts)
    return result


def vantage_preview(camera: str) -> dict[str, Any]:
    """Put camera in the active viewport and run V-Ray's "Initiate a Live-Link to Chaos Vantage" so
    the artist previews the light in Vantage. The command toggles: a second call stops the link.
    {"ok", "camera", "how" | "error", "warnings"}."""
    warnings: list[str] = []
    if get_camera(camera) is None:
        return {
            "ok": False,
            "camera": camera,
            "code": CAMERA_MISSING,
            "error": f"camera {camera!r} not found",
        }
    if not set_active_camera(camera):
        warnings.append(
            "could not put the camera in the active viewport; the live link mirrors the current view"
        )
    started, how = vantage.start_live_link()
    if not started:
        return {"ok": False, "camera": camera, "code": LIVE_LINK_FAILED, "error": how, "warnings": warnings}
    return {"ok": True, "camera": camera, "how": how, "warnings": warnings}


# --------------------------------------------------------------------------- the dock's Light


@dataclass
class LightResult:
    camera: str
    ok: bool = False
    code: str | None = None
    stage: str = "start"
    packet_id: str | None = None
    supersedes: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    match: dict[str, Any] | None = None
    send: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera": self.camera,
            "ok": self.ok,
            "code": self.code,
            "stage": self.stage,
            "packet_id": self.packet_id,
            "supersedes": self.supersedes,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "match": self.match,
            "send": self.send,
        }


def _per_camera(value: Any, camera: str) -> Any:
    return value.get(camera) if isinstance(value, dict) else value


def run(
    camera_names: Any,
    out_root: Path | str,
    daemon_base: str | None = None,
    options: Any = None,
) -> list[LightResult]:
    """Per camera: match against its reference (options.references {camera: path}, or
    options.reference_path for one), apply the best state, and (unless options.rerender is False)
    re-render a packet that supersedes the camera's newest still. options: references /
    reference_path, locks, deep, rerender, supersedes, progress, log, should_cancel, and everything
    send_packet.run takes. Without MaxGaffer every camera reports MAXGAFFER_MISSING. Never raises."""
    opts = send_packet.normalise_options(options)
    names = [camera_names] if isinstance(camera_names, str) else [str(c) for c in camera_names or []]
    progress = opts.get("progress")
    ok, why = available()
    results: list[LightResult] = []
    for index, camera in enumerate(names):
        result = LightResult(camera=camera)
        results.append(result)
        if not ok:
            result.code, result.stage, result.errors = MAXGAFFER_MISSING, "maxgaffer", [why]
            continue
        try:
            send_packet.notify_progress(progress, index, len(names), f"{camera}: matching the light")
            result.stage = "match"
            reference = _per_camera(opts.get("references"), camera) or opts.get("reference_path") or ""
            matched = match(
                camera,
                str(reference),
                locks=_per_camera(opts.get("locks"), camera),
                deep=bool(opts.get("deep", False)),
                log=opts.get("log"),
                should_cancel=opts.get("should_cancel"),
            )
            result.match = matched
            if not matched["ok"]:
                result.code, result.errors = matched["code"], [matched["error"]]
                continue
            result.stage = "apply"
            applied = apply(camera)
            if not applied["ok"]:
                result.code, result.errors = applied["code"], [applied["error"]]
                continue
            result.warnings.extend(applied["warnings"])
            if opts.get("rerender", True) is False:
                result.ok, result.stage = True, "done"
                continue
            result.stage = "rerender"
            send_opts = {k: v for k, v in opts.items() if k not in LIGHT_ONLY_OPTIONS}
            old = _per_camera(opts.get("supersedes"), camera) or latest_packet(out_root, camera)
            result.supersedes = str(old) if old else None
            sent = rerender_packet(camera, out_root, daemon_base, send_opts, result.supersedes)
            result.send = sent.as_dict()
            result.packet_id = sent.packet_id
            result.warnings.extend(sent.warnings)
            if not sent.ok:
                result.code = RERENDER_FAILED
                result.errors = [f"re-render: {e}" for e in sent.errors]
                continue
            result.ok, result.stage = True, "done"
        except Exception as exc:  # nothing a camera does stops the next one
            result.ok, result.code = False, MAXGAFFER_FAILED
            result.errors.append(f"{result.stage}: {type(exc).__name__}: {exc}")
        finally:
            send_packet.notify_progress(
                progress,
                index + 1,
                len(names),
                f"{camera}: {'relit ' + str(result.packet_id or '') if result.ok else 'failed at ' + result.stage}",
            )
    return results
