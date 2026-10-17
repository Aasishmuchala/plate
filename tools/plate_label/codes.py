"""Verdicts, keys and the reason-code table for the labelling tool. The table lives in
calib/reason_codes.yaml (Lensbook A/F codes plus PLATE's P codes); this module loads it once and
validates label input against it. New code for PLATE; codes are never renumbered because
calib/labels.jsonl is joined on them by the calibration report.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

VERDICTS: tuple[str, ...] = ("ship", "fix", "render_it")

# One keypress per verdict in the UI.
VERDICT_KEYS: dict[str, str] = {"1": "ship", "2": "fix", "3": "render_it"}

NEEDS_REASON_PICKER = frozenset({"fix", "render_it"})

# <repo>/calib/reason_codes.yaml (tools/plate_label/codes.py -> parents[2] is the repo root).
DEFAULT_CODES_FILE = Path(__file__).resolve().parents[2] / "calib" / "reason_codes.yaml"


def codes_file() -> Path:
    """PLATE_REASON_CODES (a file path) wins over the repo copy; tests and other checkouts use it."""
    env = os.environ.get("PLATE_REASON_CODES")
    return Path(env) if env else DEFAULT_CODES_FILE


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[dict[str, str], dict[str, str]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("codes"), dict) or not raw["codes"]:
        raise ValueError(f"{path}: expected a mapping with a non-empty 'codes' mapping")
    codes = {str(k): str(v) for k, v in raw["codes"].items()}
    groups = {str(k): str(v) for k, v in (raw.get("groups") or {}).items()}
    return codes, groups


def reason_codes() -> dict[str, str]:
    """code -> description, in file order."""
    return dict(_load(str(codes_file()))[0])


def reason_groups() -> dict[str, str]:
    """first letter of a code -> group title (for the picker's headings)."""
    return dict(_load(str(codes_file()))[1])


def group_of(code: str) -> str:
    return code[:1]


def validate_reasons(codes: object, verdict: str | None = None) -> list[str]:
    """Return `codes` de-duplicated in order, or raise ValueError.

    Unknown codes always raise. When `verdict` is given the label rules apply too: it must be one of
    VERDICTS, and only fix / render_it carry reasons (a ship with a failure code is a UI bug).
    Reasons are optional on fix / render_it: the rater may not find a matching code.
    """
    if isinstance(codes, str) or not isinstance(codes, (list, tuple)):
        raise ValueError("reasons must be a list of reason codes")
    table = reason_codes()
    bad = [c for c in codes if not isinstance(c, str) or c not in table]
    if bad:
        raise ValueError(f"unknown reason code(s): {', '.join(str(b) for b in bad)}")
    out = list(dict.fromkeys(codes))
    if verdict is not None:
        if verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}: expected one of {', '.join(VERDICTS)}")
        if verdict == "ship" and out:
            raise ValueError("verdict 'ship' takes no reason codes")
    return out
