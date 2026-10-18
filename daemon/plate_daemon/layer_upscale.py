"""AI-layer upscale at ingress (router plan T100, decision 55): AI layers may be upscaled, plates never.

When an engine answers smaller than the plate region it was sent (a 1K tier for a 4K plate), ingress would stretch
the take with a Lanczos resample. `install(pipeline)` puts a real upscaler in front of that: the ingress hook
`("upscale_ai_layer", hook)` on `Pipeline.ingress_hooks` (T92's seam) replaces the engine's raw bytes with an
upscaled PNG before `ingress.restore`, which then only maps the size back onto the plate.

- When. The raw result is `result_w x result_h`; the target is the size map's padded rectangle (`padded_size`, what
  ingress resamples to). The hook acts when `target_w / result_w >= 1.25` or `target_h / result_h >= 1.25` and the
  run's `params.upscale_layers` is not `off`: `factor = ceil(max(ratio_w, ratio_h))`, clamped to the engine's
  `scale` range and lowered until the output fits its `limits.max_output_mp`.
- Which engine. `auto` (the default) is Real-ESRGAN (C0, local). Without the gpu extra, CUDA or the weights (this
  Mac, CI), or under PLATE_STUB_LOCAL=1, the adapter's `stub_upscale` runs: a Lanczos resample flagged
  `stub: true`, which is what ingress would have done anyway, so a stub never makes a take worse. `magnific` is
  Magnific Precision v2, and only when the run's quote pre-declared it (`inputs.upscale.engine_id`), a Magnific
  route is allowed by the run's custody at that moment and PLATE_LIVE=1. When it cannot run (custody tightened,
  PLATE_LIVE unset, a size over its limits, a vendor failure) the free Real-ESRGAN path runs instead and the record
  says why (`fallback`): a paid take is never lost to its optional upscaler. The layer goes out as
  `role: ai_layer`; both adapters refuse `role: plate`.
- Record. `normalized.ingress_hooks.upscale_ai_layer = {engine_id, factor, stub, result_size, target, ...}` plus
  `mode`, `upscaled_size`, `output_key` (the upscaled PNG in the project's CAS), `reason` for a stub and, for
  Magnific, `route_id`, `provider_job_id`, `price_usd`. Nothing done (`off`, not small enough, no manifest): `{}`.
- Repeats. The upscaled PNG of a run attempt is journalled (`<home>/cache/layer-upscale/<run>.<attempt>.json`), so
  a daemon restarted inside NORMALIZING reuses it instead of paying Magnific twice. A re-composite (T99: the
  parent's raw take replayed, `inputs.recomposite_of`) reuses the parent's upscaled PNG, never calls Magnific and
  follows the parent's mode (a Magnific parent without a stored PNG falls back to `auto`).

The quote (`quote_node` wrapper; Edit nodes, a re-composite body passes through): `params.upscale_layers`, the
body's, else the node's (`upscale_layers` or `upscaleLayers`), is `auto` (default), `off` or `magnific`, else
422 `bad_upscale_layers`. `magnific` is checked before any run row: custody first (`CustodyViolation`, 403 at the
route, when no Magnific route is allowed by the project and the packet: an NDA project never sees it), then 422
`magnific_live_only` unless PLATE_LIVE=1. The Magnific per-image price is then added to the quote:
`quote.price_usd` = engine + upscaler, `price_status` the weaker of the two, and `inputs.upscale = {engine_id,
route_id, price_usd, price_status}`, both written before the `quoted` ledger record (the `after` wrapper), so the
folder holds the price that is approved; repeats are numbered against those inputs (the `next_attempt` wrapper).
`approve` re-checks custody and PLATE_LIVE for a declared Magnific pass before anything is spent.
`upscale_layers` stays in the run's params (repeat key, idempotency key) and is taken out of what the engine
receives (the `vendor_payload` wrapper): it is PLATE's option, not the vendor's.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from blake3 import blake3

from plate_pixels.imghdr import header_size

from . import canvas_rules
from .custody import require_route, route_allowed
from .db import dumps, tx
from .fsutil import read_json, write_json_atomic
from .machine import get_run, transition
from .models import Run
from .pipeline import PacketView, PipelineError, Staged
from .states import RunState
from .timeutil import iso

__all__ = [
    "DEFAULT_MODE",
    "HOOK_NAME",
    "INPUT_KEY",
    "MAGNIFIC",
    "MIN_RATIO",
    "MODES",
    "PARAM",
    "REAL_ESRGAN",
    "LayerUpscaler",
    "install",
    "plan_factor",
]

log = logging.getLogger(__name__)
S = RunState

HOOK_NAME = "upscale_ai_layer"
PARAM = "upscale_layers"
NODE_PARAMS = (PARAM, "upscaleLayers")
MODES = ("auto", "off", "magnific")
DEFAULT_MODE = "auto"
INPUT_KEY = "upscale"
MIN_RATIO = 1.25
REAL_ESRGAN = "real-esrgan"
MAGNIFIC = "magnific-precision-v2"
LIVE_ENV = "PLATE_LIVE"
RECOMPOSITE_KEY = "recomposite_of"  # T99: a re-composite's inputs name its parent run
LAYER_ROLE = "ai_layer"
JOURNAL_DIR = ("cache", "layer-upscale")
# Weakest last: a sum of two prices is as firm as its least firm part.
STATUS_ORDER = ("confirmed", "estimated", "unconfirmed", "fake")


class _Unavailable(RuntimeError):
    """Magnific cannot run for this run right now; the free local upscaler runs instead."""


@dataclass
class _Declaring:
    """A Magnific pass being declared by one `quote_node` call (read by the `next_attempt` and `after` wrappers)."""

    project_id: str
    node_id: str
    upscale: dict[str, Any]
    run_id: str | None = None


_DECLARING: ContextVar[_Declaring | None] = ContextVar("plate_layer_upscale_declaring", default=None)


# ---------------------------------------------------------------------------- pure helpers


def plan_factor(
    result_size: tuple[int, int],
    target: tuple[int, int],
    scale_range: tuple[int, int],
    max_output_mp: float | None = None,
) -> int | None:
    """The integer upscale factor for a `result_size` (w, h) layer that ingress maps onto `target` (w, h): None
    when neither side is at least MIN_RATIO short of the target; else `ceil(max ratio)` clamped to `scale_range`
    and lowered until `result x factor` fits `max_output_mp`. None when what is left is below 2 or below the range."""
    rw, rh = result_size
    tw, th = target
    if rw < 1 or rh < 1:
        return None
    ratio = max(tw / rw, th / rh)
    if ratio < MIN_RATIO:
        return None
    lo, hi = scale_range
    factor = max(lo, min(hi, math.ceil(round(ratio, 6))))
    if max_output_mp is not None:
        while factor > 1 and rw * factor * rh * factor / 1e6 > max_output_mp:
            factor -= 1
    if factor < max(2, lo):
        return None
    return factor


def _size_of(data: bytes) -> tuple[int, int] | None:
    """(width, height) of encoded image bytes from the header alone (`plate_pixels.imghdr.header_size`: PNG,
    JPEG, WebP, BMP, TIFF), never a decode, so a decompression bomb costs nothing here; None when the bytes are
    empty or not a readable image header. Never raises: ingress names the bad result."""
    if not data:
        return None
    try:
        return header_size(data)
    except Exception:  # header_size does not raise; this keeps the hook's promise if that ever changes
        return None


def _scale_range(manifest: Any) -> tuple[int, int]:
    p = next((p for p in manifest.params if p.name == "scale"), None)
    if p is None or p.range is None:
        return 2, 4
    return int(p.range[0]), int(p.range[1])


def _max_output_mp(manifest: Any) -> float | None:
    return manifest.limits.max_output_mp if manifest.limits is not None else None


def _weaker(a: str | None, b: str | None) -> str:
    """The less firm of two price statuses (an unknown status counts as `unconfirmed`)."""

    def rank(s: str | None) -> int:
        return STATUS_ORDER.index(s) if s in STATUS_ORDER else STATUS_ORDER.index("unconfirmed")

    return (a if rank(a) >= rank(b) else b) or "unconfirmed"


def _opaque_name(data: bytes) -> str:
    return f"{blake3(data).hexdigest()[:26]}.png"


def live() -> bool:
    return os.environ.get(LIVE_ENV) == "1"


# ---------------------------------------------------------------------------- the upscaler


class LayerUpscaler:
    """The quote-side declaration and the ingress hook for one Pipeline (see the module docstring)."""

    def __init__(self, pipeline: Any):
        self.pipeline = pipeline
        # Tests (and a future box adapter) may build the Magnific adapter: (run, manifest, route) -> adapter.
        self.magnific_factory: Callable[[Run, Any, Any], Any] | None = None
        self._local: Any = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ quote side

    def mode(self, body: Mapping[str, Any], node: Mapping[str, Any]) -> tuple[str, bool]:
        """(`params.upscale_layers`, whether it came from the node rather than the body); 422 for any other value."""
        params = body.get("params") if isinstance(body.get("params"), Mapping) else {}
        from_node = False
        value = params.get(PARAM)
        if value is None:
            node_params = node.get("params") if isinstance(node.get("params"), Mapping) else {}
            value = next((node_params[k] for k in NODE_PARAMS if node_params.get(k) is not None), None)
            from_node = value is not None
        if value is None:
            return DEFAULT_MODE, False
        if value not in MODES:
            raise PipelineError(
                f"params.{PARAM} is {value!r}; use one of {', '.join(MODES)}",
                code="bad_upscale_layers",
                allowed=list(MODES),
            )
        return str(value), from_node

    def allowed_for(self, project_id: str, graph: Any, node_id: str, body: Mapping[str, Any]) -> set[Any]:
        """The grades a run of this node may use: the project's custody ∩ its plate packet's."""
        pl = self.pipeline
        plates = canvas_rules.upstream_plates(graph, node_id)
        packet_id = body.get("packet_id") or (plates[0].get("packetId") if plates else None)
        if not isinstance(packet_id, str) or not packet_id:
            raise PipelineError(
                "generation must be anchored to a plate camera (source-view-only)", code="NO_PLATE_ANCHOR"
            )
        return pl.allowed_grades(project_id, pl.packet(project_id, packet_id).manifest)

    def magnific_manifest(self) -> Any:
        manifest = self.pipeline.manifests.get(MAGNIFIC)
        if manifest is None:
            raise PipelineError(f"no {MAGNIFIC} manifest is loaded", code="unknown_engine")
        return manifest

    def magnific_route(self, allowed: set[Any], route_id: str | None = None) -> Any:
        """The Magnific route this run may use (the declared one when named), else `CustodyViolation`."""
        routes = [r for r in self.magnific_manifest().routes if r.status == "active"]
        if route_id is not None:
            routes = [r for r in routes if r.route_id == route_id] or routes
        if not routes:
            raise PipelineError(f"{MAGNIFIC} has no active route", code="no_route")
        for r in routes:
            if route_allowed(r, allowed):
                return r
        require_route(routes[0], allowed, what=MAGNIFIC)
        return routes[0]  # unreachable: require_route raised

    def declaration(self, allowed: set[Any]) -> dict[str, Any]:
        """`inputs.upscale` for a Magnific pass: custody first (`CustodyViolation`), then PLATE_LIVE (422), then
        the manifest's per-image price."""
        from .quotes import build_quote

        route = self.magnific_route(allowed)
        if not live():
            raise PipelineError(
                f"{MAGNIFIC} is a vendor upscaler: set PLATE_LIVE=1 to pre-declare it on a quote",
                code="magnific_live_only",
            )
        q = build_quote(self.magnific_manifest(), route, {}, {})
        return {
            "engine_id": MAGNIFIC,
            "route_id": route.route_id,
            "price_usd": q.price_usd,
            "price_status": q.price_status,
        }

    def check_declared(self, run: Run) -> None:
        """At approval: a declared Magnific pass must still be allowed (custody, PLATE_LIVE)."""
        decl = run.inputs.get(INPUT_KEY)
        if not isinstance(decl, Mapping) or decl.get("engine_id") != MAGNIFIC:
            return
        pl = self.pipeline
        pv = pl.packet(run.project_id, run.packet_id)
        self.magnific_route(pl.allowed_grades(run.project_id, pv.manifest), decl.get("route_id"))
        if not live():
            raise PipelineError(
                f"run {run.id} pre-declared {MAGNIFIC}: set PLATE_LIVE=1 to approve it, or re-quote with "
                f"params.{PARAM} auto",
                code="magnific_live_only",
            )

    def declare_on(self, conn: Any, run_id: str, decl: Mapping[str, Any]) -> None:
        """Write the declaration into a QUOTED run: `inputs.upscale` and the quote's price and status (the
        `quotes` row and `runs.quote_json`), in one transaction."""
        run = get_run(conn, run_id)
        if run.state is not S.QUOTED or run.quote is None:
            raise PipelineError(f"run {run_id} is {run.state.value}, not QUOTED", code="upscale_unrecorded")
        terms = run.quote.terms_json()
        price = round(run.quote.price_usd + float(decl["price_usd"]), 6)
        terms.update(price_usd=price, price_status=_weaker(run.quote.price_status, decl.get("price_status")))
        inputs = {**run.inputs, INPUT_KEY: dict(decl)}
        with tx(conn):
            cur = conn.execute(
                "UPDATE runs SET inputs = ?, quote_json = ?, updated_utc = ? WHERE id = ? AND state = ?",
                (dumps(inputs), dumps(terms), iso(self.pipeline.clock()), run_id, S.QUOTED.value),
            )
            if cur.rowcount != 1:
                raise PipelineError(f"run {run_id} left QUOTED meanwhile", code="upscale_unrecorded")
            conn.execute("UPDATE quotes SET price_usd = ? WHERE id = ?", (price, run.quote.id))

    def cancel_undeclared(self, conn: Any, project_id: str, run_id: str, err: BaseException) -> None:
        """The declaration could not be written: the run must not be approved at the engine's price alone."""
        try:
            if get_run(conn, run_id).state is S.QUOTED:
                transition(
                    conn,
                    run_id,
                    S.CANCELLED,
                    actor="system",
                    reason=f"the {MAGNIFIC} pass could not be added to the quote ({type(err).__name__})",
                    reason_code="upscale_unrecorded",
                )
                self.pipeline.ledger(project_id, "cancelled", get_run(conn, run_id), conn=conn)
        except Exception as e:  # the refusal still goes out
            log.error("run %s could not be cancelled after the upscale declaration failed: %s", run_id, e)

    # ------------------------------------------------------------------ the ingress hook

    def hook(self, run: Run, data: bytes, staged: Staged, pv: PacketView) -> tuple[bytes, dict[str, Any]]:
        """Decision 55 (see the module docstring): the engine's raw bytes -> (bytes for ingress, record)."""
        pl = self.pipeline
        cas = pl.cas_for(run.project_id)
        digest = blake3(data).hexdigest()
        journalled = self.journalled(run, digest, cas)
        if journalled is not None:
            return journalled
        mode = self.run_mode(run)
        reused = (
            self.reuse_parent(run, digest, cas) if isinstance(run.inputs.get(RECOMPOSITE_KEY), str) else None
        )
        if reused is not None:
            return reused
        if mode == "off":
            return data, {}
        size = _size_of(data)
        if size is None:
            return data, {}  # ingress names the undecodable result
        target = tuple(int(v) for v in staged.sizemap.padded_size)
        record: dict[str, Any] = {"mode": mode, "result_size": list(size), "target": list(target)}
        out: bytes | None = None
        if mode == "magnific":
            try:
                out, extra = self.magnific(run, pv, data, size, target)
                record.update(extra)
            except Exception as e:  # never lose the paid take to its optional upscaler
                record["fallback"] = f"{MAGNIFIC} did not run: {type(e).__name__}: {e}"[:500]
                log.warning("run %s: %s", run.id, record["fallback"])
                out = None
        if out is None:
            local = self.local(data, size, target)
            if local is None:
                if "fallback" not in record:
                    return data, {}
                return data, record
            out, extra = local
            record.update(extra)
        up = _size_of(out)
        record["upscaled_size"] = list(up) if up else None
        record["output_key"] = cas.put_bytes(out, "png")
        self.journal(run, digest, record)
        return out, record

    def run_mode(self, run: Run) -> str:
        """The run's `params.upscale_layers`; a re-composite follows its parent (Magnific never on a free run)."""
        parent_id = run.inputs.get(RECOMPOSITE_KEY)
        if isinstance(parent_id, str):
            parent = self.run_of(run.project_id, parent_id)
            mode = (parent.params.get(PARAM) if parent is not None else None) or DEFAULT_MODE
            return "auto" if mode == "magnific" else str(mode)
        mode = run.params.get(PARAM) or DEFAULT_MODE
        if mode not in MODES:
            raise ValueError(f"params.{PARAM} is {mode!r}; use one of {', '.join(MODES)}")
        return str(mode)

    def run_of(self, project_id: str, run_id: str) -> Run | None:
        try:
            with self.pipeline.index.project(project_id) as conn:
                return get_run(conn, run_id)
        except Exception as e:
            log.warning("run %s not read for its upscale record: %s", run_id, e)
            return None

    def reuse_parent(self, run: Run, digest: str, cas: Any) -> tuple[bytes, dict[str, Any]] | None:
        """A re-composite's parent's upscaled PNG when these are the parent's raw bytes (`raw_key` is their BLAKE3)
        and the CAS still has the PNG: the same raw take gives the same layer, at no cost."""
        parent_id = str(run.inputs[RECOMPOSITE_KEY])
        parent = self.run_of(run.project_id, parent_id)
        norm = (parent.normalized or {}) if parent is not None else {}
        rec = (norm.get("ingress_hooks") or {}).get(HOOK_NAME)
        key = rec.get("output_key") if isinstance(rec, Mapping) else None
        if norm.get("raw_key") != digest or not isinstance(key, str) or not cas.exists(key):
            return None
        return cas.read_bytes(key), {**rec, "reused_from": parent_id}

    # -- journal: a restarted NORMALIZING step reuses the upscaled PNG instead of upscaling (or paying) again

    def journal_path(self, run: Run) -> Path:
        return Path(self.pipeline.settings.home, *JOURNAL_DIR) / f"{run.id}.{run.attempt}.json"

    def journalled(self, run: Run, digest: str, cas: Any) -> tuple[bytes, dict[str, Any]] | None:
        path = self.journal_path(run)
        if not path.is_file():
            return None
        try:
            doc = read_json(path)
            rec = doc.get("record") or {}
            key = rec.get("output_key")
            if doc.get("raw_blake3") != digest or not isinstance(key, str) or not cas.exists(key):
                return None
            return cas.read_bytes(key), dict(rec)
        except Exception as e:  # a bad journal is only a lost shortcut
            log.warning("upscale journal %s not read: %s", path.name, e)
            return None

    def journal(self, run: Run, digest: str, record: Mapping[str, Any]) -> None:
        try:
            write_json_atomic(self.journal_path(run), {"raw_blake3": digest, "record": dict(record)})
        except Exception as e:
            log.warning("upscale journal for %s not written: %s", run.id, e)

    # -- Real-ESRGAN (C0, local; the Lanczos stub where the model cannot run)

    def local_adapter(self) -> Any:
        with self._lock:
            if self._local is None:
                from plate_engines.adapters.upscale import RealESRGAN

                manifest = self.pipeline.manifests.get(REAL_ESRGAN)
                if manifest is None:
                    return None
                self._local = RealESRGAN(manifest, None, None)
            return self._local

    def local(
        self, data: bytes, size: tuple[int, int], target: tuple[int, int]
    ) -> tuple[bytes, dict[str, Any]] | None:
        """Real-ESRGAN by the planned factor: (bytes, record fields), or None when there is nothing to do."""
        from plate_engines.adapters.image_common import LocalModelUnavailable, stub_enabled

        adapter = self.local_adapter()
        if adapter is None:
            return None
        manifest = adapter.manifest
        factor = plan_factor(size, target, _scale_range(manifest), _max_output_mp(manifest))
        if factor is None:
            return None
        inputs = {"image": {"bytes": data, "role": LAYER_ROLE, "name": _opaque_name(data)}}
        params = {"scale": factor}
        adapter.quote(inputs, params)  # the plate refusal, the scale range and the limits, before any work
        prep = adapter.prepare(inputs, params)
        reason = ""
        if stub_enabled():
            out, stub, reason = adapter.stub_upscale(prep), True, "PLATE_STUB_LOCAL=1"
        else:
            try:
                with self._lock:
                    out, stub = adapter.run_pipeline(prep), False
            except LocalModelUnavailable as e:
                out, stub, reason = adapter.stub_upscale(prep), True, str(e)
        fields: dict[str, Any] = {"engine_id": REAL_ESRGAN, "factor": factor, "stub": stub}
        if reason:
            fields["reason"] = reason[:300]
        return out, fields

    # -- Magnific (D; pre-declared and paid on the quote, PLATE_LIVE=1)

    def magnific(
        self, run: Run, pv: PacketView, data: bytes, size: tuple[int, int], target: tuple[int, int]
    ) -> tuple[bytes | None, dict[str, Any]]:
        """Magnific by the planned factor: (bytes, record fields); (None, {}) when the layer needs no upscale.
        Raises `_Unavailable` (or the adapter's own error) when it cannot run; the caller falls back."""
        from plate_engines.adapters.base import Staged as EngineStaged

        decl = run.inputs.get(INPUT_KEY)
        if not isinstance(decl, Mapping) or decl.get("engine_id") != MAGNIFIC:
            raise _Unavailable(f"the quote of run {run.id} did not pre-declare {MAGNIFIC}")
        pl = self.pipeline
        route = self.magnific_route(pl.allowed_grades(run.project_id, pv.manifest), decl.get("route_id"))
        if not live():
            raise _Unavailable("PLATE_LIVE is not 1")
        manifest = self.magnific_manifest()
        factor = plan_factor(size, target, _scale_range(manifest), _max_output_mp(manifest))
        if factor is None:
            return None, {}
        adapter = self.magnific_adapter(run, pv, manifest, route)
        inputs = {"image": {"bytes": data, "role": LAYER_ROLE, "name": _opaque_name(data)}}
        adapter.quote(inputs, {"scale": factor})  # the plate refusal and the 6 MP / 50 MP limits
        job = adapter.submit(EngineStaged(inputs=inputs, params=adapter.validate_params({"scale": factor})))
        polls = max(1, int(pl.poll_timeout_s / max(pl.poll_interval_s, 1e-3)))
        for _ in range(polls):
            st = adapter.poll(job)
            if st.state == "done":
                if not st.outputs:
                    raise _Unavailable(f"{MAGNIFIC} finished without an output")
                out = adapter.fetch(st.outputs[0])
                break
            if st.state in ("failed", "refused"):
                raise _Unavailable(f"{MAGNIFIC} {st.state}: {st.message}")
            pl.sleep(pl.poll_interval_s)
        else:
            raise _Unavailable(f"{MAGNIFIC} job {job} did not finish in {pl.poll_timeout_s:g} s")
        return out, {
            "engine_id": MAGNIFIC,
            "factor": factor,
            "stub": False,
            "route_id": route.route_id,
            "provider_job_id": job,
            "price_usd": decl.get("price_usd"),
        }

    def magnific_adapter(self, run: Run, pv: PacketView, manifest: Any, route: Any) -> Any:
        """The Magnific adapter on the project's Egress transport (allowlist, metadata strip, outbound log)."""
        if self.magnific_factory is not None:
            return self.magnific_factory(run, manifest, route)
        from plate_engines.adapters.upscale import MagnificPrecisionV2

        from .egress import EgressRun

        pl = self.pipeline
        project = pl.load_project(run.project_id)
        erun = EgressRun.for_project(
            project.doc if project else {"project_id": run.project_id},
            run.id,
            packet=pv.manifest,
            legend=pv.legend,
            packet_dir=pv.root,
        )
        return MagnificPrecisionV2(
            manifest,
            route,
            transport=pl.egress_for(run.project_id).transport(erun, route),
            staging_dir=pl.settings.home / "cache" / "adapters" / run.id / "upscale",
        )


# ---------------------------------------------------------------------------- wiring into a Pipeline


def _strip_param(payload: dict[str, Any]) -> dict[str, Any]:
    params = payload.get("params")
    if isinstance(params, Mapping) and PARAM in params:
        payload = {**payload, "params": {k: v for k, v in params.items() if k != PARAM}}
    return payload


def install(pipeline: Any) -> LayerUpscaler:
    """Register the `upscale_ai_layer` ingress hook and wrap `quote_node`, `approve`, `next_attempt`, `after` and
    `vendor_payload` on `pipeline` (idempotent; the upscaler is kept at `pipeline.layer_upscaler`)."""
    existing = getattr(pipeline, "layer_upscaler", None)
    if existing is not None:
        return existing
    up = LayerUpscaler(pipeline)
    pipeline.layer_upscaler = up
    if not any(name == HOOK_NAME for name, _ in pipeline.ingress_hooks):
        pipeline.ingress_hooks.append((HOOK_NAME, up.hook))
    orig_quote, orig_approve = pipeline.quote_node, pipeline.approve
    orig_next, orig_after, orig_payload = pipeline.next_attempt, pipeline.after, pipeline.vendor_payload

    def quote_node(
        conn: Any, project_id: str, node_id: str, body: Mapping[str, Any], *, now: datetime | None = None
    ) -> Any:
        body = dict(body or {})
        params = body.get("params") if isinstance(body.get("params"), Mapping) else {}
        project = pipeline.load_project(project_id)
        g = canvas_rules.as_graph(project.graph) if project is not None else None
        node = next((n for n in g.nodes if n.get("id") == node_id), None) if g is not None else None
        if node is None or node.get("kind") != "Edit" or params.get(RECOMPOSITE_KEY) is not None:
            return orig_quote(conn, project_id, node_id, body, now=now)
        mode, from_node = up.mode(body, node)
        if from_node:
            body["params"] = {**params, PARAM: mode}
        if mode != "magnific":
            return orig_quote(conn, project_id, node_id, body, now=now)
        decl = up.declaration(up.allowed_for(project_id, g, node_id, body))
        state = _Declaring(project_id, node_id, decl)
        token = _DECLARING.set(state)
        try:
            run, quote = orig_quote(conn, project_id, node_id, body, now=now)
        finally:
            _DECLARING.reset(token)
        # The quote never reached `after` (another wrapper made the run): declare now; its `quoted` record is short.
        if state.run_id is None:
            log.warning("run %s: the %s pass was declared after its quoted record", run.id, MAGNIFIC)
            try:
                up.declare_on(conn, run.id, decl)
            except Exception as e:
                up.cancel_undeclared(conn, project_id, run.id, e)
                raise
        run = get_run(conn, run.id)
        return run, run.quote

    def next_attempt(
        conn: Any,
        node_id: str,
        inputs: Mapping[str, Any],
        params: Mapping[str, Any],
        route: Mapping[str, Any],
    ) -> int:
        state = _DECLARING.get()
        if state is not None and state.node_id == node_id and INPUT_KEY not in inputs:
            inputs = {**inputs, INPUT_KEY: state.upscale}
        return orig_next(conn, node_id, inputs, params, route)

    def after(conn: Any, project_id: str, run_id: str, before: RunState, *, note: str | None = None) -> None:
        state = _DECLARING.get()
        if (
            state is not None
            and state.run_id is None
            and before is S.DRAFT
            and project_id == state.project_id
        ):
            run = get_run(conn, run_id)
            if run.state is S.QUOTED and run.node_id == state.node_id:
                state.run_id = run_id
                try:
                    up.declare_on(conn, run_id, state.upscale)
                except Exception as e:
                    up.cancel_undeclared(conn, project_id, run_id, e)
                    raise PipelineError(
                        f"the {MAGNIFIC} pass could not be added to the quote: {e}",
                        code="upscale_unrecorded",
                        status=503,
                    ) from e
        return orig_after(conn, project_id, run_id, before, note=note)

    def approve(
        conn: Any,
        project_id: str,
        run_id: str,
        by: str,
        envelope_id: str | None = None,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> Any:
        up.check_declared(get_run(conn, run_id))
        return orig_approve(conn, project_id, run_id, by, envelope_id, reason=reason, now=now)

    def vendor_payload(run: Run, staged: Staged, seed_payload: Mapping[str, Any]) -> dict[str, Any]:
        return _strip_param(orig_payload(run, staged, seed_payload))

    pipeline.quote_node, pipeline.approve = quote_node, approve
    pipeline.next_attempt, pipeline.after, pipeline.vendor_payload = next_attempt, after, vendor_payload
    return up
