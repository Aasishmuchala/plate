"""Shared plumbing of the numeric nodes (T48): the local routes, the contexts a node module works in, and the
small helpers all of them need (parameters, PNG16 in and out of the CAS, material coverage, report building).

A node module (`set_node`, `recolour_node`, `lightmix_node`, `light_node`, `scout_node`) exposes `KIND` and:

    prepare(q: QuoteContext) -> Prepared    at quote time: read the graph node and request, check them against
                                            the packets, return the deterministic spec the run is keyed on and
                                            the packet it is anchored to
    normalize(c: RunContext) -> dict        NORMALIZING: do the work, store outputs in the CAS, return the
                                            `normalized` dict (`outputs` = CAS keys of the result PNGs)
    gate(c: RunContext) -> GateReport       GATING: score what was stored (never what was computed in memory)
    score(report) -> float                  the number siblings would be ranked by
    preview(c: RunContext) -> dict          (set, lightmix, recolour) numbers for the editors, nothing stored

The plugin nodes (Light, Scout) add `ACTION`, `job_payload(spec, packet_id)`, `settle(pipeline, run, job)` and
`result_blob(run, job, settled)`: what to ask the dock, whether its answer has fully arrived, and what to keep.
runner.py shows how a run walks QUEUED to DONE around them.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..cas import CAS, KEY_RE
from ..models import Run
from ..pipeline import PacketView, PipelineError, decode_png16, encode_png16

__all__ = [
    "NUMERIC_ENGINE_ID",
    "NUMERIC_ROUTE",
    "PLUGIN_ENGINE_ID",
    "PLUGIN_ROUTE",
    "SPEC_KEY",
    "Prepared",
    "QuoteContext",
    "RunContext",
    "Settle",
    "cas_has",
    "engine_ref",
    "finite_or_none",
    "gate_report",
    "material_share",
    "pick",
    "plain",
    "same_camera",
    "to_display_png16",
]

SPEC_KEY = "numeric"  # runs.inputs[SPEC_KEY]: the node's deterministic spec
NUMERIC_ENGINE_ID = "plate-numeric"
PLUGIN_ENGINE_ID = "plate-plugin"
PROVIDER = "plate-local"


def _local_route(engine_id: str, route_id: str, model_version: str) -> dict[str, Any]:
    # Local and confirmed C0: nothing a numeric node does leaves the box, so custody never blocks it.
    return {
        "engine_id": engine_id,
        "provider": PROVIDER,
        "region": "local",
        "route_id": route_id,
        "custody_grade": "C0",
        "custody_status": "confirmed",
        "upload_visibility": "local",
        "model_version": model_version,
    }


NUMERIC_ROUTE = _local_route(NUMERIC_ENGINE_ID, "numeric-local", "numeric-1")
PLUGIN_ROUTE = _local_route(PLUGIN_ENGINE_ID, "plugin-local", "plugin-1")


def pick(params: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    """The first of `names` that `params` holds with a value (the UI writes camelCase, the API snake_case)."""
    for name in names:
        value = params.get(name)
        if value is not None and value != "":
            return value
    return default


def cas_has(cas: CAS, key: Any) -> bool:
    """Is `key` a BLAKE3 hex key of an object in this CAS? False (never an exception) for anything else."""
    return isinstance(key, str) and KEY_RE.match(key) is not None and cas.exists(key)


def plain(x: Any) -> Any:
    """numpy values, tuples and Paths -> plain JSON types; NaN and inf -> None (strict JSON, what a response needs)."""
    if isinstance(x, Mapping):
        return {str(k): plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set, frozenset)):
        return [plain(v) for v in x]
    if isinstance(x, np.ndarray):
        return plain(x.tolist())
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, (float, np.floating)):
        f = float(x)
        return f if math.isfinite(f) else None
    return x


def finite_or_none(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


@dataclass(frozen=True)
class Prepared:
    """What `prepare` returns: the run's spec (JSON, deterministic: it is hashed into the idempotency key) and
    the packet the run is anchored to (`runs.packet_id`; the Set node's hero)."""

    spec: dict[str, Any]
    packet_id: str


@dataclass(frozen=True)
class Settle:
    """Where a plugin run stands once its job is finished: `waiting` (the dock is done but the thing the run
    expects, e.g. the superseding packet, has not arrived), `ready` (fetch the result) or `failed`."""

    status: str  # "waiting" | "ready" | "failed"
    detail: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class QuoteContext:
    """What `prepare` sees: the graph node, the request's params over the node's, and the upstream plates.

    `param(...)` looks at the request's params first (what the artist just typed), then the node's saved ones.
    """

    pipeline: Any
    conn: Any
    project_id: str
    node: dict[str, Any]
    graph: Any
    node_params: dict[str, Any]
    overlay: dict[str, Any]
    body: dict[str, Any]
    plates: list[dict[str, Any]]
    cas: CAS
    notes: list[str] = field(default_factory=list)

    @property
    def node_id(self) -> str:
        return str(self.node["id"])

    @property
    def params(self) -> dict[str, Any]:
        return {**self.node_params, **self.overlay}

    def param(self, *names: str, default: Any = None) -> Any:
        for source in (self.overlay, self.node_params):
            value = pick(source, *names)
            if value is not None:
                return value
        return default

    def packet(self, packet_id: str) -> PacketView:
        return self.pipeline.packet(self.project_id, packet_id)

    def plate_packet_id(self) -> str:
        """The packet this node hangs off: the request's `packet_id`, else the nearest upstream Plate's."""
        packet_id = self.body.get("packet_id") or next(
            (p.get("packetId") for p in self.plates if p.get("packetId")), None
        )
        if not isinstance(packet_id, str) or not packet_id:
            raise PipelineError(
                f"{self.node.get('kind')} node {self.node_id} has no Plate with a packet upstream",
                code="unknown_packet",
            )
        return packet_id

    def need_asset(self, key: Any, what: str) -> str:
        if not cas_has(self.cas, key):
            raise PipelineError(f"{what}: {key!r} is not in this project's CAS", code="asset_missing")
        return key


@dataclass
class RunContext:
    """What `normalize`, `gate` and `preview` see: the spec, the CAS and cached readers for the packets.

    A context made for a run has `run` (its normalized dict, route and result are reachable through it); a
    preview context has none and only the spec."""

    runner: Any
    cas: CAS
    project_id: str
    spec: dict[str, Any]
    packet_id: str | None = None
    run: Run | None = None

    @classmethod
    def for_run(cls, runner: Any, run: Run, cas: CAS) -> RunContext:
        return cls(runner, cas, run.project_id, dict(run.inputs.get(SPEC_KEY) or {}), run.packet_id, run)

    @property
    def pipeline(self) -> Any:
        return self.runner.pipeline

    @property
    def normalized(self) -> dict[str, Any]:
        return dict((self.run.normalized if self.run is not None else None) or {})

    def packet(self, packet_id: str | None = None) -> PacketView:
        pid = packet_id or self.packet_id
        if not pid:
            raise PipelineError("this context has no packet", code="unknown_packet")
        return self.pipeline.packet(self.project_id, pid)

    def put_png16(self, rgb16: np.ndarray) -> str:
        return self.cas.put_bytes(encode_png16(rgb16), "png")

    def read_png16(self, key: str) -> np.ndarray:
        return decode_png16(self.cas.read_bytes(key))

    def image16(self, packet_id: str) -> np.ndarray:
        """A packet's picture for this run: a CAS PNG named in `spec.images` (an upstream node's output), else
        the packet's display master."""
        key = (self.spec.get("images") or {}).get(packet_id)
        if key:
            return self.read_png16(key)
        return self.packet(packet_id).plate16()


def material_share(pv: PacketView, material_id: str) -> np.ndarray:
    """Summed Cryptomatte material coverage of one id, float32 (H, W) in [0, 1] (soft anti-aliased edges)."""
    from plate_gates.material_de import id_to_u32

    pair = pv.crypto("material")
    if pair is None:
        raise PipelineError(f"packet {pv.packet_id} has no material Cryptomatte", code="no_cryptomatte")
    ranks, cov = pair
    share = np.where(ranks == np.uint32(id_to_u32(material_id)), cov, 0.0).sum(axis=2)
    return np.clip(share, 0.0, 1.0).astype(np.float32)


def same_camera(a: Mapping[str, Any], b: Mapping[str, Any], atol: float = 1e-5) -> bool:
    """Do two manifest camera blocks look from the same place with the same lens at the same size? A relit
    re-render must (source-view-only: a Light result that moved the camera is a different shot)."""
    try:
        ra, rb = a["resolution"], b["resolution"]
        if (ra["width"], ra["height"]) != (rb["width"], rb["height"]):
            return False
        ma = np.asarray(a["world_from_camera"], dtype=np.float64)
        mb = np.asarray(b["world_from_camera"], dtype=np.float64)
        return (
            bool(np.allclose(ma, mb, atol=atol))
            and abs(float(a["focal_length_mm"]) - float(b["focal_length_mm"])) < 1e-6
        )
    except (KeyError, TypeError, ValueError):
        return False


def to_display_png16(display8: np.ndarray) -> np.ndarray:
    """Display RGB on the 0..255 float scale -> uint16 on 0..65535 (x257, the plate's own convention)."""
    return np.rint(np.clip(np.asarray(display8, dtype=np.float64), 0.0, 255.0) * 257.0).astype(np.uint16)


def engine_ref(engine_id: str = NUMERIC_ENGINE_ID, version: str = "numeric-1") -> Any:
    from plate_gates.report import EngineRef

    return EngineRef(id=engine_id, version=version)


def gate_report(
    c: RunContext,
    *,
    tier: str | None,
    metrics: Mapping[str, Any],
    checks: list[Any],
    notes: list[str] | None = None,
    engine: Any = None,
) -> Any:
    """A versioned GateReport under the committed threshold set; the verdict follows from the checks."""
    from plate_gates import thresholds
    from plate_gates.report import GateReport

    return GateReport(
        threshold_set_id=thresholds.load().id,
        engine=engine or engine_ref(c.run.route.engine_id if c.run is not None else NUMERIC_ENGINE_ID),
        tier=tier,
        metrics=dict(metrics),
        checks=checks,
        backend_notes=list(notes or []),
    )
