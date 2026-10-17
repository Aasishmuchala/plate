"""Weekly canaries: twelve owned plates and four moves through every engine, and the drift that suspends one.

New for the PLATE monorepo (router plan T27; BUILD_PLAN section 4 "Routing": weekly canaries on twelve owned
plates and four moves auto-suspend an engine whose metrics drift or whose model string changes). A canary is a
fixed, owned input set sent to an engine on a schedule so that a vendor changing its model underneath us shows
up as a number, not as a client complaint.

`run(owned_plates, moves, adapters, gates)` drives each adapter through the same interface the pipeline uses
(`validate_params`, `submit(Staged)`, `poll`, `fetch`; nothing else) and scores every output with `gates`. It makes
no request itself: the adapters carry the transport, which is a recorded-fixture transport in tests and a live one
only under PLATE_LIVE=1 with a budget envelope (the caller's business). Image engines (`image_edit`, `image_gen`,
`local_image`) take the plates; video engines (`video_*`, `local_video`) take the moves; an upscaler gets
nothing (it is never run on a plate) and is listed under `skipped`.

    adapters: {engine_id: adapter}      an adapter exposes .manifest.kind, .engine_id, .route_id, .model_version
    gates:    (case, output_bytes, engine_id) -> "pass" | "fail" | "advisory"  (or a bool, a mapping or an object
              with .verdict, such as plate_gates' GateReport)

A case that raises inside the adapter (a refusal, a timeout, an unrecorded request) counts as a fail with the
error kept; an exception from `gates` is a bug in the harness and is not swallowed. The model string is the one
the vendor reports (`usage.model_version` or `usage.model` on the final poll), else the adapter's configured one;
if the cases of one run disagree it becomes "mixed:<a>|<b>", which differs from any single string.

`drift(previous, current)` says whether to suspend: the pass rate fell by more than 15 points, or the model string
changed (a missing string where there was one counts as a change; no previous run is a baseline, never drift).
With `alerts=` it appends a `canary_drift` and an `engine_suspended` alert, which `health.AlertLog.suspended()`
and `HealthBoard.apply_alerts` turn into a router skip. A person resumes the engine (`AlertLog.resume`).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from blake3 import blake3

from .adapters.base import Staged
from .health import AlertLog, as_alert_log

N_PLATES = 12
N_MOVES = 4
DROP_POINTS = 15.0  # a fall of more than this many percentage points suspends the engine
PLATE_KINDS = frozenset({"image_edit", "image_gen", "local_image"})
MOVE_KINDS = frozenset({"video_i2v", "video_r2v", "video_startend", "local_video"})
VERDICTS = ("pass", "fail", "advisory")


class CanaryError(ValueError):
    """The canary set or a gate result does not have the documented shape."""


@dataclass(frozen=True)
class CanaryCase:
    """One owned input. inputs use the engine's input names (image, prompt, image_tail ...)."""

    case_id: str
    kind: Literal["plate", "move"]
    inputs: Mapping[str, Any]
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    verdict: str  # pass | fail | advisory
    model_version: str | None = None
    output_id: str | None = None  # blake3[:16] of the output bytes
    error: str | None = None


@dataclass(frozen=True)
class EngineCanary:
    engine_id: str
    route_id: str
    n: int
    passes: int
    model_version: str | None
    results: tuple[CaseResult, ...] = ()

    @property
    def pass_rate(self) -> float:
        return self.passes / self.n if self.n else 0.0


@dataclass(frozen=True)
class CanaryReport:
    at: str
    engines: dict[str, EngineCanary]
    skipped: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "at": self.at,
            "skipped": dict(self.skipped),
            "engines": {
                k: {**asdict(e), "pass_rate": e.pass_rate, "results": [asdict(r) for r in e.results]}
                for k, e in sorted(self.engines.items())
            },
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CanaryReport:
        engines = {}
        for k, e in raw.get("engines", {}).items():
            engines[k] = EngineCanary(
                engine_id=e["engine_id"],
                route_id=e["route_id"],
                n=e["n"],
                passes=e["passes"],
                model_version=e.get("model_version"),
                results=tuple(CaseResult(**r) for r in e.get("results", [])),
            )
        return cls(at=raw.get("at", ""), engines=engines, skipped=dict(raw.get("skipped", {})))


def as_case(item: CanaryCase | Mapping[str, Any], kind: Literal["plate", "move"], index: int) -> CanaryCase:
    if isinstance(item, CanaryCase):
        if item.kind != kind:
            raise CanaryError(f"case {item.case_id!r} is a {item.kind}, listed as a {kind}")
        return item
    data = dict(item)
    return CanaryCase(
        case_id=str(data.get("case_id") or f"{kind}-{index + 1:02d}"),
        kind=kind,
        inputs=data["inputs"],
        params=data.get("params", {}),
    )


def normalise_verdict(result: Any) -> str:
    if isinstance(result, bool):
        return "pass" if result else "fail"
    if isinstance(result, str):
        verdict = result
    elif isinstance(result, Mapping):
        verdict = result.get("verdict")
    else:
        verdict = getattr(result, "verdict", None)
    if verdict not in VERDICTS:
        raise CanaryError(f"gate result {result!r} has no verdict of {VERDICTS}")
    return verdict


def _observed_version(status: Any, adapter: Any) -> str | None:
    usage = getattr(status, "usage", None) or {}
    for key in ("model_version", "model"):
        value = usage.get(key) if isinstance(usage, Mapping) else None
        if isinstance(value, str) and value:
            return value
    configured = getattr(adapter, "model_version", None)
    return configured if isinstance(configured, str) and configured else None


def _drive(
    adapter: Any,
    case: CanaryCase,
    sleep: Callable[[float], None],
    max_polls: int,
    poll_interval_s: float,
) -> tuple[bytes | None, str | None, str | None]:
    """(output bytes, model version, error). Any exception in the adapter becomes an error string."""
    version: str | None = None
    try:
        validate = getattr(adapter, "validate_params", None)
        params = validate(case.params) if callable(validate) else dict(case.params)
        job = adapter.submit(Staged(inputs=dict(case.inputs), params=params))
        status = None
        for _ in range(max_polls):
            status = adapter.poll(job)
            if status.state in ("done", "failed", "refused"):
                break
            sleep(poll_interval_s)
        else:
            return (
                None,
                version,
                f"timeout: job {job} still {status.state if status else '?'} after {max_polls} polls",
            )
        version = _observed_version(status, adapter)
        if status.state != "done":
            return None, version, f"{status.state}: {status.message}"
        if not status.outputs:
            return None, version, "done with no outputs"
        return adapter.fetch(status.outputs[0]), version, None
    except Exception as exc:
        return None, version, f"{type(exc).__name__}: {exc}"


def _joined_version(versions: Sequence[str | None]) -> str | None:
    seen = sorted({v for v in versions if v})
    if not seen:
        return None
    return seen[0] if len(seen) == 1 else "mixed:" + "|".join(seen)


def run(
    owned_plates: Sequence[CanaryCase | Mapping[str, Any]],
    moves: Sequence[CanaryCase | Mapping[str, Any]],
    adapters: Mapping[str, Any],
    gates: Callable[[CanaryCase, bytes, str], Any],
    *,
    n_plates: int = N_PLATES,
    n_moves: int = N_MOVES,
    advisory_is_pass: bool = True,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    max_polls: int = 240,
    poll_interval_s: float = 5.0,
) -> CanaryReport:
    """Run the canary set through every adapter and score it. The set must be 12 plates and 4 moves."""
    if len(owned_plates) != n_plates or len(moves) != n_moves:
        raise CanaryError(
            f"the canary set is {n_plates} owned plates and {n_moves} moves; "
            f"got {len(owned_plates)} plates and {len(moves)} moves"
        )
    plates = [as_case(p, "plate", i) for i, p in enumerate(owned_plates)]
    move_cases = [as_case(m, "move", i) for i, m in enumerate(moves)]
    now = (clock or (lambda: datetime.now(UTC)))()
    nap = sleep or time.sleep

    engines: dict[str, EngineCanary] = {}
    skipped: dict[str, str] = {}
    for engine_id in sorted(adapters):
        adapter = adapters[engine_id]
        kind = adapter.manifest.kind
        if kind in PLATE_KINDS:
            cases = plates
        elif kind in MOVE_KINDS:
            cases = move_cases
        else:
            skipped[engine_id] = f"kind {kind!r} has no canary set (an upscaler is never run on a plate)"
            continue
        results: list[CaseResult] = []
        for case in cases:
            data, version, error = _drive(adapter, case, nap, max_polls, poll_interval_s)
            if data is None:
                results.append(CaseResult(case.case_id, "fail", version, None, error))
                continue
            verdict = normalise_verdict(gates(case, data, engine_id))
            results.append(CaseResult(case.case_id, verdict, version, blake3(data).hexdigest()[:16]))
        ok = ("pass", "advisory") if advisory_is_pass else ("pass",)
        engines[engine_id] = EngineCanary(
            engine_id=engine_id,
            route_id=str(getattr(adapter, "route_id", "")),
            n=len(results),
            passes=sum(1 for r in results if r.verdict in ok),
            model_version=_joined_version([r.model_version for r in results]),
            results=tuple(results),
        )
    stamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return CanaryReport(at=stamp, engines=engines, skipped=skipped)


# ---------------------------------------------------------------------------- drift


def _read(entry: Any, name: str) -> Any:
    return entry.get(name) if isinstance(entry, Mapping) else getattr(entry, name, None)


def drift_reasons(previous: Any, current: Any, drop_points: float = DROP_POINTS) -> list[str]:
    """Why `current` should suspend the engine, compared with `previous` (empty list = no drift)."""
    if previous is None or current is None:
        return []
    reasons = []
    fell = round((float(_read(previous, "pass_rate")) - float(_read(current, "pass_rate"))) * 100, 6)
    if fell > drop_points:
        reasons.append(
            f"pass rate fell {fell:.1f} points ({_read(previous, 'pass_rate'):.3f} -> {_read(current, 'pass_rate'):.3f})"
        )
    before, after = _read(previous, "model_version"), _read(current, "model_version")
    if before is not None and before != after:
        reasons.append(f"model string changed ({before!r} -> {after!r})")
    return reasons


def drift(
    previous: Any,
    current: Any,
    *,
    alerts: AlertLog | str | Path | None = None,
    drop_points: float = DROP_POINTS,
) -> bool:
    """True when the engine should be suspended. previous / current are EngineCanary (or mappings with
    pass_rate, model_version and engine_id). No previous run is a baseline: False."""
    reasons = drift_reasons(previous, current, drop_points)
    log = as_alert_log(alerts)
    if reasons and log is not None:
        engine_id = _read(current, "engine_id") or _read(previous, "engine_id")
        message = "; ".join(reasons)
        log.write(
            "canary_drift",
            message,
            severity="critical",
            engine_id=engine_id,
            route_id=_read(current, "route_id"),
            data={
                "reasons": reasons,
                "previous": _read(previous, "pass_rate"),
                "current": _read(current, "pass_rate"),
            },
        )
        log.write("engine_suspended", message, severity="critical", engine_id=engine_id)
    return bool(reasons)


def compare(
    previous: CanaryReport | None,
    current: CanaryReport,
    *,
    alerts: AlertLog | str | Path | None = None,
    drop_points: float = DROP_POINTS,
) -> list[str]:
    """Engine ids to suspend: drift() over every engine that has a previous result."""
    if previous is None:
        return []
    return [
        engine_id
        for engine_id, now in sorted(current.engines.items())
        if drift(previous.engines.get(engine_id), now, alerts=alerts, drop_points=drop_points)
    ]
