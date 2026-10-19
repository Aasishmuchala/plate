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
sibling ends. Bus failures are logged, never fatal. A ledger record is never dropped (Wave 12 below): `quoted`,
`approved` and `override` fail closed (a quote or an approval the ledger cannot record is cancelled,
`ledger_unavailable` 503, before anything is spent; an override is refused and nothing changes), and every
other kind the file cannot take waits in the index's ledger outbox until it can.

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

Wave 9 (router plan T92; decisions 46, 47, 50, 51, 54 and 55):

- References. The Reference nodes wired to an Edit's `refs` input become `inputs.reference_images`
  (`[{hash, role, node}]`, edge order), refused at the quote with 422 `references_unsupported` (the engine
  declares no `reference_images` input), `too_many_references` (over its `max_items`) or `reference_missing`
  (not in the project's CAS). The vendor gets each as an 8-bit PNG re-encoded from the CAS (no metadata), never
  longer than the engine's largest legal side (INTER_AREA, never enlarged), under an opaque name; the
  `submitted` record lists their hashes. A reference is an engine input only: the composite stays mask-locked.
- Base. An Edit whose `image` input is fed by a take (any node but a Plate) starts from that take's
  `outputHash` (`inputs.base`, `base_node`, `base_run`), 422 `no_base` when it has none in the CAS. Egress
  sends the base, the composite lands on the base, the text lock restores from the plate, and the gates score
  against the plate inside the union of this mask and the base run's (`normalized.mask_union_key`), so
  outside the union the output is the plate bit for bit (Wave 12 below: the base must be a take of its node).
- Mask to engines. An engine whose manifest declares a `mask` input (and whose adapter takes one) gets the
  staged mask in the egress geometry (`inputs.mask`, 0/255 grey PNG; `normalized.mask_sent`).
- Recipe tokens. `params.tokens` (`{"[TOKEN]": text}`) override a recipe's token defaults
  (`inputs.recipe_tokens`); recipes resolve from the project's folder, then the studio's, then the package.
- Pinned size map. A run whose `inputs.staging` holds `sizemap` and `png_key` is staged from them (only the mask
  is recomputed), so a re-composite lands where its parent's region crop did.
- Ingress hooks. `Pipeline.ingress_hooks` (`[(name, fn(run, bytes, staged, packet) -> (bytes, record))]`) run on
  the engine's bytes before `ingress.restore`; records land in `normalized.ingress_hooks`, a raising hook fails
  the run with `reason_code ingress_hook:<name>`.
- Degraded routes. `health_board()` marks every route `route_tracker.degraded()` names as `degraded` (never
  routed to); the quote's router reasons name each degraded route of the engine.

Wave 10 (router plan T113):

- Reference tokens. `@ref1`..`@refN` in the prompt (N = the Reference nodes wired to the Edit, in edge order)
  become the engine's own wording for the K-th reference image: the manifest's `reference_token` (on its
  `reference_images` input, else on the manifest; `{k}` marks the number, a token without it takes " K"
  appended), default `reference image {k}`. The route dict carries `reference_token` when the manifest has
  one. Refused at the quote, before a run row, with 422 `bad_ref_token`: `@refK` with K past N, an `@ref` with
  no number (or any other `@ref...` word, such as `@ref0` or `@ref1a`), or any token on an engine that
  declares no `reference_images` input. `inputs.prompt` is the substituted text (what the quote shows, the
  ledger records and the engine receives); `inputs.prompt_raw` keeps the artist's text when it differed.

Wave 12 (router plan T129; decisions 72 and 80):

- A base is a take of its node. `base_input` takes an `outputHash` only when it is `outputs[0]` (or
  `result_key`) of a run of the feeding node in this project that ended DONE, or GATE_FAIL with a recorded
  override (`base_take_for`). Anything else is 422 `base_not_a_take` (an upload or a foreign blob: import it
  first, `POST /import` gates it) or `base_gate_fail`. The lineage (`lineage_depth`: the chain of
  `inputs.base_run`, an import counting as one generation) plus this generation must stay within
  `canvas_rules.MAX_GENERATION_DEPTH`, else 422 `DEPTH_EXCEEDED` naming the chain; a take relayed through
  another node's `outputHash` is measured the same way. The run stores `base_run`, `base_state`,
  `base_override_reason` and `base_depth`, and the gate report carries them as its top-level `base`.
  `prior_mask` is the base run's `mask_union_key`, else its `mask_key`; there is no pixel-difference term, and a
  base run with neither fails NORMALIZING (`base_mask_missing`).
- The quote validates what the run will need, before a run row: `priority` (422 `bad_priority`; 0 is the
  client-deadline class), every number in `params` finite (`bad_params`), the editable mask itself
  (`editable_mask_from_spec`: `mask_empty`, `mask_size`, `bad_mask`), Mask ids that are legend ids of the
  packet (`bad_mask`), a Mask node's `outputHash` at the plate's size (`mask_size`), and, on a manifest engine,
  the adapter's `validate_params` on exactly what staging sends (`engine_params`; `bad_params` with the
  adapter's message). A recipe path that is not a runnable recipe is `unknown_recipe` naming the path only.
- Attempts are numbered and the run row made under a per-node lock (`node_lock`), so concurrent quotes of one
  node number 1..N; an attempt past `ATTEMPT_MAX` is 422 `attempt_exhausted`. A quote that fails after its row
  exists leaves no DRAFT or QUOTED row behind (`quote failed: <error>`, CANCELLED).
- Approval. The daily and unattended caps count the spend of every project index in this home
  (`budgets.spend_snapshot_all`; approvals in this process are serialised so the count stays true), and a
  quote that lapsed or was made after `now` (the clock moved) is EXPIRED and ledgered `expired`.

Wave 12 (router plan T145; decisions 73 and 75):

- The ledger. One `Ledger` per project (`ledger_for`, with this pipeline's clock; T134 memoises its reads).
  `FAIL_CLOSED_KINDS` are `quoted`, `approved` and `override`. Before an append, the project's
  `ledger_outbox` is drained in order (`drain_ledger_outbox`: always when the caller passes `conn`, else when
  this process parked something there); a record of any other kind that cannot be appended, or that would
  overtake rows still parked, is parked behind them (`enqueue_outbox`, WARNING `queued in the ledger outbox`).
  The outbox is also drained on boot (`resume`) and by the executor's watch (`orphaned_runs`). Records written
  with `conn` carry `data.event_seq`, the run's newest `run_events` seq. A `failed` record whose reason is
  `submit_state_unknown` carries `data.billing_unknown: true` and never `refunded` (the provider may hold it).
- One run's records in transition order, once each. `after` holds a per-run lock and ledgers every
  transition since the last one this process ledgered for the run (only the newest when it has seen none),
  each with the `event_seq` of the newest event before the next transition: an approval a cancel overtook is
  still on the record, before its `cancelled`, and the second of two racing calls writes nothing.
- Execution. `start_runs(project_id, run_ids, background=...)` is the one way a route starts runs: with
  `PLATE_RUN_WORKERS=0` (`executor.inline`, what the test suite sets) a BackgroundTask runs `execute_all` as
  before; otherwise `executor.RunExecutor` (default 8 workers) drives them off the request pool, at most the
  route's `max_inflight` at a time (the manifest route's, else `max_inflight` on a manifest-less engine's
  route dict, else 2). The pool and its watch start on first use (`start_runs` or the boot `resume`), so a
  process that only drives its own runs (the CLI) never starts them; `close()` stops it. The watch re-drives
  pipeline runs in a worker state that nothing in this process is driving and whose last move is older than
  `ORPHAN_GRACE_S` (a driver in another process, the CLI, is left alone while it is moving).
- `execute` retries a step that hits a busy or locked index (`sqlite3.OperationalError`) after 0.5, 1, 2, 4,
  then 8 s, up to `BUSY_RETRIES` in a row, then re-raises; it stops driving (the run keeps its state for the
  next boot) once the executor is stopped.
- Boot. `resume` notes `resume` on every run it picks up and hands them to the executor (inline: the old
  serial loop). A run in FETCHING, NORMALIZING or GATING that already has `RESUME_LIMIT` (2) `resume` notes from
  those states is FAILED `resume_limit` instead of a third attempt (the result blob stays in the CAS).
- `project_list` lists a folder whose `project.json` does not read as `{"project_id", "root", "broken": true,
  "detail"}` (WARNING) instead of hiding it.

Wave 13 (router plan T151; decision 83):

- Project leases. A project another daemon's live lease covers (`lease.held_by_other`) is left alone by
  `resume` and by the executor's watch (`orphaned_runs`): its runs stay with that daemon, with a WARNING
  `project <id> is leased by <host> pid <pid>: its runs stay with that daemon` at most once per project per
  `lease.LEASE_TTL_S`. Every write goes through `project.lock`, which raises `lease.ProjectLeased` (409
  `project_leased`); `quote_node` and `approve` ask first (`refuse_if_leased`), so a refused quote leaves no run
  row behind. A `quoted` or `approved` record the lease refuses cancels the run in this index only
  (`project_leased`, nothing ledgered: the folder never had it) and `ProjectLeased` goes on; an `override`
  changes nothing; any other kind waits in the ledger outbox as for any other failure. `project_head` (so `GET
  /projects`) carries `lease: {host, pid, daemon_id, heartbeat_utc, live, mine}` or None.

Wave 13 (router plan T152; decisions 84, 85 and 86):

- The router's inputs are computed once, at ingest. `router_summary(conn, pv)` reads `detail_resolvability`'s
  answer from the index's `packet_router` row (`ROUTER_SUMMARY_VERSION`); a missing row, or one of another
  version, is computed (`PacketView.detail_resolvability`: Z and the object Cryptomatte from one open of the EXR)
  and stored. `warm_router` runs at the end of an ingest (`api.create_app`'s finish) for a Tier A packet with a
  beauty EXR; a failure there is a WARNING and the quote computes lazily. `quote_node` passes its connection to
  `render_it`, so a quote never decodes the EXR for the router once the row exists.
- A scripted crash exits the process on every execution path. With `crash_exits_process` (set by
  `python -m plate_daemon`), `execute` turns a `SimulatedCrash` into `os._exit(70)`: the executor's threads, the
  inline BackgroundTask and the boot resume die as a crashed daemon would, like the dev `step` route already did.
- The result's format is recorded, not judged. `normalized.ingress` carries `raw_format`, `raw_bit_depth`,
  `raw_channels`, `alpha_dropped`, `alpha_transparent` and `lossy` (`plate_pixels.ingress.result_info`); the
  gate report names a lossy container, a dropped alpha and a fully transparent one in `backend_notes`
  (`result_notes`). A float result with a non-finite pixel fails NORMALIZING (`normalize_error`, `non-finite`).
"""

from __future__ import annotations

import base64
import copy
import importlib
import inspect
import json
import logging
import math
import os
import re
import sqlite3
import struct
import sys
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from blake3 import blake3

from . import budgets, canvas_rules, lease
from .adapter import PollResult, SimulatedCrash, lookup_fn, submit_is_idempotent
from .cas import CAS, hash_bytes, hash_file
from .config import Settings, check_project_id
from .custody import CustodyViolation, Grade, project_allowed, require_route, route_allowed
from .db import dumps, loads, tx
from .executor import DEFAULT_MAX_INFLIGHT, RunExecutor
from .idempotency import canonical_json
from .index import Index, packet_entry
from .ledger import Ledger, drain_outbox, enqueue_outbox, outbox_count
from .machine import IllegalTransition, annotate, append_note, create_run, get_run, transition
from .models import Quote, Run, RunCreate
from .projects import Project, ProjectError
from .quotes import QuoteExpired, approve_group, quote_run
from .quotes import approve as approve_one
from .states import TERMINAL, WORKER_STATES, Priority, RunState
from .timeutil import iso, parse_iso, utcnow
from .worker import GateOutcome, rank_siblings, route_key, siblings
from .worker import step as worker_step

__all__ = [
    "ASSUMED_OK",
    "ATTEMPT_MAX",
    "AdapterBridge",
    "CustodyViolation",
    "FAKE_ENGINE_ID",
    "FAKE_ROUTE",
    "IngressHook",
    "PacketView",
    "Pipeline",
    "PipelineError",
    "QUOTABLE_KINDS",
    "REFERENCE_INPUT",
    "STAGING_KEY",
    "Staged",
    "StagedEngine",
    "UNSET_CUSTODY",
    "adapter_class",
    "decode_png16",
    "encode_png16",
    "index_busy",
    "is_pipeline_run",
    "lease_view",
    "project_head",
    "repeat_canonical",
    "repeat_key",
    "result_notes",
    "run_workers",
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
# Decision 84: the version of the `packet_router` rows this code writes; a row of another version is recomputed.
ROUTER_SUMMARY_VERSION = "2026.10.19"
SUBGRAPH_KEY = "subgraph"
REPEAT_EXCLUDED_INPUTS = (SUBGRAPH_KEY, ROUTER_KEY)  # snapshots, not what the engine is asked to do
REPEAT_KEY_HEX = 16
# A ledger record of these kinds is the only proof of a quote or an approval: without it the run stops.
# Decision 73: a record of these kinds is in the folder or the request is refused; quoted and approved are also
# cancelled (nothing may spend without them), an override simply changes nothing.
FAIL_CLOSED_KINDS = frozenset({"quoted", "approved", "override"})
CANCEL_ON_LOSS_KINDS = frozenset({"quoted", "approved"})
LEASED = "project_leased"  # decision 83: the reason code of a run a foreign lease stopped in this index
SUBMIT_STATE_UNKNOWN = "submit_state_unknown"
CRASH_EXIT_CODE = 70  # decision 85: what a scripted SimulatedCrash exits with under crash_exits_process
# Decision 86: what NORMALIZING records about the engine's result (plate_pixels.ingress.result_info).
RESULT_INFO_KEYS = (
    "raw_format",
    "raw_bit_depth",
    "raw_channels",
    "alpha_dropped",
    "alpha_transparent",
    "lossy",
)
# Decision 75: the run executor. 0 workers runs inline through a BackgroundTask (the test suite sets it).
RUN_WORKERS_ENV = "PLATE_RUN_WORKERS"
DEFAULT_RUN_WORKERS = 8
BUSY_BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 8.0)  # then 8 s again, up to BUSY_RETRIES in a row
BUSY_RETRIES = 10
RESUME_NOTE = "resume"
RESUME_LIMIT = 2  # earlier resume notes in a local stage: the next boot fails the run instead
RESUME_LIMITED = frozenset({RunState.FETCHING, RunState.NORMALIZING, RunState.GATING})
ORPHAN_GRACE_S = 60.0  # a run that moved more recently may have a driver in another process (the CLI)
CLOSE_TIMEOUT_S = 5.0
LEDGER_MEMO = 4096  # runs whose last ledgered transition this process remembers
ROUTE_MEMO = 1024
MASK_KEY_PARAMS = ("maskHash", "samHash")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RECIPE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
DEFAULT_FEATHER_PX = 8.0
DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_POLL_TIMEOUT_S = 3600.0
STAGE_CACHE = 8
PACKET_CACHE = 4
# Wave 12 (T129): attempts are numbered under a per-node lock and stop here (decision 80).
ATTEMPT_MAX = 1_000_000
# A legend id as the packet schema writes it: object and material Cryptomatte hashes, Tier C segment labels.
MASK_ID = re.compile(r"^(o:[0-9a-f]{8}|m:[0-9a-f]{8}|s:[0-9]{5})$")
# Decision 72: the states a base take may be in (GATE_FAIL only with a recorded override); the lineage walk's cap.
BASE_STATES = ("DONE", "GATE_FAIL")
LINEAGE_MAX_HOPS = 8
# Params that steer PLATE and never reach an engine: recipe tokens (decision 50) and the AI-layer upscale switch
# (`layer_upscale.PARAM`, T100). `engine_params` drops them; the quote validates what is left.
PLATE_PARAMS = frozenset({"tokens", "upscale_layers"})
# What the quote's mask probe refuses (decision 80); any other failure to build the mask is left for staging.
MASK_REFUSALS = frozenset({"mask_empty", "mask_size", "bad_mask", "mask_locked", "mask_missing"})
# Wave 9 (T92): reference images (decision 46), the Edit-on-upstream base (decision 47), ingress hooks (55).
REFERENCE_INPUT = "reference_images"
REFERENCE_HANDLE = "refs"
REFERENCE_ROLES = ("style", "object", "sky", "person", "swatch")
DEFAULT_REFERENCE_ROLE = "style"
# Wave 10 (T113): `@ref1`..`@refN` in a prompt. A word starting `@ref` that is not a lone positive number is
# a bad token (`@ref`, `@ref0`, `@ref1a`); `@refine` and `a@ref1` are plain text.
REF_TOKEN = re.compile(r"(?<![A-Za-z0-9_])@ref(?![A-Za-z_])[A-Za-z0-9_]*")
DEFAULT_REFERENCE_TOKEN = "reference image {k}"
BASE_HANDLE = "image"
MASK_INPUT = "mask"
INGRESS_HOOK_CODE = "ingress_hook"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
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
    "runway_video",
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


def run_workers(env: Mapping[str, str] | None = None) -> int:
    """`PLATE_RUN_WORKERS`: the run executor's workers (default 8; 0 runs inline). A value that is not a whole
    number of at least 0 is a warning and the default."""
    raw = (os.environ if env is None else env).get(RUN_WORKERS_ENV, "").strip()
    if not raw:
        return DEFAULT_RUN_WORKERS
    try:
        n = int(raw)
    except ValueError:
        n = -1
    if n < 0:
        log.warning("%s=%r is not a whole number >= 0; using %d", RUN_WORKERS_ENV, raw, DEFAULT_RUN_WORKERS)
        return DEFAULT_RUN_WORKERS
    return n


def index_busy(e: BaseException) -> bool:
    """A write that waited out the index's busy timeout (another connection held it): worth a retry."""
    text = str(e).lower()
    return isinstance(e, sqlite3.OperationalError) and ("locked" in text or "busy" in text)


@dataclass
class _RunMoves:
    """One run in this process: the lock that keeps its ledger records in transition order, and the
    `run_events` seq of the last transition already ledgered (None: none seen yet)."""

    lock: threading.RLock = field(default_factory=threading.RLock)
    seq: int | None = None


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


def _opaque_name(data: bytes) -> str:
    """What a vendor sees as a file name: the bytes' own digest (no project, packet or camera name)."""
    return f"{blake3(data).hexdigest()[:26]}.png"


def _image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of an image: a PNG's IHDR, else a decode; None when the bytes are not an image."""
    if len(data) >= 24 and data[:8] == _PNG_SIGNATURE and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    return None if img is None else (int(img.shape[1]), int(img.shape[0]))


def _non_finite(params: Any) -> tuple[str, float] | None:
    """(where, value) of the first float in `params` (mappings and lists, walked without recursion) that is
    not finite (`inf`, `-inf`, `nan`), else None."""
    stack: list[tuple[str, Any]] = [("params", params)]
    while stack:
        where, value = stack.pop()
        if isinstance(value, float):
            if not math.isfinite(value):
                return where, value
        elif isinstance(value, Mapping):
            stack.extend((f"{where}.{k}", v) for k, v in reversed(list(value.items())))
        elif isinstance(value, (list, tuple)):
            stack.extend((f"{where}[{i}]", v) for i, v in reversed(list(enumerate(value))))
    return None


class _QuoteTransport:
    """What a manifest adapter is built with at the quote (`check_engine_params`): it validates, it never sends."""

    def handle_request(self, request: Any) -> Any:
        raise RuntimeError("an adapter built at the quote never sends a request")

    def close(self) -> None:
        return None


def _plain(record: Any) -> dict[str, Any]:
    """A hook's record as JSON-safe data (it is stored in `normalized`)."""
    if record is None:
        return {}
    out = json.loads(json.dumps(record, default=str))
    return out if isinstance(out, dict) else {"value": out}


def _edge_landing(
    source_kind: Any, target_kind: Any, edge: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """(the type an edge carries, the input id it lands on), resolved through `canvas_rules.PORTS` as the rules
    resolve a missing handle: the source's first output, the target's first input that takes that type."""
    outs = (
        (canvas_rules.PORTS.get(source_kind) or {}).get("outputs", ()) if isinstance(source_kind, str) else ()
    )
    sh = edge.get("sourceHandle")
    out = next((p for p in outs if p[0] == sh), None) if sh else (outs[0] if outs else None)
    etype = out[1] if out is not None else None
    ins = (
        (canvas_rules.PORTS.get(target_kind) or {}).get("inputs", ()) if isinstance(target_kind, str) else ()
    )
    th = edge.get("targetHandle")
    if th:
        port = next((p for p in ins if p[0] == th), None)
    else:
        port = next((p for p in ins if etype is not None and etype in p[2]), ins[0] if ins else None)
    return etype, (port[0] if port is not None else None)


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
        """`router_math.detail_resolvability` on this still (Tier A: Z and the object Cryptomatte; cached). Z and
        the Cryptomatte come from one open of the EXR unless the Cryptomatte is already decoded (T152)."""
        if "detail" not in self._cache:
            from plate_gates import router_math
            from plate_pixels import exr as exr_io

            exr = self.files.get("beauty_exr")
            if exr is None:
                raise PipelineError(f"packet {self.packet_id} has no Z and object Cryptomatte")
            path = self.root / exr["path"]
            if "crypto_object" in self._cache:
                pair = self._cache["crypto_object"]
                z = exr_io.read_elements(path, self.manifest, ["zdepth"])["zdepth"]
            else:
                z, pair = exr_io.read_z_and_cryptomatte(path, self.manifest, "object")
                self._cache["crypto_object"] = pair
            if pair is None:
                raise PipelineError(f"packet {self.packet_id} has no Z and object Cryptomatte")
            zd = next((e for e in exr.get("elements") or () if e.get("element") == "zdepth"), {})
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


# A NORMALIZING hook (decision 55): the engine's raw bytes in, (bytes, a record for `normalized`) out.
IngressHook = Callable[[Run, bytes, Staged, PacketView], tuple[bytes, dict[str, Any]]]


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
        for name in ("image", MASK_INPUT):
            if name in inputs:
                inputs[name] = self._as_bytes(inputs[name])
        if isinstance(inputs.get(REFERENCE_INPUT), list):
            inputs[REFERENCE_INPUT] = [self._as_bytes(r) for r in inputs[REFERENCE_INPUT]]
        params = self.adapter.validate_params(dict(payload.get("params") or {}))
        return self.adapter.submit(EngineStaged(inputs=inputs, params=params))

    @staticmethod
    def _as_bytes(item: Any) -> Any:
        """An image input as the adapters read it: the payload's `b64` decoded into `bytes`."""
        if isinstance(item, Mapping) and "b64" in item:
            return {**{k: v for k, v in item.items() if k != "b64"}, "bytes": base64.b64decode(item["b64"])}
        return item

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
    """The `plate_engines.adapters` class that speaks `manifest` on `route` (by engine id and provider). A class
    that names the route's provider wins over one that names none (a shared base such as the Seedance fallbacks'),
    so a second provider for the same engine (Runway, Wave 11) is never answered by another provider's base."""
    agnostic: type | None = None
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
            if manifest.id not in ids:
                continue
            if provider == route.provider:
                return obj
            if provider is None and agnostic is None:
                agnostic = obj
    return agnostic


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
        crash_exits_process: bool = False,
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
        # Decision 85: a scripted SimulatedCrash ends the process (python -m plate_daemon), on every path.
        self.crash_exits_process = crash_exits_process
        self.last_human_utc: datetime | None = None
        self._lock = threading.RLock()
        self._node_locks: dict[tuple[str, str], threading.Lock] = {}  # (project, node) -> attempt numbering
        self._approval_lock = threading.Lock()  # one cap decision at a time, so the summed spend stays true
        self._staged: OrderedDict[tuple[str, int], Staged] = OrderedDict()
        self._packets: OrderedDict[tuple[str, str], PacketView] = OrderedDict()
        self._inner: dict[str, Any] = {}
        self._egress: dict[str, Any] = {}
        self._ranked: set[str] = set()
        self._health: tuple[Any, dict[str, Any]] | None = None
        # Decision 55: applied in order to the engine's bytes before ingress.restore (T100 adds the upscaler).
        self.ingress_hooks: list[tuple[str, IngressHook]] = []
        self._hook_failed: dict[tuple[str, int], str] = {}  # (run, attempt) -> the reason code step() records
        # route_health.Tracker (T85/T107 attach it); None: no route is ever marked degraded.
        self.route_tracker: Any = None
        # Decision 73: one Ledger per project; per-run ledger order; projects with records parked in the outbox.
        self._ledgers: dict[str, Ledger] = {}
        self._moves: OrderedDict[str, _RunMoves] = OrderedDict()
        self._outbox_dirty: set[str] = set()
        # Decision 75: runs drive on a bounded pool (started on first use), never on the request pool.
        self._driving: dict[tuple[str, str], int] = {}  # (project, run) -> drivers in this process
        self._routes: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self.executor = RunExecutor(
            self._execute_pair,
            workers=run_workers(),
            max_inflight_for=self.max_inflight_for,
            route_key_for=self.route_key_for,
        )
        self._executor_live = False
        self._closed = False
        self._lease_warned: dict[str, float] = {}  # project -> monotonic time of the last "is leased" WARNING

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

    def leased_elsewhere(self, project_id: str) -> lease.LeaseInfo | None:
        """Decision 83: another daemon's live lease on the project (None: none, or leases are off in this
        process), with a WARNING at most once per project per `LEASE_TTL_S`."""
        holder = lease.held_by_other(self.project_dir(project_id))
        if holder is None:
            return None
        now = time.monotonic()
        with self._lock:
            last = self._lease_warned.get(project_id)
            warn = last is None or now - last >= lease.LEASE_TTL_S
            if warn:
                self._lease_warned[project_id] = now
        if warn:
            log.warning(
                "project %s is leased by %s pid %s: its runs stay with that daemon",
                project_id,
                holder.host,
                holder.pid,
            )
        return holder

    def refuse_if_leased(self, project_id: str) -> None:
        """`lease.ProjectLeased` when another daemon's live lease covers the project (asked before a quote or an
        approval makes or moves a run row; `project.lock` refuses the write itself either way)."""
        root = self.project_dir(project_id)
        holder = lease.held_by_other(root)
        if holder is not None:
            raise lease.ProjectLeased.of(holder, Path(root).absolute())

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

    def _ingested_row(self, project_id: str, packet_id: str) -> Any:
        """The packet's index row (path, status, manifest_blake3), refused unless the packet is ingested."""
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
        return row

    def packet(self, project_id: str, packet_id: str) -> PacketView:
        row = self._ingested_row(project_id, packet_id)
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
        yet`), then the alert logs' suspensions apply (rebuilt when an alert file changes), then decision 51:
        every route the `route_tracker` reports is `degraded` (shown, never routed to). A down or suspended
        entry already on the board says more and is kept."""
        board = self._alert_board()
        degraded = self.degraded_routes()
        if not degraded:
            return board
        from plate_engines.health import HealthBoard, status_of

        marked = HealthBoard(board, clock=self.clock)
        for key, d in degraded.items():
            cur = board.get(key)
            if cur is not None and status_of(cur) not in ("ok", "degraded"):
                continue
            marked.set(
                key,
                "degraded",
                reason=f"degraded: {d.get('failures')} consecutive provider failures since {d.get('since')} "
                f"(last: {d.get('reason') or 'unknown'})",
            )
        return marked.as_mapping()

    def degraded_routes(self) -> dict[str, dict[str, Any]]:
        """`route_tracker.degraded()` (`{"<engine>/<route>": {failures, since, reason}}`), or {} without a
        tracker. A tracker that cannot be read degrades nothing; the warning says so."""
        tracker = getattr(self, "route_tracker", None)
        if tracker is None:
            return {}
        try:
            return {str(k): dict(v) for k, v in (tracker.degraded() or {}).items()}
        except Exception as e:
            log.warning("route health not read: %s", e)
            return {}

    def degraded_reasons(self, engine_id: str, health: Mapping[str, Any] | None = None) -> list[str]:
        """`health: <key> degraded: ...` for every degraded entry of this engine (its own key or a route's)."""
        from plate_engines.health import reason_of, status_of

        if health is None and not self.degraded_routes():
            return []  # nothing is degraded: no need to build the board (a local quote loads no manifests)
        board = self.health_board() if health is None else health
        return [
            f"health: {key} {reason_of(entry)}"
            for key, entry in sorted(board.items())
            if (key == engine_id or key.startswith(f"{engine_id}/")) and status_of(entry) == "degraded"
        ]

    def _alert_board(self) -> dict[str, Any]:
        """The manifests' active routes assumed ok, with the alert logs' suspensions (cached on the files)."""
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
        reasons.extend(self.degraded_reasons(manifest.id, health))
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
                *self.degraded_reasons(engine_id),
            ]
            if self.reference_token(engine_id) is not None:
                route["reference_token"] = self.reference_token(engine_id)
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
        picked = {
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
        if self.reference_token(engine_id) is not None:  # T113: the engine's wording for `@refK`
            picked["reference_token"] = self.reference_token(engine_id)
        return picked

    def router_summary(self, conn: Any, pv: PacketView) -> dict[str, Any]:
        """Decision 84: `detail_resolvability`'s answer for the packet, from its `packet_router` row when that
        row is of `ROUTER_SUMMARY_VERSION`; otherwise computed (the EXR decoded once) and stored, replacing an
        older row. A row that will not parse is recomputed; a store that fails is logged and the answer stands."""
        row = conn.execute(
            "SELECT json FROM packet_router WHERE packet_id = ? AND version = ?",
            (pv.packet_id, ROUTER_SUMMARY_VERSION),
        ).fetchone()
        if row is not None:
            try:
                hit = json.loads(row["json"])
            except ValueError as e:
                log.warning("router summary of %s does not parse (%s); recomputing", pv.packet_id, e)
            else:
                if isinstance(hit, dict):
                    return hit
        d = pv.detail_resolvability()
        try:
            with tx(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO packet_router (packet_id, version, json, computed_utc) "
                    "VALUES (?, ?, ?, ?)",
                    (pv.packet_id, ROUTER_SUMMARY_VERSION, dumps(_plain(d)), iso(self.clock())),
                )
        except sqlite3.Error as e:  # a cache: the next quote computes it again
            log.warning("router summary of %s not stored: %s", pv.packet_id, e)
        return d

    def warm_router(self, project_id: str, packet_id: str) -> dict[str, Any] | None:
        """Decision 84, at the end of an ingest: the router summary of an ingested Tier A packet with a beauty EXR,
        computed and stored now so its first quote decodes nothing for the router. None for any other packet, and
        for any failure (a WARNING; the quote computes it lazily). The packet is read into a view of its own,
        never into the packet cache: only the row outlives the warm-up (a packet nobody quotes holds no decoded
        layers, and a share that goes away later reads as gone)."""
        t0 = time.monotonic()
        try:
            row = self._ingested_row(project_id, packet_id)
            pv = PacketView.load(packet_id, row["path"], row["manifest_blake3"])
            if pv.tier != "A" or "beauty_exr" not in pv.files:
                return None
            with self.index.project(project_id) as conn:
                d = self.router_summary(conn, pv)
        except Exception as e:
            log.warning(
                "router summary for %s not computed at ingest (%s: %s); the first quote computes it",
                packet_id,
                type(e).__name__,
                e,
            )
            return None
        log.info("router summary for %s computed in %.2f s", packet_id, time.monotonic() - t0)
        return d

    def render_it(self, pv: PacketView, task: str, conn: Any = None) -> dict[str, Any]:
        """Doctrine 4 before spend: the detail the frame can resolve, as `run.inputs["router"]`. Advisory
        unless `PLATE_ROUTER_MODE=blocking`, where a `vray_only` frame is refused (409 `render_it`); people
        stay allowed (AI for people by region is the router's own exception). With `conn` (the project index)
        the numbers come from `router_summary`, so the EXR is decoded only when no current row exists."""
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
            d = self.router_summary(conn, pv) if conn is not None else pv.detail_resolvability()
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

    def load_recipe(self, ref: Any, project_id: str | None = None) -> Any:
        """A runnable still recipe by id (the project's `recipes/` folder, then the studio's, then the package:
        decision 50), or by a `.yaml` path (`recipes.load_runnable`). A path that is not a runnable recipe is
        refused naming the path only: the parser's context would quote the file's lines (decision 80)."""
        from . import recipes

        if not isinstance(ref, str) or not (
            _RECIPE_ID.match(ref) or Path(ref).suffix.lower() in (".yaml", ".yml")
        ):
            raise PipelineError(f"recipe {ref!r}: a recipe id or a .yaml path", code="unknown_recipe")
        by_path = not _RECIPE_ID.match(ref)
        try:
            r = recipes.load_runnable(ref, project_dir=self.project_dir(project_id) if project_id else None)
        except recipes.RecipeError as e:
            if by_path:
                raise PipelineError(
                    f"recipe {ref}: not a readable runnable recipe file", code="unknown_recipe"
                ) from None
            raise PipelineError(str(e), code="unknown_recipe") from e
        except Exception:  # a document that is not a recipe mapping at all
            raise PipelineError(
                f"recipe {ref}: not a readable runnable recipe file", code="unknown_recipe"
            ) from None
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
        *,
        engine_id: str | None = None,
        references: Sequence[Any] | None = None,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """(prompt, prompt_source, negative_prompt, raw_prompt). Explicit > recipe (decision 33); a recipe is
        composed by the LLM when the Anthropic route's custody allows it, else filled from the Project Sheet
        alone. The node's `params.tokens` (decision 50) override the recipe's token defaults on both paths.

        With `engine_id` (the quote passes it with the node's resolved `references`) every `@refK` in the text
        becomes that engine's name for the K-th reference image (`substitute_ref_tokens`; 422 `bad_ref_token`).
        `prompt` is then the substituted text and `raw_prompt` what the artist's text said before; the two are
        the same string when nothing was substituted."""
        raw, source, negative = self._compose_prompt(project, pv, body, params_node, recipe)
        if raw is None or engine_id is None:
            return raw, source, negative, raw
        return self.substitute_ref_tokens(raw, engine_id, references or ()), source, negative, raw

    def reference_token(self, engine_id: str) -> str | None:
        """The engine's own wording for a reference image, as its manifest states it: `reference_token` on the
        `reference_images` input, else on the manifest itself. None when the manifest has none (the default,
        `reference image {k}`, then applies)."""
        item = self._declared(engine_id, "inputs", REFERENCE_INPUT)
        for holder in (item, self._engine_manifest_for(engine_id)):
            if holder is None:
                continue
            token = (
                holder.get("reference_token")
                if isinstance(holder, Mapping)
                else getattr(holder, "reference_token", None)
            )
            if isinstance(token, str) and token.strip():
                return token
        return None

    def substitute_ref_tokens(self, prompt: str, engine_id: str, references: Sequence[Any]) -> str:
        """Replace each `@refK` (K = 1..N, N = len(references)) with the engine's name for the K-th reference
        image. Everything is checked before anything is replaced: 422 `bad_ref_token` for a token on an engine
        that declares no `reference_images` input, for K past N and for an `@ref` word that is not `@ref` plus
        a positive number. A prompt with no `@ref` token comes back untouched."""
        found = list(REF_TOKEN.finditer(prompt))
        if not found:
            return prompt
        n = len(references)
        named = ", ".join(dict.fromkeys(m.group(0) for m in found))
        if self._declared(engine_id, "inputs", REFERENCE_INPUT) is None:
            raise PipelineError(
                f"the prompt uses {named}, but {engine_id} takes no reference images; remove the token or "
                f"pick an engine whose manifest declares {REFERENCE_INPUT}",
                code="bad_ref_token",
                tokens=[m.group(0) for m in found],
                references=n,
            )
        template = self.reference_token(engine_id) or DEFAULT_REFERENCE_TOKEN
        if "{k}" not in template:
            template = f"{template} {{k}}"
        for m in found:
            tok = m.group(0)
            digits = tok[len("@ref") :]
            if not digits.isascii() or not digits.isdigit() or digits.startswith("0"):
                raise PipelineError(
                    f"{tok} is not a reference token; name a reference with @ref1..@ref{max(n, 1)}",
                    code="bad_ref_token",
                    token=tok,
                    references=n,
                )
            if int(digits) > n:
                have = f"{n} Reference image{'s are' if n != 1 else ' is'} wired" if n else "none is wired"
                raise PipelineError(
                    f"the prompt names {tok}, but {have} to this Edit; wire the Reference node or fix the "
                    "number",
                    code="bad_ref_token",
                    token=tok,
                    references=n,
                )
        return REF_TOKEN.sub(lambda m: template.replace("{k}", str(int(m.group(0)[len("@ref") :]))), prompt)

    def _compose_prompt(
        self,
        project: Project,
        pv: PacketView,
        body: Mapping[str, Any],
        params_node: Mapping[str, Any],
        recipe: Any,
    ) -> tuple[str | None, str | None, str | None]:
        """(text, prompt_source, negative_prompt) before any `@ref` token is touched."""
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
        tokens = self.recipe_tokens(body, params_node)
        text = recipes.assemble_runnable(recipe, sheet, overrides=tokens or None)
        negative = recipe.negative_prompt or recipes.DEFAULT_NEGATIVE_PROMPT
        try:
            client = self.llm_client(project.project_id, self.allowed_grades(project.project_id, pv.manifest))
            if client.available:
                from .llm import composer

                facts = composer.facts_from_packet(pv.manifest, pv.legend)
                out = composer.compose(
                    recipe, sheet, facts, intent=recipe.title, client=client, overrides=tokens or None
                )
                if out.llm_used:
                    return out.prompt, "composed", out.negative_prompt
                if out.llm_error:
                    log.info("composer fell back to the template for %s: %s", recipe.id, out.llm_error)
        except Exception as e:  # a composer that cannot answer (unrecorded prompt) never blocks a quote
            log.info("composer not used for %s: %s: %s", recipe.id, type(e).__name__, e)
        return text, "recipe", negative

    @staticmethod
    def recipe_tokens(body: Mapping[str, Any], params_node: Mapping[str, Any]) -> dict[str, str]:
        """`params.tokens` (`{"[TOKEN]": text}`): the quote body's params, else the node's; {} when neither
        names any. Keys are the literal bracketed token texts the recipe uses (422 `bad_tokens` otherwise)."""
        body_params = body.get("params") if isinstance(body.get("params"), Mapping) else {}
        raw = (
            body_params.get("tokens") if body_params.get("tokens") is not None else params_node.get("tokens")
        )
        if raw is None:
            return {}
        if not isinstance(raw, Mapping) or not all(
            isinstance(k, str) and k.startswith("[") and k.endswith("]") and isinstance(v, str)
            for k, v in raw.items()
        ):
            raise PipelineError(
                'params.tokens must map literal "[TOKEN]" texts to text, e.g. {"[SEASON]": "winter"}',
                code="bad_tokens",
            )
        return {str(k): str(v) for k, v in raw.items()}

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
        has ids only. `params.textLock` does not enter here: the packet's `policy.text_lock` always wins. Every
        id must be a legend id of the packet (decision 80, `check_mask_id`: 422 `bad_mask` naming it), and an
        `outputHash` a CAS key."""
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
            if raw_ids is not None and not isinstance(raw_ids, list):
                raise PipelineError(
                    f"Mask node {mask_node['id']}: ids must be a list of legend ids", code="bad_mask"
                )
            ids = [self.check_mask_id(mask_node["id"], i, pv) for i in raw_ids or ()]
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
                if not _HEX64.match(mask_node["outputHash"]):
                    raise PipelineError(
                        f"Mask node {mask_node['id']}: outputHash must be a CAS key (64 hex)", code="bad_mask"
                    )
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

    def check_mask_id(self, node_id: str, cid: Any, pv: PacketView) -> str:
        """A Mask node's id as the packet knows it: `o:`/`m:` + 8 hex or `s:` + 5 digits (the schema's legend id),
        listed in the packet's legend (an `s:` label may also be one its segment map carries). 422 `bad_mask`
        naming the id otherwise, before a run row (decision 80)."""
        if not isinstance(cid, str) or not MASK_ID.match(cid):
            shown = cid if isinstance(cid, str) else repr(cid)
            raise PipelineError(
                f"Mask node {node_id}: {shown!r} is not a legend id (o: or m: and 8 hex digits, s: and 5 digits)",
                code="bad_mask",
                id=shown,
            )
        if cid in self.legend_ids(pv):
            return cid
        if cid.startswith("s:"):
            seg = pv.segments()
            if seg is not None and bool((np.asarray(seg) == int(cid[2:])).any()):
                return cid
        raise PipelineError(
            f"Mask node {node_id}: {cid} is not an id of packet {pv.packet_id}", code="bad_mask", id=cid
        )

    @staticmethod
    def legend_ids(pv: PacketView) -> frozenset[str]:
        """Every id the packet's legend lists (cached on the packet view)."""
        hit = pv._cache.get("legend_ids")
        if hit is None:
            entries = (pv.legend or {}).get("entries") if isinstance(pv.legend, Mapping) else None
            hit = frozenset(
                str(e["id"]) for e in entries or () if isinstance(e, Mapping) and isinstance(e.get("id"), str)
            )
            pv._cache["legend_ids"] = hit
        return hit

    def check_mask_keys(self, project_id: str, spec: Mapping[str, Any], pv: PacketView) -> None:
        """A union's CAS masks (and a Mask node's `outputHash`) exist and decode at the plate's size (when the
        manifest states it)."""
        keys = list(spec.get("keys") or ())
        if spec.get("kind") == "cas" and isinstance(spec.get("key"), str):
            keys.append(spec["key"])
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

    def reference_inputs(
        self, project_id: str, graph: Any, node_id: str, engine_id: str
    ) -> list[dict[str, Any]]:
        """Decision 46: the Reference nodes wired to the node's `refs` input (an edge whose `targetHandle` is
        `refs`, or whose source is a Reference), in edge order, as `[{hash, role, node}]`. Refused before any
        run row, in this order: 422 `references_unsupported` (the engine declares no `reference_images`
        input), `too_many_references` (over its `max_items`), `reference_missing` (no image, or not in this
        project's CAS); `bad_reference` for a non-Reference on `refs` or an unknown role."""
        g = canvas_rules.as_graph(graph)
        by_id = {n["id"]: n for n in g.nodes}
        refs: list[dict[str, Any]] = []
        for e in g.edges:
            src = by_id.get(e["source"]) if e["target"] == node_id else None
            if src is None or (e.get("targetHandle") != REFERENCE_HANDLE and src.get("kind") != "Reference"):
                continue
            if src.get("kind") != "Reference":
                raise PipelineError(
                    f"{src['id']} is a {src.get('kind')} node; the refs input takes Reference nodes only",
                    code="bad_reference",
                    node=src["id"],
                )
            params = src.get("params") if isinstance(src.get("params"), Mapping) else {}
            role = params.get("role") or DEFAULT_REFERENCE_ROLE
            if role not in REFERENCE_ROLES:
                raise PipelineError(
                    f"Reference node {src['id']}: role {role!r} is not one of {list(REFERENCE_ROLES)}",
                    code="bad_reference",
                    node=src["id"],
                )
            refs.append({"hash": params.get("referenceHash"), "role": role, "node": src["id"]})
        if not refs:
            return []
        item = self._declared(engine_id, "inputs", REFERENCE_INPUT)
        if item is None:
            raise PipelineError(
                f"{engine_id} takes no reference images ({len(refs)} wired to {node_id}); unwire them or pick "
                f"an engine whose manifest declares {REFERENCE_INPUT}",
                code="references_unsupported",
                references=len(refs),
            )
        cap = item.get("max_items") if isinstance(item, Mapping) else getattr(item, "max_items", None)
        if cap is not None and len(refs) > int(cap):
            raise PipelineError(
                f"{len(refs)} reference images wired to {node_id}; {engine_id} takes at most {int(cap)}",
                code="too_many_references",
                references=len(refs),
                max_items=int(cap),
            )
        cas = self.cas_for(project_id)
        for r in refs:
            key = r["hash"]
            if not isinstance(key, str) or not _HEX64.match(key):
                raise PipelineError(
                    f"Reference node {r['node']} has no image; upload one (POST /assets?kind=reference)",
                    code="reference_missing",
                    node=r["node"],
                )
            if not cas.exists(key):
                raise PipelineError(
                    f"reference {key[:12]} of node {r['node']} is not in this project's CAS; upload it again",
                    code="reference_missing",
                    node=r["node"],
                )
        return refs

    def plate_size(self, pv: PacketView) -> tuple[int, int]:
        """(width, height) of the packet's display master: the manifest's file entry, else the decoded plate."""
        entry = pv.files.get(pv.plate_role) or {}
        w, h = entry.get("width"), entry.get("height")
        if isinstance(w, int) and isinstance(h, int):
            return w, h
        ph, pw = pv.plate16().shape[:2]
        return int(pw), int(ph)

    def base_input(
        self, conn: Any, project_id: str, graph: Any, node_id: str, pv: PacketView
    ) -> dict[str, Any] | None:
        """Decision 47: an Edit whose `image` input is fed by a take (any node but a Plate) starts from it:
        `{base, base_node, base_run, base_state, base_override_reason, base_depth}`. None when a Plate (or
        nothing) feeds `image`: nothing changes. 422 `no_base` when the take has no output in the CAS (an Edit
        never silently falls back to the plate), `bad_base` when the source is not a still, `ambiguous_base`
        for two takes, `base_size` for a take that is not the plate's size.

        Decision 72, after those checks: the hash must be a take of that node in this project
        (`base_take_for`: DONE, or GATE_FAIL with a recorded override), else 422 `base_not_a_take` or
        `base_gate_fail`; and the take's lineage plus this generation must stay within
        `canvas_rules.MAX_GENERATION_DEPTH`, else 422 `DEPTH_EXCEEDED` with the chain of run ids. A hash that is
        another node's take (relayed through this one's `outputHash`) is measured on its own lineage first, so a
        relayed take too deep to build on says so."""
        g = canvas_rules.as_graph(graph)
        by_id = {n["id"]: n for n in g.nodes}
        target = by_id.get(node_id) or {}
        takes: list[dict[str, Any]] = []
        for e in g.edges:
            src = by_id.get(e["source"]) if e["target"] == node_id else None
            if src is None:
                continue
            etype, port = _edge_landing(src.get("kind"), target.get("kind"), e)
            if port != BASE_HANDLE or src.get("kind") == "Plate":
                continue
            if etype != "image16":
                raise PipelineError(
                    f"{src['id']} ({src.get('kind')}) sends {etype or 'nothing'}; the image input takes a still",
                    code="bad_base",
                    node=src["id"],
                )
            if all(t["id"] != src["id"] for t in takes):
                takes.append(src)
        if not takes:
            return None
        if len(takes) > 1:
            raise PipelineError(
                f"{node_id}: {', '.join(t['id'] for t in takes)} all feed its image input; wire one take",
                code="ambiguous_base",
            )
        src = takes[0]
        key = src.get("outputHash")
        cas = self.cas_for(project_id)
        if not isinstance(key, str) or not _HEX64.match(key) or not cas.exists(key):
            raise PipelineError(
                f"{src['id']} has no output in this project's CAS; make and pick a take first (an Edit never "
                "silently falls back to the plate)",
                code="no_base",
                node=src["id"],
            )
        size = _image_size(cas.read_bytes(key))
        want = self.plate_size(pv)
        if size != want:
            shown = f"{size[0]} x {size[1]}" if size else "not an image"
            raise PipelineError(
                f"the take {key[:12]} of {src['id']} is {shown}; the plate is {want[0]} x {want[1]}",
                code="base_size",
                node=src["id"],
            )
        take = self.base_take_for(conn, src["id"], key)
        if take is None:
            relayed = self.base_take_for(conn, None, key)
            if relayed is not None:  # another node's take: too deep is the stronger refusal
                self.check_depth(conn, node_id, relayed.id)
            raise PipelineError(
                f"{src['id']} outputHash {key[:12]} is not a take of that node in this project; an upload or a "
                "foreign blob is never a base (import it first)",
                code="base_not_a_take",
                node=src["id"],
            )
        from .export import recorded_override

        override = recorded_override(conn, take.id) if take.state is S.GATE_FAIL else None
        if take.state is S.GATE_FAIL and not override:
            raise PipelineError(
                f"{src['id']} take {key[:12]} (run {take.id}) failed its gates; override it with a reason before "
                "building on it",
                code="base_gate_fail",
                node=src["id"],
                run_id=take.id,
            )
        depth = self.check_depth(conn, node_id, take.id)
        return {
            "base": key,
            "base_node": src["id"],
            "base_run": take.id,
            "base_state": take.state.value,
            "base_override_reason": override,
            "base_depth": depth,
        }

    def check_depth(self, conn: Any, node_id: str, base_run: str) -> int:
        """The base run's lineage depth; 422 `DEPTH_EXCEEDED` (the chain of run ids in the message) when one
        more generation on top of it passes `canvas_rules.MAX_GENERATION_DEPTH` (doctrine 7)."""
        chain, capped = self._lineage(conn, base_run)
        depth = LINEAGE_MAX_HOPS if capped else len(chain)
        if depth + 1 > canvas_rules.MAX_GENERATION_DEPTH:
            raise PipelineError(
                f"{node_id} would be generation {depth + 1} from the plate (base lineage "
                f"{' <- '.join(chain)}); at most {canvas_rules.MAX_GENERATION_DEPTH} generations",
                code="DEPTH_EXCEEDED",
                chain=chain,
            )
        return depth

    @staticmethod
    def base_take_for(conn: Any, node_id: str | None, key: str) -> Run | None:
        """The newest run of `node_id` (of any node when None) in this project whose `outputs[0]` or
        `result_key` is `key` and whose state is DONE or GATE_FAIL, else None (decision 72)."""
        sql = "SELECT id, normalized, result_key FROM runs WHERE state IN (?, ?)"
        args: list[Any] = list(BASE_STATES)
        if node_id is not None:
            sql += " AND node_id = ?"
            args.append(node_id)
        for row in conn.execute(sql + " ORDER BY updated_utc DESC, id DESC", args):
            try:
                outs = (loads(row[1]) or {}).get("outputs") or []
            except (TypeError, ValueError, AttributeError):
                outs = []
            if (outs and outs[0] == key) or row[2] == key:
                return get_run(conn, str(row[0]))
        return None

    @classmethod
    def base_run_for(cls, conn: Any, node_id: str, key: str) -> str | None:
        """The id of `base_take_for(conn, node_id, key)` (the name before Wave 12), or None."""
        take = cls.base_take_for(conn, node_id, key)
        return take.id if take is not None else None

    @staticmethod
    def _lineage(conn: Any, run_id: str | None) -> tuple[list[str], bool]:
        """(`run_id` and the `inputs.base_run` of each run before it, newest first; capped): the walk stops at a
        cycle or after `LINEAGE_MAX_HOPS` runs (capped True). A run not in the index ends the chain, counted
        once (it was a generation; nothing more is known of it)."""
        chain: list[str] = []
        rid = run_id
        while isinstance(rid, str) and rid:
            if rid in chain or len(chain) >= LINEAGE_MAX_HOPS:
                return chain, True
            chain.append(rid)
            row = conn.execute("SELECT inputs FROM runs WHERE id = ?", (rid,)).fetchone()
            try:
                inputs = loads(row[0]) if row is not None else None
            except (TypeError, ValueError):
                inputs = None
            rid = inputs.get("base_run") if isinstance(inputs, Mapping) else None
        return chain, False

    def lineage_chain(self, conn: Any, run_id: str | None) -> list[str]:
        """The run ids of `run_id`'s lineage, newest first (at most `LINEAGE_MAX_HOPS`)."""
        return self._lineage(conn, run_id)[0]

    def lineage_depth(self, conn: Any, run_id: str | None) -> int:
        """1 + the lineage depth of the run's `inputs.base_run`, 0 for none (an import or a take on the plate is
        1); a cycle or a chain of more than `LINEAGE_MAX_HOPS` runs counts as `LINEAGE_MAX_HOPS`."""
        chain, capped = self._lineage(conn, run_id)
        return LINEAGE_MAX_HOPS if capped else len(chain)

    def next_attempt(
        self,
        conn: Any,
        node_id: str,
        inputs: Mapping[str, Any],
        params: Mapping[str, Any],
        route: Mapping[str, Any],
    ) -> int:
        """1 + the highest attempt of this node's earlier runs of the same request (decision 40), else 1. 422
        `attempt_exhausted` once that would pass `ATTEMPT_MAX` (decision 80). `quote_node` calls it under the
        node's lock, so two concurrent quotes never take the same number."""
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
        if best >= ATTEMPT_MAX:
            raise PipelineError(
                f"{node_id} has made this request {best} times; the limit is {ATTEMPT_MAX} attempts (change the "
                "request: prompt, params or mask)",
                code="attempt_exhausted",
                attempts=best,
            )
        return best + 1

    def node_lock(self, project_id: str, node_id: str) -> threading.Lock:
        """The lock attempt numbering and run creation of one node hold (decision 80)."""
        with self._lock:
            lock = self._node_locks.get((project_id, node_id))
            if lock is None:
                lock = self._node_locks[(project_id, node_id)] = threading.Lock()
            return lock

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
        the engine is allowed; after the mask, the base (`no_base`, `bad_base`, `ambiguous_base`, `base_size`)
        and the references (`references_unsupported`, `too_many_references`, `reference_missing`), then the
        prompt's `@refK` tokens (`bad_ref_token`), then (decision 80) `bad_priority`, non-finite params
        (`bad_params`), the editable mask itself (`mask_empty`, `mask_size`, `bad_mask`), the manifest adapter's
        `validate_params` (`bad_params`), `bad_attempt` and `attempt_exhausted`. Body (all
        optional): engine_id, route_id, packet_id, task, shot_class, recipe, prompt, params (`params.tokens`
        fills a recipe's tokens and never reaches the engine), preset, region_crop, feather_px, priority,
        attempt (server-side when absent, numbered under the node's lock). A failure once the run row exists
        cancels it (`quote failed: <error>`) before the error goes on. A project another daemon leases is
        `lease.ProjectLeased` (409) before anything else is read (decision 83)."""
        now = now or self.clock()
        project = self.load_project(project_id)
        if project is None:
            raise PipelineError(f"no project {project_id}", code="not_found", status=404)
        self.refuse_if_leased(project_id)
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
        recipe = self.load_recipe(recipe_ref, project_id) if recipe_ref else None
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
        router = self.render_it(pv, task, conn)
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
        base = self.base_input(conn, project_id, g, node_id, pv)
        references = self.reference_inputs(project_id, g, node_id, engine_id)
        inputs: dict[str, Any] = {"plate": pv.plate_hash, "mask": mask, STAGING_KEY: staging}
        if base is not None:
            inputs.update(base)
        if references:
            inputs[REFERENCE_INPUT] = references
        params = dict(body.get("params") or {})
        params.pop("tokens", None)  # recipe text, not an engine parameter
        prompt, prompt_source, negative, prompt_raw = self.resolve_prompt(
            project, pv, body, params_node, recipe, engine_id=engine_id, references=references
        )
        if prompt is None and self.prompt_required(engine_id):
            raise PipelineError(
                f"{engine_id} needs a prompt: give prompt, or a recipe to build one from",
                code="prompt_required",
            )
        if prompt is not None:
            inputs["prompt"] = prompt
            inputs["prompt_source"] = prompt_source
            if prompt_raw != prompt:  # @refK became the engine's wording: the artist's text stays beside it
                inputs["prompt_raw"] = prompt_raw
        if recipe is not None:
            inputs["recipe_id"] = recipe.id
            tokens = self.recipe_tokens(body, params_node)
            if tokens and prompt_source in ("recipe", "composed"):
                inputs["recipe_tokens"] = tokens
        if (
            negative
            and "negative_prompt" not in params
            and self._declared(engine_id, "params", "negative_prompt") is not None
        ):
            params["negative_prompt"] = negative
        inputs[ROUTER_KEY] = router
        inputs[SUBGRAPH_KEY] = subgraph_snapshot(project.graph, upstream)
        # Decision 80: everything the run will need, before a run row exists.
        try:
            priority = Priority.parse(body["priority"] if body.get("priority") is not None else "interactive")
        except ValueError as e:
            raise PipelineError(str(e), code="bad_priority") from e
        bad = _non_finite(params)
        if bad is not None:
            raise PipelineError(
                f"{bad[0]} is {bad[1]!r}; every number a run is asked for must be finite",
                code="bad_params",
                param=bad[0],
            )
        self.probe_mask(node_id, mask, pv, self.cas_for(project_id))
        self.check_engine_params(route, params)
        attempt = body.get("attempt")
        if attempt is not None and (
            isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= ATTEMPT_MAX
        ):
            raise PipelineError(
                f"attempt {attempt!r}: a whole number from 1 to {ATTEMPT_MAX}, or none (the daemon numbers it)",
                code="bad_attempt",
            )
        run: Run | None = None
        try:
            with self.node_lock(project_id, node_id):
                if attempt is None:
                    attempt = self.next_attempt(conn, node_id, inputs, params, route)
                spec = RunCreate(
                    project_id=project_id,
                    packet_id=packet_id,
                    node_id=node_id,
                    priority=priority,
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
                    transition(
                        conn, run.id, S.CANCELLED, actor="system", reason=f"could not be priced: {e}"[:500]
                    )
                    raise PipelineError(f"could not price {engine_id}: {e}", code="unpriceable") from e
            self.after(conn, project_id, run.id, S.DRAFT)
        except Exception as e:
            if run is not None:
                self._cancel_failed_quote(conn, project_id, run.id, e)
            raise
        return run, q

    def _cancel_failed_quote(self, conn: Any, project_id: str, run_id: str, err: BaseException) -> None:
        """A quote that failed after its run row was made leaves no DRAFT or QUOTED row: CANCELLED (`quote
        failed: <error type>`), published, and ledgered when a `quoted` record may exist. Errors here are
        logged; the caller re-raises the original."""
        try:
            before = get_run(conn, run_id).state
            if before in TERMINAL:
                return
            transition(
                conn, run_id, S.CANCELLED, actor="system", reason=f"quote failed: {type(err).__name__}"[:500]
            )
            if before is S.QUOTED:
                self.after(conn, project_id, run_id, before)
            else:
                self.publish(
                    project_id,
                    run_id,
                    {"kind": "run.state", "run_id": run_id, "from": before.value, "to": S.CANCELLED.value},
                )
        except Exception as e:  # the original error is what the caller sees
            log.warning("run %s could not be cancelled after its quote failed: %s", run_id, e)

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
        record cancels the group (503 `ledger_unavailable`). The daily and unattended caps count the spend of
        every project index in this home (`budgets.spend_snapshot_all`, decision 80); a quote that lapsed or
        was made after `now` is EXPIRED, ledgered `expired`, and `QuoteExpired` goes on. A project another
        daemon leases is `lease.ProjectLeased` (409) with nothing moved (decision 83)."""
        now = now or self.clock()
        self.refuse_if_leased(project_id)
        run = get_run(conn, run_id)
        self.require_custody(run)
        if by == "human":
            self.touch_human(now)

        def alert(decision: budgets.Decision) -> None:
            try:
                budgets.publish_alert(self.bus, project_id, run_id, decision)
            except Exception as e:  # the bus is for watchers
                log.warning("budget alert for %s not published: %s", run_id, e)

        kwargs: dict[str, Any] = {
            "policy": self.policy,
            "last_human_interaction_utc": self.last_human_utc,
            "on_alert": alert,
        }
        with self._approval_lock:  # decision 80: the caps count every project, one decision at a time
            if by != "human":  # a human approves without asking the caps
                kwargs["spent"] = budgets.spend_snapshot_all(self.index, now, self.last_human_utc)
            try:
                if run.sibling_group:
                    runs = approve_group(conn, run.sibling_group, by, envelope_id, now, **kwargs)
                else:
                    runs = [approve_one(conn, run_id, by, envelope_id, now, **kwargs)]
            except QuoteExpired as e:
                for rid in getattr(e, "run_ids", None) or ():
                    try:  # QUOTED -> EXPIRED is in the folder too
                        self.after(conn, project_id, rid, S.QUOTED)
                    except Exception as e2:
                        log.warning("expired run %s not published or ledgered: %s", rid, e2)
                raise
        try:
            for r in runs:
                self.after(conn, project_id, r.id, S.QUOTED, note=reason)
        except PipelineError as e:
            if e.code == "ledger_unavailable":  # one unrecorded approval stops the group: none of it is spent
                for r in runs:
                    self._cancel_unledgered(conn, project_id, r.id, "approved", e)
            raise
        except lease.ProjectLeased as e:  # leased since the check: the group stops here, in this index only
            for r in runs:
                self._cancel_unledgered(conn, project_id, r.id, "approved", e, code=LEASED, record=False)
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

    def probe_mask(self, node_id: str, spec: Mapping[str, Any], pv: PacketView, cas: CAS) -> None:
        """The quote's mask probe (decision 80): `editable_mask_from_spec` at the manifest's plate size (the
        plate is not decoded here). Its mask refusals (`MASK_REFUSALS`) surface as they are; a packet layer that
        cannot be read is left for staging to report (it fails the run there, with the same message)."""
        w, h = self.plate_size(pv)
        try:
            self.editable_mask_from_spec(spec, pv, cas, shape=(h, w))
        except PipelineError as e:
            if e.code in MASK_REFUSALS:
                raise
            log.warning("mask of %s not probed at the quote: %s", node_id, e)
        except (OSError, ValueError) as e:  # an undecodable layer is a staging failure, not a mask refusal
            log.warning("mask of %s not probed at the quote: %s: %s", node_id, type(e).__name__, e)

    def editable_mask(self, run: Run, pv: PacketView, cas: CAS) -> np.ndarray:
        """What the run may change: its mask spec (`editable_mask_from_spec`; the packet's editable ids when the
        run names none)."""
        return self.editable_mask_from_spec(
            run.inputs.get("mask") or {"kind": "ids", "ids": pv.editable_ids()}, pv, cas
        )

    def editable_mask_from_spec(
        self,
        spec: Mapping[str, Any],
        pv: PacketView,
        cas: CAS,
        shape: tuple[int, int] | None = None,
    ) -> np.ndarray:
        """What a mask spec may change: always within the packet's editable ids and never on a locked id
        (decision 34; every path, so nothing composites onto a locked id). `shape` (h, w) defaults to the
        decoded plate's; the quote passes the manifest's (`probe_mask`), so `mask_empty`, `mask_size` and
        `bad_mask` are refused before a run row (decision 80)."""
        h, w = shape if shape is not None else pv.plate16().shape[:2]
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
            raise PipelineError(f"unknown mask kind {kind!r}", code="bad_mask")
        mask = mask & pv.editable_px((h, w)) & ~pv.locked_px((h, w))
        if not mask.any():
            raise PipelineError(
                "the editable mask is empty (inside the packet's editable ids, outside its locked ids)",
                code="mask_empty",
            )
        return mask

    def engine_params(self, route: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
        """What staging sends the engine for a run's `params` on `route` (the vendor payload's `params`): the
        params less what only PLATE reads (`PLATE_PARAMS`). Every route maps them the same way today; the quote
        validates exactly this (decision 80)."""
        return {k: v for k, v in params.items() if k not in PLATE_PARAMS}

    @staticmethod
    def make_adapter(
        cls: type, manifest: Any, route: Any, *, transport: Any, staging_dir: Path | None = None
    ) -> Any:
        """A `plate_engines` adapter as staging builds it: the manifest, the route, the transport, and the
        adapter's `staging_dir` under PLATE_HOME when it takes one (results never land in the OS temp)."""
        kwargs: dict[str, Any] = {}
        if staging_dir is not None and "staging_dir" in inspect.signature(cls).parameters:
            kwargs["staging_dir"] = staging_dir
        return cls(manifest, route, transport=transport, **kwargs)

    def check_engine_params(self, route: Mapping[str, Any], params: Mapping[str, Any]) -> None:
        """Decision 80: a manifest engine's adapter, built as staging builds it (on a transport that never
        sends), validates `engine_params(route, params)` at the quote; `ParamError` / `InputError` are 422
        `bad_params` with the adapter's message. In-process engines (`fake`, `engines`) have no adapter."""
        eid = route.get("engine_id")
        if eid == FAKE_ENGINE_ID or eid in self.engines:
            return
        manifest = self.manifests.get(eid)
        if manifest is None:
            raise PipelineError(f"unknown engine {eid!r}", code="unknown_engine")
        rid = route.get("route_id") or route.get("provider")
        r = next((x for x in manifest.routes if x.route_id == rid), None)
        cls = adapter_class(manifest, r) if r is not None else None
        if cls is None:
            raise PipelineError(f"no adapter speaks {eid} via {rid}", code="no_adapter")
        from plate_engines.adapters.base import InputError, ParamError

        transport = None if r.upload_visibility == "local" else _QuoteTransport()
        try:
            adapter = self.make_adapter(cls, manifest, r, transport=transport)
        except Exception as e:
            raise PipelineError(
                f"{eid} via {rid}: the adapter cannot be built ({type(e).__name__}: {e})", code="no_adapter"
            ) from e
        try:
            adapter.validate_params(self.engine_params(route, params))
        except (ParamError, InputError) as e:
            raise PipelineError(str(e), code="bad_params") from e

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
        """egress.prepare for this run (deterministic, cached; recomputed after a restart). The source is the
        run's base when it has one (decision 47), else the plate; a pinned size map and PNG
        (`inputs.staging.sizemap` + `png_key`, decision 54) are reused as they are, only the mask is
        recomputed."""
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
        pinned = self.pinned_egress(opts, pv, cas)
        if pinned is not None:
            png, sm, png_key = pinned
        else:
            png, sm = pixels_egress.prepare(
                self.base_image(run, pv, cas) if run.inputs.get("base") else pv.plate16(),
                pv.manifest,
                self.engine_manifest(run),
                mask=mask,
                region_crop=bool(opts.get("region_crop")),
            )
            png_key = cas.put_bytes(png, "png")
        staged = Staged(
            png=png,
            sizemap=sm,
            mask=mask,
            png_key=png_key,
            mask_key=cas.put_bytes(_mask_png(mask), "png"),
            opaque_name=_opaque_name(png),
        )
        with self._lock:
            self._staged[key] = staged
            while len(self._staged) > STAGE_CACHE:
                self._staged.popitem(last=False)
        return staged

    def pinned_egress(
        self, opts: Mapping[str, Any], pv: PacketView, cas: CAS
    ) -> tuple[bytes, Any, str] | None:
        """(png, SizeMap, png_key) from `inputs.staging.sizemap` and `png_key` when both are there, else None.
        The PNG must be in the CAS at the size map's sent size, and the size map must be of this plate."""
        sm_doc, png_key = opts.get("sizemap"), opts.get("png_key")
        if not isinstance(sm_doc, Mapping) or not isinstance(png_key, str):
            return None
        from plate_pixels.egress import SizeMap

        if not _HEX64.match(png_key) or not cas.exists(png_key):
            raise PipelineError(
                f"the pinned egress PNG {png_key[:12]} is not in the project's CAS", code="pinned_png_missing"
            )
        try:
            sm = SizeMap.from_dict(sm_doc)
        except (KeyError, TypeError, ValueError) as e:
            raise PipelineError(f"the pinned size map is not a size map: {e}", code="bad_sizemap") from e
        png = cas.read_bytes(png_key)
        if sm.src != self.plate_size(pv) or _image_size(png) != sm.dst:
            raise PipelineError(
                f"the pinned size map maps {sm.src[0]} x {sm.src[1]} to {sm.dst[0]} x {sm.dst[1]}; it does not fit "
                f"this plate and its PNG",
                code="bad_sizemap",
            )
        return png, sm, png_key

    def base_image(self, run: Run, pv: PacketView, cas: CAS) -> np.ndarray:
        """The run's base take as uint16 RGB at the plate's size (decision 47)."""
        key = run.inputs.get("base")
        if not isinstance(key, str) or not cas.exists(key):
            raise PipelineError(
                f"the base {str(key)[:12]} is not in this project's CAS (an Edit never silently falls back to the "
                "plate)",
                code="no_base",
            )
        img = decode_png16(cas.read_bytes(key))
        plate = pv.plate16()
        if img.shape != plate.shape:
            raise PipelineError(
                f"the base {key[:12]} is {img.shape[1]} x {img.shape[0]}; the plate is {plate.shape[1]} x "
                f"{plate.shape[0]}",
                code="base_size",
            )
        return img

    def sends_mask(self, run: Run) -> bool:
        """Does the engine get the staged mask? Its manifest declares a `mask` input and whatever speaks it
        takes one: a `plate_engines` adapter without `NATIVE_MASK` refuses a mask outright (PLATE composites
        inside the mask instead), so the mask goes only where it is understood."""
        eid = run.route.engine_id
        if self._declared(eid, "inputs", MASK_INPUT) is None:
            return False
        if eid == FAKE_ENGINE_ID or eid in self.engines:
            return bool(getattr(self.engines.get(eid, self.engine), "NATIVE_MASK", True))
        manifest = self.manifests.get(eid)
        rid = run.route_dict.get("route_id") or run.route.provider
        route = (
            next((r for r in manifest.routes if r.route_id == rid), None) if manifest is not None else None
        )
        cls = adapter_class(manifest, route) if route is not None else None
        return cls is not None and bool(getattr(cls, "NATIVE_MASK", True))

    @staticmethod
    def engine_mask(staged: Staged) -> bytes:
        """The staged mask in the egress geometry, so it registers with the sent PNG: the size map's padded
        content box (the mask's own pixels inside the frame, 0 = keep beyond it), resampled to the sent size
        with INTER_NEAREST; a 0/255 grey PNG."""
        sm = staged.sizemap
        m = np.where(staged.mask, 255, 0).astype(np.uint8)
        h, w = m.shape
        x0, y0, x1, y1 = sm.padded_rect
        out = np.zeros((y1 - y0, x1 - x0), np.uint8)
        ix0, iy0, ix1, iy1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
        if ix1 > ix0 and iy1 > iy0:
            out[iy0 - y0 : iy1 - y0, ix0 - x0 : ix1 - x0] = m[iy0:iy1, ix0:ix1]
        if (out.shape[1], out.shape[0]) != tuple(sm.dst):
            out = cv2.resize(out, tuple(sm.dst), interpolation=cv2.INTER_NEAREST)
        ok, buf = cv2.imencode(".png", out)
        if not ok:
            raise ValueError("cv2 could not encode the engine mask")
        return bytes(buf)

    def largest_side(self, run: Run) -> int | None:
        """The longest side the engine takes (its largest legal preset, else its arbitrary envelope), or None."""
        from plate_pixels.sizes import legal_sizes

        legal = legal_sizes(self.engine_manifest(run))
        if legal:
            return max(max(s.width, s.height) for s in legal)
        return legal.arbitrary.max_side if legal.arbitrary is not None else None

    @staticmethod
    def reference_png(data: bytes, max_side: int | None) -> bytes:
        """A reference as the vendor gets it: decoded as 8-bit colour and re-encoded as a PNG (pixels only, so
        nothing of any metadata survives), its longest side at most `max_side` (INTER_AREA, never enlarged)."""
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            raise PipelineError("a reference image in the CAS is not decodable", code="bad_reference")
        h, w = img.shape[:2]
        if max_side is not None and max(w, h) > max_side:
            k = max_side / max(w, h)
            size = (max_side, max(1, round(h * k))) if w >= h else (max(1, round(w * k)), max_side)
            img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", np.ascontiguousarray(img))
        if not ok:
            raise ValueError("cv2 could not encode the reference")
        return bytes(buf)

    def vendor_payload(self, run: Run, staged: Staged, seed_payload: Mapping[str, Any]) -> dict[str, Any]:
        """What the engine receives: the egress PNG under an opaque name, the prompt, the params; the staged
        mask in the same geometry when the engine takes one; the reference images, each under its own opaque
        name (decision 46)."""
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
        if self.sends_mask(run):
            mask_png = self.engine_mask(staged)
            inputs[MASK_INPUT] = {
                "b64": base64.b64encode(mask_png).decode("ascii"),
                "mime": "image/png",
                "name": _opaque_name(mask_png),
                "role": "mask",
            }
        refs = run.inputs.get(REFERENCE_INPUT)
        if refs:
            cas = self.cas_for(run.project_id)
            side = self.largest_side(run)
            sent = []
            for r in refs:
                data = self.reference_png(cas.read_bytes(r["hash"]), side)
                sent.append(
                    {
                        "b64": base64.b64encode(data).decode("ascii"),
                        "mime": "image/png",
                        "name": _opaque_name(data),
                        "role": r.get("role") or DEFAULT_REFERENCE_ROLE,
                    }
                )
            inputs[REFERENCE_INPUT] = sent
        payload: dict[str, Any] = {
            "engine_id": run.route.engine_id,
            "inputs": inputs,
            "params": self.engine_params(run.route_dict, run.params),
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
        bridge = AdapterBridge(
            self.make_adapter(
                cls,
                manifest,
                route,
                transport=transport,
                staging_dir=self.settings.home / "cache" / "adapters" / run.id,
            )
        )
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

    def apply_ingress_hooks(
        self, run: Run, data: bytes, staged: Staged, pv: PacketView
    ) -> tuple[bytes, dict[str, Any] | None]:
        """Decision 55: `ingress_hooks` in order on the engine's raw bytes. Returns the bytes and
        `{name: record}` (None when there are no hooks). A raising hook (or one that returns no bytes) stops the
        run: `PipelineError` with code `ingress_hook:<name>`, which `step` records as the run's reason code."""
        if not self.ingress_hooks:
            return data, None
        records: dict[str, Any] = {}
        for name, hook in list(self.ingress_hooks):
            code = f"{INGRESS_HOOK_CODE}:{name}"
            try:
                out, record = hook(run, data, staged, pv)
                if not isinstance(out, (bytes, bytearray)) or not out:
                    raise TypeError(f"returned {type(out).__name__}, not image bytes")
            except Exception as e:
                with self._lock:
                    self._hook_failed[(run.id, run.attempt)] = code
                raise PipelineError(
                    f"ingress hook {name} failed: {type(e).__name__}: {e}"[:500], code=code
                ) from e
            data = bytes(out)
            records[name] = _plain(record)
        return data, records

    def prior_mask(self, run: Run, pv: PacketView, cas: CAS) -> np.ndarray:
        """What the base already changed (decisions 47 and 72): its run's `mask_union_key`, else its `mask_key`.
        There is no pixel-difference term (it made "outside the union is the plate" true by definition): a base
        run whose masks cannot be read fails the run (`base_mask_missing`, NORMALIZING -> FAILED)."""
        h, w = pv.plate16().shape[:2]
        rid = run.inputs.get("base_run")
        norm: Mapping[str, Any] = {}
        if isinstance(rid, str):
            try:
                with self.index.project(run.project_id) as conn:
                    norm = get_run(conn, rid).normalized or {}
            except Exception as e:
                log.warning("base run %s of %s not read: %s", rid, run.id, e)
        for name in ("mask_union_key", "mask_key"):
            key = norm.get(name)
            if not isinstance(key, str) or not _HEX64.match(key) or not cas.exists(key):
                continue
            try:
                return self._grey_mask(cas, key, w, h)
            except PipelineError as e:
                log.warning("%s of base run %s not usable for %s: %s", name, rid, run.id, e)
        with self._lock:
            self._hook_failed[(run.id, run.attempt)] = "base_mask_missing"  # step() records it as the reason
        raise PipelineError(
            f"the base run {rid or '(none)'} has no readable mask (mask_union_key / mask_key): what the base "
            "changed is unknown, so nothing can be composited on it",
            code="base_mask_missing",
        )

    def normalize(self, run: Run, cas: CAS) -> dict[str, Any]:
        """NORMALIZING: the engine's bytes (after the ingress hooks) back onto the plate, or onto the run's base
        (decision 47), composited inside the mask, text locked from the plate."""
        from plate_gates import composite, textlock
        from plate_pixels import ingress

        pv = self.packet(run.project_id, run.packet_id)
        staged = self.stage(run, cas)
        plate16 = pv.plate16()
        base_key = run.inputs.get("base")
        under = self.base_image(run, pv, cas) if base_key else plate16
        raw, hooks = self.apply_ingress_hooks(run, cas.read_bytes(run.result_key), staged, pv)
        restored, rep = ingress.restore(raw, staged.sizemap, under, ring_mask=~staged.mask)
        opts = run.inputs.get(STAGING_KEY) or {}
        feather = float(opts.get("feather_px") or DEFAULT_FEATHER_PX)
        comp = composite.composite_masked(under, restored, staged.mask, feather_px=feather)
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
            *RESULT_INFO_KEYS,
        )
        out: dict[str, Any] = {
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
            "mask_sent": self.sends_mask(run),
        }
        if hooks is not None:
            out["ingress_hooks"] = hooks
        if base_key:
            union = staged.mask | self.prior_mask(run, pv, cas)
            out["base"] = base_key
            out["mask_union_key"] = cas.put_bytes(_mask_png(union), "png")
            out["composite"]["mask_union_share"] = float(union.mean())
        return out

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
        """GATING: score the stored output against the plate, inside the run's mask or, on a base (decision 47),
        inside the union of the chain's masks; the report is versioned in the project's gates/ folder."""
        from . import gating

        norm = run.normalized or {}
        outputs = norm.get("outputs") or []
        if not outputs:
            raise PipelineError("nothing to gate: the run has no output")
        pv = self.packet(run.project_id, run.packet_id)
        staged = self.stage(run, cas)
        out16 = decode_png16(cas.read_bytes(outputs[0]))
        mask = staged.mask
        extra: dict[str, Any] = {"textlock_px": norm.get("textlock_px"), "output": outputs[0]}
        if isinstance(norm.get("mask_union_key"), str):
            h, w = out16.shape[:2]
            mask = self._grey_mask(cas, norm["mask_union_key"], w, h)
        if norm.get("base"):
            extra["base_hash"] = norm["base"]
        base = self.base_record(run)
        report = gating.run_gates(
            pv.plate16(),
            out16,
            {"path": pv.root, "manifest": pv.manifest},
            pv.legend,
            pv.tier,
            {"id": run.route.engine_id, "version": str(run.route_dict.get("model_version") or "") or None},
            mask=mask,
            registration=norm.get("ingress"),
            depth_fn=self.depth_fn,
            edges=pv.edges(),
            crypto_material=pv.crypto("material") if pv.tier == "A" else None,
            extra_metrics=extra,
        )
        # Decision 86: the result's format, beside its registration.
        for note in result_notes(norm.get("ingress")):
            if note not in report.backend_notes:
                report.backend_notes.append(note)
        stored = report.model_dump(mode="json")
        if base is not None:  # decision 72: the sidecar says what the take was built on
            stored["base"] = base
        project_dir = self.project_dir(run.project_id)
        svc = self.report_service(run.project_id)
        if svc is not None:
            path = svc.write(run.id, stored)
        else:
            from plate_gates.report import write

            path = write(report, project_dir, run.id)
        try:
            ref = Path(path).relative_to(project_dir).as_posix()
        except ValueError:
            ref = Path(path).as_posix()
        return GateOutcome(
            passed=report.verdict != "fail",
            report=stored,
            score=gating.score_of(report),
            report_path=ref,
        )

    @staticmethod
    def base_record(run: Run) -> dict[str, Any] | None:
        """`{run_id, state, override_reason, depth}` of the take a run was built on (decision 72), or None."""
        if not run.inputs.get("base"):
            return None
        return {
            "run_id": run.inputs.get("base_run"),
            "state": run.inputs.get("base_state"),
            "override_reason": run.inputs.get("base_override_reason"),
            "depth": run.inputs.get("base_depth"),
        }

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
        with self._lock:
            hook_code = self._hook_failed.pop((run.id, run.attempt), None)
        if hook_code is not None and after is S.FAILED:
            # The worker records any normalize failure as normalize_error; an ingress hook is named instead.
            annotate(conn, run_id, expect=S.FAILED, reason_code=hook_code)
        if after is not before:
            self.after(conn, project_id, run_id, before)
        return after

    def execute(
        self, run_id: str | Run, project_id: str | None = None, *, max_steps: int = 100_000
    ) -> RunState:
        """Drive a QUEUED (or resumed) run to a terminal state, sleeping between idle polls. `run_id` may be
        the Run itself. A busy or locked index is retried with backoff (`BUSY_BACKOFF_S`, `BUSY_RETRIES` in a
        row, then re-raised); once the executor is stopped the run is left where it is for the next boot. A
        `SimulatedCrash` exits the process (`CRASH_EXIT_CODE`) when `crash_exits_process` is set (decision 85),
        else it goes on."""
        if isinstance(run_id, Run):
            project_id, run_id = project_id or run_id.project_id, run_id.id
        pid = project_id or self.index.locate_run(run_id)
        if pid is None:
            raise KeyError(f"run {run_id} is not in any project index")
        cas = self.cas_for(pid)
        key = (pid, run_id)
        with self._lock:  # the executor's watch never takes a run this process is driving
            self._driving[key] = self._driving.get(key, 0) + 1
        try:
            state = self._drive(pid, run_id, cas, max_steps)
        except SimulatedCrash as e:  # decision 85: executor thread, BackgroundTask and boot resume alike
            if self.crash_exits_process:
                sys.stderr.write(f"plate_daemon: {e}; exiting as a crashed process would\n")
                sys.stderr.flush()
                os._exit(CRASH_EXIT_CODE)
            raise
        finally:
            with self._lock:
                left = self._driving.get(key, 1) - 1
                if left > 0:
                    self._driving[key] = left
                else:
                    self._driving.pop(key, None)
        self._rank_group(pid, run_id)
        return state

    def _drive(self, pid: str, run_id: str, cas: CAS, max_steps: int) -> RunState:
        busy = 0
        with self.index.project(pid) as conn:
            for _ in range(max_steps):
                try:
                    before = get_run(conn, run_id).state
                    if before in TERMINAL or before not in WORKER_STATES:
                        return before
                    if self.executor.stop_event.is_set():
                        log.info("run %s left in %s: the run executor is stopping", run_id, before.value)
                        return before
                    state = self.step(conn, pid, run_id, cas)
                except sqlite3.OperationalError as e:
                    if not index_busy(e) or busy >= BUSY_RETRIES:
                        raise
                    delay = BUSY_BACKOFF_S[min(busy, len(BUSY_BACKOFF_S) - 1)]
                    busy += 1
                    log.warning(
                        "run %s: the project index is busy (%s); retry %d of %d in %g s",
                        run_id,
                        e,
                        busy,
                        BUSY_RETRIES,
                        delay,
                    )
                    self.sleep(delay)
                    continue
                busy = 0
                if state in TERMINAL:
                    return state
                if state is before:
                    self.sleep(self.poll_interval_s)
        raise RuntimeError(f"run {run_id} did not finish within {max_steps} steps")

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

    # ------------------------------------------------------------------ the run executor (decision 75)

    def start_runs(self, project_id: str, run_ids: Iterable[str], *, background: Any = None) -> None:
        """Drive these QUEUED runs to their end, off the request: inline (`PLATE_RUN_WORKERS=0`) as a
        BackgroundTask of `background` (in this thread when there is none), else on the run executor."""
        ids = [run_ids] if isinstance(run_ids, str) else list(run_ids)
        if not ids:
            return
        if self.executor.inline:
            if background is not None:
                background.add_task(self.execute_all, project_id, ids)
            else:
                self.execute_all(project_id, ids)
            return
        self._start_executor()
        self.executor.submit(project_id, ids)

    def _start_executor(self) -> bool:
        """Start the pool and its orphan watch on first use; False when inline or closed."""
        if self.executor.inline:
            return False
        with self._lock:
            if self._closed:
                return False
            if self._executor_live:
                return True
            self._executor_live = True
        try:
            self.executor.start()
        except RuntimeError as e:  # closed meanwhile (shutdown): the runs stay QUEUED for the next boot
            log.warning("run executor not started: %s", e)
            return False
        self.executor.watch(self.orphaned_runs)
        return True

    def close(self, timeout_s: float = CLOSE_TIMEOUT_S) -> bool:
        """Stop the run executor (queued runs stay QUEUED for the next boot). True when every worker ended."""
        with self._lock:
            self._closed = True
        return self.executor.stop(timeout_s)

    def _execute_pair(self, project_id: str, run_id: str) -> RunState:
        return self.execute(run_id, project_id)

    def _route_of(self, project_id: str, run_id: str) -> dict[str, Any]:
        key = (project_id, run_id)
        with self._lock:
            hit = self._routes.get(key)
        if hit is not None:
            return hit
        with self.index.project(project_id) as conn:
            route = get_run(conn, run_id).route_dict
        with self._lock:
            self._routes[key] = route
            while len(self._routes) > ROUTE_MEMO:
                self._routes.popitem(last=False)
        return route

    def route_max_inflight(self, route: Mapping[str, Any]) -> int:
        """The route's `max_inflight`: the manifest route's (by route id, then provider), else a manifest-less
        engine's own route dict's, else `executor.DEFAULT_MAX_INFLIGHT` (2)."""
        manifest = self.manifests.get(route.get("engine_id")) if route.get("engine_id") else None
        if manifest is not None:
            routes = list(manifest.routes)
            hit = next((r for r in routes if r.route_id == route.get("route_id")), None) or next(
                (r for r in routes if r.provider == route.get("provider")), None
            )
            if hit is not None:
                return max(1, int(hit.max_inflight))
        declared = route.get("max_inflight")
        if isinstance(declared, int) and not isinstance(declared, bool) and declared >= 1:
            return declared
        return DEFAULT_MAX_INFLIGHT

    def max_inflight_for(self, project_id: str, run_id: str) -> int:
        return self.route_max_inflight(self._route_of(project_id, run_id))

    def route_key_for(self, project_id: str, run_id: str) -> str:
        return route_key(self._route_of(project_id, run_id))

    def orphaned_runs(self) -> list[tuple[str, str]]:
        """The executor's watch: `(project_id, run_id)` of every pipeline run in a worker state that nothing in
        this process is driving and whose last move is older than `ORPHAN_GRACE_S` (its driver died: a step that
        raised past its retries, a lost thread). Each project's ledger outbox is drained on the way. A project
        another daemon leases is skipped (decision 83)."""
        states = tuple(s.value for s in WORKER_STATES)
        marks = ",".join("?" * len(states))
        cutoff = utcnow() - timedelta(seconds=ORPHAN_GRACE_S)
        with self._lock:
            driving = set(self._driving)
        out: list[tuple[str, str]] = []
        for pid in self.index.project_ids():
            try:
                if self.leased_elsewhere(pid) is not None:  # decision 83: that daemon drives them
                    continue
                with self.index.project(pid) as conn:
                    self.drain_ledger_outbox(conn, pid)
                    rows = conn.execute(
                        f"SELECT id, updated_utc FROM runs WHERE state IN ({marks}) ORDER BY created_utc, id",
                        states,
                    ).fetchall()
                    for rid, updated in ((r[0], r[1]) for r in rows):
                        if (pid, rid) in driving:
                            continue
                        try:
                            moved = parse_iso(updated)
                        except ValueError:
                            moved = None
                        if moved is not None and moved > cutoff:
                            continue
                        if is_pipeline_run(get_run(conn, rid)):
                            out.append((pid, rid))
            except Exception as e:  # one unreadable index never stops the watch
                log.warning("orphan scan of project %s failed: %s: %s", pid, type(e).__name__, e)
        return out

    def resume(self) -> list[str]:
        """Drive on every pipeline run a stopped daemon left in a worker state (start-up; the worker's STAGING
        rules still apply, so nothing is resubmitted blind). Each project's ledger outbox is drained first; each
        run gets a `resume` note and goes to the executor (inline: driven here, one after another). A run in
        FETCHING, NORMALIZING or GATING with `RESUME_LIMIT` earlier `resume` notes from those states is FAILED
        `resume_limit` instead. A project another daemon leases is skipped, outbox and all (decision 83).
        Returns the run ids it picked up."""
        picked: list[str] = []
        for pid in self.index.project_ids():
            try:
                todo = self._resume_project(pid)
            except Exception as e:  # one broken project never keeps the others from resuming
                log.warning("resume of project %s failed: %s: %s", pid, type(e).__name__, e)
                continue
            if todo:
                self.start_runs(pid, todo)
                picked.extend(todo)
        return picked

    def _resume_project(self, pid: str) -> list[str]:
        if self.leased_elsewhere(pid) is not None:  # decision 83: its runs stay with the lease holder
            return []
        states = tuple(s.value for s in WORKER_STATES)
        marks = ",".join("?" * len(states))
        with self._lock:
            driving = set(self._driving)
        todo: list[str] = []
        with self.index.project(pid) as conn:
            self.drain_ledger_outbox(conn, pid)
            ids = [
                r[0]
                for r in conn.execute(
                    f"SELECT id FROM runs WHERE state IN ({marks}) ORDER BY created_utc, id", states
                )
            ]
            for rid in ids:
                if (pid, rid) in driving:
                    continue
                run = get_run(conn, rid)
                if not is_pipeline_run(run) or self._resume_limited(conn, pid, run):
                    continue
                append_note(
                    conn,
                    rid,
                    kind=RESUME_NOTE,
                    actor="system",
                    reason=f"the daemon started with this run in {run.state.value}; driving it on",
                )
                todo.append(rid)
        return todo

    def _resume_limited(self, conn: Any, project_id: str, run: Run) -> bool:
        """FAILED `resume_limit` for a run the daemon already stopped on twice in a local stage (a result that
        kills the process would otherwise crash every boot). True when the run was failed."""
        if run.state not in RESUME_LIMITED:
            return False
        limited = tuple(s.value for s in RESUME_LIMITED)
        marks = ",".join("?" * len(limited))
        n = conn.execute(
            f"SELECT COUNT(*) FROM run_events WHERE run_id = ? AND kind = ? AND to_state IN ({marks})",
            (run.id, RESUME_NOTE, *limited),
        ).fetchone()[0]
        if n < RESUME_LIMIT:
            return False
        kept = (
            f"the result blob is kept at {run.result_key}" if run.result_key else "no result blob was fetched"
        )
        reason = f"the daemon stopped twice while driving this run; {kept}"
        try:
            transition(conn, run.id, S.FAILED, actor="system", reason=reason, reason_code="resume_limit")
        except IllegalTransition:  # it moved meanwhile: not ours to fail
            return False
        log.error("run %s failed resume_limit in %s: %s", run.id, run.state.value, reason)
        self.after(conn, project_id, run.id, run.state)
        return True

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
        """Publish the move a run just made from `before` to its current state, and ledger it.

        Under the run's ledger lock, every transition since the last one this process ledgered for the run is
        written, in order, each once (decision 73): an approval a racing cancel overtook is still recorded
        before its `cancelled`, and the second of two calls that saw the same move writes nothing. A run this
        process has not seen yet gets its newest transition only. `note` goes on the first record written."""
        moves = self._moves_for(run_id)
        with moves.lock:
            run = get_run(conn, run_id)
            event = {"kind": "run.state", "run_id": run_id, "from": before.value, "to": run.state.value}
            if run.reason_code:
                event["reason_code"] = run.reason_code
            self.publish(project_id, run_id, event)
            self._ledger_moves(conn, project_id, run, moves, note=note)

    def _moves_for(self, run_id: str) -> _RunMoves:
        with self._lock:
            moves = self._moves.get(run_id)
            if moves is None:
                moves = self._moves[run_id] = _RunMoves()
                while len(self._moves) > LEDGER_MEMO:
                    self._moves.popitem(last=False)
            else:
                self._moves.move_to_end(run_id)
            return moves

    @staticmethod
    def _transitions_since(conn: Any, run_id: str, after_seq: int | None) -> list[tuple[int, RunState, int]]:
        """`(transition seq, to_state, event_seq)` for the run's transitions after `after_seq` (only the newest
        when None, or when the run's events end before `after_seq`: the index was rebuilt under this process),
        where `event_seq` is the newest event before the next transition (decision 73)."""
        top, last = conn.execute(
            "SELECT MAX(seq), MAX(CASE WHEN kind = 'transition' THEN seq END) FROM run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if last is None:
            return []
        if after_seq is None or top < after_seq:
            after_seq = int(last) - 1
        rows = conn.execute(
            "SELECT seq, kind, to_state FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
            (run_id, after_seq),
        ).fetchall()
        out: list[list[Any]] = []
        for seq, kind, to_state in ((r[0], r[1], r[2]) for r in rows):
            if kind == "transition":
                out.append([int(seq), RunState(to_state), int(seq)])
            elif out:
                out[-1][2] = int(seq)
        return [(t, st, ev) for t, st, ev in out]

    def _ledger_moves(
        self, conn: Any, project_id: str, run: Run, moves: _RunMoves, *, note: str | None = None
    ) -> None:
        """Ledger the run's transitions since `moves.seq` (the caller holds `moves.lock`)."""
        todo = self._transitions_since(conn, run.id, moves.seq)
        if todo and todo[-1][1] is not run.state:  # it moved again meanwhile: build from the newer row
            run = get_run(conn, run.id)
        for seq, to_state, event_seq in todo:
            kind = LEDGER_KINDS.get(to_state)
            if kind is not None:
                snap = run if to_state is run.state else run.model_copy(update={"state": to_state})
                self.ledger(project_id, kind, snap, note=note, conn=conn, data={"event_seq": event_seq})
                note = None
            moves.seq = seq

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
        self,
        conn: Any,
        project_id: str,
        run_id: str,
        kind: str,
        err: BaseException,
        *,
        code: str = "ledger_unavailable",
        record: bool = True,
    ) -> None:
        """CANCELLED with `code` (`ledger_unavailable`): the run's quote or approval is not in the folder's
        ledger, so it must not spend. Published on the bus; the `cancelled` record is written or parked in the
        outbox unless `record` is False (a foreign lease refused it: the folder is that daemon's, and this
        index alone holds the cancel). The record that failed stays unwritten: every move up to this cancel
        counts as ledgered."""
        moves = self._moves_for(run_id)
        try:
            with moves.lock:
                before = get_run(conn, run_id).state
                if before in TERMINAL:
                    return
                transition(
                    conn,
                    run_id,
                    S.CANCELLED,
                    actor="system",
                    reason=f"{code}: the {kind} record could not be written ({type(err).__name__})",
                    reason_code=code,
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
                        "reason_code": code,
                    },
                )
                done = self._transitions_since(conn, run_id, None)
                if done:
                    moves.seq = done[-1][0]
                if record:
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

        With `conn`, the record carries `data.event_seq` (the run's newest `run_events` seq; a caller's own
        `data.event_seq` wins) and the project's ledger outbox is drained first. A record that cannot be
        appended is never dropped (decision 73): `quoted` and `approved` cancel the run (`ledger_unavailable`,
        through `conn` or a connection of its own) and raise `PipelineError` 503, `override` raises it and
        changes nothing, and every other kind is parked in the outbox (WARNING `queued in the ledger outbox`),
        as is one that would overtake records still parked there."""
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
        given = extra.get("data") if isinstance(extra.get("data"), Mapping) else {}
        if conn is not None and "event_seq" not in given:
            seq = self.event_seq(conn, run.id)
            if seq is not None:
                data["event_seq"] = seq
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
            refs = run.inputs.get(REFERENCE_INPUT)
            if refs:  # decision 46: what else left with the plate
                data["references"] = [r.get("hash") for r in refs if isinstance(r, Mapping)]
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
            if run.reason_code == SUBMIT_STATE_UNKNOWN:  # the provider may hold the job: never a refund
                data["billing_unknown"] = True
            elif not run.provider_job_id and kind in ("failed", "cancelled"):
                data["refunded"] = True
        if note:
            rec["reason"] = note[:500]
        rec.update(extra)
        data.update(rec.pop("data", None) or {})
        rec["data"] = data
        rec = {k: v for k, v in rec.items() if v is not None and v != ""}
        if conn is not None or project_id in self._outbox_dirty:
            self._drain_before_append(conn, project_id)
        if kind not in FAIL_CLOSED_KINDS and project_id in self._outbox_dirty:
            self._park(conn, project_id, kind, run, rec, "earlier records are still waiting there")
            return
        try:
            self.ledger_for(project_id).append(rec)
        except lease.ProjectLeased as e:  # decision 83: the folder is another daemon's for now
            if kind not in FAIL_CLOSED_KINDS:  # appended once this daemon holds the project again
                self._park(conn, project_id, kind, run, rec, str(e))
                return
            log.warning("ledger record %s for %s refused: %s", kind, run.id, e)
            if kind in CANCEL_ON_LOSS_KINDS:
                self._cancel_leased(conn, project_id, run.id, kind, e)
            raise
        except Exception as e:
            if kind not in FAIL_CLOSED_KINDS:  # the index holds the state; the outbox keeps the record
                self._park(conn, project_id, kind, run, rec, f"{type(e).__name__}: {e}")
                return
            if kind not in CANCEL_ON_LOSS_KINDS:  # an override: refused, and nothing about the run changes
                log.error("ledger record %s for %s not written: %s; refused", kind, run.id, e)
                raise PipelineError(
                    f"the project ledger is unavailable ({type(e).__name__}: {e}); the {kind} of run {run.id} "
                    "was not recorded and nothing was changed",
                    code="ledger_unavailable",
                    status=503,
                    run_id=run.id,
                ) from e
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

    def _cancel_leased(
        self, conn: Any, project_id: str, run_id: str, kind: str, err: lease.ProjectLeased
    ) -> None:
        """A `quoted` or `approved` record a foreign lease refused: the run is CANCELLED `project_leased` in
        this index only (nothing is ledgered or parked: the folder never had the record)."""
        if conn is not None:
            self._cancel_unledgered(conn, project_id, run_id, kind, err, code=LEASED, record=False)
            return
        try:
            with self.index.project(project_id) as own:
                self._cancel_unledgered(own, project_id, run_id, kind, err, code=LEASED, record=False)
        except Exception as e2:
            log.error("run %s could not be cancelled after a lease refused its %s: %s", run_id, kind, e2)

    def ledger_for(self, project_id: str) -> Ledger:
        """The project's one `Ledger` (built with this pipeline's clock; rebuilt when its folder moved)."""
        root = Path(self.project_dir(project_id))
        with self._lock:
            led = self._ledgers.get(project_id)
            if led is None or led.project_dir != root:
                led = self._ledgers[project_id] = Ledger(root, clock=self.clock)
            return led

    @staticmethod
    def event_seq(conn: Any, run_id: str) -> int | None:
        """The run's newest `run_events` seq (decision 73's `data.event_seq`), None when it has none."""
        try:
            row = conn.execute("SELECT MAX(seq) FROM run_events WHERE run_id = ?", (run_id,)).fetchone()
        except sqlite3.Error as e:
            log.warning("event seq of run %s not read: %s", run_id, e)
            return None
        return int(row[0]) if row is not None and row[0] is not None else None

    def drain_ledger_outbox(self, conn: Any, project_id: str) -> int:
        """Append what the project's ledger outbox holds, in order (`ledger.drain_outbox`); returns how many
        records went in. A failure is a warning: the rows stay for the next append, boot or watch."""
        try:
            n = drain_outbox(conn, self.ledger_for(project_id))
            left = outbox_count(conn)
        except Exception as e:
            log.warning("ledger outbox of %s not drained: %s: %s", project_id, type(e).__name__, e)
            return 0
        with self._lock:
            if left:
                self._outbox_dirty.add(project_id)
            else:
                self._outbox_dirty.discard(project_id)
        if n:
            log.info("ledger outbox of %s: %d record(s) appended, %d waiting", project_id, n, left)
        return n

    def _drain_before_append(self, conn: Any, project_id: str) -> None:
        if conn is not None:
            self.drain_ledger_outbox(conn, project_id)
            return
        try:
            with self.index.project(project_id) as own:
                self.drain_ledger_outbox(own, project_id)
        except Exception as e:
            log.warning("ledger outbox of %s not drained: %s: %s", project_id, type(e).__name__, e)

    def _park(self, conn: Any, project_id: str, kind: str, run: Run, rec: dict[str, Any], why: str) -> None:
        """Put a record the ledger cannot take now into the project's outbox (decision 73)."""
        try:
            if conn is not None:
                enqueue_outbox(conn, rec, clock=self.clock)
            else:
                with self.index.project(project_id) as own:
                    enqueue_outbox(own, rec, clock=self.clock)
        except Exception as e:
            log.error(
                "ledger record %s for %s not written (%s) and not queued in the ledger outbox either: %s: %s",
                kind,
                run.id,
                why,
                type(e).__name__,
                e,
            )
            return
        with self._lock:
            self._outbox_dirty.add(project_id)
        log.warning("ledger record %s for %s queued in the ledger outbox: %s", kind, run.id, why)

    # ------------------------------------------------------------------ helpers for the routes

    def project_list(self) -> list[dict[str, Any]]:
        """The head of every known project.json: indexed projects plus folders under projects_dir. A folder
        whose project.json does not read is listed as `{"project_id", "root", "broken": true, "detail"}` (its
        indexed id, else the folder name; WARNING) instead of being hidden."""
        seen: dict[str, dict[str, Any]] = {}
        roots: list[tuple[str | None, Path]] = [
            (pid, Path(self.project_dir(pid))) for pid in self.index.project_ids()
        ]
        pdir = self.settings.projects_dir
        if pdir is not None and pdir.is_dir():
            roots.extend((None, p) for p in sorted(pdir.iterdir()) if (p / "project.json").is_file())
        for known, root in roots:
            try:
                project = Project.load(root)
            except FileNotFoundError:
                continue
            except (ProjectError, ValueError, OSError) as e:
                pid = known or root.name
                if pid not in seen:
                    detail = f"{type(e).__name__}: {e}"[:500]
                    log.warning("project %s at %s does not read: %s", pid, root, detail)
                    seen[pid] = {"project_id": pid, "root": str(root), "broken": True, "detail": detail}
                continue
            seen.setdefault(project.project_id, project_head(project))
        return [seen[k] for k in sorted(seen)]


def result_notes(ingress: Mapping[str, Any] | None) -> list[str]:
    """Decision 86: the gate report's `backend_notes` lines for the result's format (`normalized.ingress`)."""
    if not isinstance(ingress, Mapping):
        return []
    notes = []
    if ingress.get("lossy"):
        depth = ingress.get("raw_bit_depth")
        notes.append(f"result container {ingress.get('raw_format')}: lossy {depth}-bit")
    if ingress.get("alpha_dropped"):
        notes.append("result alpha dropped")
    if ingress.get("alpha_transparent"):
        notes.append("result alpha fully transparent")
    return notes


def project_head(project: Project) -> dict[str, Any]:
    """What `GET /projects` lists for one project: the head of its project.json, and its lease."""
    return {
        "project_id": project.project_id,
        "custody": project.custody,
        "created_utc": project.doc.get("created_utc"),
        "engine_pins": project.engine_pins,
        "root": str(project.root),
        "nodes": len(project.graph.get("nodes") or []),
        "lease": lease_view(project.root),
    }


def lease_view(project_dir: Path | str) -> dict[str, Any] | None:
    """`{host, pid, daemon_id, heartbeat_utc, live, mine}` of the project's `lease.json`; None without one."""
    info = lease.read(project_dir)
    if info is None:
        return None
    me = lease.identity()
    return {
        "host": info.host,
        "pid": info.pid,
        "daemon_id": info.daemon_id,
        "heartbeat_utc": info.heartbeat_utc,
        "live": lease.is_live(info),
        "mine": me is not None and info.daemon_id == me.daemon_id,
    }
