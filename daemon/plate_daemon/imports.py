"""External files imported as runs (router plan T74, decision 42).

A take made outside PLATE (Higgsfield, Runway, or by hand) comes back as a run of its own, so it is gated,
ledgered, reviewed and exported like any other take:

    import_file(pipeline, project_id, packet_id, path, *, source, node_id=None, cost_usd=0, note=None,
                by="human") -> Run                                      DONE, GATE_FAIL (or FAILED)

What it checks before anything is stored, in this order (a refusal leaves no run row and no CAS object):

- `source` is `higgsfield`, `runway` or `manual` (422 `unknown_source`), `cost_usd` a finite number >= 0 and
  `by` an approver (`human`, `auto`, `agent:<envelope_id>`), else 422;
- the project and the packet exist (404 `not_found`), and so does `node_id` when one is named (404);
- custody: the import's route must be a grade the project and the packet allow (`custody.require_route`). A
  Higgsfield or Runway file is grade D (its plate already went to that vendor), so on an NDA project it is
  `CustodyViolation` (403); a manual file is C0;
- the file decodes as a display-referred image (422 `unreadable_image`; a scene-linear file is refused too: the
  gates score display masters) at exactly the plate's size (422 `size_mismatch`: nothing is resized or
  registered onto the plate here; the gates measure the file as it is).

The run. Route `{engine_id: "import:<source>", provider: "external", custody_grade: D | C0, custody_status:
"confirmed", upload_visibility: "unknown" | "local"}`; inputs `{plate, mask, imported: {path_name, source, note,
blake3}, staging}`, where `mask` is what `Pipeline.mask_spec` reads for `node_id` (its nearest upstream Mask
node, else the packet's policy), `imported.blake3` the raw file's key, and `staging` the pipeline's marker with
`node_kind: "Edit", imported: true`, so `POST /runs/{id}/gate/rescore` scores an import again like an Edit take
and a daemon stopped mid-import has `Pipeline.resume` fail the run (no engine speaks `import:*`) instead of
leaving it in flight. A node's snapshot rides in `inputs.subgraph` as a quote's does. The attempt is numbered
by decision 40: 1 + the highest attempt of earlier runs of the same node (or of no node) whose inputs (less
`subgraph` and `router`), params and route are the same, so a second import of one file is attempt 2.

The moves, each published on the bus as `run.state`: QUOTED at the declared price (`cost_usd`,
`price_status: "declared"`), APPROVED by `by` (the budget rules of `quotes.approve`), QUEUED, STAGING,
SUBMITTED (the provider job id is `import-<blake3[:24]>`), RUNNING, FETCHING, NORMALIZING (the raw file is the
run's `result_key` and `normalized.raw_key`; a 16-bit PNG of it with the run's lineage embedded is
`outputs[0]`, its perceptual hash recorded; the editable mask is stored as `normalized.mask_key`), GATING
(`gating.run_gates` with the arguments `Pipeline.gate` passes: the packet's plate, the editable mask, its
G-buffer edges and material Cryptomatte; an import has no ingress report, so the gate measures the
registration itself and a moved camera shows as `camera_drift`), then DONE or GATE_FAIL with the report written
by `gate_reports.Service`. `quoted` (with `cost_usd_quoted`), `approved` and `done` / `gated` (and `failed` or
`cancelled` when it comes to that) are ledgered with `data.imported: true`; `quoted` and `approved` fail
closed (503 `ledger_unavailable`, the run cancelled) like every quote. Nothing leaves the box, so the outbound
log is never written. Refusals are `PipelineError` (its `code` and `status` are the HTTP answer) or
`CustodyViolation`.
"""

from __future__ import annotations

import logging
import math
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import budgets, canvas_rules, gating
from . import lineage as lineage_mod
from .cas import CAS, CASError, normalize_ext
from .config import check_project_id
from .custody import require_route
from .db import loads, tx
from .gate_reports import Service as ReportService
from .machine import DuplicateRequest, IllegalTransition, annotate, create_run, get_run, transition
from .models import APPROVED_BY_RE, QUOTE_TTL, Quote, Run, RunCreate
from .pipeline import (
    LEDGER_KINDS,
    STAGING_KEY,
    PacketView,
    Pipeline,
    PipelineError,
    decode_png16,
    encode_png16,
    repeat_canonical,
    subgraph_snapshot,
)
from .quotes import AutoApprovalDenied, EnvelopeRequired, QuoteExpired, QuoteNotFound, insert_quote
from .quotes import approve as approve_one
from .states import Priority, RunState

__all__ = [
    "ENGINE_PREFIX",
    "SOURCES",
    "import_file",
    "import_route",
    "is_import_run",
    "next_attempt",
]

log = logging.getLogger(__name__)
S = RunState

ENGINE_PREFIX = "import:"
# source -> the custody grade of the file: a vendor's output means the plate already went to that vendor.
SOURCES: dict[str, str] = {"higgsfield": "D", "runway": "D", "manual": "C0"}
ACTOR = "import"
MAX_NOTE_CHARS = 500
JOB_ID_HEX = 24
RAW_FALLBACK_EXT = "bin"
# The moves an import ledgers (decision 42): the quote, the approval and the end, whatever it is.
LEDGERED = frozenset({S.QUOTED, S.APPROVED, S.DONE, S.GATE_FAIL, S.FAILED, S.CANCELLED})


def import_route(source: str) -> dict[str, Any]:
    """The route an import of `source` runs on (decision 42). Raises PipelineError 422 for an unknown source."""
    grade = SOURCES.get(source) if isinstance(source, str) else None
    if grade is None:
        raise PipelineError(
            f"unknown import source {source!r}; use one of {', '.join(SOURCES)}", code="unknown_source"
        )
    local = grade == "C0"
    return {
        "engine_id": f"{ENGINE_PREFIX}{source}",
        "provider": "external",
        "region": "local" if local else "external",
        "route_id": f"import-{source}",
        "custody_grade": grade,
        "custody_status": "confirmed",
        "upload_visibility": "local" if local else "unknown",
        "model_version": source,
    }


def is_import_run(run: Run) -> bool:
    return run.route.engine_id.startswith(ENGINE_PREFIX) and isinstance(run.inputs.get("imported"), Mapping)


def next_attempt(
    conn: Any,
    node_id: str | None,
    inputs: Mapping[str, Any],
    params: Mapping[str, Any],
    route: Mapping[str, Any],
) -> int:
    """Decision 40, as `Pipeline.next_attempt`, for a node or for no node (`node_id IS NULL`)."""
    want = repeat_canonical(inputs, params, route)
    best = 0
    for row in conn.execute("SELECT attempt, inputs, params, route FROM runs WHERE node_id IS ?", (node_id,)):
        try:
            same = repeat_canonical(loads(row[1]) or {}, loads(row[2]) or {}, loads(row[3]) or {}) == want
        except (TypeError, ValueError):
            continue
        if same:
            best = max(best, int(row[0] or 0))
    return best + 1


# ---------------------------------------------------------------------------- checks before anything is stored


def _check_args(source: Any, cost_usd: Any, by: Any, note: Any) -> tuple[dict[str, Any], float, str | None]:
    route = import_route(source)
    if isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float)):
        raise PipelineError(f"cost_usd must be a number of USD, got {cost_usd!r}", code="bad_cost")
    price = float(cost_usd)
    if not math.isfinite(price) or price < 0:
        raise PipelineError(f"cost_usd must be a finite amount >= 0, got {cost_usd!r}", code="bad_cost")
    if not isinstance(by, str) or not APPROVED_BY_RE.match(by):
        raise PipelineError(f"by must be human, auto or agent:<envelope_id>, got {by!r}", code="bad_approver")
    if note is not None and not isinstance(note, str):
        raise PipelineError("note must be text", code="bad_note")
    text = (note or "").strip()[:MAX_NOTE_CHARS] or None
    return route, price, text


def _packet(pl: Pipeline, project_id: str, packet_id: str) -> PacketView:
    try:
        return pl.packet(project_id, packet_id)
    except PipelineError as e:
        if e.code == "unknown_packet":
            raise PipelineError(str(e), code="not_found", status=404, packet_id=packet_id) from e
        raise


def _decode(raw: bytes, suffix: str, cache: Path) -> np.ndarray:
    """uint16 RGB (H, W, 3) of a display-referred image file's bytes (8-bit files are promoted by x257, as
    `PacketView.plate16` does). Raises PipelineError 422 `unreadable_image`."""
    from plate_pixels import imageio

    cache.mkdir(parents=True, exist_ok=True)
    # decoded from these bytes, the ones that go into the CAS (never a second read of a file that may change)
    with tempfile.TemporaryDirectory(dir=cache) as tmp:
        p = Path(tmp) / f"import{suffix or '.bin'}"
        p.write_bytes(raw)
        try:
            img = imageio.read(p)
        except Exception as e:  # any decoder failure is the same answer: not an image this daemon reads
            raise PipelineError(
                f"not a readable image ({type(e).__name__}: {e})"[:500], code="unreadable_image"
            ) from e
    if img.encoding != "srgb":
        raise PipelineError(
            "the file is scene-linear: an import is a display-referred take (PNG, TIFF or JPEG)",
            code="unreadable_image",
        )
    rgb = np.asarray(img.rgb)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise PipelineError(f"expected an RGB image, got shape {rgb.shape}", code="unreadable_image")
    scale = 65535.0 if img.bit_depth >= 16 else 255.0
    arr = np.clip(np.rint(np.nan_to_num(rgb, nan=0.0) * scale), 0, scale).astype(np.uint16)
    return arr if scale == 65535.0 else arr * np.uint16(257)


def _raw_ext(path: Path) -> str:
    try:
        return normalize_ext(path.suffix or RAW_FALLBACK_EXT)
    except CASError:
        return RAW_FALLBACK_EXT


# ---------------------------------------------------------------------------- the run's moves


def _moved(
    pl: Pipeline, conn: Any, project_id: str, run_id: str, before: RunState, data: Mapping[str, Any]
) -> Run:
    """Publish the move a run just made from `before`; ledger it with `data` when it is a ledgered state."""
    run = get_run(conn, run_id)
    event = {"kind": "run.state", "run_id": run_id, "from": before.value, "to": run.state.value}
    if run.reason_code:
        event["reason_code"] = run.reason_code
    pl.publish(project_id, run_id, event)
    if run.state in LEDGERED:
        pl.ledger(project_id, LEDGER_KINDS[run.state], run, conn=conn, data=dict(data))
    return run


def _step(
    pl: Pipeline,
    conn: Any,
    project_id: str,
    run_id: str,
    to: RunState,
    data: Mapping[str, Any],
    **fields: Any,
) -> Run:
    before = get_run(conn, run_id).state
    transition(conn, run_id, to, actor=ACTOR, **fields)
    return _moved(pl, conn, project_id, run_id, before, data)


def _stop(
    pl: Pipeline,
    conn: Any,
    project_id: str,
    run_id: str,
    to: RunState,
    code: str,
    reason: str,
    data: Mapping[str, Any],
) -> Run:
    """End the run in FAILED or CANCELLED with a reason code (never raises past a run already terminal)."""
    try:
        return _step(pl, conn, project_id, run_id, to, data, reason=reason[:500], reason_code=code)
    except IllegalTransition:
        return get_run(conn, run_id)


def _with_lineage(pl: Pipeline, run: Run, project_dir: Path, png: bytes) -> tuple[bytes, bool]:
    """The PNG with the run's lineage embedded (the report it will be scored in named), else as given."""
    n = max(ReportService(project_dir).versions(run.id), default=0) + 1
    tmp = pl.settings.home / "cache" / f"import_{run.id}_{run.attempt}.png"
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(png)
        lin = lineage_mod.Lineage(
            project_id=run.project_id,
            node_id=run.node_id or "none",
            run_id=run.id,
            packet_id=run.packet_id,
            engine_id=run.route.engine_id,
            engine_version=str(run.route_dict.get("model_version") or "") or None,
            gate_report=f"gates/{run.id}.{n}.json",
        )
        lineage_mod.embed(tmp, lin)
        return tmp.read_bytes(), True
    except Exception as e:  # lineage is for drop-to-resolve; the take is still the take without it
        log.warning("lineage not embedded in imported run %s: %s", run.id, e)
        return png, False
    finally:
        tmp.unlink(missing_ok=True)


def _record_phash(pl: Pipeline, project_id: str, cas: CAS, key: str) -> None:
    try:
        from .phash import PhashIndex

        path = cas.find(key)
        if path is None:
            raise FileNotFoundError(f"{key[:12]} is not in the CAS")
        with pl.index.project(project_id) as conn:
            PhashIndex(conn, project_id).add_file(path, key)
    except Exception as e:  # a missing phash only weakens drop-to-resolve for re-encoded copies
        log.warning("phash of imported %s in %s not recorded: %s", key[:12], project_id, e)


def _alert(pl: Pipeline, project_id: str, run_id: str) -> Any:
    def alert(decision: budgets.Decision) -> None:
        try:
            budgets.publish_alert(pl.bus, project_id, run_id, decision)
        except Exception as e:  # the bus is for watchers
            log.warning("budget alert for imported run %s not published: %s", run_id, e)

    return alert


def _mask_png(mask: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.where(mask, 255, 0).astype(np.uint8))
    if not ok:
        raise ValueError("cv2 could not encode the mask")
    return bytes(buf)


def _normalize(
    pl: Pipeline, run: Run, pv: PacketView, cas: CAS, image16: np.ndarray, project_dir: Path
) -> dict[str, Any]:
    """NORMALIZING for an import: a lineage-tagged 16-bit PNG copy of the file (no composite: the gates measure
    what the vendor or the artist changed, outside the mask included) and the editable mask it is held to."""
    data, embedded = _with_lineage(pl, run, project_dir, encode_png16(image16))
    out_key = cas.put_bytes(data, "png")
    _record_phash(pl, run.project_id, cas, out_key)
    mask = pl.editable_mask(run, pv, cas)
    return {
        "outputs": [out_key],
        "raw_key": run.result_key,
        "mask_key": cas.put_bytes(_mask_png(mask), "png"),
        "ingress": None,
        "composite": None,
        "textlock_px": None,
        "mask_share": float(mask.mean()),
        "imported": dict(run.inputs.get("imported") or {}),
        "lineage": embedded,
    }


def _gate(pl: Pipeline, run: Run, pv: PacketView, cas: CAS, project_dir: Path) -> tuple[bool, dict[str, Any]]:
    """GATING with the arguments `Pipeline.gate` passes; the report is written as `gates/<run_id>.<n>.json`.
    Returns (passed, the run's gate_report as the worker stores it)."""
    norm = run.normalized or {}
    outputs = norm.get("outputs") or []
    out16 = decode_png16(cas.read_bytes(outputs[0]))
    raw = cv2.imdecode(np.frombuffer(cas.read_bytes(norm["mask_key"]), np.uint8), cv2.IMREAD_GRAYSCALE)
    report = gating.run_gates(
        pv.plate16(),
        out16,
        {"path": pv.root, "manifest": pv.manifest},
        pv.legend,
        pv.tier,
        {"id": run.route.engine_id, "version": str(run.route_dict.get("model_version") or "") or None},
        mask=raw > 127,
        registration=norm.get("ingress"),
        depth_fn=pl.depth_fn,
        edges=pv.edges(),
        crypto_material=pv.crypto("material") if pv.tier == "A" else None,
        extra_metrics={"textlock_px": norm.get("textlock_px"), "output": outputs[0]},
    )
    path = Path(ReportService(project_dir).write(run.id, report))
    try:
        ref = path.relative_to(project_dir).as_posix()
    except ValueError:
        ref = path.as_posix()
    passed = report.verdict != "fail"
    stored = report.model_dump(mode="json")
    stored["verdict"] = "pass" if passed else "fail"  # what the worker keeps on the run (worker._gating)
    stored["score"] = float(gating.score_of(report))
    stored["report_path"] = ref
    return passed, stored


# ---------------------------------------------------------------------------- the entry point


def import_file(
    pipeline: Pipeline,
    project_id: str,
    packet_id: str,
    path: str | Path,
    *,
    source: str,
    node_id: str | None = None,
    cost_usd: float = 0,
    note: str | None = None,
    by: str = "human",
) -> Run:
    """Import `path` as a run of `project_id` on `packet_id` (see the module docstring). Returns the run at its
    end: DONE or GATE_FAIL, or FAILED when normalizing or gating raised (the reason is on the run)."""
    pl = pipeline
    route, price, note = _check_args(source, cost_usd, by, note)
    try:
        check_project_id(project_id)
    except ValueError as e:
        raise PipelineError(str(e), code="bad_project_id") from e
    project = pl.load_project(project_id)
    if project is None:
        raise PipelineError(f"no project {project_id}", code="not_found", status=404, project_id=project_id)
    pv = _packet(pl, project_id, packet_id)
    graph = canvas_rules.as_graph(project.graph)
    if node_id is not None and not any(n.get("id") == node_id for n in graph.nodes):
        raise PipelineError(
            f"project {project_id} has no node {node_id}", code="not_found", status=404, node_id=node_id
        )
    grade = require_route(
        route, pl.allowed_grades(project_id, pv.manifest), what=f"a file imported from {source}"
    )
    file = Path(path)
    try:
        raw = file.read_bytes()
    except OSError as e:
        raise PipelineError(f"cannot read {file.name}: {e}"[:500], code="unreadable_image") from e
    image16 = _decode(raw, file.suffix.lower(), pl.settings.home / "cache")
    plate16 = pv.plate16()
    if image16.shape != plate16.shape:
        (ph, pw), (fh, fw) = plate16.shape[:2], image16.shape[:2]
        raise PipelineError(
            f"{file.name} is {fw} x {fh}; the plate is {pw} x {ph} (an import is never resized onto it)",
            code="size_mismatch",
            plate=[pw, ph],
            file=[fw, fh],
        )
    mask = pl.mask_spec(graph, node_id or "", pv)
    pl.check_mask_keys(project_id, mask, pv)

    cas = pl.cas_for(project_id)
    raw_key = cas.put_bytes(raw, _raw_ext(file))
    imported = {"path_name": file.name, "source": source, "note": note, "blake3": raw_key}
    inputs: dict[str, Any] = {
        "plate": pv.plate_hash,
        "mask": mask,
        "imported": imported,
        STAGING_KEY: {"node_kind": "Edit", "imported": True, "region_crop": False},
    }
    if node_id is not None:
        keep = canvas_rules.ancestors(graph, node_id) | {node_id}
        inputs["subgraph"] = subgraph_snapshot(project.graph, keep)
    params: dict[str, Any] = {}
    data = {"imported": True, "source": source}
    project_dir = pl.project_dir(project_id)
    now = pl.clock()

    with pl.index.project(project_id) as conn:
        spec = RunCreate(
            project_id=project_id,
            packet_id=packet_id,
            node_id=node_id,
            priority=Priority.INTERACTIVE,
            inputs=inputs,
            params=params,
            route=route,
            attempt=next_attempt(conn, node_id, inputs, params, route),
        )
        run = create_run(conn, spec, now=now)
        pl.index.remember(run.id, project_id)
        rid = run.id
        quote = Quote(
            run_id=rid,
            provider=route["provider"],
            model=route["engine_id"],
            model_version=route["model_version"],
            region=route["region"],
            custody_grade=grade.value,
            price_usd=price,
            eta_s=0.0,
            created_utc=now,
            expires_utc=now + QUOTE_TTL,
            route_id=route["route_id"],
            price_status="declared",
        )
        with tx(conn):
            insert_quote(conn, quote)
            transition(
                conn,
                rid,
                S.QUOTED,
                actor=ACTOR,
                now=now,
                reason=f"imported from {source}: {price:.4f} USD declared",
                quote_id=quote.id,
                quote_json=quote.terms_json(),
            )
        _moved(pl, conn, project_id, rid, S.DRAFT, data)  # fail-closed: 503 and CANCELLED without a record

        try:
            approve_one(
                conn,
                rid,
                by,
                None,
                now,
                policy=pl.policy,
                last_human_interaction_utc=pl.last_human_utc,
                on_alert=_alert(pl, project_id, rid),
            )
        except (AutoApprovalDenied, EnvelopeRequired, QuoteExpired, QuoteNotFound, IllegalTransition) as e:
            _stop(pl, conn, project_id, rid, S.CANCELLED, "approval_refused", str(e), data)
            raise PipelineError(
                f"the import was not approved: {e}"[:500], code="approval_refused", status=409, run_id=rid
            ) from e
        if by == "human":
            pl.touch_human(now)
        _moved(pl, conn, project_id, rid, S.QUOTED, data)  # fail-closed like the quote

        _step(pl, conn, project_id, rid, S.QUEUED, data)
        try:
            _step(pl, conn, project_id, rid, S.STAGING, data)
        except DuplicateRequest as e:
            _stop(pl, conn, project_id, rid, S.CANCELLED, "duplicate_request", str(e), data)
            raise PipelineError(str(e), code="duplicate_request", status=409, run_id=rid) from e
        annotate(conn, rid, expect=S.STAGING, submit_started_utc=now)
        annotate(
            conn,
            rid,
            expect=S.STAGING,
            provider_job_id=f"import-{raw_key[:JOB_ID_HEX]}",
            submitted_utc=now,
        )
        _step(
            pl,
            conn,
            project_id,
            rid,
            S.SUBMITTED,
            data,
            reason=f"imported from {source}; nothing left the box",
        )
        _step(pl, conn, project_id, rid, S.RUNNING, data)
        _step(pl, conn, project_id, rid, S.FETCHING, data, result_url=f"{ENGINE_PREFIX}{raw_key}")
        run = _step(
            pl,
            conn,
            project_id,
            rid,
            S.NORMALIZING,
            data,
            reason=f"{len(raw)} bytes of {file.name} into the CAS",
            result_key=raw_key,
        )
        try:
            normalized = _normalize(pl, run, pv, cas, image16, project_dir)
        except Exception as e:  # recorded as the failure reason, like the worker's normalize_error
            code = e.code if isinstance(e, PipelineError) else "normalize_error"
            return _stop(
                pl, conn, project_id, rid, S.FAILED, code, f"normalize raised {type(e).__name__}: {e}", data
            )
        run = _step(pl, conn, project_id, rid, S.GATING, data, normalized=normalized)
        try:
            passed, report = _gate(pl, run, pv, cas, project_dir)
        except Exception as e:  # the worker's gate_error
            log.exception("gating imported run %s failed", rid)
            return _stop(
                pl,
                conn,
                project_id,
                rid,
                S.FAILED,
                "gate_error",
                f"gate raised {type(e).__name__}: {e}",
                data,
            )
        if passed:
            return _step(pl, conn, project_id, rid, S.DONE, data, gate_report=report)
        return _step(
            pl,
            conn,
            project_id,
            rid,
            S.GATE_FAIL,
            data,
            reason="gates failed",
            gate_report=report,
            reason_code="gate_fail",
        )
