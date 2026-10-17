"""Batch Cameras: Send Packet for a list of per-camera recipes, unattended, with a summary file.

Each recipe goes through send_packet.run on its own (its own recipe, its own daemon check), so one
camera's failure never stops the next. Progress is reported per camera and per stage through
options.progress(done, total, message). When the batch ends, batch_<YYYYmmdd-HHMMSS>.json (no ':'
in the name) lands in out_root with every result, written temp-then-os.replace as UTF-8 JSON.
The packets record render.session "batch".
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_json
from ..recipes import CameraRecipe, RecipeError
from . import send_packet

SUMMARY_PREFIX = "batch_"
FORMAT_VERSION = 1


def _stamp(now: float | None = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now))


def _utc(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def summary_path(out_root: Path | str, stamp: str | None = None) -> Path:
    """out_root/batch_<stamp>.json, with -2, -3 ... when a batch already wrote that second."""
    root = Path(out_root)
    stamp = stamp or _stamp()
    path = root / f"{SUMMARY_PREFIX}{stamp}.json"
    n = 2
    while path.exists():
        path = root / f"{SUMMARY_PREFIX}{stamp}-{n}.json"
        n += 1
    return path


def run(
    recipes: Any,
    out_root: Path | str,
    daemon_base: str | None = None,
    options: Any = None,
) -> list[send_packet.SendResult]:
    """send_packet.run for each recipe (a CameraRecipe, a dict of its fields, or a camera name),
    in order. Returns the SendResults; the summary path is on each result's batch_summary and in the
    last progress message. Never raises across recipes."""
    opts = send_packet.normalise_options(options)
    progress = opts.get("progress")
    items = [recipes] if isinstance(recipes, (str, CameraRecipe, dict)) else list(recipes or [])
    total = len(items)
    started = time.time()
    results: list[send_packet.SendResult] = []
    for index, item in enumerate(items):
        try:
            recipe = CameraRecipe.coerce(item) if not isinstance(item, str) else None
            camera = recipe.camera if recipe is not None else item
        except (RecipeError, TypeError) as exc:
            label = item.get("camera") if isinstance(item, dict) else getattr(item, "camera", None)
            result = send_packet.SendResult(camera=str(label or "?"), stage="recipe")
            result.errors.append(f"recipe: {exc}")
            results.append(result)
            send_packet.notify_progress(progress, index + 1, total, f"{result.camera}: recipe refused")
            continue

        def relay(_done: int, _total: int, message: str, _index: int = index) -> None:
            send_packet.notify_progress(progress, _index, total, message)

        per = dict(opts)
        per["progress"] = relay
        per.setdefault("session", "batch")
        recipes_map = dict(per.get("recipes") or {})
        if recipe is not None:
            recipes_map[camera] = recipe
        per["recipes"] = recipes_map
        try:
            got = send_packet.run([camera], out_root, daemon_base, per)
        except Exception as exc:  # send_packet.run does not raise; this is a last net
            got = [send_packet.SendResult(camera=camera, errors=[f"{type(exc).__name__}: {exc}"])]
        results.extend(got)
        send_packet.notify_progress(
            progress,
            index + 1,
            total,
            f"{camera}: {'sent' if got and got[0].ok else 'failed'} ({index + 1}/{total})",
        )

    path = summary_path(out_root, _stamp(started))
    summary = {
        "format_version": FORMAT_VERSION,
        "started_utc": _utc(started),
        "finished_utc": _utc(),
        "out_root": str(Path(out_root)),
        "cameras": total,
        "sent": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "results": [r.as_dict() for r in results],
    }
    try:
        atomic_write_json(path, summary)
    except OSError as exc:
        for r in results:
            r.warnings.append(f"batch summary not written ({exc})")
    else:
        for r in results:
            r.batch_summary = str(path)
    send_packet.notify_progress(
        progress, total, total, f"batch done: {summary['sent']}/{total} sent, summary {path.name}"
    )
    return results
