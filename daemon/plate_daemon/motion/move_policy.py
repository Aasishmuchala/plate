"""Move policy: how risky a camera move is, and whether the daemon lets it run (router plan T43).

The table is Lensbook's RISK_LADDER (`recipes/risk_ladder.yaml`, ported in T33): locked_tripod none, push_in low,
slider_drift low, crane_rise med, light_sweep med, orbit_le_30 high, aerial_drift high, threshold_cross highest, each
with its repair codes. `evaluate(move_kind, params, router_verdict)` starts from the ladder and only ever escalates:

- dials (the Lensbook dials; they only raise the risk): a reveal over 3 m (`reveal_m`) is at least med and over
  10 m is high; an orbit over 30 degrees (`orbit_deg`) is highest; slider travel over 10 cm (`travel_cm`) is at
  least med;
- `high` and `highest` are blocked unless the node carries an override with a reason of at least 10 characters
  (`params["override"]`: the reason text, a list of overrides, or an `{reason}` object; the same minimum as the
  GATE_FAIL override). An overridden move runs under the label `unseen geometry guessed`, which follows the pixels;
- the render-it router (T28) is consulted last. A `render_move` verdict BLOCKS regardless of any override when the
  router is in blocking mode (P3), because the numbers say the move invents more than an engine may; in advisory
  mode (P1) it is a reason and the move is labelled `unseen geometry guessed`, never blocked.

The result is plain data: `{risk, blocked, label, reasons, move_kind, move, repair_codes, invents, override,
dials, router}`. `risk` is the canvas node's `riskLabel` (canvas_rules blocks `high`/`highest` edges without an
override; this is the same ladder with the dials applied).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from ..canvas_rules import UNSEEN_GEOMETRY_LABEL
from ..export import MIN_OVERRIDE_REASON
from ..recipes import RISKS, RecipeError, RiskRow, risk_for, risk_ladder

__all__ = [
    "BLOCKED_RISKS",
    "ORBIT_MAX_DEG",
    "REVEAL_HIGH_M",
    "REVEAL_MED_M",
    "SLIDER_MAX_CM",
    "evaluate",
    "moves",
    "normalise_move",
    "override_reason",
    "risk_rank",
]

REVEAL_MED_M = 3.0  # "+3 m reveals": over this a push or a rise is at least med
REVEAL_HIGH_M = 10.0  # "+10 m invents": over this it is high
ORBIT_MAX_DEG = 30.0  # past this the model is drawing the unseen elevation
SLIDER_MAX_CM = 10.0  # "travel stays under ten centimetres"
BLOCKED_RISKS = ("high", "highest")
REVEAL_MOVES = ("push_in", "crane_rise")


def moves() -> tuple[str, ...]:
    """The move keys on the ladder, in Lensbook's order."""
    return tuple(r.key for r in risk_ladder())


def risk_rank(risk: str) -> int:
    """none 0, low 1, med 2, high 3, highest 4."""
    return RISKS.index(risk)


def normalise_move(move_kind: str) -> str:
    """`Push-in`, `push in` and `push_in` are one move; anything else must be on the ladder."""
    key = re.sub(r"[\s\-]+", "_", str(move_kind).strip().lower())
    return key


def override_reason(params: Mapping[str, Any]) -> str | None:
    """The override reason text in `params["override"]` (a string, a list of strings / `{reason}` objects, or one
    `{reason}` object), or None when there is none. Short reasons are returned as is; `evaluate` judges them."""
    raw = params.get("override", params.get("overrides"))
    if raw in (None, False, "", [], {}):
        return None
    items = raw if isinstance(raw, list | tuple) else [raw]
    best = ""
    for item in items:
        text = item.get("reason") if isinstance(item, Mapping) else item if isinstance(item, str) else None
        if isinstance(text, str) and len(text.strip()) > len(best):
            best = text.strip()
    return best or ""


def _number(params: Mapping[str, Any], name: str) -> float | None:
    v = params.get(name)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise ValueError(f"move parameter {name} must be a finite number, got {v!r}")
    if v < 0:
        raise ValueError(f"move parameter {name} cannot be negative, got {v!r}")
    return float(v)


def _with(detail: str, head: str) -> str:
    return f"{head}: {detail}" if detail else head


def _router_view(router_verdict: Any) -> dict[str, Any] | None:
    """mode / verdict / explanation of a RouterVerdict model or its dict, else None."""
    if router_verdict is None:
        return None
    if isinstance(router_verdict, Mapping):
        mode, verdict = router_verdict.get("mode"), router_verdict.get("verdict")
        text = router_verdict.get("explanation")
    else:
        mode = getattr(router_verdict, "mode", None)
        verdict = getattr(router_verdict, "verdict", None)
        text = getattr(router_verdict, "explanation", None)
    if mode not in ("advisory", "blocking"):
        raise ValueError(f"router verdict mode must be advisory or blocking, got {mode!r}")
    return {"mode": mode, "verdict": verdict, "explanation": text or ""}


def evaluate(
    move_kind: str, params: Mapping[str, Any] | None = None, router_verdict: Any = None
) -> dict[str, Any]:
    """Risk, block decision and label for one move. Raises RecipeError for a move that is not on the ladder and
    ValueError for a malformed dial or router verdict."""
    params = params or {}
    key = normalise_move(move_kind)
    try:
        row: RiskRow = risk_for(key)
    except RecipeError:
        raise RecipeError(f"unknown move {move_kind!r}; the ladder has {list(moves())}") from None

    risk = row.risk
    dials: dict[str, Any] = {}
    reasons: list[str] = []

    def raise_to(level: str, why: str) -> None:
        nonlocal risk
        if risk_rank(level) > risk_rank(risk):
            risk = level
            reasons.append(why)

    reveal = _number(params, "reveal_m")
    if reveal is not None and key in REVEAL_MOVES:
        dials["reveal_m"] = reveal
        if reveal > REVEAL_HIGH_M:
            raise_to(
                "high", f"reveal of {reveal:g} m is over {REVEAL_HIGH_M:g} m: the model invents the rest"
            )
        elif reveal > REVEAL_MED_M:
            raise_to("med", f"reveal of {reveal:g} m is over {REVEAL_MED_M:g} m")
    orbit = _number(params, "orbit_deg")
    if orbit is not None:
        dials["orbit_deg"] = orbit
        if orbit > ORBIT_MAX_DEG:
            raise_to(
                "highest", f"orbit of {orbit:g} degrees is over {ORBIT_MAX_DEG:g}: the elevation is drawn"
            )
    travel = _number(params, "travel_cm")
    if travel is not None and key == "slider_drift":
        dials["travel_cm"] = travel
        if travel > SLIDER_MAX_CM:
            raise_to("med", f"slider travel of {travel:g} cm is over {SLIDER_MAX_CM:g} cm")

    reason_text = override_reason(params)
    overridden = reason_text is not None and len(reason_text) >= MIN_OVERRIDE_REASON
    if reason_text is not None and not overridden:
        reasons.append(f"override reason needs at least {MIN_OVERRIDE_REASON} characters")

    blocked = False
    label: str | None = None
    if risk in BLOCKED_RISKS:
        if overridden:
            label = UNSEEN_GEOMETRY_LABEL
            reasons.append(f"{risk} risk move runs on override: {reason_text}")
        else:
            blocked = True
            reasons.append(f"{risk} risk move is blocked until it is overridden with a reason")

    router = _router_view(router_verdict)
    if router is not None and router["verdict"] == "render_move":
        if router["mode"] == "blocking":
            blocked = True
            label = None
            reasons.append(_with(router["explanation"], "router verdict render_move blocks in blocking mode"))
        else:
            label = UNSEEN_GEOMETRY_LABEL
            reasons.append(_with(router["explanation"], "router advises rendering this move (advisory)"))
    if not reasons:
        reasons.append(f"{row.label.lower()} risk: {row.invents}")

    return {
        "risk": risk,
        "blocked": blocked,
        "label": label,
        "reasons": reasons,
        "move_kind": key,
        "move": row.move,
        "ladder_risk": row.risk,
        "repair_codes": list(row.repair_codes),
        "invents": row.invents,
        "override": {"reason": reason_text, "accepted": bool(overridden)}
        if reason_text is not None
        else None,
        "dials": dials,
        "router": router,
    }
