"""Verdicts, keys and the fixed reason-code table for the labelling tool, plus the validation rules
(fix / render_it need at least one reason, O99 needs a note). New code for PLATE; the codes are the
ones in the build plan (T8) and must not drift, because calib/labels.jsonl is joined on them later.
"""

from __future__ import annotations

VERDICTS: tuple[str, ...] = ("ship", "fix", "render_it")

# One keypress per verdict in the UI; the terminal loop uses the lowercase letters.
VERDICT_KEYS: dict[str, str] = {"S": "ship", "F": "fix", "R": "render_it"}

REASON_CODES: dict[str, str] = {
    "G01": "camera shift / registration",
    "G02": "invented edges or openings on architecture",
    "G03": "unseen geometry guessed",
    "G04": "aspect or preset drift",
    "C01": "material restyled",
    "C02": "cross-view colour drift",
    "C03": "tone or exposure off",
    "T01": "AI over-texture (fails texture band)",
    "T02": "soft / mushy detail",
    "T03": "flicker",
    "P01": "implausible people",
    "P02": "people scale wrong",
    "M01": "camera not locked",
    "M02": "first/last frame mismatch",
    "M03": "drift or invention over time",
    "X01": "lettering or logo changed",
    "O01": "moderation refused",
    "O99": "other (note required)",
}

# UI grouping by code prefix (the first letter).
REASON_GROUPS: dict[str, str] = {
    "G": "Geometry",
    "C": "Colour",
    "T": "Texture",
    "P": "People",
    "M": "Motion",
    "X": "Lettering",
    "O": "Other",
}

NEEDS_REASON = frozenset({"fix", "render_it"})


def validate_reasons(codes: list[str], verdict: str | None = None, note: str = "") -> list[str]:
    """Return `codes` de-duplicated in order, or raise ValueError.

    Unknown codes always raise. When `verdict` is given the label rules apply too: the verdict must be
    one of VERDICTS, fix / render_it need at least one reason, and O99 needs a non-empty note.
    Codes are case-sensitive here; the CLI upper-cases user input before calling.
    """
    if isinstance(codes, str) or not isinstance(codes, (list, tuple)):
        raise ValueError("reasons must be a list of reason codes")
    bad = [c for c in codes if not isinstance(c, str) or c not in REASON_CODES]
    if bad:
        raise ValueError(f"unknown reason code(s): {', '.join(str(b) for b in bad)}")
    out = list(dict.fromkeys(codes))
    if verdict is not None:
        if verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}: expected one of {', '.join(VERDICTS)}")
        if verdict in NEEDS_REASON and not out:
            raise ValueError(f"verdict {verdict!r} needs at least one reason code")
    if "O99" in out and not (note or "").strip():
        raise ValueError("reason O99 (other) needs a non-empty note")
    return out
