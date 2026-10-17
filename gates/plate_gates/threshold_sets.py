"""Earlier threshold sets, still readable (router plan T80).

`thresholds.yaml` holds the ACTIVE set. When a number changes, the set it replaces is copied byte for byte into
`threshold_sets/<set_id>.yaml` and never edited again, so a gate report that carries `threshold_set_id` can
always be re-read against the bounds it was scored with.

    load_set("ts-2026.10.17-draft")     # the frozen set (a ThresholdSet, same API as thresholds.load())
    load_set()                          # the active set, i.e. thresholds.load()
    available()                         # every set id that loads: the active one first, then the frozen ones
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import yaml

from . import thresholds
from .thresholds import Threshold, ThresholdSet

__all__ = ["FROZEN_DIR", "available", "load_set"]

FROZEN_DIR = Path(__file__).with_name("threshold_sets")
_SET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@cache
def _load_frozen(set_id: str) -> ThresholdSet:
    path = FROZEN_DIR / f"{set_id}.yaml"
    if not path.is_file():
        raise KeyError(f"unknown threshold set {set_id!r} (frozen sets: {available()[1:]})")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw["set_id"] != set_id:
        raise ValueError(f"{path.name} declares set_id {raw['set_id']!r}")
    entries = raw["metrics"]
    ids = [e["id"] for e in entries]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate metric id in {path.name}")
    return ThresholdSet(id=set_id, metrics={e["id"]: Threshold(**e) for e in entries})


def load_set(set_id: str | None = None) -> ThresholdSet:
    """The set with this id: the active one (thresholds.yaml) when `set_id` is None or names it, else the frozen
    copy under threshold_sets/. KeyError for an id that was never a set."""
    active = thresholds.load()
    if set_id is None or set_id == active.id:
        return active
    if not _SET_ID.match(set_id):
        raise KeyError(f"unknown threshold set {set_id!r}")
    return _load_frozen(set_id)


def available() -> list[str]:
    """Set ids that load: the active set first, then the frozen ones in name order."""
    frozen = sorted(p.stem for p in FROZEN_DIR.glob("*.yaml")) if FROZEN_DIR.is_dir() else []
    return [thresholds.load().id, *[i for i in frozen if i != thresholds.load().id]]
