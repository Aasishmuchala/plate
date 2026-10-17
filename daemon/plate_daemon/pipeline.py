"""The pipeline: a canvas node quoted into a run, and an approved run carried to a gated, stored output.

`Pipeline` sits on top of the worker (`worker.step`, one transition at a time, the only module that calls a
provider) and fills in what the seed left as stubs. A run made here carries `inputs.staging` (the PLATE-side
options), `inputs.plate` (the BLAKE3 of the plate the packet names) and `inputs.mask` (the editable ids); the
seed's `POST /runs` runs keep the worker's stub normalize and gate.

    quote_node(conn, project_id, node_id, body)  -> (Run QUOTED, Quote)    graph checked, custody checked
    approve(conn, project_id, run_id, by, ...)   -> [Run APPROVED]          a best-of-N group approves as one
    queue(...) / execute(run_id)                 -> the run's final state   SSE and ledger on every move
    step(conn, project_id, run_id)               -> one transition          what `POST /runs/{id}/step` drives

The stages of `execute`, in the state machine's order:

- STAGING -> SUBMITTED: custody first (`custody.require_route` on the project's and the packet's grades, so
  a D route on an NDA project raises `CustodyViolation` before a byte is built), then `plate_pixels.egress.
  prepare` on the packet's display master with the editable mask (the Mask node's ids or CAS mask, else the
  packet's `policy.editable_ids`, never a locked id) and a region crop when the node asks for one. The PNG and
  the mask go into the project's CAS; the vendor payload carries the PNG under an opaque name. A cloud engine
  is a `plate_engines` adapter built on `Egress.transport(...)` (allowlist, metadata strip, leak scan,
  outbound log) behind `AdapterBridge`; `fake` is the daemon's in-process FakeEngine (C0, local).
- SUBMITTED / RUNNING: the adapter's poll; FETCHING follows at once (result URLs expire).
- NORMALIZING: `ingress.restore` (undo the size map, ECC-register on the ring outside the mask),
  `composite_masked` (feather inside the mask, ring tone match: outside the mask is the plate by
  construction), `textlock.apply` (signage ids and OCR boxes pasted back bit for bit), a 16-bit PNG into the
  CAS with lineage embedded when `lineage.embed` exists (T26). The run's `outputs` name that PNG.
- GATING: `gating.run_gates`; the report is written as `gates/<run_id>.<n>.json` (`gate_reports.Service`,
  never overwritten) and the verdict decides DONE or GATE_FAIL.

Every transition is published on the bus as `{"kind": "run.state", "run_id", "from", "to"}` and, where the
ledger has a kind for it, appended to the project's hash-chained `runs.jsonl` with enough fields to rebuild
the index from the folder. A best-of-N group is ranked and published as `siblings.ranked` when its last
sibling ends. Bus failures are logged, never fatal. Ledger failures are logged too, except for `quoted` and
`approved`: a quote or an approval the ledger cannot record is cancelled (`ledger_unavailable`, 503) before
anything is spent, so the folder never misses a run that could bill.

What `quote_node` decides before a run row exists (wave 8, decisions 33, 34, 40 and 44):

- Doctrine and routing. The body's `task` (`relight` by default, `rephotograph_close` for a re-photograph
  recipe) and `shot_class` (`wide` unless the node or the recipe says otherwise) go to
  `plate_engines.router.choose` on the chosen engine only, with the project's pins, the engine cards folded
  from the ledger and a `HealthBoard` that assumes every active route is ok until canaries say otherwise
  (`alerts.jsonl` suspensions apply). Refusals: `router_refused:<code>` 422, `retired_pin` and `pin_conflict`
  409, custody as `CustodyViolation` 403. The route dict carries the router's `reasons`.
- Render-it (doctrine 4). Tier A packets get `router_math.detail_resolvability` from the packet's Z and object
  Cryptomatte; the answer is stored as `run.inputs["router"] = {verdict, D_px_median, per_class, mode,
  reasons}` (the quote route copies it to the response as `router`). Advisory by default; with
  `PLATE_ROUTER_MODE=blocking` a `vray_only` frame is refused with 409 `render_it` (people stay allowed: AI
  for people by region is the router's own exception). Tier C answers `unknown`.
- Prompt. Explicit (`body.prompt`, else the node's `params.prompt`) beats a recipe (`body.recipe` or
  `params.recipe`: the template filled from the Project Sheet, tailored by the composer when the Anthropic
  route's custody allows it); `inputs.prompt_source` says which. An engine whose manifest requires a prompt
  and gets none is refused with 422 `prompt_required`.
- Mask. A Mask node's `ids`, `maskHash` and `samHash` are one union; `editable_mask` always intersects it with
  the packet's editable ids and removes every locked id, on every path.
- Attempt. With no `attempt` in the body the run takes 1 + the highest attempt of the node's earlier runs
  whose inputs (less `subgraph` and `router`), params and route are the same, so a repeat never collides on
  the idempotency key. `inputs.subgraph` snapshots the node and its ancestors as project.json holds them.
- Ledger. `quoted` and `submitted` records carry `data.model`, `data.task`, `data.shot_class` and
  `data.repeat_key` (the BLAKE3 of that same canonical tuple, 16 hex) for the engine cards and reconcile.
"""

from __future__ import annotations

import base64
import copy
import importlib
import inspect
import json
import logging
import os
import re
import struct
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from blake3 import blake3

from . import budgets, canvas_rules
from .adapter import PollResult, lookup_fn, submit_is_idempotent
from .cas import CAS, hash_bytes, hash_file
from .config import Settings, check_project_id
from .custody import CustodyViolation, Grade, project_allowed, require_route, route_allowed
from .db import loads
from .idempotency import canonical_json
from .index import Index, packet_entry
from .ledger import Ledger
from .machine import create_run, get_run, transition
from .models import Quote, Run, RunCreate
from .projects import Project, ProjectError
from .quotes import approve as approve_one
from .quotes import approve_group, quote_run
from .states import TERMINAL, WORKER_STATES, Priority, RunState
from .timeutil import utcnow
from .worker import GateOutcome, rank_siblings, siblings
from .worker import step as worker_step

__all__ = [
    "ASSUMED_OK",
    "AdapterBridge",
    "CustodyViolation",
    "FAKE_ENGINE_ID",
    "FAKE_ROUTE",
    "PacketView",
    "Pipeline",
    "PipelineError",
    "QUOTABLE_KINDS",
    "STAGING_KEY",
    "StagedEngine",
    "UNSET_CUSTODY",
    "adapter_class",
    "decode_png16",
    "encode_png16",
    "is_pipeline_run",
    "project_head",
    "repeat_canonical",
    "repeat_key",
    "subgraph_snapshot",
]

log = logging.getLogger(__name__)
S = RunState

STAGING_KEY = "staging"
FAKE_ENGINE_ID = "fake"
# The in-process FakeEngine: local, confirmed C0. Its outputs follow its input (it is never a vendor).
FAKE_ROUTE: dict[str, Any] = {
    "engine_id": FAKE_ENGINE_ID,
    "provider": "fake",
    "region": "local",
    "route_id": "fake-local",
    "custody_grade": "C0",
    "custody_status": "confirmed",
    "upload_visibility": "local",
    "model_version": "fake",
}
FAKE_SIZES: dict[str, Any] = {"sizes": {"auto": True}}  # any size, native pixels
# A project made because a packet arrived first: nothing leaves (custody.md section 5) until a human sets it.
UNSET_CUSTODY: dict[str, Any] = {"nda": False, "allowed_grades": ["C0"], "unset": True}
# Stills only here; wave 4 (motion/, nodes/) extends quote_node for Motion, CropPair and People.
QUOTABLE_KINDS = frozenset({"Edit"})
IMAGE_ENGINE_KINDS = ("image_edit", "local_image")
# The tasks an Edit node may name (plate_engines.tasks vocabulary); other tasks belong to other node kinds.
EDIT_TASKS = ("relight", "people", "vegetation", "sky", "rephotograph_close")
SHOT_CLASSES = ("wide", "medium", "close", "detail")  # recipes.SHOT_CLASSES
DEFAULT_TASK = "relight"
DEFAULT_SHOT_CLASS = "wide"
# Decision 44: until canaries feed a board, every active route is assumed ok, and the quote says so.
ASSUMED_OK = "assumed ok: no canary signal yet"
ALERTS_JSONL = "alerts.jsonl"
ROUTER_KEY = "router"
SUBGRAPH_KEY = "subgraph"
REPEAT_EXCLUDED_INPUTS = (SUBGRAPH_KEY, ROUTER_KEY)  # snapshots, not what the engine is asked to do
REPEAT_KEY_HEX = 16
# A ledger record of these kinds is the only proof of a quote or an approval: without it the run stops.
FAIL_CLOSED_KINDS = frozenset({"quoted", "approved"})
MASK_KEY_PARAMS = ("maskHash", "samHash")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RECIPE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
DEFAULT_FEATHER_PX = 8.0
DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_POLL_TIMEOUT_S = 3600.0
STAGE_CACHE = 8
PACKET_CACHE = 2
ADAPTER_MODULES = (
    "gemini",
    "openai_images",
    "atlas_image",
    "bfl",
    "local_qwen",
    "upscale",
    "atlas_video",
    "seedance_fallbacks",
    "vertex_veo",
    "local_wan",
)

# Ledger kind for the state a transition lands in (states without one, e.g. QUEUED or RUNNING, are not ledgered).
LEDGER_KINDS: dict[RunState, str] = {
    S.QUOTED: "quoted",
    S.APPROVED: "approved",
    S.SUBMITTED: "submitted",
    S.NORMALIZING: "fetched",
    S.DONE: "done",
    S.GATE_FAIL: "gated",
    S.FAILED: "failed",
    S.REFUSED: "refused",
    S.CANCELLED: "cancelled",
    S.EXPIRED: "expired",
}


class PipelineError(ValueError):
    """A request the pipeline refuses before any spend. `status` and `code` become the HTTP answer."""

    def __init__(self, message: str, *, code: str = "pipeline", status: int = 422, **extra: Any):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


def is_pipeline_run(run: Run) -> bool:
    return isinstance(run.inputs.get(STAGING_KEY), Mapping)


def repeat_canonical(inputs: Mapping[str, Any], params: Mapping[str, Any], route: Mapping[str, Any]) -> bytes:
    """What makes two runs of a node the same request (decision 40): inputs without the subgraph and router
    snapshots, params, and the route without the router's `reasons` (they explain, they do not route)."""
    return canonical_json(
        {
            "inputs": {k: v for k, v in inputs.items() if k not in REPEAT_EXCLUDED_INPUTS},
            "params": dict(params),
            "route": {k: v for k, v in route.items() if k != "reasons"},
        }
    )


def repeat_key(run: Run) -> str:
    """`data.repeat_key` of the ledger: the same value for repeats of one request (engine-card determinism)."""
    return blake3(repeat_canonical(run.inputs, run.params, run.route_dict)).hexdigest()[:REPEAT_KEY_HEX]


def subgraph_snapshot(graph: Mapping[str, Any], keep: set[str]) -> dict[str, Any]:
    """The nodes in `keep` and the edges between them, as project.json stores them (the UI's
    `RunDetail.subgraph`)."""
    raw = graph["graph"] if isinstance(graph.get("graph"), Mapping) else graph
    nodes = [
        copy.deepcopy(n)
        for n in raw.get("nodes") or []
        if isinstance(n, Mapping) and canvas_rules.node_data(n).get("id") in keep
    ]
    edges = []
    for e in raw.get("edges") or []:
        try:
            ed = canvas_rules.as_edge(e)
        except (TypeError, ValueError, KeyError):
            continue
        if ed.get("source") in keep and ed.get("target") in keep:
            edges.append(copy.deepcopy(e))
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------- pixels in and out of the CAS


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


# sRGB (rendering intent 0, perceptual) and the gAMA a PNG decoder without sRGB support should use (1/2.2).
SRGB_CHUNKS = _png_chunk(b"sRGB", b"\x00") + _png_chunk(b"gAMA", struct.pack(">I", 45455))
_IHDR_END = 8 + 4 + 4 + 13 + 4  # signature, length, type, the 13 IHDR bytes, CRC


def encode_png16(rgb16: np.ndarray) -> bytes:
    """A 16-bit RGB PNG of a uint16 (H, W, 3) array, tagged sRGB (an `sRGB` and a `gAMA` chunk right after
    IHDR, no ICC profile, no other metadata), so a viewer shows the display master's colours as they are."""
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(np.asarray(rgb16, np.uint16)[..., ::-1]))
    if not ok:
        raise ValueError("cv2 could not encode the 16-bit output")
    png = bytes(buf)
    if png[12:16] != b"IHDR":
        raise ValueError("cv2 wrote a PNG whose first chunk is not IHDR")
    return png[:_IHDR_END] + SRGB_CHUNKS + png[_IHDR_END:]


def decode_png16(data: bytes) -> np.ndarray:
    """uint16 (H, W, 3) RGB from PNG bytes (8-bit images are promoted by x257)."""
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError("not a decodable image")
    if img.ndim == 2:
        img = np.repeat(img[..., None], 3, axis=2)
    img = np.ascontiguousarray(img[..., :3][..., ::-1])
    return img.astype(np.uint16) * 257 if img.dtype == np.uint8 else img.astype(np.uint16)


def _mask_png(mask: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.where(mask, 255, 0).astype(np.uint8))
    if not ok:
        raise ValueError("cv2 could not encode the mask")
    return bytes(buf)


# ---------------------------------------------------------------------------- the packet as the pipeline reads it


@dataclass
class PacketView:
    """A packet folder (by reference, read-only) with its plate, Cryptomatte and segments decoded on demand."""

    packet_id: str
    root: Path
    manifest: dict[str, Any]
    legend: dict[str, Any] | None
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, packet_id: str, root: Path | str, manifest_blake3: str | None = None) -> PacketView:
        root = Path(root)
        raw = (root / "manifest.json").read_bytes()
        if manifest_blake3 is not None and hash_bytes(raw) != manifest_blake3:
            raise PipelineError(
                f"packet {packet_id}: manifest.json changed since it was ingested; re-send the packet",
                code="packet_changed",
                status=409,
            )
        manifest = json.loads(raw.decode("utf-8-sig"))
        legend = None
        ref = (manifest.get("files") or {}).get("id_legend")
        if ref and (root / ref["path"]).is_file():
            legend = json.loads((root / ref["path"]).read_text(encoding="utf-8-sig"))
        return cls(packet_id=packet_id, root=root, manifest=manifest, legend=legend)

    @property
    def tier(self) -> str:
        return str(self.manifest.get("tier") or "A")

    @property
    def policy(self) -> dict[str, Any]:
        return dict(self.manifest.get("policy") or {})

    @property
    def files(self) -> dict[str, Any]:
        return self.manifest.get("files") or {}

    @property
    def plate_role(self) -> str:
        for role in ("display_tiff16", "client_plate"):
            if role in self.files:
                return role
        raise PipelineError(
            f"packet {self.packet_id} names no display master (display_tiff16 / client_plate)"
        )

    @property
    def plate_hash(self) -> str:
        return self.files[self.plate_role]["hash"]["hex"]

    def editable_ids(self) -> list[str]:
        return [str(i) for i in self.policy.get("editable_ids") or []]

    def locked_ids(self) -> list[str]:
        return [str(i) for i in self.policy.get("locked_ids") or []]

    def plate16(self) -> np.ndarray:
        """The display master as uint16 RGB, after checking its bytes are the ones the manifest hashed."""
        if "plate16" not in self._cache:
            from plate_pixels import imageio

            path = self.root / self.files[self.plate_role]["path"]
            digest, _ = hash_file(path)
            if digest != self.plate_hash:
                raise PipelineError(
                    f"packet {self.packet_id}: {path.name} changed since it was ingested",
                    code="packet_changed",
                    status=409,
                )
            img = imageio.read(path)
            if img.encoding != "srgb":
                raise PipelineError(f"{path.name} is scene-linear: egress takes the display master only")
            scale = 65535.0 if img.bit_depth >= 16 else 255.0
            arr = np.clip(np.rint(img.rgb * scale), 0, scale).astype(np.uint16)
            self._cache["plate16"] = arr if scale == 65535.0 else arr * np.uint16(257)
        return self._cache["plate16"]

    def crypto(self, which: str) -> tuple[np.ndarray, np.ndarray] | None:
        """(ranks, coverage) of the object or material Cryptomatte, or None (Tier C, no EXR)."""
        key = f"crypto_{which}"
        if key not in self._cache:
            exr = self.files.get("beauty_exr")
            if exr is None:
                self._cache[key] = None
            else:
                from plate_pixels.exr import read_cryptomatte

                self._cache[key] = read_cryptomatte(self.root / exr["path"], self.manifest, which)
        return self._cache[key]

    def segments(self) -> np.ndarray | None:
        if "segments" not in self._cache:
            ref = self.files.get("segments")
            seg = None
            if ref is not None:
                seg = cv2.imread(str(self.root / ref["path"]), cv2.IMREAD_UNCHANGED)
            self._cache["segments"] = seg
        return self._cache["segments"]

    def id_mask(self, cid: str) -> np.ndarray:
        """bool (H, W): the pixels of one legend id (o: object, m: material, s: Tier C segment)."""
        prefix, _, rest = cid.partition(":")
        if prefix in ("o", "m"):
            pair = self.crypto("object" if prefix == "o" else "material")
            if pair is None:
                raise PipelineError(f"{cid}: packet {self.packet_id} has no Cryptomatte")
            from plate_pixels.exr import id_mask

            return id_mask(pair[0], pair[1], cid)
        if prefix == "s":
            seg = self.segments()
            if seg is None:
                raise PipelineError(f"{cid}: packet {self.packet_id} has no segment map")
            return np.asarray(seg) == int(rest)
        raise PipelineError(f"{cid}: unknown id prefix (o:, m: or s:)")

    def _ids_mask(self, ids: list[str], shape: tuple[int, int]) -> np.ndarray:
        mask = np.zeros(shape, dtype=bool)
        for cid in ids:
            try:
                mask |= self.id_mask(cid)
            except PipelineError:
                continue  # an id of a layer this packet does not carry
        return mask

    def editable_px(self, shape: tuple[int, int]) -> np.ndarray:
        """bool (H, W): what the packet policy lets any edit touch. The union of `policy.editable_ids` when
        it lists any; otherwise the whole frame on Tier C (no ids to list) or when `lock_by_default` is off,
        and nothing on a lock-by-default Tier A packet."""
        if "editable_px" not in self._cache:
            ids = self.editable_ids()
            if ids:
                mask = self._ids_mask(ids, shape)
            elif self.tier == "C" or self.policy.get("lock_by_default") is False:
                mask = np.ones(shape, dtype=bool)
            else:
                mask = np.zeros(shape, dtype=bool)
            self._cache["editable_px"] = mask
        return self._cache["editable_px"]

    def locked_px(self, shape: tuple[int, int]) -> np.ndarray:
        """bool (H, W): the union of `policy.locked_ids` (never edited, whatever a mask asks)."""
        if "locked_px" not in self._cache:
            self._cache["locked_px"] = self._ids_mask(self.locked_ids(), shape)
        return self._cache["locked_px"]

    def detail_resolvability(self) -> dict[str, Any]:
        """`router_math.detail_resolvability` on this still (Tier A: Z and the object Cryptomatte; cached)."""
        if "detail" not in self._cache:
            from plate_gates import router_math
            from plate_pixels.exr import read_elements

            exr = self.files.get("beauty_exr")
            pair = self.crypto("object")
            if exr is None or pair is None:
                raise PipelineError(f"packet {self.packet_id} has no Z and object Cryptomatte")
            zd = next((e for e in exr.get("elements") or () if e.get("element") == "zdepth"), {})
            z = read_elements(self.root / exr["path"], self.manifest, ["zdepth"])["zdepth"]
            cam = self.manifest.get("camera") or {}
            self._cache["detail"] = router_math.detail_resolvability(
                z,
                pair[0],
                self.legend or {"entries": []},
                cam.get("intrinsics_px"),
                self.manifest.get("units"),
                bg_value=zd.get("background_value"),
            )
        return self._cache["detail"]

    def edges(self) -> tuple[Any, dict[str, Any]] | None:
        """`gbuffer.from_packet` for Tier A (cached), else None."""
        if self.tier != "A" or "beauty_exr" not in self.files:
            return None
        if "edges" not in self._cache:
            from plate_gates import gbuffer

            self._cache["edges"] = gbuffer.from_packet(self.root, manifest=self.manifest)
        return self._cache["edges"]


@dataclass(frozen=True)
class Staged:
    """What STAGING produced for one run: the PNG the vendor sees, its size map, the editable mask."""

    png: bytes
    sizemap: Any
    mask: np.ndarray
    png_key: str
    mask_key: str
    opaque_name: str


# ---------------------------------------------------------------------------- engines behind the worker


class StagedEngine:
    """The worker's engine for one pipeline run: custody and egress on submit, the inner engine for the rest.

    The worker hands `submit` the seed payload it staged (engine, inputs by hash, params, variant); this
    replaces the inputs with the egress PNG before the inner engine sees them, so nothing but the prepared
    plate and the prompt ever reaches an adapter."""

    def __init__(self, pipeline: Pipeline, run: Run, inner: Any, cas: CAS):
        self.pipeline = pipeline
        self.run = run
        self.inner = inner
        self.cas = cas
        self.stub = bool(getattr(inner, "stub", False))
        self.idempotent_submit = submit_is_idempotent(inner)
        self.supports_lookup = lookup_fn(inner) is not None

    def fault_point(self, state: RunState, phase: str) -> None:
        fp = getattr(self.inner, "fault_point", None)
        if fp is not None:
            fp(state, phase)

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        self.pipeline.require_custody(self.run)
        staged = self.pipeline.stage(self.run, self.cas)
        return self.inner.submit(self.pipeline.vendor_payload(self.run, staged, payload), idempotency_key)

    def poll(self, provider_job_id: str) -> PollResult:
        return self.inner.poll(provider_job_id)

    def fetch(self, result_url: str) -> bytes:
        return self.inner.fetch(result_url)

    def cancel(self, provider_job_id: str) -> None:
        self.inner.cancel(provider_job_id)

    def lookup(self, idempotency_key: str) -> str | None:
        fn = lookup_fn(self.inner)
        if fn is None:
            raise NotImplementedError("the inner engine has no lookup")
        return fn(idempotency_key)


class AdapterBridge:
    """A `plate_engines` adapter (submit(Staged) / poll -> JobStatus) behind the worker's protocol."""

    stub = False
    idempotent_submit = False
    supports_lookup = False

    def __init__(self, adapter: Any):
        self.adapter = adapter

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        from plate_engines.adapters.base import Staged as EngineStaged

        inputs = dict(payload.get("inputs") or {})
        img = inputs.get("image")
        if isinstance(img, Mapping) and "b64" in img:
            inputs["image"] = {
                **{k: v for k, v in img.items() if k != "b64"},
                "bytes": base64.b64decode(img["b64"]),
            }
        params = self.adapter.validate_params(dict(payload.get("params") or {}))
        return self.adapter.submit(EngineStaged(inputs=inputs, params=params))

    def poll(self, provider_job_id: str) -> PollResult:
        st = self.adapter.poll(provider_job_id)
        if st.state == "done":
            if not st.outputs:
                return PollResult("failed", detail="the engine finished without an output")
            return PollResult("succeeded", result_url=st.outputs[0])
        if st.state in ("failed", "refused"):
            return PollResult(st.state, detail=st.message or None)
        return PollResult(st.state)

    def fetch(self, result_url: str) -> bytes:
        return self.adapter.fetch(result_url)

    def cancel(self, provider_job_id: str) -> None:
        fn = getattr(self.adapter, "cancel", None)
        if fn is None:
            raise NotImplementedError(f"{self.adapter.engine_id} has no provider cancel")
        fn(provider_job_id)


def adapter_class(manifest: Any, route: Any) -> type | None:
    """The `plate_engines.adapters` class that speaks `manifest` on `route` (by engine id and provider)."""
    for name in ADAPTER_MODULES:
        try:
            module = importlib.import_module(f"plate_engines.adapters.{name}")
        except Exception:  # an adapter that cannot import is an adapter that is not there
            continue
        for obj in vars(module).values():
            if not isinstance(obj, type) or obj.__module__ != module.__name__:
                continue
            ids = getattr(obj, "SUPPORTED_ENGINES", None) or getattr(obj, "ENGINE_IDS", None) or ()
            provider = getattr(obj, "PROVIDER", None)
            if manifest.id in ids and provider in (None, route.provider):
                return obj
    return None


# ---------------------------------------------------------------------------- the pipeline


class Pipeline:
    """See the module docstring. `engine` is the daemon's in-process FakeEngine (`fake`); `engines` maps
    further engine ids to daemon-protocol adapters (tests, local stubs); every other engine id needs a
    manifest and a `plate_engines` adapter, built on the project's `Egress`."""

    def __init__(
        self,
        *,
        settings: Settings,
        index: Index,
        cas_for: Callable[[str], CAS],
        bus: Any = None,
        engine: Any = None,
        engines: Mapping[str, Any] | None = None,
        manifests: Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
        policy: budgets.Policy | None = None,
        ocr: Any = None,
        depth_fn: Callable[[np.ndarray], np.ndarray] | None = None,
    ):
        self.settings = settings
        self.index = index
        self.cas_for = cas_for
        self.bus = bus
        self.engine = engine
        self.engines: dict[str, Any] = dict(engines or {})
        self._manifests = dict(manifests) if manifests is not None else None
        self.clock = clock
        self.sleep = sleep
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s
        self.policy = policy or budgets.Policy()
        self.ocr = ocr
        self.depth_fn = depth_fn
        self.last_human_utc: datetime | None = None
        self._lock = threading.RLock()
        self._staged: OrderedDict[tuple[str, int], Staged] = OrderedDict()
        self._packets: OrderedDict[tuple[str, str], PacketView] = OrderedDict()
        self._inner: dict[str, Any] = {}
        self._egress: dict[str, Any] = {}
        self._ranked: set[str] = set()
        self._health: tuple[Any, dict[str, Any]] | None = None

    # ------------------------------------------------------------------ projects

    @property
    def manifests(self) -> dict[str, Any]:
        if self._manifests is None:
            from plate_engines.loader import load_manifests

            self._manifests = load_manifests()
        return self._manifests

    def project_dir(self, project_id: str) -> Path:
        return self.index.project_root(project_id) or self.settings.projects_dir / check_project_id(
            project_id
        )

    def load_project(self, project_id: str) -> Project | None:
        root = self.project_dir(project_id)
        if not (root / "project.json").is_file():
            return None
        return Project.load(root)

    def ensure_project(self, project_id: str) -> Project:
        """The project's folder, made with UNSET_CUSTODY (C0 only) when a packet arrives before the project."""
        project = self.load_project(project_id)
        if project is not None:
            return project
        root = self.project_dir(project_id)
        try:
            with self._lock:
                project = Project.create(root, project_id, dict(UNSET_CUSTODY), now=self.clock())
        except FileExistsError:
            project = Project.load(root)
        self.index.set_project_root(project_id, root, project.custody)
        return project

    def register_packet(self, project_id: str, packet_id: str) -> None:
        """Record an ingested packet in the project's `packets.json` (by reference: path and manifest hash)."""
        project = self.ensure_project(project_id)
        with self.index.project(project_id) as conn:
            entry = packet_entry(conn, packet_id)
        if entry.get("status") != "ingested":
            return
        with project.lock(timeout_s=5.0):
            project.register_packet(packet_id, entry)

    def touch_human(self, when: datetime | None = None) -> None:
        self.last_human_utc = when or self.clock()

    # ------------------------------------------------------------------ packets and custody

    def packet(self, project_id: str, packet_id: str) -> PacketView:
        with self.index.project(project_id) as conn:
            row = conn.execute(
                "SELECT path, status, manifest_blake3 FROM packets WHERE packet_id = ?", (packet_id,)
            ).fetchone()
        if row is None:
            raise PipelineError(f"packet {packet_id} is not in project {project_id}", code="unknown_packet")
        if row["status"] != "ingested":
            raise PipelineError(
                f"packet {packet_id} is {row['status']}",
                code="packet_not_ready",
                status=409,
                status_now=row["status"],
            )
        key = (packet_id, row["manifest_blake3"])
        with self._lock:
            hit = self._packets.get(key)
            if hit is not None:
                self._packets.move_to_end(key)
                return hit
        pv = PacketView.load(packet_id, row["path"], row["manifest_blake3"])
        with self._lock:
            self._packets[key] = pv
            while len(self._packets) > PACKET_CACHE:
                self._packets.popitem(last=False)
        return pv

    def allowed_grades(self, project_id: str, manifest: Mapping[str, Any] | None = None) -> set[Grade]:
        """Project custody ∩ packet `policy.custody`, NDA capped at C0/C1; no project.json means C0."""
        project = self.load_project(project_id)
        packet_custody = ((manifest or {}).get("policy") or {}).get("custody")
        return project_allowed(project.custody if project else None, packet_custody)

    def require_custody(self, run: Run) -> Grade:
        pv = self.packet(run.project_id, run.packet_id)
        allowed = self.allowed_grades(run.project_id, pv.manifest)
        return require_route(self.custody_route(run), allowed, what=run.route.engine_id)

    def custody_route(self, run: Run) -> dict[str, Any]:
        """The route custody is checked on. A pipeline run, or any run whose route states a grade: that route.
        A seed `POST /runs` run with a bare route is served by the daemon's own engine (`engine_for`), so its
        custody is that engine's route."""
        route = run.route_dict
        if is_pipeline_run(run) or route.get("custody_grade"):
            return route
        engine = self.engines.get(run.route.engine_id, self.engine)
        return dict(getattr(engine, "route", None) or FAKE_ROUTE, engine_id=run.route.engine_id)

    # ------------------------------------------------------------------ routing (doctrine, custody, router)

    def check_task(self, task: str, shot_class: str) -> None:
        """The doctrine half of the router that holds for every engine, local ones included: no forbidden task,
        a re-photograph on close and medium shots only (movie-grade is a render)."""
        from plate_engines import router as erouter
        from plate_engines.tasks import FORBIDDEN_TASKS

        if task in FORBIDDEN_TASKS:
            raise PipelineError(
                f"task {task!r} is forbidden by doctrine (geometry first, plate untouched)",
                code="router_refused:doctrine",
            )
        if task not in EDIT_TASKS:
            raise PipelineError(
                f"task {task!r}: an Edit node takes one of {list(EDIT_TASKS)}", code="bad_task"
            )
        if shot_class not in SHOT_CLASSES:
            raise PipelineError(
                f"shot_class {shot_class!r}: expected one of {list(SHOT_CLASSES)}", code="bad_shot_class"
            )
        if task in erouter.REPHOTOGRAPH_TASKS and shot_class not in erouter.RETAKE_SHOT_CLASSES:
            raise PipelineError(
                f"task {task!r} on a {shot_class} shot: re-photograph is allowed on close and medium shots "
                "only (movie-grade is a render, a wide stays a render)",
                code="router_refused:doctrine",
            )

    def alert_paths(self) -> list[Path]:
        """Where engine suspensions are read from: `<home>/alerts.jsonl` and the nightly's `logs/alerts.jsonl`."""
        out: list[Path] = []
        for p in (self.settings.home / ALERTS_JSONL, self.settings.home / "logs" / ALERTS_JSONL):
            if p not in out:
                out.append(p)
        return out

    def health_board(self) -> dict[str, Any]:
        """Decision 44: every active route of the loaded manifests is `ok` (`assumed ok: no canary signal
        yet`), then the alert logs' suspensions apply. Rebuilt when an alert file changes."""
        from plate_engines.health import AlertLog, HealthBoard

        stamp_parts = []
        for p in self.alert_paths():
            try:
                st = p.stat()
            except OSError:
                continue
            stamp_parts.append((str(p), st.st_mtime_ns, st.st_size))
        manifests = self.manifests
        stamp = (id(manifests), tuple(stamp_parts))
        with self._lock:
            if self._health is not None and self._health[0] == stamp:
                return self._health[1]
        board = HealthBoard(clock=self.clock)
        for m in manifests.values():
            for r in m.routes:
                if r.status == "active":
                    board.set(f"{m.id}/{r.route_id}", "ok", reason=ASSUMED_OK)
        for path_s, _mtime, _size in stamp_parts:
            try:
                board.apply_alerts(AlertLog(path_s))
            except Exception as e:  # an unreadable alert log suspends nothing; say so
                log.warning("alerts in %s not applied: %s", path_s, e)
        mapping = board.as_mapping()
        with self._lock:
            self._health = (stamp, mapping)
        return mapping

    def task_pins(self, project: Project | None, engine_id: str, task: str) -> dict[str, Any]:
        """The project's pins as `router.choose` takes them (task -> Pin). project.json may key a pin by task
        (`{"relight": {"engine_id", "version"}}`) or by engine (`{engine_id: version}`, which pins this
        engine for the task being quoted); a task key wins."""
        from plate_engines import router as erouter
        from plate_engines.tasks import TASKS

        pins: dict[str, Any] = {}
        by_engine: Any = None
        for key, value in ((project.engine_pins if project else None) or {}).items():
            try:
                if key in TASKS:
                    pins[key] = erouter.as_pin(value)
                elif key == engine_id and (value is None or isinstance(value, str)):
                    by_engine = erouter.Pin(engine_id=engine_id, version=value or None)
            except (TypeError, ValueError) as e:
                raise PipelineError(f"project pin {key!r} is not a pin: {e}", code="bad_pin") from e
        if by_engine is not None and task not in pins:
            pins[task] = by_engine
        return pins

    def route_choice(
        self,
        project_id: str,
        manifest: Any,
        routes: list[Any],
        allowed: set[Grade],
        task: str,
        shot_class: str,
    ) -> tuple[Any, list[str]]:
        """`router.choose` on this engine's `routes` only (pins, cards and health still apply). Returns the
        RouteChoice and its reasons, the health assumption named when it was assumed."""
        from plate_engines import router as erouter
        from plate_engines.cards import from_ledger

        project = self.load_project(project_id)
        pins = self.task_pins(project, manifest.id, task)
        pin = pins.get(task)
        if pin is not None and pin.engine_id != manifest.id:
            served = any(
                m.id == pin.engine_id and pin.version in (None, m.version) for m in self.manifests.values()
            )
            if not served:
                raise PipelineError(
                    f"project pins {task!r} to {pin.engine_id}@{pin.version or '*'}, which no manifest serves "
                    "any more; pick another version or engine by hand",
                    code="retired_pin",
                    status=409,
                )
            raise PipelineError(
                f"project pins {task!r} to {pin.engine_id}; quote that engine or change the project's pin",
                code="pin_conflict",
                status=409,
            )
        try:
            cards = from_ledger(Ledger(self.project_dir(project_id), clock=self.clock).records(), {})
        except Exception as e:  # cards rank; without them the router ranks on price
            log.warning("engine cards for %s not read: %s", project_id, e)
            cards = []
        health = self.health_board()
        try:
            choice = erouter.choose(
                erouter.Task(name=task, shot_class=shot_class, kind=IMAGE_ENGINE_KINDS),
                {manifest.id: manifest.model_copy(update={"routes": list(routes)})},
                sorted(g.value for g in allowed),
                pins,
                cards,
                health,
                erouter.Prefs(),
            )
        except erouter.RetiredPin as e:
            raise PipelineError(e.reason, code="retired_pin", status=409) from e
        except erouter.Refusal as e:
            if e.code == "custody":
                require_route(routes[0], allowed, what=manifest.id)  # raises CustodyViolation
                raise PipelineError(e.reason, code="router_refused:custody", status=403) from e
            raise PipelineError(e.reason, code=f"router_refused:{e.code}") from e
        reasons = list(choice.reasons)
        entry = health.get(f"{manifest.id}/{choice.route_id}")
        if entry is not None and getattr(entry, "reason", "") == ASSUMED_OK:
            reasons.append(f"health: {manifest.id}/{choice.route_id} {ASSUMED_OK}")
        return choice, reasons

    def pick_route(
        self,
        project_id: str,
        pv: PacketView,
        engine_id: str,
        route_id: str | None,
        *,
        task: str = DEFAULT_TASK,
        shot_class: str = DEFAULT_SHOT_CLASS,
    ) -> dict[str, Any]:
        """The route a node runs on: doctrine first, then custody (403), then the router on the chosen engine's
        runnable routes. The route dict carries the router's `reasons`."""
        self.check_task(task, shot_class)
        allowed = self.allowed_grades(project_id, pv.manifest)
        if engine_id == FAKE_ENGINE_ID or engine_id in self.engines:
            engine = self.engines.get(engine_id, self.engine)
            route = dict(getattr(engine, "route", None) or FAKE_ROUTE, engine_id=engine_id)
            grade = require_route(route, allowed, what=engine_id)
            route["reasons"] = [
                f"local: {engine_id} runs in this process ({grade.value}); no vendor route is involved",
                f"doctrine: task {task!r} on a {shot_class} shot",
            ]
            return route
        manifest = self.manifests.get(engine_id)
        if manifest is None:
            raise PipelineError(f"unknown engine {engine_id!r}", code="unknown_engine")
        if manifest.kind not in IMAGE_ENGINE_KINDS:
            raise PipelineError(
                f"{engine_id} is a {manifest.kind} engine; an Edit node takes an image editor",
                code="engine_kind",
            )
        candidates = [r for r in manifest.routes if r.status == "active"]
        if route_id is not None:
            candidates = [r for r in candidates if r.route_id == route_id]
            if not candidates:
                raise PipelineError(f"{engine_id} has no active route {route_id!r}", code="unknown_route")
        ok = [r for r in candidates if route_allowed(r, allowed)]
        if not ok:
            if not candidates:
                raise PipelineError(f"{engine_id} has no active route", code="unknown_route")
            require_route(candidates[0], allowed, what=engine_id)  # raises CustodyViolation
        runnable = [r for r in ok if adapter_class(manifest, r) is not None]
        if not runnable:
            raise PipelineError(f"no adapter speaks {engine_id} via {ok[0].route_id}", code="no_adapter")
        choice, reasons = self.route_choice(project_id, manifest, runnable, allowed, task, shot_class)
        r = next(x for x in runnable if x.route_id == choice.route_id)
        if manifest.sizes is None and r.upload_visibility != "local":
            raise PipelineError(
                f"{engine_id} declares no sizes: egress would have to guess what {r.provider} accepts",
                code="no_sizes",
            )
        return {
            "engine_id": engine_id,
            "provider": r.provider,
            "region": r.region or "unknown",
            "route_id": r.route_id,
            "custody_grade": r.custody_grade,
            "custody_status": r.custody_status,
            "upload_visibility": r.upload_visibility,
            "model_version": r.model_slug or manifest.model_id or manifest.version,
            "reasons": reasons,
        }

    def render_it(self, pv: PacketView, task: str) -> dict[str, Any]:
        """Doctrine 4 before spend: the detail the frame can resolve, as `run.inputs["router"]`. Advisory
        unless `PLATE_ROUTER_MODE=blocking`, where a `vray_only` frame is refused (409 `render_it`); people
        stay allowed (AI for people by region is the router's own exception)."""
        from plate_gates import router_math

        try:
            mode = router_math.router_mode(os.environ.get(router_math.ROUTER_MODE_ENV))
        except ValueError as e:
            raise PipelineError(str(e), code="router_mode", status=500) from e
        if pv.tier != "A" or "beauty_exr" not in pv.files:
            return {
                "verdict": "unknown",
                "D_px_median": None,
                "per_class": {},
                "mode": mode,
                "reasons": ["render-it: Tier C has relative depth only; a person decides (PACKET.md 11)"],
            }
        try:
            d = pv.detail_resolvability()
        except Exception as e:  # advisory numbers: a packet they cannot be read from is unknown, not refused
            log.warning("render-it numbers for %s not computed: %s", pv.packet_id, e)
            return {
                "verdict": "unknown",
                "D_px_median": None,
                "per_class": {},
                "mode": mode,
                "reasons": [f"render-it: not computed ({type(e).__name__}: {e})"[:300]],
            }
        verdict = str(d.get("verdict") or "unknown")
        median = d.get("D_px_median")
        median = round(float(median), 3) if median is not None else None
        per_class = {str(k): round(float(v), 3) for k, v in (d.get("per_class") or {}).items()}
        why = {
            "vray_only": f"below {router_math.D_VRAY_ONLY_PX:g} px a generator would invent the detail: render "
            "it in V-Ray (AI for people and patches by region)",
            "ai_by_region": f"{router_math.D_VRAY_ONLY_PX:g} to {router_math.D_WHOLE_FRAME_PX:g} px: AI by "
            "region; no whole-frame re-photograph",
            "whole_frame_ok": f"at or above {router_math.D_WHOLE_FRAME_PX:g} px: a whole-frame re-photograph "
            "may be allowed on close and medium frames",
        }.get(verdict, "no textured pixels to measure: a person decides")
        shown = f"{median:.1f} px" if median is not None else "n/a"
        out = {
            "verdict": verdict,
            "D_px_median": median,
            "per_class": per_class,
            "mode": mode,
            "reasons": [f"render-it: detail D median {shown} -> {verdict} ({mode}); {why}"],
        }
        if mode == "blocking" and verdict == "vray_only" and task != "people":
            raise PipelineError(
                f"render it: the frame resolves {shown} of detail; {why}",
                code="render_it",
                status=409,
                router=out,
            )
        return out

    # ------------------------------------------------------------------ the prompt

    def load_recipe(self, ref: Any) -> Any:
        """A runnable still recipe by id, or by a `.yaml` path (`recipes.load_runnable`)."""
        from . import recipes

        if not isinstance(ref, str) or not (
            _RECIPE_ID.match(ref) or Path(ref).suffix.lower() in (".yaml", ".yml")
        ):
            raise PipelineError(f"recipe {ref!r}: a recipe id or a .yaml path", code="unknown_recipe")
        try:
            r = recipes.load_runnable(ref)
        except recipes.RecipeError as e:
            raise PipelineError(str(e), code="unknown_recipe") from e
        if r.kind not in ("still_edit", "still_rephotograph"):
            raise PipelineError(
                f"recipe {r.id} is a {r.kind} recipe; an Edit node takes a still recipe", code="recipe_kind"
            )
        return r

    def llm_client(self, project_id: str, allowed: set[Grade]) -> Any:
        """The composer's Anthropic client: the run's grades (custody first), recorded fixtures by default, its
        calls ledgered in the project."""
        from .llm.client import AnthropicClient

        return AnthropicClient(
            allowed_grades=sorted(g.value for g in allowed),
            ledger=Ledger(self.project_dir(project_id), clock=self.clock),
        )

    def resolve_prompt(
        self,
        project: Project,
        pv: PacketView,
        body: Mapping[str, Any],
        params_node: Mapping[str, Any],
        recipe: Any,
    ) -> tuple[str | None, str | None, str | None]:
        """(prompt, prompt_source, negative_prompt). Explicit > recipe (decision 33); a recipe is composed by
        the LLM when the Anthropic route's custody allows it, else filled from the Project Sheet alone."""
        explicit = body.get("prompt") or params_node.get("prompt")
        if isinstance(explicit, str) and explicit.strip():
            return explicit, "explicit", None
        if recipe is None:
            return None, None, None
        from . import recipes

        try:
            sheet = recipes.ProjectSheet.from_dict((project.doc.get("settings") or {}).get("project_sheet"))
        except recipes.RecipeError as e:
            raise PipelineError(str(e), code="bad_project_sheet") from e
        text = recipes.assemble_runnable(recipe, sheet)
        negative = recipe.negative_prompt or recipes.DEFAULT_NEGATIVE_PROMPT
        try:
            client = self.llm_client(project.project_id, self.allowed_grades(project.project_id, pv.manifest))
            if client.available:
                from .llm import composer

                facts = composer.facts_from_packet(pv.manifest, pv.legend)
                out = composer.compose(recipe, sheet, facts, intent=recipe.title, client=client)
                if out.llm_used:
                    return out.prompt, "composed", out.negative_prompt
                if out.llm_error:
                    log.info("composer fell back to the template for %s: %s", recipe.id, out.llm_error)
        except Exception as e:  # a composer that cannot answer (unrecorded prompt) never blocks a quote
            log.info("composer not used for %s: %s: %s", recipe.id, type(e).__name__, e)
        return text, "recipe", negative

    def _engine_manifest_for(self, engine_id: str) -> Any:
        if engine_id == FAKE_ENGINE_ID or engine_id in self.engines:
            return getattr(self.engines.get(engine_id, self.engine), "manifest", None)
        return self.manifests.get(engine_id)

    def _declared(self, engine_id: str, what: str, name: str) -> Mapping[str, Any] | Any | None:
        """The manifest's `inputs` or `params` entry called `name`, or None."""
        m = self._engine_manifest_for(engine_id)
        items = (m.get(what) if isinstance(m, Mapping) else getattr(m, what, None)) or ()
        for item in items:
            n = item.get("name") if isinstance(item, Mapping) else getattr(item, "name", None)
            if n == name:
                return item
        return None

    def prompt_required(self, engine_id: str) -> bool:
        item = self._declared(engine_id, "inputs", "prompt")
        if item is None:
            return False
        return bool(
            item.get("required", True) if isinstance(item, Mapping) else getattr(item, "required", True)
        )

    # ------------------------------------------------------------------ quoting a node

    def mask_spec(self, graph: Any, node_id: str, pv: PacketView) -> dict[str, Any]:
        """The editable mask a node edits: the nearest upstream Mask node, else the packet's policy.

        A Mask node's `ids`, `maskHash` (painted) and `samHash` (segmented) are one union (decision 34):
        `{"kind": "union", "ids", "keys", "source"}` when it carries a CAS mask, `{"kind": "ids", ...}` when it
        has ids only. `params.textLock` does not enter here: the packet's `policy.text_lock` always wins."""
        g = canvas_rules.as_graph(graph)
        by_id = {n["id"]: n for n in g.nodes}
        up: dict[str, list[str]] = {}
        for e in g.edges:
            up.setdefault(e["target"], []).append(e["source"])
        frontier, seen = list(up.get(node_id, ())), {node_id}
        mask_node = None
        while frontier and mask_node is None:
            nxt: list[str] = []
            for nid in frontier:
                if nid in seen:
                    continue
                seen.add(nid)
                n = by_id.get(nid)
                if n is not None and n.get("kind") == "Mask":
                    mask_node = n
                    break
                nxt.extend(up.get(nid, ()))
            frontier = nxt
        spec: dict[str, Any]
        if mask_node is not None:
            params = mask_node.get("params") if isinstance(mask_node.get("params"), Mapping) else {}
            source = f"mask:{mask_node['id']}"
            raw_ids = params.get("ids")
            ids = [str(i) for i in raw_ids] if isinstance(raw_ids, list) else []
            keys: list[str] = []
            for name in MASK_KEY_PARAMS:
                key = params.get(name)
                if key is None or key == "":
                    continue
                if not isinstance(key, str) or not _HEX64.match(key):
                    raise PipelineError(
                        f"Mask node {mask_node['id']}: {name} must be a CAS key (64 hex)", code="bad_mask"
                    )
                if key not in keys:
                    keys.append(key)
            if keys:
                spec = {"kind": "union", "ids": ids, "keys": keys, "source": source}
            elif ids:
                spec = {"kind": "ids", "ids": ids, "source": source}
            elif isinstance(mask_node.get("outputHash"), str):
                spec = {"kind": "cas", "key": mask_node["outputHash"], "source": source}
            else:
                raise PipelineError(
                    f"Mask node {mask_node['id']} has no ids, no painted mask and no segment mask",
                    code="mask_empty",
                )
        elif pv.editable_ids():
            spec = {"kind": "ids", "ids": pv.editable_ids(), "source": "policy"}
        elif pv.tier == "C":
            spec = {"kind": "frame", "source": "tier_c"}
        else:
            raise PipelineError(
                f"packet {pv.packet_id} lists no editable ids and the node has no Mask", code="mask_empty"
            )
        if spec["kind"] in ("ids", "union"):
            locked = sorted(set(spec["ids"]) & set(pv.locked_ids()))
            if locked:
                raise PipelineError(f"ids {locked} are locked by the packet policy", code="mask_locked")
        return spec

    def check_mask_keys(self, project_id: str, spec: Mapping[str, Any], pv: PacketView) -> None:
        """A union's CAS masks exist and decode at the plate's size (when the manifest states it)."""
        keys = list(spec.get("keys") or ())
        if not keys:
            return
        cas = self.cas_for(project_id)
        entry = pv.files.get(pv.plate_role) or {}
        size = (entry.get("width"), entry.get("height"))
        for key in keys:
            if not cas.exists(key):
                raise PipelineError(f"mask {key[:12]} is not in the project's CAS", code="mask_missing")
            raw = cv2.imdecode(np.frombuffer(cas.read_bytes(key), np.uint8), cv2.IMREAD_GRAYSCALE)
            if raw is None:
                raise PipelineError(f"mask {key[:12]} is not a decodable image", code="bad_mask")
            if all(isinstance(v, int) for v in size) and (raw.shape[1], raw.shape[0]) != size:
                raise PipelineError(
                    f"mask {key[:12]} is {raw.shape[1]} x {raw.shape[0]}; the plate is {size[0]} x {size[1]}",
                    code="mask_size",
                )

    def next_attempt(
        self,
        conn: Any,
        node_id: str,
        inputs: Mapping[str, Any],
        params: Mapping[str, Any],
        route: Mapping[str, Any],
    ) -> int:
        """1 + the highest attempt of this node's earlier runs of the same request (decision 40), else 1."""
        want = repeat_canonical(inputs, params, route)
        best = 0
        for row in conn.execute(
            "SELECT attempt, inputs, params, route FROM runs WHERE node_id = ?", (node_id,)
        ):
            try:
                same = repeat_canonical(loads(row[1]) or {}, loads(row[2]) or {}, loads(row[3]) or {}) == want
            except (TypeError, ValueError):
                continue
            if same:
                best = max(best, int(row[0] or 0))
        return best + 1

    def quote_node(
        self,
        conn: Any,
        project_id: str,
        node_id: str,
        body: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> tuple[Run, Quote]:
        """A canvas node -> a QUOTED run. Refusals, all before a run row exists: 404 project or node; 422 rules,
        mask, engine, task (`router_refused:<code>`) and `prompt_required`; 409 packet not ingested,
        `retired_pin`, `pin_conflict` and `render_it` (blocking mode); `CustodyViolation` (403) when no route of
        the engine is allowed. Body (all optional): engine_id, route_id, packet_id, task, shot_class, recipe,
        prompt, params, preset, region_crop, feather_px, priority, attempt (server-side when absent)."""
        now = now or self.clock()
        project = self.load_project(project_id)
        if project is None:
            raise PipelineError(f"no project {project_id}", code="not_found", status=404)
        g = canvas_rules.as_graph(project.graph)
        node = next((n for n in g.nodes if n.get("id") == node_id), None)
        if node is None:
            raise PipelineError(f"project {project_id} has no node {node_id}", code="not_found", status=404)
        if node.get("kind") not in QUOTABLE_KINDS:
            raise PipelineError(
                f"{node.get('kind')} nodes are not quoted by this daemon yet", code="node_kind_unsupported"
            )
        upstream = canvas_rules.ancestors(g, node_id) | {node_id}
        broken = [v for v in canvas_rules.validate_graph(g) if v["edge"]["target"] in upstream]
        if broken:
            v = broken[0]
            raise PipelineError(v["message"], code=v["code"], violations=broken)
        plates = canvas_rules.upstream_plates(g, node_id)
        if not plates:
            raise PipelineError(
                "generation must be anchored to a plate camera (source-view-only)", code="NO_PLATE_ANCHOR"
            )
        packet_id = body.get("packet_id") or plates[0].get("packetId")
        if not isinstance(packet_id, str) or not packet_id:
            raise PipelineError(f"Plate node {plates[0]['id']} names no packet", code="unknown_packet")
        pv = self.packet(project_id, packet_id)
        params_node = node.get("params") if isinstance(node.get("params"), Mapping) else {}
        recipe_ref = body.get("recipe") or params_node.get("recipe")
        recipe = self.load_recipe(recipe_ref) if recipe_ref else None
        engine_id = (
            body.get("engine_id")
            or params_node.get("engine")
            or params_node.get("engine_id")
            or (recipe.engine if recipe is not None else None)
        )
        if not isinstance(engine_id, str) or not engine_id:
            raise PipelineError("say which engine: engine_id in the body or params.engine on the node")
        default_task = (
            "rephotograph_close"
            if recipe is not None and recipe.kind == "still_rephotograph"
            else DEFAULT_TASK
        )
        task = str(body.get("task") or params_node.get("task") or default_task)
        shot_class = str(
            body.get("shot_class")
            or params_node.get("shot_class")
            or params_node.get("shotClass")
            or (recipe.shot_class if recipe is not None else None)
            or DEFAULT_SHOT_CLASS
        )
        route = self.pick_route(
            project_id, pv, engine_id, body.get("route_id"), task=task, shot_class=shot_class
        )
        router = self.render_it(pv, task)
        router["reasons"] = [*router["reasons"], *route.get("reasons", ())]
        region_crop = body.get("region_crop")
        if region_crop is None:
            region_crop = bool(params_node.get("region_crop") or params_node.get("regionCrop"))
        staging = {
            "region_crop": bool(region_crop),
            "feather_px": float(body.get("feather_px") or DEFAULT_FEATHER_PX),
            "preset": body.get("preset"),
            "node_kind": node.get("kind"),
            "task": task,
            "shot_class": shot_class,
        }
        mask = self.mask_spec(g, node_id, pv)
        self.check_mask_keys(project_id, mask, pv)
        inputs: dict[str, Any] = {"plate": pv.plate_hash, "mask": mask, STAGING_KEY: staging}
        params = dict(body.get("params") or {})
        prompt, prompt_source, negative = self.resolve_prompt(project, pv, body, params_node, recipe)
        if prompt is None and self.prompt_required(engine_id):
            raise PipelineError(
                f"{engine_id} needs a prompt: give prompt, or a recipe to build one from",
                code="prompt_required",
            )
        if prompt is not None:
            inputs["prompt"] = prompt
            inputs["prompt_source"] = prompt_source
        if recipe is not None:
            inputs["recipe_id"] = recipe.id
        if (
            negative
            and "negative_prompt" not in params
            and self._declared(engine_id, "params", "negative_prompt") is not None
        ):
            params["negative_prompt"] = negative
        inputs[ROUTER_KEY] = router
        inputs[SUBGRAPH_KEY] = subgraph_snapshot(project.graph, upstream)
        attempt = body.get("attempt")
        if attempt is None:
            attempt = self.next_attempt(conn, node_id, inputs, params, route)
        spec = RunCreate(
            project_id=project_id,
            packet_id=packet_id,
            node_id=node_id,
            priority=Priority.parse(body.get("priority") or "interactive"),
            inputs=inputs,
            params=params,
            route=route,
            attempt=int(attempt),
        )
        run = create_run(conn, spec, now=now)
        self.index.remember(run.id, project_id)
        try:
            run, q = quote_run(conn, run.id, now=now, manifests=self.manifests)
        except ValueError as e:
            transition(conn, run.id, S.CANCELLED, actor="system", reason=f"could not be priced: {e}"[:500])
            raise PipelineError(f"could not price {engine_id}: {e}", code="unpriceable") from e
        self.after(conn, project_id, run.id, S.DRAFT)
        return run, q

    # ------------------------------------------------------------------ approval and queueing

    def approve(
        self,
        conn: Any,
        project_id: str,
        run_id: str,
        by: str,
        envelope_id: str | None = None,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> list[Run]:
        """QUOTED -> APPROVED for the run, or for its whole best-of-N group. Custody is re-checked first for
        every run (the project's custody may have tightened since the quote). An approval the ledger cannot
        record cancels the group (503 `ledger_unavailable`)."""
        now = now or self.clock()
        run = get_run(conn, run_id)
        self.require_custody(run)
        if by == "human":
            self.touch_human(now)

        def alert(decision: budgets.Decision) -> None:
            try:
                budgets.publish_alert(self.bus, project_id, run_id, decision)
            except Exception as e:  # the bus is for watchers
                log.warning("budget alert for %s not published: %s", run_id, e)

        kwargs = {"policy": self.policy, "last_human_interaction_utc": self.last_human_utc, "on_alert": alert}
        if run.sibling_group:
            runs = approve_group(conn, run.sibling_group, by, envelope_id, now, **kwargs)
        else:
            runs = [approve_one(conn, run_id, by, envelope_id, now, **kwargs)]
        try:
            for r in runs:
                self.after(conn, project_id, r.id, S.QUOTED, note=reason)
        except PipelineError as e:
            if e.code == "ledger_unavailable":  # one unrecorded approval stops the group: none of it is spent
                for r in runs:
                    self._cancel_unledgered(conn, project_id, r.id, "approved", e)
            raise
        return runs

    def queue(self, conn: Any, project_id: str, run_id: str) -> Run:
        run = transition(conn, run_id, S.QUEUED, actor="api")
        self.after(conn, project_id, run_id, S.APPROVED)
        return run

    # ------------------------------------------------------------------ staging (egress)

    @staticmethod
    def _grey_mask(cas: CAS, key: str, w: int, h: int) -> np.ndarray:
        raw = cv2.imdecode(np.frombuffer(cas.read_bytes(key), np.uint8), cv2.IMREAD_GRAYSCALE)
        if raw is None or raw.shape != (h, w):
            raise PipelineError(f"mask {key[:12]} is not a {w} x {h} grey PNG", code="mask_size")
        return raw > 127

    def editable_mask(self, run: Run, pv: PacketView, cas: CAS) -> np.ndarray:
        """What the run may change: its mask spec, always within the packet's editable ids and never on a
        locked id (decision 34; every path, so nothing composites onto a locked id)."""
        spec = run.inputs.get("mask") or {"kind": "ids", "ids": pv.editable_ids()}
        h, w = pv.plate16().shape[:2]
        kind = spec.get("kind")
        if kind == "frame":
            mask = np.ones((h, w), dtype=bool)
        elif kind == "cas":
            mask = self._grey_mask(cas, spec["key"], w, h)
        elif kind in ("ids", "union"):
            mask = np.zeros((h, w), dtype=bool)
            for cid in spec.get("ids") or ():
                mask |= pv.id_mask(cid)
            for key in spec.get("keys") or ():
                mask |= self._grey_mask(cas, key, w, h)
        else:
            raise PipelineError(f"unknown mask kind {kind!r}")
        mask = mask & pv.editable_px((h, w)) & ~pv.locked_px((h, w))
        if not mask.any():
            raise PipelineError(
                "the editable mask is empty (inside the packet's editable ids, outside its locked ids)",
                code="mask_empty",
            )
        return mask

    def engine_manifest(self, run: Run) -> Any:
        """What egress sizes the plate for. A local model with no declared sizes takes the frame at native
        size (no vendor preset applies); a cloud engine without sizes was refused at the quote."""
        engine_id = run.route.engine_id
        if engine_id == FAKE_ENGINE_ID or engine_id in self.engines:
            engine = self.engines.get(engine_id, self.engine)
            return getattr(engine, "manifest", None) or FAKE_SIZES
        manifest = self.manifests.get(engine_id)
        if manifest is None or (
            manifest.sizes is None and run.route_dict.get("upload_visibility") == "local"
        ):
            return FAKE_SIZES
        return manifest

    def stage(self, run: Run, cas: CAS | None = None) -> Staged:
        """egress.prepare for this run (deterministic, cached; recomputed after a restart)."""
        key = (run.id, run.attempt)
        with self._lock:
            hit = self._staged.get(key)
            if hit is not None:
                self._staged.move_to_end(key)
                return hit
        from plate_pixels import egress as pixels_egress

        cas = cas or self.cas_for(run.project_id)
        pv = self.packet(run.project_id, run.packet_id)
        if run.inputs.get("plate") not in (None, pv.plate_hash):
            raise PipelineError(f"packet {pv.packet_id}: the plate is not the one this run was quoted on")
        opts = run.inputs.get(STAGING_KEY) or {}
        mask = self.editable_mask(run, pv, cas)
        png, sm = pixels_egress.prepare(
            pv.plate16(),
            pv.manifest,
            self.engine_manifest(run),
            mask=mask,
            region_crop=bool(opts.get("region_crop")),
        )
        digest = blake3(png).hexdigest()
        staged = Staged(
            png=png,
            sizemap=sm,
            mask=mask,
            png_key=cas.put_bytes(png, "png"),
            mask_key=cas.put_bytes(_mask_png(mask), "png"),
            opaque_name=f"{digest[:26]}.png",
        )
        with self._lock:
            self._staged[key] = staged
            while len(self._staged) > STAGE_CACHE:
                self._staged.popitem(last=False)
        return staged

    def vendor_payload(self, run: Run, staged: Staged, seed_payload: Mapping[str, Any]) -> dict[str, Any]:
        """What the engine receives: the egress PNG under an opaque name, the prompt, the params."""
        image = {
            "b64": base64.b64encode(staged.png).decode("ascii"),
            "mime": "image/png",
            "name": staged.opaque_name,
            "role": "plate",
            "sizemap": staged.sizemap.to_dict(),
        }
        inputs: dict[str, Any] = {"image": image}
        if isinstance(run.inputs.get("prompt"), str):
            inputs["prompt"] = run.inputs["prompt"]
        payload: dict[str, Any] = {
            "engine_id": run.route.engine_id,
            "inputs": inputs,
            "params": dict(run.params),
        }
        if seed_payload.get("variant") is not None:
            payload["variant"] = seed_payload["variant"]
        return payload

    def egress_for(self, project_id: str) -> Any:
        with self._lock:
            eg = self._egress.get(project_id)
            if eg is None:
                from .egress import Egress
                from .outbound_log import OutboundLog

                eg = Egress(self.settings, OutboundLog.for_project(self.project_dir(project_id)))
                self._egress[project_id] = eg
            return eg

    def inner_engine(self, run: Run) -> Any:
        eid = run.route.engine_id
        if eid in self.engines:
            return self.engines[eid]
        if eid == FAKE_ENGINE_ID:
            if self.engine is None:
                raise PipelineError("the daemon has no fake engine", code="no_adapter")
            return self.engine
        with self._lock:
            hit = self._inner.get(run.id)
        if hit is not None:
            return hit
        manifest = self.manifests.get(eid)
        if manifest is None:
            raise PipelineError(f"unknown engine {eid!r}", code="unknown_engine")
        rid = run.route_dict.get("route_id") or run.route.provider
        route = next((r for r in manifest.routes if r.route_id == rid), None)
        cls = adapter_class(manifest, route) if route is not None else None
        if cls is None:
            raise PipelineError(f"no adapter speaks {eid} via {rid}", code="no_adapter")
        transport = None
        if route.upload_visibility != "local":
            from .egress import EgressRun

            pv = self.packet(run.project_id, run.packet_id)
            project = self.load_project(run.project_id)
            erun = EgressRun.for_project(
                project.doc if project else {"project_id": run.project_id},
                run.id,
                packet=pv.manifest,
                legend=pv.legend,
                packet_dir=pv.root,
            )
            transport = self.egress_for(run.project_id).transport(erun, route)
        kwargs: dict[str, Any] = {}
        if (
            "staging_dir" in inspect.signature(cls).parameters
        ):  # results stay under PLATE_HOME, not the OS temp
            kwargs["staging_dir"] = self.settings.home / "cache" / "adapters" / run.id
        bridge = AdapterBridge(cls(manifest, route, transport=transport, **kwargs))
        with self._lock:
            self._inner[run.id] = bridge
        return bridge

    def engine_for(self, run: Run, cas: CAS) -> Any:
        """The engine the worker drives: a StagedEngine for pipeline runs; for the seed's `POST /runs` runs the
        daemon's own engine, whatever their route says (they never leave the box)."""
        if not is_pipeline_run(run):
            return self.engines.get(run.route.engine_id, self.engine)
        return StagedEngine(self, run, self.inner_engine(run), cas)

    # ------------------------------------------------------------------ normalizing (ingress) and gating

    def text_lock(self, pv: PacketView, plate16: np.ndarray) -> np.ndarray:
        if pv.policy.get("text_lock") is False:
            return np.zeros(plate16.shape[:2], dtype=bool)
        from plate_gates import textlock
        from plate_gates.io import to_uint8

        crypto = pv.crypto("object") if pv.tier == "A" else None
        return textlock.masks(to_uint8(plate16), pv.legend or {}, crypto, self.ocr)

    def normalize(self, run: Run, cas: CAS) -> dict[str, Any]:
        """NORMALIZING: the engine's bytes back onto the plate, composited inside the mask, text locked."""
        from plate_gates import composite, textlock
        from plate_pixels import ingress

        pv = self.packet(run.project_id, run.packet_id)
        staged = self.stage(run, cas)
        plate16 = pv.plate16()
        restored, rep = ingress.restore(
            cas.read_bytes(run.result_key), staged.sizemap, plate16, ring_mask=~staged.mask
        )
        opts = run.inputs.get(STAGING_KEY) or {}
        feather = float(opts.get("feather_px") or DEFAULT_FEATHER_PX)
        comp = composite.composite_masked(plate16, restored, staged.mask, feather_px=feather)
        lock = self.text_lock(pv, plate16)
        if lock.any():
            comp = textlock.apply(plate16, comp, lock)
        data, embedded = self._with_lineage(run, encode_png16(comp))
        out_key = cas.put_bytes(data, "png")
        self._record_phash(run.project_id, cas, out_key)
        keep = (
            "shift_px",
            "residual_px",
            "registered",
            "camera_drift",
            "dx",
            "dy",
            "warp_applied",
            "result_size",
            "aspect_crop",
            "resample",
            "region",
            "sizemap_mode",
        )
        return {
            "outputs": [out_key],
            "raw_key": run.result_key,
            "egress_key": staged.png_key,
            "mask_key": staged.mask_key,
            "sizemap": staged.sizemap.to_dict(),
            "ingress": {k: rep.get(k) for k in keep},
            "composite": {
                "feather_px": feather,
                "tone_match_ring": True,
                "mask_share": float(staged.mask.mean()),
            },
            "textlock_px": int(lock.sum()),
            "lineage": embedded,
        }

    def _record_phash(self, project_id: str, cas: CAS, key: str) -> None:
        """The output's perceptual hash in the index, so a re-encoded copy dropped later still resolves
        (`phash.resolve_drop`). A failure is logged; it never fails the run."""
        try:
            from .phash import PhashIndex

            path = cas.find(key)
            if path is None:
                raise FileNotFoundError(f"{key[:12]} is not in the CAS")
            with self.index.project(project_id) as conn:
                PhashIndex(conn, project_id).add_file(path, key)
        except Exception as e:
            log.warning("phash of %s in %s not recorded: %s", key[:12], project_id, e)

    def _with_lineage(self, run: Run, data: bytes) -> tuple[bytes, bool]:
        """Embed the run's lineage in the output PNG when `lineage.embed` exists (T26); else the bytes as given."""
        try:
            from . import lineage
        except ImportError:
            return data, False
        if not hasattr(lineage, "embed") or not hasattr(lineage, "Lineage"):
            return data, False
        tmp = self.settings.home / "cache" / f"lineage_{run.id}_{run.attempt}.png"
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            lin = lineage.Lineage(
                project_id=run.project_id,
                node_id=run.node_id or "none",
                run_id=run.id,
                packet_id=run.packet_id,
                engine_id=run.route.engine_id,
                engine_version=str(run.route_dict.get("model_version") or "") or None,
                gate_report=self._next_report_ref(run),
            )
            lineage.embed(tmp, lin)
            return tmp.read_bytes(), True
        except Exception as e:  # lineage is a convenience for drop-to-resolve, never a reason to fail a run
            log.warning("lineage not embedded in %s: %s", run.id, e)
            return data, False
        finally:
            tmp.unlink(missing_ok=True)

    def report_service(self, project_id: str) -> Any:
        try:
            from .gate_reports import Service
        except ImportError:
            return None
        return Service(self.project_dir(project_id))

    def _next_report_ref(self, run: Run) -> str:
        svc = self.report_service(run.project_id)
        n = (max(svc.versions(run.id), default=0) + 1) if svc is not None else 1
        return f"gates/{run.id}.{n}.json"

    def gate(self, run: Run, cas: CAS) -> GateOutcome:
        """GATING: score the stored output; the report is versioned in the project's gates/ folder."""
        from . import gating

        norm = run.normalized or {}
        outputs = norm.get("outputs") or []
        if not outputs:
            raise PipelineError("nothing to gate: the run has no output")
        pv = self.packet(run.project_id, run.packet_id)
        staged = self.stage(run, cas)
        out16 = decode_png16(cas.read_bytes(outputs[0]))
        report = gating.run_gates(
            pv.plate16(),
            out16,
            {"path": pv.root, "manifest": pv.manifest},
            pv.legend,
            pv.tier,
            {"id": run.route.engine_id, "version": str(run.route_dict.get("model_version") or "") or None},
            mask=staged.mask,
            registration=norm.get("ingress"),
            depth_fn=self.depth_fn,
            edges=pv.edges(),
            crypto_material=pv.crypto("material") if pv.tier == "A" else None,
            extra_metrics={"textlock_px": norm.get("textlock_px"), "output": outputs[0]},
        )
        project_dir = self.project_dir(run.project_id)
        svc = self.report_service(run.project_id)
        if svc is not None:
            path = svc.write(run.id, report)
        else:
            from plate_gates.report import write

            path = write(report, project_dir, run.id)
        try:
            ref = Path(path).relative_to(project_dir).as_posix()
        except ValueError:
            ref = Path(path).as_posix()
        return GateOutcome(
            passed=report.verdict != "fail",
            report=report.model_dump(mode="json"),
            score=gating.score_of(report),
            report_path=ref,
        )

    # ------------------------------------------------------------------ driving

    def step(self, conn: Any, project_id: str, run_id: str, cas: CAS | None = None) -> RunState:
        """One worker transition with the pipeline's hooks; published and ledgered when the state moved."""
        cas = cas or self.cas_for(project_id)
        run = get_run(conn, run_id)
        before = run.state
        if before in TERMINAL or before not in WORKER_STATES:
            return before
        try:
            engine = self.engine_for(run, cas)
        except PipelineError as e:
            to = S.CANCELLED if before is S.QUEUED else S.FAILED
            transition(conn, run_id, to, actor="worker", reason=str(e)[:500], reason_code=e.code)
            self.after(conn, project_id, run_id, before)
            return to
        pipeline_run = is_pipeline_run(run)
        after = worker_step(
            conn,
            engine,
            run_id,
            self.clock,
            cas=cas,
            poll_timeout_s=self.poll_timeout_s,
            normalize_fn=self.normalize if pipeline_run else None,
            gate_fn=self.gate if pipeline_run else None,
        )
        if after is not before:
            self.after(conn, project_id, run_id, before)
        return after

    def execute(
        self, run_id: str | Run, project_id: str | None = None, *, max_steps: int = 100_000
    ) -> RunState:
        """Drive a QUEUED (or resumed) run to a terminal state, sleeping between idle polls. `run_id` may be
        the Run itself."""
        if isinstance(run_id, Run):
            project_id, run_id = project_id or run_id.project_id, run_id.id
        pid = project_id or self.index.locate_run(run_id)
        if pid is None:
            raise KeyError(f"run {run_id} is not in any project index")
        cas = self.cas_for(pid)
        state = S.QUEUED
        with self.index.project(pid) as conn:
            for _ in range(max_steps):
                before = get_run(conn, run_id).state
                if before in TERMINAL or before not in WORKER_STATES:
                    state = before
                    break
                state = self.step(conn, pid, run_id, cas)
                if state in TERMINAL:
                    break
                if state is before:
                    self.sleep(self.poll_interval_s)
            else:
                raise RuntimeError(f"run {run_id} did not finish within {max_steps} steps")
        self._rank_group(pid, run_id)
        return state

    def execute_all(self, project_id: str, run_ids: list[str]) -> dict[str, str]:
        """Background entry point: run each queued run to its end; a crash of one never stops the others."""
        out: dict[str, str] = {}
        for rid in run_ids:
            try:
                out[rid] = self.execute(rid, project_id).value
            except Exception as e:  # logged and recorded; the run's own state says what happened
                log.exception("run %s stopped: %s", rid, e)
                out[rid] = f"error: {type(e).__name__}"
        return out

    def resume(self) -> list[str]:
        """Drive on every pipeline run a stopped daemon left in a worker state (start-up; the worker's STAGING
        rules still apply, so nothing is resubmitted blind). Returns the run ids it picked up."""
        picked: list[str] = []
        states = tuple(s.value for s in WORKER_STATES)
        marks = ",".join("?" * len(states))
        for pid in self.index.project_ids():
            with self.index.project(pid) as conn:
                ids = [
                    r[0]
                    for r in conn.execute(
                        f"SELECT id FROM runs WHERE state IN ({marks}) ORDER BY created_utc, id", states
                    )
                ]
                todo = [rid for rid in ids if is_pipeline_run(get_run(conn, rid))]
            if todo:
                self.execute_all(pid, todo)
                picked.extend(todo)
        return picked

    def cancel(
        self, conn: Any, project_id: str, run_id: str, *, actor: str = "human", reason: str | None = None
    ) -> Run:
        from .worker import cancel_run

        run = get_run(conn, run_id)
        before = run.state
        try:
            engine = self.engine_for(run, self.cas_for(project_id))
        except PipelineError:
            engine = self.engine
        out = cancel_run(conn, engine, run_id, actor=actor, reason=reason)
        self.after(conn, project_id, run_id, before)
        return out

    def _rank_group(self, project_id: str, run_id: str) -> None:
        with self.index.project(project_id) as conn:
            run = get_run(conn, run_id)
            group = run.sibling_group
            if not group or group in self._ranked:
                return
            runs = siblings(conn, group)
            if not runs or any(r.state not in TERMINAL for r in runs):
                return
            ranked = rank_siblings(conn, group)
        with self._lock:
            if group in self._ranked:
                return
            self._ranked.add(group)
        self.publish(
            project_id,
            None,
            {
                "kind": "siblings.ranked",
                "group": group,
                "order": [r.id for r in ranked],
                "scores": [r.gate_score for r in ranked],
                "states": [r.state.value for r in ranked],
            },
        )

    # ------------------------------------------------------------------ bus and ledger

    def publish(self, project_id: str, run_id: str | None, event: dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(run_id, event, project_id=project_id)
        except Exception as e:  # the bus is for watchers; a failed publish never stops a run
            log.warning("event %s for %s not published: %s", event.get("kind"), run_id, e)

    def after(
        self, conn: Any, project_id: str, run_id: str, before: RunState, *, note: str | None = None
    ) -> None:
        """Publish and ledger the move a run just made from `before` to its current state."""
        run = get_run(conn, run_id)
        event = {"kind": "run.state", "run_id": run_id, "from": before.value, "to": run.state.value}
        if run.reason_code:
            event["reason_code"] = run.reason_code
        self.publish(project_id, run_id, event)
        kind = LEDGER_KINDS.get(run.state)
        if kind is not None:
            self.ledger(project_id, kind, run, note=note, conn=conn)

    @staticmethod
    def routing_data(run: Run) -> dict[str, Any]:
        """`data.model`, `data.task`, `data.shot_class`, `data.repeat_key` for the engine cards and reconcile."""
        staging = run.inputs.get(STAGING_KEY) if isinstance(run.inputs.get(STAGING_KEY), Mapping) else {}
        out = {
            "model": run.route_dict.get("model_version"),
            "task": staging.get("task") or run.inputs.get("task"),
            "shot_class": staging.get("shot_class") or run.inputs.get("shot_class"),
            "repeat_key": repeat_key(run),
        }
        return {k: v for k, v in out.items() if v is not None}

    def _cancel_unledgered(
        self, conn: Any, project_id: str, run_id: str, kind: str, err: BaseException
    ) -> None:
        """CANCELLED with `ledger_unavailable`: the run's quote or approval is not in the folder's ledger, so it
        must not spend. Published on the bus; the `cancelled` record is attempted (and only warned about)."""
        try:
            before = get_run(conn, run_id).state
            if before in TERMINAL:
                return
            transition(
                conn,
                run_id,
                S.CANCELLED,
                actor="system",
                reason=f"ledger_unavailable: the {kind} record could not be written ({type(err).__name__})",
                reason_code="ledger_unavailable",
            )
            run = get_run(conn, run_id)
            self.publish(
                project_id,
                run_id,
                {
                    "kind": "run.state",
                    "run_id": run_id,
                    "from": before.value,
                    "to": run.state.value,
                    "reason_code": "ledger_unavailable",
                },
            )
            self.ledger(project_id, "cancelled", run, conn=conn)
        except Exception as e:  # the 503 still goes out; the index says what state the run is in
            log.error("run %s could not be cancelled after the ledger failed: %s", run_id, e)

    def ledger(
        self,
        project_id: str,
        kind: str,
        run: Run,
        *,
        note: str | None = None,
        conn: Any = None,
        **extra: Any,
    ) -> None:
        """Append one record for `run` to the project's runs.jsonl (state and fields the index rebuild reads).

        Any failure is a warning, except for `quoted` and `approved`: then the run is cancelled
        (`ledger_unavailable`, through `conn` or a connection of its own) and `PipelineError` 503 is raised."""
        route = run.route_dict
        rec: dict[str, Any] = {
            "kind": kind,
            "run_id": run.id,
            "engine_id": route.get("engine_id"),
            "route_id": route.get("route_id") or route.get("provider"),
            "custody_grade": run.quote.custody_grade if run.quote else None,
            "node_id": run.node_id,
        }
        data: dict[str, Any] = {"state": run.state.value, "packet_id": run.packet_id}
        if kind in ("quoted", "submitted"):
            data.update(self.routing_data(run))
        if kind == "quoted":
            rec["cost_usd_quoted"] = run.quote.price_usd if run.quote else None
            data.update(
                {
                    "priority": run.priority.label,
                    "inputs": run.inputs,
                    "params": run.params,
                    "route": route,
                    "attempt": run.attempt,
                    "quote": run.quote.terms_json() if run.quote else None,
                }
            )
        elif kind == "approved":
            rec["by"] = run.approved_by
            rec["cost_usd_quoted"] = run.quote.price_usd if run.quote else None
        elif kind == "submitted":
            rec["provider_job_id"] = run.provider_job_id
            data["idempotency_key"] = run.idempotency_key
        elif kind == "fetched":
            rec["provider_job_id"] = run.provider_job_id
            data["result_key"] = run.result_key
        elif kind in ("done", "gated"):
            rec["provider_job_id"] = run.provider_job_id
            rec["gate_report"] = run.gate_report_path or None
            data.update(
                {
                    "outputs": run.outputs,
                    "result_key": run.result_key,
                    "verdict": (run.gate_report or {}).get("verdict"),
                    "score": run.gate_score,
                }
            )
        else:  # failed, refused, cancelled, expired, override and the rest
            rec["provider_job_id"] = run.provider_job_id
            if run.reason_code:
                data["reason_code"] = run.reason_code
            if run.error:
                rec["reason"] = run.error[:500]
            if not run.provider_job_id and kind in ("failed", "cancelled"):
                data["refunded"] = True
        if note:
            rec["reason"] = note[:500]
        rec.update(extra)
        data.update(rec.pop("data", None) or {})
        rec["data"] = data
        rec = {k: v for k, v in rec.items() if v is not None and v != ""}
        try:
            Ledger(self.project_dir(project_id), clock=self.clock).append(rec)
        except Exception as e:
            if kind not in FAIL_CLOSED_KINDS:  # the index holds the state; reindex cannot see this one
                log.warning("ledger record %s for %s not written: %s", kind, run.id, e)
                return
            log.error("ledger record %s for %s not written: %s; the run is cancelled", kind, run.id, e)
            if conn is not None:
                self._cancel_unledgered(conn, project_id, run.id, kind, e)
            else:
                try:
                    with self.index.project(project_id) as own:
                        self._cancel_unledgered(own, project_id, run.id, kind, e)
                except Exception as e2:
                    log.error("run %s could not be cancelled after the ledger failed: %s", run.id, e2)
            raise PipelineError(
                f"the project ledger is unavailable ({type(e).__name__}: {e}); run {run.id} was cancelled "
                "before anything was spent",
                code="ledger_unavailable",
                status=503,
                run_id=run.id,
            ) from e

    # ------------------------------------------------------------------ helpers for the routes

    def project_list(self) -> list[dict[str, Any]]:
        """The head of every known project.json: indexed projects plus folders under projects_dir."""
        seen: dict[str, dict[str, Any]] = {}
        roots = [self.project_dir(pid) for pid in self.index.project_ids()]
        pdir = self.settings.projects_dir
        if pdir is not None and pdir.is_dir():
            roots.extend(p for p in sorted(pdir.iterdir()) if (p / "project.json").is_file())
        for root in roots:
            try:
                project = Project.load(root)
            except (FileNotFoundError, ProjectError, ValueError):
                continue
            seen.setdefault(project.project_id, project_head(project))
        return [seen[k] for k in sorted(seen)]


def project_head(project: Project) -> dict[str, Any]:
    """What `GET /projects` lists for one project: the head of its project.json."""
    return {
        "project_id": project.project_id,
        "custody": project.custody,
        "created_utc": project.doc.get("created_utc"),
        "engine_pins": project.engine_pins,
        "root": str(project.root),
        "nodes": len(project.graph.get("nodes") or []),
    }
