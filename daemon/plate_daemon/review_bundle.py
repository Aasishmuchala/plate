"""Client review bundles: a zip with a static HTML page, watermarked 8-bit proxies and gate badges.

BUILD_PLAN decision 21: the client reviews before the gate through a bundle that opens from `file://` with
no install, no server and no network. `export_review(project, run_ids, out_dir)` writes

    review-<bundle_id>.zip                     the client's file
        review-<bundle_id>/index.html          one file: CSS, JS and the take data inlined (vanilla JS, no CDN)
        review-<bundle_id>/README.txt          how to open it and how to send comments back
        review-<bundle_id>/manifest.json       bundle id, watermark, per-file BLAKE3 (no project or run ids)
        review-<bundle_id>/takes/r<N>_a.png    the plate (before), 8-bit sRGB, watermarked, <= max_width
        review-<bundle_id>/takes/r<N>_b.png    the candidate (after), same treatment, lineage iTXt kept
        review-<bundle_id>/takes/r<N>_gate.json  the gate badge for the take
    review-<bundle_id>.json                    the studio's copy, NOT in the zip: take -> run_id, project id,
                                               zip BLAKE3 and size, and the ledger head at export time

What the client page does: per take a before/after wipe (CSS `clip-path` and a range input), a before/after
toggle, a 100% (proxy pixel) zoom, a gate badge (pass, fail or advisory, with the first failing metric), and a
comment form that builds a `mailto:` link and a downloadable `comments.json`. It makes no network call: there
is no fetch, no XHR, no external font, script or image, and no credential anywhere in the bundle.

Proxies are derived from the stored images (16-bit PNG/TIFF, linear EXR, or an 8-bit file), never upscaled,
resampled in float, quantised once to 8 bits and tagged sRGB. The watermark (`REVIEW` by default; printable
ASCII) is tiled diagonally at 12% opacity so a screenshot cannot pass as a deliverable. The candidate's
lineage record (decision 26) is copied into the proxy as the `plate:lineage` iTXt chunk, so a proxy dropped
back onto PLATE still resolves to its run. File names carry the take number only: no project id, run id
or engine id. The lineage record itself holds those ids; pass `keep_lineage=False` for a bundle that must not.

Where a run's images come from (`resolve_take`): the run's ledger records carry them under `data`:

    result_key (or outputs[0])   the candidate's BLAKE3 in the project CAS
    plate_key                    the plate's BLAKE3 in the project CAS, or
    packet_id                    a packet registered in packets.json: its `display_tiff16`, else
                                 `client_plate`, else the `beauty_exr` re-derived to display colour

The gate badge comes from the newest `gates/<run_id>.<n>.json` (gate_reports.Service); a run with no report
shows "not scored". A caller that already knows the files passes `sources={run_id: TakeSource(...)}`.

Hosting on Cloudflare R2 behind signed, expiring URLs is documented only (review_template/HOSTING.md); this
module never uploads. It only reads the project folder and writes into `out_dir`: the project's `review/`
folder is the usual place, and a caller writing there while another process writes the project does not need
the project lock because nothing in the project's own files changes.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from blake3 import blake3

from . import lineage as lineage_mod
from .fsutil import read_json, replace_with_retry, temp_sibling, write_json_atomic
from .gate_reports import Service as GateReports
from .ledger import Ledger
from .projects import Project
from .timeutil import iso, utcnow

__all__ = [
    "MAX_WIDTH_LIMIT",
    "ReviewBundle",
    "ReviewError",
    "TakeInfo",
    "TakeSource",
    "build_review",
    "export_review",
    "gate_badge",
    "load_display",
    "make_proxy",
    "resolve_take",
]

SCHEMA = 1
TEMPLATE_DIR = Path(__file__).resolve().parent / "review_template"
DEFAULT_WATERMARK = "REVIEW"
WATERMARK_OPACITY = 0.12
WATERMARK_MAX_CHARS = 40
MAX_WIDTH_LIMIT = 2048  # the bundle never carries a proxy wider than this, whatever the caller asks
MIN_WIDTH = 64
STILL_SUFFIXES = (".png", ".tif", ".tiff", ".jpg", ".jpeg", ".exr")
VIDEO_SUFFIXES = (".mp4", ".mov")
PLATE_ROLES = ("display_tiff16", "client_plate", "beauty_exr")
REPLY_TO_RE = re.compile(r"^[^\s<>\"',;:?&=@]+@[^\s<>\"',;:?&=@]+$")
ZIP_DATE = (2026, 1, 1, 0, 0, 0)  # fixed, so the same inputs give the same zip bytes
STATUS_ORDER = {"fail": 0, "advisory": 1, "pass": 2}


class ReviewError(ValueError):
    """A run cannot go into a bundle (no image, wrong media, bad argument)."""


@dataclass(frozen=True)
class TakeSource:
    """The files behind one take. `plate_manifest` (a packet manifest) tells how to read a beauty EXR and,
    through `camera.safe_frame`, how to crop an overscan plate to the candidate's frame."""

    run_id: str
    plate: Path
    candidate: Path
    caption: str | None = None
    plate_manifest: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class TakeInfo:
    """What went into the bundle for one take (the studio's view: it holds the run id)."""

    label: str
    run_id: str
    a_file: str
    b_file: str
    gate_file: str
    width: int
    height: int
    badge: dict[str, Any]
    a_blake3: str
    b_blake3: str
    size_mismatch: bool = False


@dataclass(frozen=True)
class ReviewBundle:
    zip_path: Path
    size_bytes: int
    zip_blake3: str
    bundle_id: str
    sidecar_path: Path
    takes: list[TakeInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------- gate badge


def gate_badge(report: Mapping[str, Any] | None, *, report_n: int | None = None) -> dict[str, Any]:
    """The client-facing summary of a gate report: pass, fail or advisory, the first failing metric, counts.
    Thresholds and metric internals stay in the report; the badge names the first failing check and its value.
    No report gives `status: "none"` ("not scored")."""
    if not isinstance(report, Mapping):
        return {"status": "none", "label": "NOT SCORED", "first_failing": None, "n_checks": 0}
    checks = [c for c in (report.get("checks") or []) if isinstance(c, Mapping)]
    status = report.get("verdict")
    if status not in STATUS_ORDER:  # a plain dict report without a verdict: derive it like GateReport does
        if any(c.get("status") == "fail" for c in checks):
            status = "fail"
        elif not checks or any(c.get("status") == "advisory" for c in checks):
            status = "advisory"
        else:
            status = "pass"
    first = next((c for c in checks if c.get("status") == "fail"), None)
    if first is None and status == "advisory":
        first = next((c for c in checks if c.get("status") == "advisory"), None)
    first_failing = None
    if first is not None:
        value = first.get("value")
        first_failing = {
            "metric_id": str(first.get("metric_id", "?")),
            "value": value if isinstance(value, (int, float)) and not isinstance(value, bool) else None,
            "status": first.get("status"),
        }
    badge: dict[str, Any] = {
        "status": status,
        "label": status.upper(),
        "first_failing": first_failing,
        "n_checks": len(checks),
        "n_fail": sum(1 for c in checks if c.get("status") == "fail"),
        "n_advisory": sum(1 for c in checks if c.get("status") == "advisory"),
        "tier": report.get("tier") if isinstance(report.get("tier"), str) else None,
        "gate_version": report.get("gate_version"),
        "threshold_set_id": report.get("threshold_set_id"),
    }
    if report_n is not None:
        badge["report_n"] = report_n
    return badge


# ---------------------------------------------------------------------------- reading images


def _srgb_oetf(lin: np.ndarray) -> np.ndarray:
    v = np.clip(np.asarray(lin, dtype=np.float64), 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1.0 / 2.4) - 0.055).astype(np.float32)


def _exr_rgb(path: Path) -> np.ndarray:
    """Linear RGB (H, W, 3) float32 from the first R/G/B channel set in an EXR, any part or layer."""
    import OpenEXR

    exr = OpenEXR.File(str(path), separate_channels=True)
    planes: dict[str, np.ndarray] = {}
    for part in exr.parts:
        for name, ch in part.channels.items():
            planes[name] = ch.pixels
    for prefix in ("", "RGBA.", "beauty.", "Beauty."):
        if all(prefix + c in planes for c in "RGB"):
            return np.stack([planes[prefix + c] for c in "RGB"], axis=-1).astype(np.float32, copy=False)
    for name in sorted(planes):
        if name.endswith(".R") and name[:-2] + ".G" in planes and name[:-2] + ".B" in planes:
            stem = name[:-2]
            return np.stack([planes[f"{stem}.{c}"] for c in "RGB"], axis=-1).astype(np.float32, copy=False)
    raise ReviewError(f"{path.name}: no R, G, B channels to make a proxy from")


def load_display(path: str | Path, manifest: Mapping[str, Any] | None = None) -> np.ndarray:
    """An image file as display-referred sRGB float32 (H, W, 3) in [0, 1].

    PNG, TIFF and JPEG are read as stored (16-bit keeps its precision until the proxy is quantised); float
    TIFF and EXR are scene-linear and go through the sRGB transfer, or through the packet's OCIO pipeline when
    `manifest` is given and the file is its beauty EXR."""
    from plate_pixels import imageio

    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".exr":
        if manifest is not None and (manifest.get("colour") or {}).get("max_colour_mode") in (
            "ocio",
            "legacy_gamma",
        ):
            from plate_pixels.exr import read_elements
            from plate_pixels.ocio import Pipeline

            beauty = read_elements(p, manifest, ["beauty"])["beauty"][..., :3]
            rgb = Pipeline.from_manifest(manifest).rederive_display(beauty) / 255.0
            return np.ascontiguousarray(np.clip(rgb, 0.0, 1.0), dtype=np.float32)
        return np.ascontiguousarray(_srgb_oetf(_exr_rgb(p)))
    if suffix not in STILL_SUFFIXES:
        raise ReviewError(
            f"{p.name}: a review bundle holds stills (png, tif, jpg, exr), not {suffix or 'this file'}"
        )
    img = imageio.read(p)
    rgb = np.clip(np.nan_to_num(img.rgb, nan=0.0), 0.0, None)
    if img.encoding == "linear":
        rgb = _srgb_oetf(rgb)
    return np.ascontiguousarray(np.clip(rgb, 0.0, 1.0), dtype=np.float32)


# ---------------------------------------------------------------------------- the proxy


def check_watermark(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("a review bundle is always watermarked: watermark must be non-empty text")
    if len(text) > WATERMARK_MAX_CHARS or not all(32 <= ord(c) < 127 for c in text):
        raise ValueError(f"watermark must be printable ASCII, at most {WATERMARK_MAX_CHARS} characters")
    return text.strip()


def _watermark_alpha(h: int, w: int, text: str) -> tuple[np.ndarray, np.ndarray]:
    """(ink, shadow) coverage masks in [0, 1], text tiled on a 28 degree diagonal across an h x w frame."""
    import cv2

    font = cv2.FONT_HERSHEY_DUPLEX
    scale = max(0.5, w / 1500.0 * 1.3)
    thick = max(1, int(round(scale * 2)))
    (tw, th), base = cv2.getTextSize(text, font, scale, thick)
    step_x = int(tw * 1.9) + 1
    step_y = int((th + base) * 5.0) + 1
    side = int(np.ceil(np.hypot(h, w))) + step_x
    canvas = np.zeros((side, side), np.uint8)
    for row, y in enumerate(range(th, side + step_y, step_y)):
        offset = (row % 2) * (step_x // 2)
        for x in range(-step_x + offset, side, step_x):
            cv2.putText(canvas, text, (x, y), font, scale, 255, thick, cv2.LINE_AA)
    rot = cv2.getRotationMatrix2D((side / 2.0, side / 2.0), 28.0, 1.0)
    canvas = cv2.warpAffine(canvas, rot, (side, side), flags=cv2.INTER_LINEAR)
    y0, x0 = (side - h) // 2, (side - w) // 2
    ink = canvas[y0 : y0 + h, x0 : x0 + w].astype(np.float32) / 255.0
    shadow = cv2.dilate(ink, np.ones((3, 3), np.uint8))
    return ink, shadow


def make_proxy(
    rgb: np.ndarray, *, max_width: int = MAX_WIDTH_LIMIT, watermark: str | None = DEFAULT_WATERMARK
) -> np.ndarray:
    """Display float (H, W, 3) -> proxy float in [0, 1]: downscaled to `max_width` when wider (area filter,
    never upscaled) and watermarked. Quantise once, when writing (`imageio.write_png8`)."""
    import cv2

    a = np.ascontiguousarray(np.clip(np.asarray(rgb, dtype=np.float32), 0.0, 1.0))
    h, w = a.shape[:2]
    if w > max_width:
        nh = max(1, int(round(h * max_width / w)))
        a = np.ascontiguousarray(cv2.resize(a, (max_width, nh), interpolation=cv2.INTER_AREA))
        h, w = nh, max_width
    if watermark is not None:
        ink, shadow = _watermark_alpha(h, w, check_watermark(watermark))
        a = a * (1.0 - WATERMARK_OPACITY * shadow[..., None])  # a faint dark edge so it reads on white
        a = a + WATERMARK_OPACITY * ink[..., None] * (1.0 - a)  # then the white ink toward 1.0
    return np.ascontiguousarray(np.clip(a, 0.0, 1.0), dtype=np.float32)


def _write_proxy(
    rgb: np.ndarray, dest: Path, source: Path, *, max_width: int, watermark: str, keep_lineage: bool
) -> tuple[int, int]:
    from plate_pixels import imageio

    proxy = make_proxy(rgb, max_width=max_width, watermark=watermark)
    imageio.write_png8(dest, proxy)
    if keep_lineage:
        lin = lineage_mod.read(source)
        if lin is not None:
            lineage_mod.embed(dest, lin)
    return int(proxy.shape[1]), int(proxy.shape[0])


# ---------------------------------------------------------------------------- finding a run's files


def _run_data(project: Project, run_id: str) -> dict[str, Any]:
    records, _ = project.ledger_records()
    merged: dict[str, Any] = {}
    seen = False
    for rec in records:
        if rec.get("run_id") != run_id:
            continue
        seen = True
        data = rec.get("data")
        if isinstance(data, dict):
            merged.update(data)
    if not seen:
        raise ReviewError(f"run {run_id} is not in this project's ledger")
    return merged


def _inside(root: Path, candidate: Path) -> Path:
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (ValueError, OSError):
        raise ReviewError(f"{candidate} is outside {root}") from None
    return resolved


def _packet_plate(project: Project, packet_id: str) -> tuple[Path, dict[str, Any]]:
    entry = project.packets().get(packet_id)
    if entry is None:
        raise ReviewError(f"packet {packet_id} is not registered in this project")
    folder = Path(entry["path"])
    if not folder.is_absolute():
        folder = project.root / folder
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        raise ReviewError(f"packet {packet_id}: {manifest_path} is missing")
    manifest = read_json(manifest_path)
    files = manifest.get("files") or {}
    for role in PLATE_ROLES:
        meta = files.get(role)
        if isinstance(meta, dict) and meta.get("path"):
            return _inside(folder, folder / meta["path"]), manifest
    raise ReviewError(f"packet {packet_id} has no display image ({', '.join(PLATE_ROLES)})")


def resolve_take(project: Project, run_id: str) -> TakeSource:
    """The plate and candidate files of a run, from its ledger records (see the module docstring)."""
    data = _run_data(project, run_id)
    cas = project.cas()
    key = data.get("result_key")
    if not key and isinstance(data.get("outputs"), list) and data["outputs"]:
        key = data["outputs"][0]
    if not key:
        raise ReviewError(f"run {run_id} has no result in the ledger (data.result_key)")
    candidate = cas.find(str(key))
    if candidate is None:
        raise ReviewError(f"run {run_id}: result {str(key)[:12]} is not in the project CAS")
    manifest: dict[str, Any] | None = None
    if data.get("plate_key"):
        plate = cas.find(str(data["plate_key"]))
        if plate is None:
            raise ReviewError(f"run {run_id}: plate {str(data['plate_key'])[:12]} is not in the project CAS")
    elif data.get("packet_id"):
        plate, manifest = _packet_plate(project, str(data["packet_id"]))
    else:
        raise ReviewError(f"run {run_id} has no plate in the ledger (data.plate_key or data.packet_id)")
    return TakeSource(run_id=run_id, plate=plate, candidate=candidate, plate_manifest=manifest)


def _crop_to_safe_frame(
    plate: np.ndarray, manifest: Mapping[str, Any] | None, size: tuple[int, int]
) -> np.ndarray:
    """An overscan plate (16:9 vendor frame) is cropped to the deliverable safe frame when the candidate has
    exactly the safe frame's size; anything else is left alone and flagged by the size check."""
    if manifest is None:
        return plate
    sf = (manifest.get("camera") or {}).get("safe_frame")
    if not isinstance(sf, dict):
        return plate
    try:
        x, y, w, h = (int(sf[k]) for k in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return plate
    ph, pw = plate.shape[:2]
    if (w, h) == size and (pw, ph) != size and x >= 0 and y >= 0 and x + w <= pw and y + h <= ph:
        return np.ascontiguousarray(plate[y : y + h, x : x + w])
    return plate


# ---------------------------------------------------------------------------- the page


_SLOT = re.compile(r"<!--PLATE:(CSS|JS|DATA)-->|\{\{TITLE\}\}")


def _json_for_script(obj: Any) -> str:
    text = json.dumps(obj, ensure_ascii=True, separators=(",", ":"), sort_keys=False, allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _render_index(title: str, data: dict[str, Any]) -> str:
    skeleton = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")
    css = (TEMPLATE_DIR / "review.css").read_text(encoding="utf-8")
    js = (TEMPLATE_DIR / "review.js").read_text(encoding="utf-8")
    slots = {"CSS": css, "JS": js, "DATA": _json_for_script(data)}

    def fill(m: re.Match[str]) -> str:
        return html.escape(title) if m.group(0) == "{{TITLE}}" else slots[m.group(1)]

    return _SLOT.sub(fill, skeleton)


def _render_readme(title: str, reply_to: str | None, watermark: str, n_takes: int) -> str:
    text = (TEMPLATE_DIR / "README.txt").read_text(encoding="utf-8")
    reply = f"Send it to {reply_to}." if reply_to else "Send it back to the studio that sent you this review."
    plain = "".join(c if 32 <= ord(c) < 127 else "?" for c in title)
    for key, value in (
        ("{{TITLE}}", plain),
        ("{{REPLY}}", reply),
        ("{{WATERMARK}}", watermark),
        ("{{TAKES}}", str(n_takes)),
    ):
        text = text.replace(key, value)
    return text


# ---------------------------------------------------------------------------- the exporter


def _as_project(project: Project | str | Path) -> Project:
    return project if isinstance(project, Project) else Project.load(project)


def _check_args(run_ids: Sequence[str], max_width: int, reply_to: str | None) -> list[str]:
    if isinstance(run_ids, str) or not run_ids:
        raise ValueError("run_ids must be a non-empty list of run ids")
    ordered = list(dict.fromkeys(run_ids))
    if not all(isinstance(r, str) and r for r in ordered):
        raise ValueError("run_ids must be non-empty strings")
    if (
        isinstance(max_width, bool)
        or not isinstance(max_width, int)
        or not MIN_WIDTH <= max_width <= MAX_WIDTH_LIMIT
    ):
        raise ValueError(f"max_width must be a whole number from {MIN_WIDTH} to {MAX_WIDTH_LIMIT}")
    if reply_to is not None and not REPLY_TO_RE.match(reply_to):
        raise ValueError(f"reply_to {reply_to!r} is not a plain e-mail address")
    return ordered


def _file_hash(path: Path) -> str:
    return blake3(path.read_bytes()).hexdigest()


def build_review(
    project: Project | str | Path,
    run_ids: Sequence[str],
    out_dir: str | Path,
    watermark: str = DEFAULT_WATERMARK,
    max_width: int = MAX_WIDTH_LIMIT,
    *,
    title: str = "Review",
    reply_to: str | None = None,
    sources: Mapping[str, TakeSource] | None = None,
    keep_lineage: bool = True,
    clock=utcnow,
) -> ReviewBundle:
    """Build the bundle and return what was written. `export_review` is this, reduced to the zip path."""
    proj = _as_project(project)
    order = _check_args(run_ids, max_width, reply_to)
    watermark = check_watermark(watermark)
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        raise ValueError("title must be 1 to 120 characters")
    title = title.strip()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gates = GateReports(proj.root)

    stage = out / f".build-{uuid.uuid4().hex[:12]}"
    (stage / "takes").mkdir(parents=True)
    try:
        takes: list[TakeInfo] = []
        page_takes: list[dict[str, Any]] = []
        for n, run_id in enumerate(order, start=1):
            src = sources[run_id] if sources and run_id in sources else resolve_take(proj, run_id)
            if src.candidate.suffix.lower() in VIDEO_SUFFIXES:
                raise ReviewError(f"run {run_id}: video takes are not in review bundles yet (stills only)")
            label = f"r{n}"
            cand = load_display(src.candidate)
            plate = load_display(src.plate, src.plate_manifest)
            plate = _crop_to_safe_frame(plate, src.plate_manifest, (cand.shape[1], cand.shape[0]))
            a_path, b_path = stage / "takes" / f"{label}_a.png", stage / "takes" / f"{label}_b.png"
            aw, ah = _write_proxy(
                plate, a_path, src.plate, max_width=max_width, watermark=watermark, keep_lineage=keep_lineage
            )
            bw, bh = _write_proxy(
                cand,
                b_path,
                src.candidate,
                max_width=max_width,
                watermark=watermark,
                keep_lineage=keep_lineage,
            )
            report = gates.latest(run_id)
            badge = gate_badge(report.report if report else None, report_n=report.n if report else None)
            if report is not None:
                badge["shipped"] = report.shipped
            mismatch = (aw, ah) != (bw, bh)
            gate_name = f"{label}_gate.json"
            (stage / "takes" / gate_name).write_text(
                json.dumps({"take": label, **badge}, indent=1, allow_nan=False) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            info = TakeInfo(
                label=label,
                run_id=run_id,
                a_file=f"takes/{a_path.name}",
                b_file=f"takes/{b_path.name}",
                gate_file=f"takes/{gate_name}",
                width=bw,
                height=bh,
                badge=badge,
                a_blake3=_file_hash(a_path),
                b_blake3=_file_hash(b_path),
                size_mismatch=mismatch,
            )
            takes.append(info)
            page_takes.append(
                {
                    "label": label,
                    "name": src.caption or f"Take {n}",
                    "a": info.a_file,
                    "b": info.b_file,
                    "width": bw,
                    "height": bh,
                    "a_size": [aw, ah],
                    "size_mismatch": mismatch,
                    "badge": badge,
                }
            )

        identity = {
            "takes": [{"a": t.a_blake3, "b": t.b_blake3, "badge": t.badge} for t in takes],
            "watermark": watermark,
            "title": title,
            "max_width": max_width,
        }
        bundle_id = blake3(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        folder = f"review-{bundle_id}"
        created = iso(clock())
        page = {
            "schema": SCHEMA,
            "bundle_id": bundle_id,
            "title": title,
            "reply_to": reply_to,
            "watermark": watermark,
            "created_utc": created,
            "takes": page_takes,
        }
        (stage / "index.html").write_text(_render_index(title, page), encoding="utf-8", newline="\n")
        (stage / "README.txt").write_text(
            _render_readme(title, reply_to, watermark, len(takes)), encoding="utf-8", newline="\n"
        )
        manifest = {
            "schema": SCHEMA,
            "bundle_id": bundle_id,
            "created_utc": created,
            "watermark": watermark,
            "max_width": max_width,
            "files": {
                "index.html": _file_hash(stage / "index.html"),
                **{t.a_file: t.a_blake3 for t in takes},
                **{t.b_file: t.b_blake3 for t in takes},
            },
            "takes": [
                {"label": t.label, "a": t.a_file, "b": t.b_file, "gate": t.gate_file, "badge": t.badge}
                for t in takes
            ],
        }
        write_json_atomic(stage / "manifest.json", manifest, indent=1)

        zip_path = out / f"{folder}.zip"
        tmp_zip = temp_sibling(zip_path)
        try:
            with zipfile.ZipFile(tmp_zip, "w") as zf:
                for path in sorted(p for p in stage.rglob("*") if p.is_file()):
                    arc = f"{folder}/{path.relative_to(stage).as_posix()}"
                    zi = zipfile.ZipInfo(arc, date_time=ZIP_DATE)
                    zi.compress_type = zipfile.ZIP_STORED if path.suffix == ".png" else zipfile.ZIP_DEFLATED
                    zi.external_attr = 0o644 << 16
                    zf.writestr(zi, path.read_bytes())
            replace_with_retry(tmp_zip, zip_path)
        finally:
            tmp_zip.unlink(missing_ok=True)

        size = zip_path.stat().st_size
        zip_hash = _file_hash(zip_path)
        seq, head = Ledger(proj.root).head()
        sidecar = out / f"{folder}.json"
        write_json_atomic(
            sidecar,
            {
                "schema": SCHEMA,
                "bundle_id": bundle_id,
                "project_id": proj.project_id,
                "nda": proj.nda,
                "created_utc": created,
                "zip": zip_path.name,
                "zip_blake3": zip_hash,
                "size_bytes": size,
                "watermark": watermark,
                "max_width": max_width,
                "keep_lineage": keep_lineage,
                "ledger_head": {"seq": seq, "hash": head},
                "takes": [
                    {
                        "label": t.label,
                        "run_id": t.run_id,
                        "status": t.badge["status"],
                        "size_mismatch": t.size_mismatch,
                    }
                    for t in takes
                ],
            },
            indent=1,
        )
        return ReviewBundle(
            zip_path=zip_path,
            size_bytes=size,
            zip_blake3=zip_hash,
            bundle_id=bundle_id,
            sidecar_path=sidecar,
            takes=takes,
        )
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def export_review(
    project: Project | str | Path,
    run_ids: Sequence[str],
    out_dir: str | Path,
    watermark: str = DEFAULT_WATERMARK,
    max_width: int = MAX_WIDTH_LIMIT,
    **options: Any,
) -> Path:
    """Write the review bundle for `run_ids` into `out_dir` and return the zip's path.

    `options` are `build_review`'s keyword-only arguments: `title`, `reply_to`, `sources`, `keep_lineage`,
    `clock`. Use `build_review` when the size, bundle id or per-take detail is wanted."""
    return build_review(project, run_ids, out_dir, watermark, max_width, **options).zip_path
