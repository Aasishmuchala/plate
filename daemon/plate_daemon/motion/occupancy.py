"""Camera occupancy along a motion packet's path: which "room" the camera is in at every frame, and which approved
still stands for each room in a Seedance 2.5 reference-to-video previs (router plan T44, BUILD_PLAN P3).

A room is read off the Cryptomatte id sequence the motion packet carries (`files.id_seq`, rank-0 object ids per
frame), never off pixels an engine made:

- per frame, the pixel share of every object id (background 0 counts in the denominator, so a share is a share of
  the frame); the room's identity is its **top-3 architecture ids** by share (legend `class: architecture`, object
  ids `o:<8 hex>` only), ids under MIN_SHARE (0.5 % of the frame) left out so 2 cm markers and slivers at the frame
  edge do not flicker the top-3;
- consecutive frames stay in one room while their top-3 sets overlap by >= 50 % (the overlap coefficient
  |A n B| / min(|A|, |B|): a frame that sees fewer architecture ids is compared on what it does see; two empty sets
  overlap fully, an empty set against a non-empty one not at all);
- the packet's `motion.path_crossings` (threshold, room_change, occluder; PACKET.md 10) are hard boundaries: the
  crossing's frame opens a new room whatever the ids say.

`rooms_along_path(...)` returns `[{room_id, frames: (a, b), engine_seconds: (ta, tb), dominant_ids, boundary,
id_shares, camera_position}]`: frames inclusive, seconds on the engine clip's timeline from the time map
(`packet_time_s(a)` to `packet_time_s(b + 1)`, so rooms tile the clip), `dominant_ids` the room's top-3 over its
mean shares, `boundary` what opened it (`start`, `ids`, or the crossing kind), `camera_position` the mean camera
centre over the room's frames (None without camera_frames).

`choose_refs(rooms, approved_stills)` picks the references: the hero still first with `role: first_frame`; then each
room gets the approved still whose camera sees the room's dominant ids with the highest share (sum of the still's
legend `pixel_share` over those ids), ties to the still whose camera centre is nearest the room's; one still
serving several rooms is sent once; at most MAX_REFS (30, Seedance 2.5's image limit) references, a room past the
cap falls back to its best still already in the list. Each ref is `{role, still, room_id, room_ids, index, scores}`
with `index` the 1-based position the prompt names it by (`@img<index>`).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from plate_gates import reprojection
from plate_gates.timemap import TimeMap

__all__ = [
    "ARCH_CLASSES",
    "CROSSING_KINDS",
    "MAX_REFS",
    "MIN_OVERLAP",
    "MIN_SHARE",
    "TOP_K",
    "architecture_ids",
    "camera_position",
    "choose_refs",
    "frame_shares",
    "load_motion_packet",
    "merge_legends",
    "overlap",
    "packet_fps",
    "path_crossings",
    "rooms_along_path",
    "sequence_range",
    "still_shares",
    "top_ids",
]

ARCH_CLASSES = frozenset({"architecture"})
TOP_K = 3
MIN_OVERLAP = 0.5
MIN_SHARE = 0.005
MAX_REFS = 30  # Seedance 2.5 reference-to-video takes 30 images (manifest seedance-2.5-r2v, max_items)
TIE_DIGITS = 6  # scores equal to 1e-6 are a tie, settled by camera distance
CROSSING_KINDS = ("threshold", "room_change", "occluder")  # precedence when two crossings share a frame


# ---------------------------------------------------------------------------- the packet


def load_motion_packet(packet_motion: Any) -> tuple[Path | None, dict[str, Any]]:
    """(packet folder | None, manifest) from a packet folder, its manifest.json path, a manifest mapping or a
    `(folder, manifest)` pair."""
    if isinstance(packet_motion, tuple) and len(packet_motion) == 2:
        root, manifest = packet_motion
        return (Path(root) if root is not None else None), dict(manifest)
    return reprojection.load_packet(packet_motion)


def _read_legend(root: Path | None, manifest: Mapping[str, Any]) -> dict[str, Any]:
    if root is None:
        raise ValueError("no legend given and no packet folder to read id_legend.json from")
    ref = (manifest.get("files") or {}).get("id_legend") or {"path": "id_legend.json"}
    return json.loads((root / ref["path"]).read_text(encoding="utf-8"))


def _entries(legend: Any) -> list[Mapping[str, Any]]:
    if legend is None:
        return []
    if isinstance(legend, Mapping):
        items = legend.get("entries") or []
    else:
        items = legend
    return [e for e in items if isinstance(e, Mapping) and isinstance(e.get("id"), str)]


def merge_legends(*legends: Any) -> dict[str, Any]:
    """One legend from several of the same scene: the first entry for an id wins, ids seen only in a later
    legend are added (a motion packet's legend lists the hero frame; a room further down the path may only be
    classified by the still that sees it)."""
    seen: dict[str, Mapping[str, Any]] = {}
    for legend in legends:
        for e in _entries(legend):
            seen.setdefault(e["id"], e)
    return {"entries": list(seen.values())}


def architecture_ids(legend: Any) -> frozenset[str]:
    """Object ids (`o:`) whose class is architecture."""
    return frozenset(
        e["id"] for e in _entries(legend) if e["id"].startswith("o:") and e.get("class") in ARCH_CLASSES
    )


def path_crossings(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """`motion.path_crossings` as `[{frame, kind}]` sorted by frame (an unknown kind is a room_change)."""
    out = []
    for c in (manifest.get("motion") or {}).get("path_crossings") or []:
        if not isinstance(c, Mapping) or not isinstance(c.get("frame"), int):
            continue
        kind = c.get("kind") if c.get("kind") in CROSSING_KINDS else "room_change"
        out.append({"frame": int(c["frame"]), "kind": kind})
    return sorted(out, key=lambda c: (c["frame"], CROSSING_KINDS.index(c["kind"])))


def sequence_range(manifest: Mapping[str, Any]) -> tuple[int, int] | None:
    files = manifest.get("files") or {}
    for role in ("id_seq", "clay_seq", "z_seq"):
        ref = files.get(role)
        if isinstance(ref, Mapping) and ref.get("kind") == "sequence":
            return int(ref["first"]), int(ref["last"])
    frames = (manifest.get("camera") or {}).get("frames") or {}
    if "start" in frames and "end" in frames:
        return int(frames["start"]), int(frames["end"])
    return None


def packet_fps(root: Path | None, manifest: Mapping[str, Any]) -> Fraction:
    fps = ((manifest.get("camera") or {}).get("frames") or {}).get("fps")
    if fps is None and root is not None:
        try:
            fps = reprojection.load_camera_frames(root, manifest).get("fps")
        except (OSError, ValueError, KeyError):
            fps = None
    if fps is None:
        raise ValueError("the motion packet names no frame rate (camera.frames.fps)")
    return Fraction(fps).limit_denominator(100000) if isinstance(fps, float) else Fraction(fps)


def _id_frames(root: Path | None, manifest: Mapping[str, Any], id_seq: Any) -> dict[int, Any]:
    """{packet frame: path or id array}. A list is indexed from the sequence's first frame."""
    if id_seq is None:
        if root is None:
            raise ValueError("no id_seq given and no packet folder to read files.id_seq from")
        return dict(reprojection.sequence_paths(root, manifest, "id"))
    if isinstance(id_seq, Mapping):
        return {int(k): v for k, v in id_seq.items()}
    rng = sequence_range(manifest)
    start = rng[0] if rng else 0
    return {start + i: v for i, v in enumerate(id_seq)}


def _ids_array(item: Any, manifest: Mapping[str, Any]) -> np.ndarray:
    """Rank-0 object ids as uint32 (H, W): an EXR frame of the id sequence, or an array (uint32 as is, float32
    Cryptomatte values reinterpreted bit for bit)."""
    if isinstance(item, (str, Path)):
        return reprojection.read_rank0_ids(item, manifest)
    a = np.asarray(item)
    if a.ndim != 2:
        raise ValueError(f"an id frame must be (H, W), got shape {a.shape}")
    if a.dtype == np.float32:
        return np.ascontiguousarray(a).view(np.uint32)
    return a.astype(np.uint32, copy=False)


# ---------------------------------------------------------------------------- shares and overlap


def frame_shares(ids: np.ndarray) -> dict[str, float]:
    """{`o:<8 hex>`: share of the frame} for every non-zero rank-0 id."""
    a = np.asarray(ids)
    total = float(a.size) or 1.0
    values, counts = np.unique(a, return_counts=True)
    return {f"o:{int(v):08x}": float(c) / total for v, c in zip(values, counts, strict=True) if int(v) != 0}


def top_ids(
    shares: Mapping[str, float], allowed: Iterable[str], k: int = TOP_K, min_share: float = MIN_SHARE
) -> list[str]:
    """The k ids of `allowed` with the largest share (at least `min_share`), largest first, ties by id."""
    allowed = set(allowed)
    ranked = sorted(
        ((s, i) for i, s in shares.items() if i in allowed and s >= min_share), key=lambda t: (-t[0], t[1])
    )
    return [i for _, i in ranked[:k]]


def overlap(a: Iterable[str], b: Iterable[str]) -> float:
    """Overlap coefficient |A n B| / min(|A|, |B|); 1.0 for two empty sets, 0.0 when only one is empty."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def _camera_positions(root: Path | None, manifest: Mapping[str, Any]) -> dict[int, np.ndarray]:
    if root is None:
        return {}
    try:
        doc = reprojection.load_camera_frames(root, manifest)
    except (OSError, ValueError, KeyError):
        return {}
    out: dict[int, np.ndarray] = {}
    for f in doc.get("frames") or []:
        m = np.asarray(f.get("world_from_camera"), dtype=np.float64)
        if m.shape == (4, 4):
            out[int(f["frame"])] = m[:3, 3]
    return out


def camera_position(camera: Any) -> np.ndarray | None:
    """Camera centre (world) from a manifest camera block, a 4x4 world_from_camera or an (x, y, z)."""
    if camera is None:
        return None
    if isinstance(camera, Mapping):
        if "position" in camera:
            return camera_position(camera["position"])
        camera = camera.get("world_from_camera")
        if camera is None:
            return None
    a = np.asarray(camera, dtype=np.float64)
    if a.shape == (4, 4):
        return a[:3, 3]
    if a.shape == (3,):
        return a
    return None


# ---------------------------------------------------------------------------- rooms


def rooms_along_path(
    packet_motion: Any,
    id_seq: Any = None,
    legend: Any = None,
    timemap: TimeMap | None = None,
    *,
    top_k: int = TOP_K,
    min_overlap: float = MIN_OVERLAP,
    min_share: float = MIN_SHARE,
) -> list[dict[str, Any]]:
    """The rooms the camera passes through, in path order (see the module docstring).

    `packet_motion` is the motion packet (folder, manifest path, manifest mapping or (folder, manifest));
    `id_seq` overrides `files.id_seq` ({frame: path | array} or a list from the first frame); `legend` overrides
    the packet's id_legend.json; `timemap` puts the rooms on the engine clip's timeline (default: the packet's
    own rate to 24 fps, real time)."""
    root, manifest = load_motion_packet(packet_motion)
    frames = _id_frames(root, manifest, id_seq)
    if not frames:
        raise ValueError("the motion packet has no id frames")
    if legend is None:
        legend = _read_legend(root, manifest)
    arch = architecture_ids(legend)
    numbers = sorted(frames)
    if timemap is None:
        timemap = TimeMap(packet_fps(root, manifest), 24, (numbers[0], numbers[-1]))

    shares = {n: frame_shares(_ids_array(frames[n], manifest)) for n in numbers}
    tops = {n: top_ids(shares[n], arch, top_k, min_share) for n in numbers}
    crossings = path_crossings(manifest)

    runs: list[tuple[list[int], str]] = []
    for i, n in enumerate(numbers):
        if i == 0:
            runs.append(([n], "start"))
            continue
        prev = numbers[i - 1]
        hard = [c["kind"] for c in crossings if prev < c["frame"] <= n]
        kind = hard[0] if hard else None
        if kind is None and overlap(tops[n], tops[prev]) < min_overlap:
            kind = "ids"
        if kind is None:
            runs[-1][0].append(n)
        else:
            runs.append(([n], kind))

    cams = _camera_positions(root, manifest)
    duration = float(timemap.duration_s or 0)
    rooms: list[dict[str, Any]] = []
    for idx, (members, boundary) in enumerate(runs, start=1):
        a, b = members[0], members[-1]
        mean: dict[str, float] = {}
        for n in members:
            for cid, s in shares[n].items():
                if cid in arch:
                    mean[cid] = mean.get(cid, 0.0) + s / len(members)
        ta = float(timemap.packet_time_s(a))
        tb = float(timemap.packet_time_s(b + 1))
        if duration:
            ta, tb = min(ta, duration), min(tb, duration)
        pos = [cams[n] for n in members if n in cams]
        rooms.append(
            {
                "room_id": f"room_{idx}",
                "frames": (a, b),
                "engine_seconds": (round(ta, 6), round(tb, 6)),
                "dominant_ids": top_ids(mean, arch, top_k, min_share),
                "boundary": boundary,
                "id_shares": {
                    k: round(v, 6) for k, v in sorted(mean.items(), key=lambda kv: (-kv[1], kv[0]))
                },
                "camera_position": [float(v) for v in np.mean(pos, axis=0)] if pos else None,
            }
        )
    return rooms


# ---------------------------------------------------------------------------- references


def still_shares(still: Mapping[str, Any]) -> dict[str, float]:
    """What a still's camera sees: `id_shares` ({id: share}), else its packet `legend` (entries' pixel_share),
    else an `ids` array (rank-0 object ids of the still)."""
    if isinstance(still.get("id_shares"), Mapping):
        return {str(k): float(v) for k, v in still["id_shares"].items()}
    if still.get("legend") is not None:
        return {
            e["id"]: float(e.get("pixel_share") or 0.0)
            for e in _entries(still["legend"])
            if isinstance(e.get("pixel_share"), (int, float))
        }
    if still.get("ids") is not None:
        return frame_shares(_ids_array(still["ids"], {}))
    return {}


def _hero_index(stills: Sequence[Mapping[str, Any]], hero_packet_id: str | None) -> int:
    if hero_packet_id is not None:
        for i, s in enumerate(stills):
            if s.get("packet_id") == hero_packet_id:
                return i
        raise ValueError(f"no approved still of the hero packet {hero_packet_id}")
    for i, s in enumerate(stills):
        if s.get("hero") is True:
            return i
    return 0


def _rank(room: Mapping[str, Any], stills: Sequence[Mapping[str, Any]], shares: list[dict[str, float]]):
    """[(score, distance, index)] best first: highest share of the room's dominant ids, then nearest camera."""
    dominant = list(room.get("dominant_ids") or [])
    centre = camera_position(room.get("camera_position"))
    ranked = []
    for i, still in enumerate(stills):
        score = sum(shares[i].get(cid, 0.0) for cid in dominant)
        pos = camera_position(still.get("camera"))
        dist = float(np.linalg.norm(pos - centre)) if pos is not None and centre is not None else float("inf")
        ranked.append((round(score, TIE_DIGITS), dist, i))
    ranked.sort(key=lambda t: (-t[0], t[1], t[2]))
    return ranked


def choose_refs(
    rooms: Sequence[Mapping[str, Any]],
    approved_stills: Sequence[Mapping[str, Any]],
    *,
    hero_packet_id: str | None = None,
    max_refs: int = MAX_REFS,
) -> list[dict[str, Any]]:
    """References for the previs, hero first (see the module docstring). `approved_stills` are
    `{packet_id, rgb, camera, ...}` with what each camera sees as `id_shares`, `legend` or `ids`; the hero is the
    still of `hero_packet_id`, else the one marked `hero: True`, else the first."""
    stills = list(approved_stills)
    if not stills:
        raise ValueError("previs needs at least the approved hero still")
    if max_refs < 1:
        raise ValueError("max_refs must be at least 1 (the hero)")
    hero = _hero_index(stills, hero_packet_id)
    shares = [still_shares(s) for s in stills]
    refs: list[dict[str, Any]] = [
        {
            "role": "first_frame",
            "still": stills[hero],
            "room_id": None,
            "room_ids": [],
            "index": 1,
            "scores": {},
        }
    ]
    slot = {hero: 0}
    for room in rooms:
        ranked = _rank(room, stills, shares)
        score, _, best = ranked[0]
        if best not in slot:
            if len(refs) < max_refs:
                slot[best] = len(refs)
                refs.append(
                    {
                        "role": "reference_image",
                        "still": stills[best],
                        "room_id": None,
                        "room_ids": [],
                        "index": len(refs) + 1,
                        "scores": {},
                    }
                )
            else:  # the cap is reached: the best still already sent stands in
                score, _, best = next(t for t in ranked if t[2] in slot)
        ref = refs[slot[best]]
        rid = str(room["room_id"])
        ref["room_ids"].append(rid)
        ref["scores"][rid] = score
        if ref["room_id"] is None:
            ref["room_id"] = rid
    return refs
