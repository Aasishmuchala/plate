"""Quote before spend (doctrine 8): quotes, approval and agent budget envelopes.

`build_quote(engine_manifest, route, inputs, params)` prices a request from a `plate_engines` manifest: the
route's provider and region, its effective custody grade (unconfirmed terms and public uploads count as D),
the model version the route will call, the chosen size and the price (the first `price.tiers` row whose params
match the request, else `price.by[<size label>]`, else `price.usd`; times megapixels or seconds when the price
model says so, raised to `price.minimum_usd`, times the image count). A route that carries its own `price`
(Runway) is priced from that spec, every other route from the manifest's. A tier row prices by quality, image
size, resolution or audio and carries its own status; the quote names it in `price_tier`. A quote expires 15
minutes after it is made; `requote_needed(old, new)` says when a fresh quote must be shown again (price moved
more than 5 %, or the model version changed). `make_quote` uses the manifest when one is known for the run's
engine and the fake 0.04 USD price otherwise.

`quote_run` moves DRAFT -> QUOTED with the quote stored (the `quotes` row for approval bookkeeping, the full
terms in `runs.quote_json`). `approve` moves QUOTED -> APPROVED only before expiry, otherwise commits
QUOTED -> EXPIRED and raises `QuoteExpired`. A `human` approves without asking the policy (an envelope, when
named, must still have room); `auto` and `agent` approvals go through `budgets.authorize` (agents need an
envelope) and raise `EnvelopeRequired`, `EnvelopeExceeded` or `AutoApprovalDenied` when refused. The
machine reserves the spend in the same transaction (and debits the envelope). `approve_group` approves a
best-of-N group against the sum of its siblings' prices in one transaction. Envelopes are stored in the
project's index, so an envelope budgets one owner on one project.

Wave 12 (router plan T129, decision 80). The daily and unattended caps read `spent` when the caller passes one
(`Pipeline.approve` passes `budgets.spend_snapshot_all`, every project index in this home); without it they read
this connection's project, as before. A quote created more than 60 s after `now` (the clock moved back) is
refused like a lapsed one: both are EXPIRED and the `QuoteExpired` carries their `run_ids`, so the caller can
ledger the moves. An envelope's limits are finite, at most 1 000 000 USD, and live at most 366 days; a price's
count (`candidate_count`, `num_images` or `n`) is a whole number of at least 1 and every quantity is finite.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from . import budgets
from .db import loads, tx
from .machine import IllegalTransition, QuoteExpired, get_run, new_id, transition
from .models import QUOTE_TTL, BudgetEnvelope, Quote, Run
from .states import RunState
from .timeutil import iso, utcnow

__all__ = [
    "AutoApprovalDenied",
    "EnvelopeExceeded",
    "EnvelopeRequired",
    "QuoteExpired",
    "QuoteNotFound",
    "approve",
    "approve_group",
    "build_quote",
    "create_envelope",
    "expire_if_due",
    "get_envelope",
    "get_quote",
    "insert_quote",
    "make_quote",
    "quote_run",
    "requote_needed",
]

FAKE_PRICE_USD = 0.04
FAKE_ETA_S = 60.0
REQUOTE_PRICE_TOLERANCE = 0.05
CUSTODY_GRADES = ("C0", "C1", "C2", "D")
APPROVERS = ("human", "auto", "agent")
# No manifest carries an ETA yet: a per-kind guess, shown as such until the engine cards measure it (T27).
DEFAULT_ETA_S = {
    "image_edit": 60.0,
    "image_gen": 60.0,
    "upscale": 90.0,
    "local_image": 120.0,
    "video_i2v": 300.0,
    "video_r2v": 300.0,
    "video_startend": 300.0,
    "local_video": 900.0,
}
SIZE_LABEL_PARAMS = ("image_size", "resolution", "size")
DURATION_PARAMS = ("duration_s", "duration", "seconds")
COUNT_PARAMS = ("candidate_count", "num_images", "n")
# Decision 80: what an envelope may hold, and how far ahead of the approver's clock a quote may have been made.
ENVELOPE_MAX_USD = 1_000_000.0
ENVELOPE_MAX_TTL_S = 31_622_400  # 366 days
QUOTE_CLOCK_SKEW = timedelta(seconds=60)


class EnvelopeRequired(Exception):
    """An agent approval has no valid budget envelope for this price."""


class EnvelopeExceeded(EnvelopeRequired):
    """The envelope exists but the price breaks its per-run cap or its remaining total."""


class AutoApprovalDenied(EnvelopeRequired):
    """The policy refused an auto or agent approval (auto cap, daily cap, unattended cap, no quote).

    A subclass of EnvelopeRequired so the API's existing refusal mapping covers it."""


class QuoteNotFound(KeyError):
    pass


# ---------------------------------------------------------------------------- pricing (pure)


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def _pick_route(manifest: Any, route: Any) -> Any:
    routes = [r for r in manifest.routes if r.status == "active"] or list(manifest.routes)
    if route is None:
        return routes[0]
    if isinstance(route, str):
        wanted = {"route_id": route}
    else:
        wanted = {k: _get(route, k) for k in ("route_id", "provider", "region")}
    for r in manifest.routes:
        if wanted.get("route_id") and r.route_id == wanted["route_id"]:
            return r
    for r in manifest.routes:
        if wanted.get("provider") and r.provider == wanted["provider"]:
            if not wanted.get("region") or wanted["region"] in (r.region, "unknown") or r.region == "unknown":
                return r
    raise ValueError(f"{manifest.id} has no route matching {wanted}")


def _pick_size(manifest: Any, params: Mapping[str, Any]) -> dict[str, Any] | None:
    w, h = params.get("width"), params.get("height")
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        return {"w": w, "h": h, "label": ""}
    size = params.get("size")
    if isinstance(size, Mapping) and isinstance(size.get("w"), int) and isinstance(size.get("h"), int):
        return {"w": size["w"], "h": size["h"], "label": str(size.get("label", ""))}
    sizes = getattr(manifest, "sizes", None)
    for name in SIZE_LABEL_PARAMS:
        label = params.get(name)
        if isinstance(label, str) and label:
            for legal in getattr(sizes, "legal", None) or []:
                if legal.label == label:
                    return {"w": legal.w, "h": legal.h, "label": label}
            return {"w": 0, "h": 0, "label": label}
    if sizes is None:
        return None
    if sizes.auto:
        return {"w": 0, "h": 0, "label": "auto"}
    if sizes.legal:
        first = sizes.legal[0]
        return {"w": first.w, "h": first.h, "label": first.label}
    return None


def _number(params: Mapping[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        v = params.get(name)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if not math.isfinite(v):
                raise ValueError(f"{name} is {v!r}; a price is computed from finite numbers only")
            if v > 0:
                return float(v)
    return None


def _count(params: Mapping[str, Any]) -> int:
    """How many images the request asks for: the first of `COUNT_PARAMS` that is set, else 1. A whole number of
    at least 1 (a bool is not a number here)."""
    for name in COUNT_PARAMS:
        v = params.get(name)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError("candidate_count/num_images/n must be a whole number of at least 1")
        return v
    return 1


def _norm(value: Any) -> str:
    """The comparable form of a param or tier value: lower-cased text, booleans as true/false, 1.0 as 1."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip().lower()


def _tier_name(match: Mapping[str, Any]) -> str:
    """A tier row's name for Quote.price_tier: `quality=high`, joined with commas when it matches several params."""
    return ",".join(f"{k}={_norm(v) if isinstance(v, bool) else v}" for k, v in match.items())


def _tier_context(manifest: Any, params: Mapping[str, Any], label: str) -> dict[str, str]:
    """What a price tier can match on: the chosen size label, every manifest param's default, then the request's
    params over them. Unset (None) and non-scalar values never enter, so a tier on them cannot match."""
    ctx: dict[str, Any] = {"size": label}
    defaults = {p.name: p.default for p in getattr(manifest, "params", None) or []}
    for source in (defaults, params):
        for key, value in source.items():
            if isinstance(value, (str, int, float, bool)):
                ctx[key] = value
    return {key: _norm(value) for key, value in ctx.items()}


def _pick_tier(spec: Any, ctx: Mapping[str, str]) -> Any:
    """The first price tier of `spec` whose every match entry equals the context value; None when none does."""
    for tier in getattr(spec, "tiers", None) or []:
        if all(key in ctx and ctx[key] == _norm(want) for key, want in tier.match.items()):
            return tier
    return None


def build_quote(
    engine_manifest: Any,
    route: Any,
    inputs: Mapping[str, Any],
    params: Mapping[str, Any],
    *,
    now: datetime | None = None,
    run_id: str | None = None,
) -> Quote:
    """Price one request on one route of a `plate_engines.models.EngineManifest` (pure)."""
    from plate_engines.router import effective_grade

    now = now or utcnow()
    m = engine_manifest
    r = _pick_route(m, route)
    size = _pick_size(m, params)
    label = (size or {}).get("label") or ""
    route_price = getattr(r, "price", None)
    spec = route_price if route_price is not None else m.price  # a route with its own price (Runway) wins
    tier = _pick_tier(spec, _tier_context(m, params, label))
    if tier is not None:
        unit, price_status = tier.usd, tier.status
        price_tier: str | None = _tier_name(tier.match)
    else:
        unit, price_status, price_tier = spec.by.get(label, spec.usd), spec.status, None
    if spec.model == "per_megapixel":
        mp = (
            (size["w"] * size["h"] / 1e6)
            if size and size["w"] and size["h"]
            else _number(params, ("megapixels",))
        )
        if mp is None:
            raise ValueError(f"{m.id}: per_megapixel pricing needs a width and height (or megapixels)")
        qty = mp
    elif spec.model == "per_second":
        secs = _number(params, DURATION_PARAMS)
        if secs is None:
            raise ValueError(f"{m.id}: per_second pricing needs a duration ({', '.join(DURATION_PARAMS)})")
        qty = secs
    else:
        qty = 1.0
    count = _count(params)
    return Quote(
        run_id=run_id,
        provider=r.provider,
        model=m.id,
        model_version=r.model_slug or m.model_id or m.version,
        region=r.region,
        custody_grade=effective_grade(r),
        price_usd=round(max(unit * qty, spec.minimum_usd) * count, 6),
        eta_s=DEFAULT_ETA_S.get(m.kind, 120.0),
        created_utc=now,
        expires_utc=now + QUOTE_TTL,
        size=size,
        route_id=r.route_id,
        price_status=price_status,
        price_tier=price_tier,
    )


def requote_needed(old: Quote, new: Quote) -> bool:
    """A fresh quote must be shown again when the price moved more than 5 % or the model version changed."""
    if old.model_version != new.model_version:
        return True
    if old.price_usd <= 0:
        return new.price_usd > 1e-9
    return abs(new.price_usd - old.price_usd) / old.price_usd > REQUOTE_PRICE_TOLERANCE + 1e-12


def make_quote(run: Run, manifests: Mapping[str, Any] | None = None, *, now: datetime | None = None) -> Quote:
    """Price a run: from its engine manifest when `manifests` has it, else the fake 0.04 USD price.
    The fake path takes custody from the route, else D (unknown is treated as public)."""
    now = now or utcnow()
    route = run.route_dict
    if manifests and route["engine_id"] in manifests:
        return build_quote(
            manifests[route["engine_id"]], route, run.inputs, run.params, now=now, run_id=run.id
        )
    grade = route.get("custody_grade") if route.get("custody_grade") in CUSTODY_GRADES else "D"
    if route.get("custody_status") not in (None, "confirmed"):
        grade = "D"  # pending written terms counts as D (decision 5a)
    return Quote(
        run_id=run.id,
        provider=route["provider"],
        model=route["engine_id"],
        model_version=str(route.get("model_version") or "fake"),
        region=route["region"],
        custody_grade=grade,
        price_usd=FAKE_PRICE_USD,
        eta_s=FAKE_ETA_S,
        created_utc=now,
        expires_utc=now + QUOTE_TTL,
        route_id=route.get("route_id"),
        price_status="fake",
    )


# ---------------------------------------------------------------------------- storage


def insert_quote(conn: sqlite3.Connection, q: Quote) -> None:
    conn.execute(
        """INSERT INTO quotes (id, run_id, provider, model, region, custody_grade, price_usd, eta_s,
                               created_utc, expires_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            q.id,
            q.run_id,
            q.provider,
            q.model,
            q.region,
            q.custody_grade,
            q.price_usd,
            q.eta_s,
            iso(q.created_utc),
            iso(q.expires_utc),
        ),
    )


def get_quote(conn: sqlite3.Connection, quote_id: str) -> Quote:
    row = conn.execute(
        "SELECT q.*, r.quote_json AS quote_json FROM quotes q LEFT JOIN runs r ON r.id = q.run_id WHERE q.id = ?",
        (quote_id,),
    ).fetchone()
    if row is None:
        raise QuoteNotFound(quote_id)
    terms = loads(row["quote_json"])
    return Quote.from_row(row, terms if isinstance(terms, dict) and terms.get("id") == quote_id else None)


def quote_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    actor: str = "system",
    now: datetime | None = None,
    manifests: Mapping[str, Any] | None = None,
) -> tuple[Run, Quote]:
    """DRAFT -> QUOTED with a stored quote, atomically."""
    now = now or utcnow()
    with tx(conn):
        run = get_run(conn, run_id)
        if run.state is not RunState.DRAFT:
            raise IllegalTransition(run.state, RunState.QUOTED)
        q = make_quote(run, manifests, now=now)
        insert_quote(conn, q)
        run = transition(
            conn,
            run_id,
            RunState.QUOTED,
            actor=actor,
            now=now,
            reason=f"{q.provider}/{q.model} {q.model_version} {q.region} {q.custody_grade} {q.price_usd:.4f} USD",
            quote_id=q.id,
            quote_json=q.terms_json(),
        )
    return run, q


# ---------------------------------------------------------------------------- envelopes


def create_envelope(
    conn: sqlite3.Connection,
    *,
    owner: str,
    max_total_usd: float,
    max_per_run_usd: float,
    ttl_s: float,
    now: datetime | None = None,
) -> BudgetEnvelope:
    if not all(math.isfinite(v) for v in (max_total_usd, max_per_run_usd, ttl_s)):
        raise ValueError("envelope limits and ttl_s must be finite numbers")
    if max_total_usd < 0 or max_per_run_usd < 0 or ttl_s <= 0:
        raise ValueError("envelope limits must be >= 0 and ttl_s > 0")
    if max_total_usd > ENVELOPE_MAX_USD or max_per_run_usd > ENVELOPE_MAX_USD:
        raise ValueError(f"envelope limits are at most {ENVELOPE_MAX_USD:,.0f} USD")
    if ttl_s > ENVELOPE_MAX_TTL_S:
        raise ValueError(f"an envelope lives at most {ENVELOPE_MAX_TTL_S} s (366 days)")
    budgets.check_owner(owner)
    now = now or utcnow()
    env = BudgetEnvelope(
        id=new_id("env", now),
        owner=owner,
        max_total_usd=max_total_usd,
        max_per_run_usd=max_per_run_usd,
        spent_usd=0.0,
        created_utc=now,
        expires_utc=now + timedelta(seconds=ttl_s),
    )
    with tx(conn):
        conn.execute(
            """INSERT INTO budget_envelopes (id, owner, max_total_usd, max_per_run_usd, spent_usd,
                                             created_utc, expires_utc) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                env.id,
                env.owner,
                env.max_total_usd,
                env.max_per_run_usd,
                0.0,
                iso(env.created_utc),
                iso(env.expires_utc),
            ),
        )
    return env


def get_envelope(conn: sqlite3.Connection, envelope_id: str) -> BudgetEnvelope | None:
    row = conn.execute("SELECT * FROM budget_envelopes WHERE id = ?", (envelope_id,)).fetchone()
    return None if row is None else BudgetEnvelope.from_row(row)


# ---------------------------------------------------------------------------- approval


def _parse_actor(actor: str, envelope_id: str | None) -> tuple[str, str | None]:
    if isinstance(actor, str) and actor.startswith("agent:"):
        named = actor.split(":", 1)[1]
        if envelope_id and envelope_id != named:
            raise ValueError(f"actor names envelope {named} but envelope_id is {envelope_id}")
        return "agent", named
    if actor not in APPROVERS:
        raise ValueError(f"actor must be one of {APPROVERS} (or agent:<envelope_id>)")
    return actor, envelope_id


def _refusal(decision: budgets.Decision) -> EnvelopeRequired:
    if decision.code == "envelope_required":
        return EnvelopeRequired(decision.reason)
    if decision.code in ("envelope_exceeded", "envelope_expired"):
        return EnvelopeExceeded(decision.reason)
    return AutoApprovalDenied(decision.reason)


def _expired(message: str, run_ids: list[str]) -> QuoteExpired:
    """`QuoteExpired` naming the runs it moved to EXPIRED (`run_ids`), so the caller can ledger each move."""
    exc = QuoteExpired(message)
    exc.run_ids = list(run_ids)  # type: ignore[attr-defined]
    return exc


def _approve_runs(
    conn: sqlite3.Connection,
    run_ids: list[str],
    actor: str,
    envelope_id: str | None,
    now: datetime | None,
    policy: budgets.Policy | None,
    last_human_interaction_utc: datetime | None,
    on_alert: Callable[[budgets.Decision], None] | None,
    spent: budgets.Spend | None = None,
) -> list[Run]:
    actor, envelope_id = _parse_actor(actor, envelope_id)
    now = now or utcnow()
    # Refusals that must still commit (EXPIRED) or report (an alert) are raised after the transaction.
    outcome: dict[str, Any] = {"raise": None, "decision": None}

    def body() -> list[Run]:
        runs = [get_run(conn, rid) for rid in run_ids]
        for run in runs:
            if run.state is not RunState.QUOTED:
                raise IllegalTransition(run.state, RunState.APPROVED)
            if run.quote is None:
                raise QuoteNotFound(f"run {run.id} is QUOTED without a stored quote")
        future = [r for r in runs if r.quote.created_utc > now + QUOTE_CLOCK_SKEW]
        lapsed = [r for r in runs if r not in future and r.quote.expired(now)]
        if future or lapsed:
            for r in lapsed:
                transition(
                    conn,
                    r.id,
                    RunState.EXPIRED,
                    actor="system",
                    now=now,
                    reason=f"quote {r.quote.id} expired at {iso(r.quote.expires_utc)}",
                )
            for r in future:
                transition(
                    conn,
                    r.id,
                    RunState.EXPIRED,
                    actor="system",
                    now=now,
                    reason=f"quote {r.quote.id} was created at {iso(r.quote.created_utc)}, after {iso(now)}: "
                    "the clock moved",
                )
            if future:
                message = (
                    f"quote for run {', '.join(r.id for r in future)} was created in the future; the clock "
                    "moved, re-quote before approving"
                )
            else:
                message = (
                    f"quote for run {', '.join(r.id for r in lapsed)} expired; re-quote before approving"
                )
            outcome["raise"] = _expired(message, [r.id for r in (*lapsed, *future)])
            return []
        row = get_envelope(conn, envelope_id) if envelope_id else None
        if envelope_id and row is None:
            raise EnvelopeRequired(f"no envelope {envelope_id} in this project")
        env = budgets.Envelope.from_db(row) if row is not None else None
        total = sum(r.quote.price_usd for r in runs)
        if actor == "human":
            by = "human"
            problem = budgets.envelope_problem(env, total, now) if env is not None else None
            if problem is not None:
                raise EnvelopeExceeded(problem[1])
        else:
            decision = budgets.authorize(
                runs[0],
                policy or budgets.Policy(),
                env,
                last_human_interaction_utc,
                now=now,
                spent=spent
                if spent is not None
                else budgets.spend_snapshot(conn, now, last_human_interaction_utc),
                requester=actor,
                price_usd=total,
            )
            outcome["decision"] = decision
            if not decision.approve:
                outcome["raise"] = _refusal(decision)
                return []
            by = decision.by
        out = []
        for r in runs:
            conn.execute(
                "UPDATE quotes SET envelope_id = ? WHERE id = ?",
                (env.envelope_id if env else None, r.quote.id),
            )
            out.append(
                transition(
                    conn,
                    r.id,
                    RunState.APPROVED,
                    actor=by,
                    approved_by=by,
                    now=now,
                    reason=f"approved {r.quote.price_usd:.4f} USD"
                    + (f" from envelope {env.envelope_id}" if env else ""),
                )
            )
        return out

    try:
        with tx(conn):
            approved = body()
    finally:
        decision = outcome["decision"]
        if decision is not None and decision.alert and on_alert is not None:
            on_alert(decision)
    if outcome["raise"] is not None:
        raise outcome["raise"]
    return approved


def approve(
    conn: sqlite3.Connection,
    run_id: str,
    actor: str,
    envelope_id: str | None = None,
    now: datetime | None = None,
    *,
    policy: budgets.Policy | None = None,
    last_human_interaction_utc: datetime | None = None,
    on_alert: Callable[[budgets.Decision], None] | None = None,
    spent: budgets.Spend | None = None,
) -> Run:
    """QUOTED -> APPROVED as `human`, `auto` or `agent` (`agent:<envelope_id>` also accepted). `spent` is what
    the caps count as already committed (None: this connection's project)."""
    return _approve_runs(
        conn, [run_id], actor, envelope_id, now, policy, last_human_interaction_utc, on_alert, spent
    )[0]


def approve_group(
    conn: sqlite3.Connection,
    sibling_group: str,
    actor: str,
    envelope_id: str | None = None,
    now: datetime | None = None,
    *,
    policy: budgets.Policy | None = None,
    last_human_interaction_utc: datetime | None = None,
    on_alert: Callable[[budgets.Decision], None] | None = None,
    spent: budgets.Spend | None = None,
) -> list[Run]:
    """Approve every QUOTED sibling of a best-of-N group at once, against the group's total price."""
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM runs WHERE json_extract(quote_json, '$.sibling_group') = ? AND state = ? "
            "ORDER BY json_extract(quote_json, '$.sibling_index')",
            (sibling_group, RunState.QUOTED.value),
        )
    ]
    if not ids:
        raise QuoteNotFound(f"no QUOTED siblings in group {sibling_group}")
    return _approve_runs(
        conn, ids, actor, envelope_id, now, policy, last_human_interaction_utc, on_alert, spent
    )


def expire_if_due(conn: sqlite3.Connection, run_id: str, now: datetime, actor: str = "recovery") -> bool:
    """QUOTED past expiry -> EXPIRED. Returns True if it expired the run."""
    with tx(conn):
        run = get_run(conn, run_id)
        if run.state is not RunState.QUOTED or run.quote is None:
            return False
        if not run.quote.expired(now):
            return False
        transition(
            conn,
            run_id,
            RunState.EXPIRED,
            actor=actor,
            now=now,
            reason=f"quote {run.quote.id} expired at {iso(run.quote.expires_utc)}",
        )
        return True
