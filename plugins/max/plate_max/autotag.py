"""Auto-tagger: a guess at a node's class from the words in its name, material and layer.

Doctrine (spec/PACKET.md section 7): rules win. The tagger fills only the IDs no class rule matched
(class_source "unlabelled") and marks them "autotag"; it never changes a "rule" or "manual" entry. A
guess never makes anything editable: editability still needs an editable layer prefix (policy.py), so
an autotagged "vegetation" node on layer "0" stays locked. It only gives the lint, the dock and the
router a better class than "other". Stdlib only; the guesses are pure functions.

It also reads the artist's planar override (decision 43): the node user property plate_planar
("true"/"false", also yes/no, 1/0, on/off) beats legend.py's planar class rule for that node. A value
that is none of those is ignored, so the rule decides.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .rt import runtime

PLANAR_PROPERTY = "plate_planar"
_TRUE = ("true", "yes", "1", "on")
_FALSE = ("false", "no", "0", "off")

# (class, keywords). The order is the tie-break when two classes score equally; a keyword matches a
# whole word of the name (plural "s"/"es" allowed), never a fragment, so "carpet" is not "car" and
# "skylight" is not "sky".
CLASS_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("glazing", ("glass", "glz", "window")),
    ("vegetation", ("tree", "palm", "shrub", "grass", "hedge", "leaf")),
    ("sky", ("sky", "dome", "hdri")),
    ("people", ("person", "people", "human", "figure")),
    ("water", ("water", "pool", "pond")),
    ("vehicle", ("car", "vehicle", "bike")),
    ("furniture", ("sofa", "chair", "table", "bed")),
    ("ground", ("road", "paving", "ground", "floor", "sett", "lawn")),
    ("signage", ("sign", "logo", "text")),
)

# How much a hit in each field is trusted. Agreeing fields combine as 1 - prod(1 - weight).
FIELD_WEIGHTS: tuple[tuple[str, float], ...] = (("layer", 0.90), ("name", 0.80), ("material", 0.60))
CONFLICT_PENALTY = 0.5  # the winner loses this share of the best rival's confidence
MAX_CONFIDENCE = 0.99
DEFAULT_MIN_CONFIDENCE = 0.7  # apply_autotag leaves weaker guesses unlabelled

_SPLIT = re.compile(r"[^0-9A-Za-z]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")
_KEYWORD_CLASS = {kw: cls for cls, kws in CLASS_KEYWORDS for kw in kws}
_PRIORITY = {cls: i for i, (cls, _) in enumerate(CLASS_KEYWORDS)}


def tokens(text: str | None) -> list[str]:
    """Lower-case words of a name: split on anything that is not a letter or digit, then at
    camelCase and letter/digit boundaries ("PalmTree3" gives palm, tree, 3)."""
    out: list[str] = []
    for chunk in _SPLIT.split(text or ""):
        out.extend(part.lower() for part in _CAMEL.split(chunk) if part)
    return out


def _class_of_token(word: str) -> str | None:
    for candidate in (
        word,
        word[:-1] if word.endswith("s") else None,
        word[:-2] if word.endswith("es") else None,
    ):
        if candidate and candidate in _KEYWORD_CLASS:
            return _KEYWORD_CLASS[candidate]
    return None


def _field_class(text: str | None) -> str | None:
    """The first class whose keyword appears in the words of one field."""
    hits = {c for c in (_class_of_token(w) for w in tokens(text)) if c}
    return min(hits, key=_PRIORITY.__getitem__) if hits else None


def autotag(entry: dict[str, Any]) -> tuple[str, float]:
    """(class, confidence) for a node or legend entry with name, material and layer fields. "other"
    with confidence 0.0 when no word matches."""
    votes: dict[str, list[float]] = {}
    for field, weight in FIELD_WEIGHTS:
        cls = _field_class(entry.get(field))
        if cls:
            votes.setdefault(cls, []).append(weight)
    if not votes:
        return "other", 0.0
    scored: dict[str, float] = {}
    for cls, weights in votes.items():
        miss = 1.0
        for w in weights:
            miss *= 1.0 - w
        scored[cls] = 1.0 - miss
    winner = min(scored, key=lambda c: (-scored[c], _PRIORITY[c]))
    rival = max((v for c, v in scored.items() if c != winner), default=0.0)
    confidence = scored[winner] * (1.0 - CONFLICT_PENALTY * rival)
    return winner, round(min(confidence, MAX_CONFIDENCE), 4)


def apply_autotag(entry: dict[str, Any], min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> dict[str, Any]:
    """A copy of a legend entry with class and class_source="autotag" set when the entry is
    unlabelled (no rule matched) and the guess reaches min_confidence; any other entry comes back
    as an unchanged copy. Only class and class_source change (the legend schema has no room for the
    confidence)."""
    out = dict(entry)
    if out.get("class_source") != "unlabelled":
        return out
    cls, confidence = autotag(out)
    if cls != "other" and confidence >= min_confidence:
        out["class"] = cls
        out["class_source"] = "autotag"
    return out


def apply_autotag_to_legend(
    legend_doc: dict[str, Any], min_confidence: float = DEFAULT_MIN_CONFIDENCE
) -> dict[str, Any]:
    """A copy of an id_legend document with apply_autotag run over every entry."""
    return {**legend_doc, "entries": [apply_autotag(e, min_confidence) for e in legend_doc["entries"]]}


# --------------------------------------------------------------------------- the planar override


def parse_planar(value: Any) -> bool | None:
    """A plate_planar value as True/False (pymxs hands back MAXScript booleans, numbers or strings),
    None when it is missing or says neither."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value) if value in (0, 1) else None
    text = str(value).strip().strip('"').lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def planar_property(node: Any) -> bool | None:
    """The node's plate_planar user property (parse_planar), None when unset or unreadable."""
    if node is None:
        return None
    try:
        raw = runtime().getUserProp(node, PLANAR_PROPERTY)
    except Exception:
        return None
    return parse_planar(raw)


def planar_overrides(names: Iterable[str]) -> dict[str, bool]:
    """{node name: planar} for every named scene node whose plate_planar property is set (names that
    are not scene nodes, such as scatter items, have no property)."""
    rt = runtime()
    out: dict[str, bool] = {}
    for name in names:
        try:
            node = rt.getNodeByName(str(name), exact=True)
        except Exception:
            node = None
        value = planar_property(node)
        if value is not None:
            out[str(name)] = value
    return out
