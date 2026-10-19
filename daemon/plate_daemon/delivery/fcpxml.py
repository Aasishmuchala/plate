"""FCPXML 1.10 timelines: masters on the spine, alpha ProRes (and any other layer) as connected clips.

Port of STAFFAGE's `fcpxml.ts` with its escaping bug fixed: file URLs there were `file://` + `encodeURI`, which
leaves `&` and `#` alone and put a raw `&` into an attribute. Here every URL comes from `Path.as_uri()`
(percent-encoded, drive-letter aware on Windows) and the document is built with `xml.etree`, so names and
URLs are escaped by the serialiser (`Bar & Grill` -> `Bar &amp; Grill`).

    timeline = {
        "name": "Amphi plaza reel", "fps": Fraction(24000, 1001), "width": 3840, "height": 2160,
        "clips": [
            {"path": "C:/p/master_01.mov", "start_s": 0, "duration_s": 4.5, "alpha_path": "C:/p/people_01.mov"},
            {"path": "C:/p/master_02.mov", "start_s": 4.5, "duration_s": 3},
            {"path": "C:/p/titles.mov", "start_s": 5, "duration_s": 2, "lane": 2},   # connected, lane 2
        ],
    }
    write(timeline, "reel.fcpxml")

`start_s` is the position on the timeline. A clip without `lane` goes on the spine, in order (a gap before
`start_s` becomes a `<gap>`; going backwards is an error). A clip with a non-zero `lane` is connected to the
spine clip (or gap) it starts inside. `alpha_path` adds a lane-1 connected clip over the same interval.
Time is exact: `frameDuration` is 1/fps as a reduced rational (`1001/24000s` for 23.976), every offset and
duration is a whole number of frames, and the sequence is NDF with `colorSpace="1-1-1 (Rec. 709)"`.

`validate` checks the written document with `xml.etree` against the elements and attributes FCPXML 1.10
requires of this subset. `write` also refuses a timeline that references a delivered file whose sidecar says
previs, or GATE_FAIL without an override.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..export import claim
from ..fsutil import write_bytes_atomic
from .guard import DeliveryError, DeliveryRefused, Provenance, authorize, read_sidecar, write_sidecar
from .tools import to_fraction

__all__ = ["COLOR_SPACE", "FcpxmlError", "VERSION", "build", "validate", "write"]

VERSION = "1.10"
COLOR_SPACE = "1-1-1 (Rec. 709)"
_TIME = re.compile(r"^(\d+)(?:/(\d+))?s$")
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


class FcpxmlError(DeliveryError):
    pass


def _clean(text: str) -> str:
    return _XML_ILLEGAL.sub("", text)


@dataclass
class _Clip:
    path: Path
    name: str
    start: int  # frames on the timeline
    frames: int
    lane: int
    alpha_path: Path | None


class _Clock:
    def __init__(self, fps: Fraction):
        self.fps = fps
        self.fd = 1 / fps  # frame duration, seconds

    def frames(self, seconds: Any, what: str) -> int:
        try:
            s = (
                Fraction(seconds).limit_denominator(1_000_000)
                if not isinstance(seconds, Fraction)
                else seconds
            )
        except (TypeError, ValueError, ZeroDivisionError) as e:
            raise FcpxmlError(f"{what} {seconds!r} is not a number of seconds") from e
        if s < 0:
            raise FcpxmlError(f"{what} {seconds!r} is negative")
        return int(round(s * self.fps))

    def t(self, frames: int) -> str:
        if frames == 0:
            return "0s"
        t = Fraction(frames) * self.fd
        return f"{t.numerator}s" if t.denominator == 1 else f"{t.numerator}/{t.denominator}s"


def _uri(path: Path) -> str:
    return path.absolute().as_uri()


def _clips(timeline: Mapping[str, Any], clock: _Clock) -> list[_Clip]:
    raw = timeline.get("clips")
    if not raw:
        raise FcpxmlError("the timeline has no clips")
    out: list[_Clip] = []
    for i, c in enumerate(raw):
        if "path" not in c or "duration_s" not in c:
            raise FcpxmlError(f"clip {i} needs 'path' and 'duration_s'")
        path = Path(c["path"])
        frames = max(1, clock.frames(c["duration_s"], f"clip {i} duration_s"))
        start = c.get("start_s")
        lane = int(c.get("lane") or 0)
        alpha = Path(c["alpha_path"]) if c.get("alpha_path") else None
        out.append(
            _Clip(
                path=path,
                name=_clean(str(c.get("name") or path.stem)),
                start=-1 if start is None else clock.frames(start, f"clip {i} start_s"),
                frames=frames,
                lane=lane,
                alpha_path=alpha,
            )
        )
    return out


def _referenced(clips: Iterable[_Clip]) -> list[Path]:
    seen: dict[str, Path] = {}
    for c in clips:
        for p in (c.path, c.alpha_path):
            if p is not None:
                seen.setdefault(str(p), p)
    return list(seen.values())


def check_media(paths: Iterable[Path]) -> None:
    """Refuse media that a delivery sidecar marks previs or failed-without-override."""
    for p in paths:
        side = read_sidecar(p)
        if side is None:
            continue
        if side.get("previs"):
            raise DeliveryRefused(
                "PREVIS_NOT_DELIVERABLE", f"{p.name} is previs and cannot go on a delivery timeline"
            )
        failed = side.get("run_state") == "GATE_FAIL" or (side.get("gate") or {}).get("verdict") == "fail"
        if failed and not side.get("override"):
            raise DeliveryRefused("GATE_FAIL", f"{p.name} failed its gates and has no override on record")


def build(timeline: Mapping[str, Any]) -> str:
    """The FCPXML 1.10 document for `timeline` as text (see the module docstring)."""
    fps = to_fraction(timeline.get("fps"))
    width, height = int(timeline["width"]), int(timeline["height"])
    if width <= 0 or height <= 0:
        raise FcpxmlError("width and height must be positive")
    clock = _Clock(fps)
    clips = _clips(timeline, clock)

    spine_clips = [c for c in clips if c.lane == 0]
    connected = [c for c in clips if c.lane != 0]
    if not spine_clips:
        raise FcpxmlError("the timeline needs at least one spine clip (lane 0 or no lane)")

    # one asset per file, long enough for its longest use
    asset_frames: dict[str, int] = {}
    for c in clips:
        for p in (c.path, c.alpha_path):
            if p is not None:
                key = str(p)
                asset_frames[key] = max(asset_frames.get(key, 0), c.frames)
    asset_ids = {key: f"r{i + 2}" for i, key in enumerate(asset_frames)}

    root = ET.Element("fcpxml", version=VERSION)
    resources = ET.SubElement(root, "resources")
    ET.SubElement(
        resources,
        "format",
        id="r1",
        frameDuration=f"{clock.fd.numerator}/{clock.fd.denominator}s",
        width=str(width),
        height=str(height),
        colorSpace=COLOR_SPACE,
    )
    paths_by_key = {str(p): p for p in _referenced(clips)}
    for key, frames in asset_frames.items():
        p = paths_by_key[key]
        asset = ET.SubElement(
            resources,
            "asset",
            id=asset_ids[key],
            name=_clean(p.stem),
            start="0s",
            duration=clock.t(frames),
            hasVideo="1",
            format="r1",
        )
        ET.SubElement(asset, "media-rep", kind="original-media", src=_uri(p))

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name=_clean(str(timeline.get("event") or "PLATE")))
    project = ET.SubElement(event, "project", name=_clean(str(timeline.get("name") or "PLATE timeline")))

    # lay out the spine
    placed: list[tuple[ET.Element, int, int]] = []  # (element, start frame, end frame)
    spine = ET.Element("spine")
    cursor = 0
    for c in spine_clips:
        start = cursor if c.start < 0 else c.start
        if start < cursor:
            raise FcpxmlError(
                f"clip {c.name!r} starts at frame {start}, before the previous clip ends ({cursor})"
            )
        if start > cursor:
            gap = ET.SubElement(
                spine, "gap", name="Gap", offset=clock.t(cursor), start="0s", duration=clock.t(start - cursor)
            )
            placed.append((gap, cursor, start))
        el = ET.SubElement(
            spine,
            "asset-clip",
            ref=asset_ids[str(c.path)],
            offset=clock.t(start),
            name=c.name,
            start="0s",
            duration=clock.t(c.frames),
            format="r1",
        )
        if c.alpha_path is not None:
            ET.SubElement(
                el,
                "asset-clip",
                ref=asset_ids[str(c.alpha_path)],
                lane="1",
                offset="0s",
                name=f"{c.name} alpha",
                start="0s",
                duration=clock.t(c.frames),
                format="r1",
            )
        placed.append((el, start, start + c.frames))
        cursor = start + c.frames

    for c in connected:
        if c.start < 0:
            raise FcpxmlError(f"connected clip {c.name!r} needs a start_s")
        if c.alpha_path is not None:
            raise FcpxmlError(f"connected clip {c.name!r}: alpha_path is for spine clips")
        parent = next((el for el, a, b in placed if a <= c.start < b), None)
        if parent is None:
            raise FcpxmlError(
                f"connected clip {c.name!r} starts at frame {c.start}, outside every spine clip"
            )
        parent_start = next(a for el, a, _ in placed if el is parent)
        ET.SubElement(
            parent,
            "asset-clip",
            ref=asset_ids[str(c.path)],
            lane=str(c.lane),
            offset=clock.t(c.start - parent_start),
            name=c.name,
            start="0s",
            duration=clock.t(c.frames),
            format="r1",
        )

    sequence = ET.SubElement(
        project, "sequence", format="r1", duration=clock.t(cursor), tcStart="0s", tcFormat="NDF"
    )
    sequence.append(spine)
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    text = f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n{body}\n'
    validate(text)
    return text


# ---------------------------------------------------------------------------- validation


def _time(value: str | None, what: str, fd: Fraction, *, aligned: bool = True) -> Fraction:
    m = _TIME.match(value or "")
    if m is None:
        raise FcpxmlError(f"{what}: {value!r} is not an FCPXML time (e.g. 1001/24000s)")
    t = Fraction(int(m.group(1)), int(m.group(2) or 1))
    if aligned and (t / fd).denominator != 1:
        raise FcpxmlError(f"{what}: {value!r} is not a whole number of frames")
    return t


def validate(xml_text: str | bytes) -> None:
    """Parse and check the FCPXML 1.10 subset this module writes. Raises FcpxmlError."""
    text = xml_text.decode("utf-8") if isinstance(xml_text, bytes) else xml_text
    if "<!ENTITY" in text.upper():
        raise FcpxmlError("entity declarations are not allowed")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise FcpxmlError(f"not well-formed XML: {e}") from e
    if root.tag != "fcpxml" or root.get("version") != VERSION:
        raise FcpxmlError(f"root must be <fcpxml version='{VERSION}'>")
    resources = root.find("resources")
    if resources is None:
        raise FcpxmlError("missing <resources>")
    formats = resources.findall("format")
    if len(formats) != 1:
        raise FcpxmlError(f"expected one <format>, found {len(formats)}")
    fmt = formats[0]
    for attr in ("id", "frameDuration", "width", "height", "colorSpace"):
        if not fmt.get(attr):
            raise FcpxmlError(f"<format> is missing {attr}")
    fd = _time(fmt.get("frameDuration"), "format frameDuration", Fraction(1), aligned=False)
    if fd <= 0:
        raise FcpxmlError("frameDuration must be positive")
    assets: dict[str, ET.Element] = {}
    for asset in resources.findall("asset"):
        aid = asset.get("id")
        if not aid or aid in assets or aid == fmt.get("id"):
            raise FcpxmlError(f"asset id {aid!r} is missing or duplicated")
        if asset.get("format") != fmt.get("id"):
            raise FcpxmlError(f"asset {aid} does not reference the <format>")
        _time(asset.get("start"), f"asset {aid} start", fd)
        _time(asset.get("duration"), f"asset {aid} duration", fd)
        reps = asset.findall("media-rep")
        if not reps or not all((r.get("src") or "").startswith("file://") for r in reps):
            raise FcpxmlError(f"asset {aid} needs <media-rep> children with file:// src")
        assets[aid] = asset
    if not assets:
        raise FcpxmlError("no <asset> in <resources>")

    sequences = root.findall("./library/event/project/sequence")
    if len(sequences) != 1:
        raise FcpxmlError("expected library/event/project/sequence")
    seq = sequences[0]
    if seq.get("format") != fmt.get("id") or seq.get("tcFormat") != "NDF":
        raise FcpxmlError("sequence must reference the <format> and be NDF")
    _time(seq.get("duration"), "sequence duration", fd)
    spine = seq.find("spine")
    if spine is None or len(spine) == 0:
        raise FcpxmlError("sequence has an empty or missing <spine>")

    def check_clip(el: ET.Element, depth: int) -> None:
        if el.tag == "asset-clip":
            if el.get("ref") not in assets:
                raise FcpxmlError(f"asset-clip ref {el.get('ref')!r} is not in <resources>")
            for attr in ("offset", "duration"):
                _time(el.get(attr), f"asset-clip {el.get('name')} {attr}", fd)
            _time(el.get("start", "0s"), f"asset-clip {el.get('name')} start", fd)
            if depth > 0:
                lane = el.get("lane")
                if lane is None or not re.fullmatch(r"-?[1-9]\d*", lane):
                    raise FcpxmlError(f"connected clip {el.get('name')!r} needs a non-zero integer lane")
            elif el.get("lane") is not None:
                raise FcpxmlError("a spine clip has no lane")
        elif el.tag == "gap" and depth == 0:
            _time(el.get("offset"), "gap offset", fd)
            _time(el.get("duration"), "gap duration", fd)
        else:
            raise FcpxmlError(f"<{el.tag}> is not allowed here")
        for child in el:
            if child.tag in ("asset-clip",):
                check_clip(child, depth + 1)

    for el in spine:
        check_clip(el, 0)


# ---------------------------------------------------------------------------- write


def write(
    timeline: Mapping[str, Any],
    out: str | Path,
    *,
    provenance: Provenance | None = None,
    check_media_sidecars: bool = True,
) -> Path:
    """Write the FCPXML for `timeline` to `out` (a `.fcpxml` path that does not exist yet) and return it.
    With `provenance`, previs and unresolved GATE_FAIL are refused and a `<name>.gate.json` sidecar is
    written next to the timeline (an XML document has no lineage carrier; the sidecar holds it)."""
    dest = Path(out)
    if dest.suffix.lower() != ".fcpxml":
        raise DeliveryError(f"{dest.name}: FCPXML is written as .fcpxml")
    override = authorize(provenance, require_report=False) if provenance is not None else None
    text = build(timeline)
    fps = to_fraction(timeline.get("fps"))
    if check_media_sidecars:
        check_media(_referenced(_clips(timeline, _Clock(fps))))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not claim(dest):  # the claim is the check: of any number of deliveries to one name, one wins
        raise FileExistsError(f"{dest} already exists; delivery never overwrites")
    try:
        write_bytes_atomic(dest, text.encode("utf-8"))
    except BaseException:
        dest.unlink(missing_ok=True)  # our empty claim: the name is free again
        raise
    if provenance is not None:
        try:
            write_sidecar(
                dest,
                provenance,
                fmt="fcpxml",
                files=[dest],
                override=override,
                color={"color_space": COLOR_SPACE},
                extra={"fps": f"{fps.numerator}/{fps.denominator}"},
            )
        except Exception:
            dest.unlink(missing_ok=True)
            raise
    return dest
