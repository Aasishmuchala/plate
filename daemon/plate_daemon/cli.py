"""`plate`: run / gate / export / reindex / gc / pack / smoke over the daemon's own modules (router plan T34).

    plate run <recipe.yaml|id> --project <dir> --packet <id|dir> [--engine ID] [--n N]
              [--approve auto|human] [--envelope USD] [--shot-class C] [--param K=V]
              [--nda] [--allow-grades C0,C1,C2]            (the last two only when the project is new)
    plate gate <plate> <candidate> [--packet DIR] [--out report.json]
    plate export <run_id> --to <dir> --format tiff16|png16|exr [--override-reason TEXT] [--name N] [--project DIR]
    plate reindex <project> [--verify]
    plate gc <project> [--dry-run] [--min-age-hours H]
    plate pack <project> --out <zip> [--full] [--max-blob-mb MB]
    plate smoke [--live --budget-usd N --engines a,b] [--out report.json]

`<project>` is a project folder or a project id this machine knows.

Run it as `python -m plate_daemon.cli`, as the `plate` console script (`[project.scripts]`, installed by a
`uv sync`), or through the shims `scripts/plate` and `scripts\\plate.cmd`. Nothing here talks to a daemon: the
pipeline runs in this process on the same per-machine index (`PLATE_HOME`) and the same project folders, so a
run made here shows up in the UI and the ledger like any other.

Exit codes: 0 ok, 1 an error that is not one of the next three (a failed run, a locked project, a bug),
2 a gate failure (`run` ended GATE_FAIL, `gate` scored `fail`, `export` refused a failed run), 3 a custody,
budget or approval refusal (nothing was sent), 4 usage (bad arguments, an unknown run, a recipe that cannot run).

`run` follows the doctrine rules in code: the packet's plate is the only starting point (no text-to-image), the
engine never sees more than egress prepared, and the quote is printed and approved before a byte leaves.
`--approve human` (the default) waits for `y`; `--approve auto` goes through the same cap, daily and unattended
checks as the daemon's auto approval and exits 3 when one says no; `--envelope USD` makes the spend a hard
ceiling either way. `--n N` runs a best-of-N group under one quote. A vendor engine needs `PLATE_LIVE=1` (without
it the transport is recorded fixtures and could only fail); `PLATE_FAKE_ENGINE=1` swaps in the in-process fake
engine, which echoes the plate egress prepared (a JSON object sets its flags: `echo`, `shift_px`, `size`,
`behaviour`, ... as `engine_fake.FakeEngine`). Recipes that make video (`motion_croppair`) are run from the
Motion node, not here.

`smoke` replays the recorded vendor fixtures (`plate_engines/contract`) through every adapter's minimum-cost
call and prints `fixture smoke: N adapters ok`; it opens no socket. `smoke --live` is the P0 spend check: it needs
`PLATE_LIVE=1` and an explicit `--budget-usd`, prints the custody grade and the quote of every call before it is
sent and stops when the next call would take the running total past the budget.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import httpx

from . import __version__, canvas_rules, recipes
from .cas import CAS
from .config import PROJECT_ID_RE, ConfigError, Settings
from .custody import CustodyViolation
from .engine_fake import FLAG_NAMES, RESULT_SCHEME, FakeEngine
from .export import FORMATS as EXPORT_FORMATS
from .export import MIN_OVERRIDE_REASON, NotExportable, export
from .fsutil import replace_with_retry, temp_sibling, write_json_atomic
from .index import Index, RebuildError
from .ingest import PacketConflict, PacketInvalid, begin_ingest, finish_ingest, prepare_ingest
from .lock import ProjectLocked
from .machine import DuplicateRequest, IllegalTransition, RunNotFound, get_run, transition
from .pipeline import FAKE_ENGINE_ID, PacketView, Pipeline, PipelineError
from .projects import Lock, Project, ProjectError
from .quotes import (
    AutoApprovalDenied,
    EnvelopeExceeded,
    EnvelopeRequired,
    QuoteExpired,
    QuoteNotFound,
    create_envelope,
)
from .recipes import ProjectSheet, RecipeError, RunnableRecipe
from .states import RunState
from .timeutil import iso, utcnow
from .worker import FanOutError, fan_out

__all__ = [
    "CliExit",
    "CliFakeEngine",
    "EXIT_ERROR",
    "EXIT_GATE_FAIL",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_USAGE",
    "build_parser",
    "fake_engine_from_env",
    "main",
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_GATE_FAIL = 2
EXIT_REFUSED = 3
EXIT_USAGE = 4

STILL_KINDS = ("still_edit", "still_rephotograph")
FAKE_FLAGS = (*FLAG_NAMES, "echo", "shift_px")
TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"", "0", "false", "no", "off"}
SHOT_CLASSES = ("wide", "medium", "close", "detail")
ENVELOPE_TTL_S = 3600.0
LOCK_WAIT_S = 10.0
PACK_BLOB_LIMIT_MB = 50.0
GC_GRACE_HOURS = 1.0
REPO_ROOT = Path(__file__).resolve().parents[2]


class CliExit(Exception):
    """Stop with this exit code; the message goes to stderr."""

    def __init__(self, code: int, message: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------- the in-process fake engine


class CliFakeEngine(FakeEngine):
    """The daemon's FakeEngine, except that it hands back the PNG egress sent it (`echo`, the default), moved
    `shift_px` to the right if asked, so the gates score real pixels. `echo: false` keeps the FakeEngine's
    noise (give it a `size` equal to the plate, it never upscales), which is what makes a GATE_FAIL run."""

    def __init__(self, *args: Any, echo: bool = True, shift_px: int = 0, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.echo = bool(echo)
        self.shift_px = int(shift_px)
        self._sent: dict[str, bytes] = {}

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        import base64

        job = super().submit(payload, idempotency_key)
        image = (payload.get("inputs") or {}).get("image")
        if isinstance(image, Mapping) and image.get("b64"):
            self._sent[job] = base64.b64decode(image["b64"])
        return job

    def fetch(self, result_url: str) -> bytes:
        data = super().fetch(result_url)
        job = result_url[len(RESULT_SCHEME) :] if result_url.startswith(RESULT_SCHEME) else None
        if not self.echo or job not in self._sent:
            return data
        sent = self._sent[job]
        if not self.shift_px:
            return sent
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(sent, np.uint8), cv2.IMREAD_UNCHANGED)
        return bytes(cv2.imencode(".png", np.roll(img, self.shift_px, axis=1))[1])


def fake_engine_from_env(env: Mapping[str, str]) -> CliFakeEngine | None:
    """`PLATE_FAKE_ENGINE`: unset, 0 or false means the real engines; 1 (or true, yes, on) the echoing fake
    engine; a JSON object its flags."""
    raw = (env.get("PLATE_FAKE_ENGINE") or "").strip()
    if raw.lower() in FALSE_WORDS:
        return None
    if raw.lower() in TRUE_WORDS:
        flags: dict[str, Any] = {}
    else:
        try:
            flags = json.loads(raw)
        except ValueError as e:
            raise CliExit(EXIT_USAGE, f"PLATE_FAKE_ENGINE must be 1 or a JSON object of flags ({e})") from e
        if not isinstance(flags, dict):
            raise CliExit(EXIT_USAGE, "PLATE_FAKE_ENGINE must be 1 or a JSON object of flags")
    unknown = sorted(set(flags) - set(FAKE_FLAGS))
    if unknown:
        raise CliExit(
            EXIT_USAGE, f"PLATE_FAKE_ENGINE: unknown flag(s) {unknown}; known: {sorted(FAKE_FLAGS)}"
        )
    try:
        return CliFakeEngine(**flags)
    except (TypeError, ValueError) as e:
        raise CliExit(EXIT_USAGE, f"PLATE_FAKE_ENGINE: {e}") from e


# ---------------------------------------------------------------------------- session: settings, index, io


@dataclass
class Session:
    """What every command shares: the environment, the per-machine index and the two output streams."""

    env: Mapping[str, str]
    out: TextIO
    err: TextIO
    ask: Callable[[str], str]
    sleep: Callable[[float], None] = time.sleep
    _settings: Settings | None = field(default=None, repr=False)
    _index: Index | None = field(default=None, repr=False)

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def warn(self, text: str) -> None:
        print(text, file=self.err, flush=True)

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            try:
                self._settings = Settings.load(self.env)
            except ConfigError as e:
                raise CliExit(EXIT_USAGE, f"settings: {e}") from e
        return self._settings

    @property
    def index(self) -> Index:
        if self._index is None:
            self._index = Index(self.settings.home)
        return self._index

    def cas_for(self, project_id: str) -> CAS:
        """The project's CAS; puts are recorded in its index (the same wiring as `api.create_app`)."""
        root = self.index.project_root(project_id) or self.settings.projects_dir / project_id

        def record(key: str, ext: str, nbytes: int) -> None:
            with contextlib.suppress(Exception):  # the index is a cache: the object is safe in the folder
                self.index.record_asset(project_id, key, ext, nbytes)

        return CAS(root / "assets", on_put=record)

    def pipeline(self, engine: Any = None) -> Pipeline:
        return Pipeline(
            settings=self.settings,
            index=self.index,
            cas_for=self.cas_for,
            engine=engine,
            sleep=(lambda _s: None) if engine is not None else self.sleep,
        )


def resolve_project(session: Session, ref: str) -> Path:
    """A project folder from a path or a project id."""
    p = Path(ref)
    if (p / "project.json").is_file():
        return p.resolve()
    if PROJECT_ID_RE.match(ref):
        root = session.index.project_root(ref) or session.settings.projects_dir / ref
        if (root / "project.json").is_file():
            return Path(root).resolve()
    raise CliExit(
        EXIT_USAGE, f"{ref}: not a PLATE project folder (no project.json) and not a known project id"
    )


@contextmanager
def held(project_dir: Path, wait_s: float = LOCK_WAIT_S) -> Iterator[Lock]:
    """The project's writer lock, retried for a few seconds (a running daemon holds it for moments)."""
    deadline = time.monotonic() + wait_s
    lock = Lock(project_dir)
    while True:
        try:
            lock.acquire()
            break
        except ProjectLocked as e:
            if time.monotonic() >= deadline:
                raise CliExit(EXIT_ERROR, str(e)) from e
            time.sleep(0.2)
    try:
        yield lock
    finally:
        lock.release()


def usd(x: float | None) -> str:
    return "?" if x is None else f"{x:.4f} USD"


# ---------------------------------------------------------------------------- plate run


def _load_recipe(ref: str) -> RunnableRecipe:
    try:
        recipe = recipes.load_runnable(ref)
    except RecipeError as e:
        raise CliExit(EXIT_USAGE, f"recipe: {e}") from e
    if recipe.kind not in STILL_KINDS:
        raise CliExit(
            EXIT_USAGE,
            f"recipe {recipe.id} is a {recipe.kind} recipe: video runs from the Motion node "
            "(CropPair / Move policy), not from `plate run`",
        )
    return recipe


def _parse_params(items: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise CliExit(EXIT_USAGE, f"--param {item!r}: expected name=value")
        try:
            out[key.strip()] = json.loads(raw)
        except ValueError:
            out[key.strip()] = raw
    return out


def _engine_choice(session: Session, requested: str, fake: CliFakeEngine | None) -> str:
    """The engine id the run is quoted on. The fake engine replaces whatever the recipe names; a vendor needs
    PLATE_LIVE=1 (decision 8: nothing leaves by accident)."""
    if fake is not None:
        if requested != FAKE_ENGINE_ID:
            session.say(f"PLATE_FAKE_ENGINE is set: the in-process fake engine stands in for {requested}")
        return FAKE_ENGINE_ID
    if requested == FAKE_ENGINE_ID:
        raise CliExit(EXIT_USAGE, "the fake engine needs PLATE_FAKE_ENGINE=1")
    if session.env.get("PLATE_LIVE") != "1":
        raise CliExit(
            EXIT_REFUSED,
            f"{requested} is a vendor engine: set PLATE_LIVE=1 to let `plate run` send a request "
            "(the quote is still printed and approved first); PLATE_FAKE_ENGINE=1 runs it on the fake engine",
        )
    return requested


def _create_project(session: Session, root: Path, project_id: str, args: argparse.Namespace) -> Project:
    if args.allow_grades:
        grades = [g.strip().upper() for g in args.allow_grades.split(",") if g.strip()]
    else:  # D (unknown terms, public media) is opt-in
        grades = ["C0", "C1"] if args.nda else ["C0", "C1", "C2"]
    custody = {"nda": bool(args.nda), "allowed_grades": grades}
    known = session.index.project_root(project_id)
    if known is not None and (known / "project.json").is_file() and known.resolve() != root.resolve():
        raise CliExit(
            EXIT_USAGE,
            f"project id {project_id!r} is already the project at {known}: run against that folder "
            "(one id, one folder per machine)",
        )
    try:
        project = Project.create(root, project_id, custody, now=utcnow())
    except (ProjectError, FileExistsError, ValueError) as e:
        raise CliExit(EXIT_USAGE, f"cannot create the project at {root}: {e}") from e
    session.index.set_project_root(project_id, project.root, project.custody)
    session.say(f"created project {project_id} at {project.root} (custody {project.custody})")
    return project


def _existing_project(session: Session, ref: str) -> Project | None:
    root = Path(ref)
    if not (root / "project.json").is_file() and PROJECT_ID_RE.match(ref):
        root = session.index.project_root(ref) or session.settings.projects_dir / ref
    if not (root / "project.json").is_file():
        return None
    try:
        return Project.load(root)
    except (ProjectError, FileNotFoundError) as e:
        raise CliExit(EXIT_USAGE, f"{ref}: {e}") from e


def _open_project(session: Session, args: argparse.Namespace, prepared: Any | None) -> Project:
    """The project folder: the one that exists, else a new one made from the packet's project id."""
    project = _existing_project(session, args.project)
    if project is None:
        if prepared is None:
            raise CliExit(
                EXIT_USAGE, f"{args.project}: no project here; pass --packet <folder> to create one"
            )
        project = _create_project(session, Path(args.project).resolve(), prepared.project_id, args)
    if prepared is not None and prepared.project_id != project.project_id:
        raise CliExit(
            EXIT_USAGE,
            f"packet {prepared.packet_id} belongs to project {prepared.project_id!r}, "
            f"but {project.root} is project {project.project_id!r}",
        )
    session.index.set_project_root(project.project_id, project.root, project.custody)
    return project


def _ingest(session: Session, pipeline: Pipeline, project: Project, prepared: Any) -> str:
    pid = project.project_id
    with session.index.project(pid) as conn:
        status, rec = begin_ingest(
            conn, prepared.path, sender={"name": "plate-cli", "version": __version__}, prepared=prepared
        )
        if status == "ingesting" or rec.get("status") == "ingesting":
            cas_copy = session.settings.cas_copy
            rec = finish_ingest(conn, session.cas_for(pid), prepared.packet_id, cas_copy=cas_copy)
    if rec["status"] != "ingested":
        raise CliExit(
            EXIT_USAGE, f"packet {prepared.packet_id} did not ingest: {rec.get('error') or rec['status']}"
        )
    pipeline.register_packet(pid, prepared.packet_id)
    session.say(f"packet {prepared.packet_id} ingested from {prepared.path}")
    return prepared.packet_id


def _known_packet(session: Session, project: Project, packet_id: str) -> bool:
    with session.index.project(project.project_id) as conn:
        row = conn.execute("SELECT status FROM packets WHERE packet_id = ?", (packet_id,)).fetchone()
    return row is not None and row["status"] == "ingested"


def _packet_in_project(
    session: Session, pipeline: Pipeline, project: Project, ref: str, prepared: Any | None
) -> str:
    """Ingest `--packet <dir>`, or find `--packet <id>` in the project (rebuilding a stale index once)."""
    if prepared is not None:
        try:
            return _ingest(session, pipeline, project, prepared)
        except PacketConflict as e:
            raise CliExit(EXIT_USAGE, str(e)) from e
    if not _known_packet(session, project, ref):
        with held(project.root):
            session.index.rebuild_from_project(project.root)
    if not _known_packet(session, project, ref):
        raise CliExit(
            EXIT_USAGE,
            f"packet {ref!r} is neither a packet folder nor an ingested packet of {project.project_id}",
        )
    return ref


def _mask_ids(recipe: RunnableRecipe, pv: PacketView) -> list[str] | None:
    """Legend ids of the classes the recipe edits, minus what the packet locks and what its policy does not
    list as editable (the stricter side wins); None when the recipe names no class."""
    classes = [str(c) for c in (recipe.mask.get("editable_classes") or [])]
    if not classes:
        return None
    entries = (pv.legend or {}).get("entries") or []
    locked, editable = set(pv.locked_ids()), set(pv.editable_ids())
    ids = [
        str(e["id"])
        for e in entries
        if e.get("class") in classes
        and str(e["id"]) not in locked
        and (not editable or str(e["id"]) in editable)
    ]
    if not ids:
        raise CliExit(
            EXIT_USAGE,
            f"recipe {recipe.id} edits {classes}, but this packet has no editable id of those classes "
            "(its policy locks them or its legend lacks them)",
        )
    return ids


def _full_frame_mask_key(pv: PacketView, cas: CAS) -> str:
    import cv2
    import numpy as np

    h, w = pv.plate16().shape[:2]
    ok, buf = cv2.imencode(".png", np.full((h, w), 255, np.uint8))
    if not ok:
        raise CliExit(EXIT_ERROR, "could not encode the full-frame mask")
    return cas.put_bytes(bytes(buf), "png")


def _router_check(
    session: Session, recipe: RunnableRecipe, pv: PacketView, shot_class: str
) -> dict[str, Any] | None:
    """The recipe's `router_check` before any spend: the shot class it requires and, on a Tier A packet, the
    render-it router's detail verdict. Advisory prints; PLATE_ROUTER_MODE=blocking refuses."""
    check = recipe.router_check
    if not check:
        return None
    from plate_gates import router_math

    want = check.get("require_shot_class")
    if want and shot_class != want:
        raise CliExit(
            EXIT_USAGE,
            f"recipe {recipe.id} only runs on {want!r} shots and this one is {shot_class!r} (--shot-class): "
            "a wide or elevated frame is a render, not an AI re-photograph",
        )
    try:
        mode = router_math.router_mode(session.env.get("PLATE_ROUTER_MODE"))
    except ValueError as e:
        raise CliExit(EXIT_USAGE, str(e)) from e
    verdict = "unknown"
    numbers: dict[str, Any] = {}
    note = "the router needs a Tier A packet (Z buffer and ids); a human decides"
    if pv.tier == "A":
        try:
            _edges, ctx = pv.edges() or (None, None)
            elements = {e["element"]: e for e in pv.files["beauty_exr"]["elements"]}
            detail = router_math.detail_resolvability(
                ctx["z"],
                ctx["ids"],
                ctx["legend"],
                ctx["camera"]["intrinsics_px"],
                ctx["units"],
                bg_value=elements["zdepth"].get("background_value"),
            )
            whole_frame = (recipe.mask or {}).get("region", "none") == "none"
            verdict = detail["verdict"]
            if verdict == "vray_only" or (whole_frame and verdict == "ai_by_region"):
                verdict = "render_it"
            numbers = {"D_px_median": detail.get("D_px_median"), "detail_verdict": detail["verdict"]}
            note = f"median expected detail {detail.get('D_px_median')} px -> {detail['verdict']}"
        except Exception as e:  # the router must never be the reason an advisory run cannot start
            note = f"could not measure detail resolvability ({type(e).__name__}: {e}); a human decides"
    session.say(f"router ({mode}): {verdict}; {note}")
    if mode == "blocking" and verdict in (check.get("refuse_verdicts") or []):
        raise CliExit(EXIT_USAGE, f"the router says {verdict}: render this shot; nothing was quoted")
    return {"mode": mode, "verdict": verdict, **numbers}


def _graph_for(
    project: Project,
    packet_id: str,
    pv: PacketView,
    recipe: RunnableRecipe,
    engine_id: str,
    mask: dict[str, Any] | None,
) -> str:
    """Add the Plate (once per packet), an optional Mask and the recipe's Edit node; returns the Edit id.
    `mask` is the Mask node's fields (`params.ids` or `outputHash`); None leaves the packet's policy to decide."""
    graph = json.loads(json.dumps(project.graph))
    nodes, edges = graph["nodes"], graph["edges"]
    plate = next((n for n in nodes if n.get("kind") == "Plate" and n.get("packetId") == packet_id), None)
    if plate is None:
        plate = {
            "id": f"plate-{packet_id[-8:].lower()}",
            "kind": "Plate",
            "packetId": packet_id,
            "tier": pv.tier,
            "label": "Plate",
        }
        nodes.append(plate)
    src = plate["id"]
    tag = recipe.id.replace("_", "-")
    edit_id = f"edit-{tag}"
    if mask is not None:
        mask_id = f"mask-{tag}"
        nodes[:] = [n for n in nodes if n.get("id") != mask_id] + [
            {"id": mask_id, "kind": "Mask", "label": "Mask", **mask}
        ]
        edges[:] = [e for e in edges if e.get("target") != mask_id] + [
            {"id": f"{src}->{mask_id}", "source": src, "target": mask_id}
        ]
        src = mask_id
    edit = {
        "id": edit_id,
        "kind": "Edit",
        "label": recipe.title,
        "params": {"engine": engine_id, "region_crop": (recipe.mask or {}).get("region") == "mask_bbox"},
    }
    nodes[:] = [n for n in nodes if n.get("id") != edit_id] + [edit]
    edges[:] = [e for e in edges if e.get("target") != edit_id] + [
        {"id": f"{src}->{edit_id}", "source": src, "target": edit_id}
    ]
    problems = canvas_rules.check_nodes(graph) or canvas_rules.validate_graph(graph)
    if problems:
        raise CliExit(
            EXIT_USAGE, f"canvas rules refuse the graph: {problems[0]['code']}: {problems[0]['message']}"
        )
    with held(project.root):
        project.save_graph(graph)
    return edit_id


def _show_quote(session: Session, run: Any, quote: Any, n: int) -> None:
    route = run.route_dict
    session.say(f"quote {quote.id} for run {run.id}")
    session.say(
        f"  engine {route.get('engine_id')} via {route.get('route_id') or route.get('provider')} "
        f"({quote.provider}, {quote.region}), model {quote.model_version or quote.model}"
    )
    each = f"price {usd(quote.price_usd)}" + (f" each, {n} x = {usd(quote.price_usd * n)}" if n > 1 else "")
    session.say(
        f"  custody {quote.custody_grade}, {each} ({quote.price_status}), eta {quote.eta_s:.0f} s, "
        f"expires {iso(quote.expires_utc)}"
    )


def _next_attempt(session: Session, project_id: str, node_id: str, packet_id: str) -> int:
    """A re-run is a new attempt (the idempotency key includes it), so it is a new request, not a replay."""
    with session.index.project(project_id) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt), 0) FROM runs WHERE node_id = ? AND packet_id = ?",
            (node_id, packet_id),
        ).fetchone()
    return int(row[0]) + 1


def _approved(session: Session, how: str, total: float, n: int) -> bool:
    if how == "auto":
        return True
    answer = session.ask(f"approve a spend of {usd(total)}{f' ({n} runs)' if n > 1 else ''}? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def cmd_run(session: Session, args: argparse.Namespace) -> int:
    recipe = _load_recipe(args.recipe)
    fake = fake_engine_from_env(session.env)
    engine_id = _engine_choice(session, args.engine or recipe.engine, fake)
    n = args.n if args.n is not None else int(recipe.params.get("n") or 1)
    if n < 1:
        raise CliExit(EXIT_USAGE, "--n must be at least 1")
    if args.envelope is not None and args.envelope <= 0:
        raise CliExit(EXIT_USAGE, "--envelope must be a positive USD amount")
    shot_class = args.shot_class or recipe.shot_class
    pipeline = session.pipeline(fake)

    prepared = None
    if Path(args.packet).is_dir():
        try:
            prepared = prepare_ingest(Path(args.packet).resolve())
        except PacketInvalid as e:
            raise CliExit(EXIT_USAGE, f"packet {args.packet}: {'; '.join(e.errors[:5])}") from e
    project = _open_project(session, args, prepared)
    pid = project.project_id
    packet_id = _packet_in_project(session, pipeline, project, args.packet, prepared)
    pv = pipeline.packet(pid, packet_id)
    _router_check(session, recipe, pv, shot_class)

    sheet = ProjectSheet.from_dict((project.doc.get("settings") or {}).get("project_sheet"))
    prompt = recipes.assemble_runnable(recipe, sheet)
    left = recipes.unfilled_count(prompt)
    if left:
        session.warn(
            f"{left} [TOKEN](s) in the prompt are unfilled: set settings.project_sheet in project.json"
        )

    mask: dict[str, Any] | None = None
    ids = _mask_ids(recipe, pv)
    if ids is not None:
        mask = {"params": {"ids": ids}}
        session.say(f"mask: {', '.join(recipe.mask['editable_classes'])} -> {len(ids)} ids")
    elif not recipe.mask.get("lock_rest", True):
        mask = {"outputHash": _full_frame_mask_key(pv, session.cas_for(pid))}
        session.say("mask: the whole frame (the packet's locked ids stay locked)")
    edit_id = _graph_for(project, packet_id, pv, recipe, engine_id, mask)

    params = _parse_params(args.param)
    manifest = pipeline.manifests.get(engine_id)
    declared = {p.name for p in manifest.params} if manifest is not None else set()
    if recipe.negative_prompt and "negative_prompt" in declared and "negative_prompt" not in params:
        params["negative_prompt"] = recipe.negative_prompt
    elif recipe.negative_prompt and manifest is not None and "negative_prompt" not in declared:
        session.say(
            f"note: {engine_id} has no negative_prompt field, so the recipe's negative prompt is not sent"
        )
    if recipe.params.get("repair_pass"):
        session.say(
            f"note: the {recipe.params['repair_pass']!r} repair pass is a follow-up run, not part of this one"
        )

    body = {
        "engine_id": engine_id,
        "packet_id": packet_id,
        "prompt": prompt,
        "params": params,
        "attempt": _next_attempt(session, pid, edit_id, packet_id),
        "region_crop": recipe.mask.get("region") == "mask_bbox",
    }
    with session.index.project(pid) as conn:
        run, quote = pipeline.quote_node(conn, pid, edit_id, body)
        runs = [run]
        if n > 1:
            runs = fan_out(conn, run.id, n, now=utcnow())
            for r in runs[1:]:
                session.index.remember(r.id, pid)
                pipeline.after(conn, pid, r.id, RunState.DRAFT)
        _show_quote(session, run, quote, n)
        envelope_id = None
        if args.envelope is not None:
            envelope_id = create_envelope(
                conn,
                owner="user:cli",
                max_total_usd=args.envelope,
                max_per_run_usd=args.envelope,
                ttl_s=ENVELOPE_TTL_S,
                now=utcnow(),
            ).id
            session.index.remember(envelope_id, pid)
            session.say(f"  envelope {envelope_id}: at most {usd(args.envelope)} for this run")
        try:
            if not _approved(session, args.approve, quote.price_usd * n, n):
                raise CliExit(EXIT_REFUSED, "not approved: nothing was sent")
            approved = pipeline.approve(conn, pid, run.id, args.approve, envelope_id, reason="plate run")
        except (CliExit, CustodyViolation, AutoApprovalDenied, EnvelopeExceeded, EnvelopeRequired):
            for r in runs:  # a refused quote is not left to expire in the index
                if get_run(conn, r.id).state is RunState.QUOTED:
                    transition(
                        conn, r.id, RunState.CANCELLED, actor="human", reason="not approved at the CLI"
                    )
                    pipeline.after(conn, pid, r.id, RunState.QUOTED)
            raise
        for r in approved:
            pipeline.queue(conn, pid, r.id)
    session.say(f"running {len(approved)} run(s) in this process ...")
    states = {r.id: pipeline.execute(r.id, pid) for r in approved}
    return _report_runs(session, pid, states)


def _report_runs(session: Session, pid: str, states: dict[str, RunState]) -> int:
    done: list[str] = []
    gate_fail: list[str] = []
    with session.index.project(pid) as conn:
        for rid in states:
            r = get_run(conn, rid)
            line = f"run {r.id} {r.state.value}"
            if r.gate_report:
                verdict = (r.gate_report.get("report") or {}).get("verdict") or r.gate_report.get("verdict")
                line += f", gates {verdict or ('pass' if r.gate_report.get('passed') else 'fail')}"
                if r.gate_score is not None:
                    line += f" (score {r.gate_score})"
            if r.gate_report_path:
                line += f", report {r.gate_report_path}"
            if r.state in (RunState.FAILED, RunState.REFUSED, RunState.CANCELLED, RunState.EXPIRED):
                line += f" ({r.error})"
            session.say(line)
            if r.state is RunState.DONE:
                done.append(r.id)
            elif r.state is RunState.GATE_FAIL:
                gate_fail.append(r.id)
    if done:
        session.say(f"next: plate export {done[0]} --to <folder> --format tiff16")
        return EXIT_OK
    if gate_fail:
        session.say(
            f'the gates failed; `plate export {gate_fail[0]} --override-reason "..."` ships it on the record'
        )
        return EXIT_GATE_FAIL
    return EXIT_ERROR


# ---------------------------------------------------------------------------- plate gate


def _read16(path: Path, what: str) -> Any:
    """A display image as uint16 RGB (an 8-bit image is promoted by x257, as the pipeline does for plates)."""
    import numpy as np

    from plate_pixels import imageio

    if not path.is_file():
        raise CliExit(EXIT_USAGE, f"{what}: {path} is not a file")
    try:
        img = imageio.read(path)
    except Exception as e:  # an unreadable file is the user's input problem, whatever decoder said so
        raise CliExit(EXIT_USAGE, f"{what}: cannot read {path} ({type(e).__name__}: {e})") from e
    if img.encoding != "srgb":
        raise CliExit(EXIT_USAGE, f"{what}: {path.name} is scene-linear; the gates score display images")
    scale = 65535.0 if img.bit_depth >= 16 else 255.0
    arr = np.clip(np.rint(img.rgb * scale), 0, scale).astype(np.uint16)
    return arr if scale == 65535.0 else arr * np.uint16(257)


def _packet_mask(pv: PacketView, shape: tuple[int, ...]) -> Any:
    """The mask the packet's policy would edit (editable ids minus locked ones), or None for the whole frame."""
    import numpy as np

    ids = pv.editable_ids()
    if not ids:
        return None
    mask = np.zeros(shape[:2], dtype=bool)
    for cid in ids:
        mask |= pv.id_mask(cid)
    for cid in pv.locked_ids():
        if cid not in ids:
            with contextlib.suppress(PipelineError):  # a locked id of a layer this packet does not carry
                mask &= ~pv.id_mask(cid)
    return mask if mask.any() else None


def cmd_gate(session: Session, args: argparse.Namespace) -> int:
    from . import gating

    plate = _read16(Path(args.plate), "plate")
    candidate = _read16(Path(args.candidate), "candidate")
    if plate.shape != candidate.shape:
        h, w = plate.shape[:2]
        ch, cw = candidate.shape[:2]
        raise CliExit(
            EXIT_USAGE,
            f"the plate is {w} x {h} and the candidate {cw} x {ch}: the gates never resize either "
            "(register or crop the candidate back onto the plate first)",
        )
    packet: Any = None
    legend = None
    tier = "C"
    mask = edges = crypto = None
    if args.packet:
        root = Path(args.packet)
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise CliExit(EXIT_USAGE, f"--packet {root}: no manifest.json there")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        res = (manifest.get("camera") or {}).get("resolution") or {}
        if res.get("width") and (res["width"], res["height"]) != (plate.shape[1], plate.shape[0]):
            raise CliExit(
                EXIT_USAGE,
                f"the packet is {res['width']} x {res['height']} but the plate is {plate.shape[1]} x {plate.shape[0]}",
            )
        pv = PacketView.load(str(manifest.get("packet_id") or root.name), root)
        tier, legend, packet = pv.tier, pv.legend, {"path": root, "manifest": manifest}
        try:
            mask = _packet_mask(pv, plate.shape)
            if tier == "A" and "beauty_exr" in pv.files:
                edges = pv.edges()
                crypto = pv.crypto("material") if legend else None
        except Exception as e:  # a packet whose EXR cannot be read must not be scored as if it had none
            raise CliExit(
                EXIT_USAGE, f"packet {root}: cannot read its G-buffer ({type(e).__name__}: {e})"
            ) from e
    report = gating.run_gates(
        plate,
        candidate,
        packet,
        legend,
        tier,
        {"id": args.engine or "cli", "version": None},
        mask=mask,
        edges=edges,
        crypto_material=crypto,
    )
    session.say(report.to_json().rstrip("\n"))
    if args.out:
        out = Path(args.out)
        write_json_atomic(out, report.model_dump(mode="json"), indent=1)
        session.warn(f"gate report written to {out}")
    failed = [c.metric_id for c in report.checks if c.status == "fail"]
    session.warn(
        f"gate verdict {report.verdict} (tier {tier}, {len(report.checks)} checks, {len(failed)} failed)"
    )
    return EXIT_GATE_FAIL if report.verdict == "fail" else EXIT_OK


# ---------------------------------------------------------------------------- plate export


def _locate(session: Session, run_id: str, project_ref: str | None) -> tuple[str, Path]:
    """(project id, project folder) of a run; `--project` registers a folder first and re-indexes it if the run
    is not in this machine's index yet."""
    if project_ref:
        project = Project.load(resolve_project(session, project_ref))
        session.index.set_project_root(project.project_id, project.root, project.custody)
        with session.index.project(project.project_id) as conn:
            known = conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not known:
            with held(project.root):
                session.index.rebuild_from_project(project.root)
        pid = project.project_id
    else:
        pid = session.index.locate_run(run_id)
        if pid is None:
            raise CliExit(
                EXIT_USAGE,
                f"run {run_id} is not in this machine's index; name its folder with --project <dir>",
            )
    return pid, Path(session.index.project_root(pid) or session.settings.projects_dir / pid)


def cmd_export(session: Session, args: argparse.Namespace) -> int:
    from .export import recorded_override

    pid, project_dir = _locate(session, args.run_id, args.project)
    pipeline = session.pipeline()
    with session.index.project(pid) as conn:
        try:
            run = get_run(conn, args.run_id)
        except RunNotFound as e:
            raise CliExit(EXIT_USAGE, f"run {args.run_id} is not in project {pid}") from e
        had_override = recorded_override(conn, run.id) is not None
        reason = args.override_reason
        try:
            done = export(
                run,
                args.format,
                Path(args.to),
                reason,
                conn=conn,
                cas=CAS(project_dir / "assets"),
                project_dir=project_dir,
                name=args.name,
            )
        except NotExportable as e:
            session.warn(f"not exported: {e}")
            return EXIT_GATE_FAIL if run.state is RunState.GATE_FAIL else EXIT_ERROR
        except ValueError as e:
            raise CliExit(EXIT_USAGE, str(e)) from e
        if done.overridden and not had_override:
            pipeline.ledger(pid, "override", run, by="human", note=done.override_reason)
        pipeline.ledger(
            pid,
            "export",
            run,
            data={"dest_name": done.path.name, "format": done.fmt, "overridden": done.overridden},
        )
    session.say(f"exported {done.path}")
    session.say(f"gate report {done.report_path}" + (f" (from {done.report_ref})" if done.report_ref else ""))
    if done.overridden:
        session.say(f"GATE_FAIL shipped on override: {done.override_reason}")
    session.say("lineage embedded in the file (plate:lineage)")
    return EXIT_OK


# ---------------------------------------------------------------------------- plate reindex / gc / pack


def cmd_reindex(session: Session, args: argparse.Namespace) -> int:
    root = resolve_project(session, args.project)
    try:
        with held(root):
            res = session.index.rebuild_from_project(root, verify=args.verify)
    except (RebuildError, ProjectError) as e:
        raise CliExit(EXIT_ERROR, f"reindex failed: {e}") from e
    counts = ", ".join(
        f"{k} {res[k]}" for k in ("assets", "packets", "runs", "events", "project_meta") if k in res
    )
    session.say(f"reindexed {res['project_id']} from {root}: {counts}")
    for problem in res["problems"]:
        session.warn(f"  problem: {problem}")
    return EXIT_OK


def _hex_keys(text: str, universe: set[str]) -> set[str]:
    return {m for m in re.findall(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", text) if m in universe}


def referenced_keys(session: Session, project: Project) -> set[str]:
    """Every CAS key something still points at: `project.json` and `runs.jsonl` (`CAS.refcount`), plus what only
    the index, the gate reports and `packets.json` hold (a run's staged PNG and mask, its raw result, copies of
    packet files)."""
    cas = CAS(project.assets_dir)
    counts = cas.refcount(project)
    keep = {k for k, n in counts.items() if n > 0}
    universe = set(counts)
    texts: list[str] = []
    for folder in (project.gates_dir,):
        if folder.is_dir():
            texts.extend(p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.json")))
    if project.packets_json.is_file():
        texts.append(project.packets_json.read_text(encoding="utf-8"))
    pid = project.project_id
    if session.index.db_path(pid).is_file():
        with session.index.project(pid) as conn:
            cols = "inputs, params, route, payload_key, result_key, normalized, gate_report, quote_json"
            for row in conn.execute(f"SELECT {cols} FROM runs"):
                texts.extend(str(v) for v in row if v)
            keep.update(
                r[0] for r in conn.execute("SELECT blake3 FROM blobs WHERE in_cas = 1") if r[0] in universe
            )
    for text in texts:
        keep |= _hex_keys(text, universe)
    return keep


def cmd_gc(session: Session, args: argparse.Namespace) -> int:
    root = resolve_project(session, args.project)
    project = Project.load(root)
    cas = CAS(project.assets_dir)
    cutoff = time.time() - max(args.min_age_hours, 0.0) * 3600.0
    keep = referenced_keys(session, project)
    doomed: list[tuple[str, Path, int]] = []
    kept_young = 0
    for key, _ext, path in cas.iter_objects():
        if key in keep:
            continue
        st = path.stat()
        if st.st_mtime > cutoff:
            kept_young += 1
            continue
        doomed.append((key, path, st.st_size))
    stale_tmp = (
        [p for p in sorted(cas.tmp.glob("*")) if p.is_file() and p.stat().st_mtime <= cutoff]
        if cas.tmp.is_dir()
        else []
    )
    freed = sum(size for _k, _p, size in doomed)
    verb = "would delete" if args.dry_run else "deleted"
    if not args.dry_run and (doomed or stale_tmp):
        with held(root):
            for _key, path, _size in doomed:
                path.unlink(missing_ok=True)
                with contextlib.suppress(OSError):  # a shard that still holds objects stays
                    path.parent.rmdir()
            for path in stale_tmp:
                path.unlink(missing_ok=True)
        if doomed and session.index.db_path(project.project_id).is_file():
            from .db import tx

            with session.index.project(project.project_id) as conn, tx(conn):
                conn.executemany("DELETE FROM assets WHERE hash = ?", [(k,) for k, _p, _s in doomed])
    session.say(
        f"gc {project.project_id}: {verb} {len(doomed)} unreferenced blob(s), {freed / 1e6:.2f} MB; "
        f"kept {len(keep)} referenced, {kept_young} unreferenced but younger than {args.min_age_hours:g} h"
        + (f"; {verb} {len(stale_tmp)} stale temp file(s)" if stale_tmp else "")
    )
    if args.dry_run:
        for key, path, size in doomed[:20]:
            session.say(f"  {key[:16]}  {size:>12,d} bytes  {path.name}")
        if len(doomed) > 20:
            session.say(f"  ... and {len(doomed) - 20} more")
    return EXIT_OK


def cmd_pack(session: Session, args: argparse.Namespace) -> int:
    root = resolve_project(session, args.project)
    project = Project.load(root)
    out = Path(args.out).resolve()
    if out.exists():
        raise CliExit(EXIT_USAGE, f"{out} already exists; pack never overwrites")
    limit = int(args.max_blob_mb * 1024 * 1024)
    skip_dirs = {".tmp"}
    skip_names = {"project.lock", "project.lock.json", ".plate.lock", ".plate.lock.json"}
    control = {"project.json", "packets.json", "runs.jsonl"}
    snapshot: dict[str, bytes] = {}
    with held(root):  # the small control files, in one consistent moment
        for rel in sorted(control):
            if (root / rel).is_file():
                snapshot[rel] = (root / rel).read_bytes()
        for path in sorted(project.gates_dir.glob("*")) if project.gates_dir.is_dir() else []:
            if path.is_file():
                snapshot[f"gates/{path.name}"] = path.read_bytes()
    files: list[tuple[str, Path]] = []
    omitted: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        temp = path.suffix == ".tmp" or path.name.endswith(".part")
        if (
            not path.is_file()
            or path.is_symlink()
            or temp
            or set(rel.parts) & skip_dirs
            or path.name in skip_names
        ):
            continue
        key = rel.as_posix()
        if key in snapshot or path.resolve() == out or key.startswith("gates/"):
            continue
        if rel.parts[0] == "assets" and not args.full and path.stat().st_size > limit:
            omitted.append({"path": key, "bytes": path.stat().st_size})
            continue
        files.append((key, path))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_sibling(out)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for key, data in snapshot.items():
                zf.writestr(key, data)
            for key, path in files:
                zf.write(path, key)
            zf.writestr(
                "pack_manifest.json",
                json.dumps(
                    {
                        "project_id": project.project_id,
                        "created_utc": iso(utcnow()),
                        "full": bool(args.full),
                        "blob_limit_bytes": None if args.full else limit,
                        "files": len(snapshot) + len(files),
                        "omitted_blobs": omitted,
                    },
                    indent=1,
                )
                + "\n",
            )
        replace_with_retry(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    session.say(
        f"packed {project.project_id}: {len(snapshot) + len(files)} file(s) into {out} "
        f"({out.stat().st_size / 1e6:.2f} MB); {len(omitted)} blob(s) over {args.max_blob_mb:g} MB left out"
        + ("" if not omitted else " (see pack_manifest.json; --full keeps them)")
    )
    return EXIT_OK


# ---------------------------------------------------------------------------- plate smoke

SMOKE_PROMPT = "relight to late golden hour, keep every edge and every material"
SMOKE_MAX_POLLS = 40
SMOKE_POLL_S = {"image": 3.0, "video": 5.0}
SMOKE_TIMEOUT_S = 12 * 60.0
# Recordings whose names say they are the unhappy path are the last choice when a request has several answers.
UNHAPPY = (
    "refus",
    "fail",
    "error",
    "boom",
    "moderat",
    "block",
    "busy",
    "down",
    "policy",
    "rai",
    "rate",
    "bad_",
)
ATLAS_KEY = (("ATLASCLOUD_KEY",),)


@dataclass(frozen=True)
class SmokeCase:
    """One adapter route with a recorded minimum-cost call. `contract` are the folders under
    `plate_engines/contract` its recordings live in, `prefer` name fragments that pick the right answer when
    several recordings share a path; `keys` are groups of environment variables of which one must be set for a
    live call. A vendor model id the manifest does not know yet (`unverified_p0`) is filled in for the fixtures
    only: the live smoke is where it gets confirmed."""

    engine_id: str
    provider: str
    contract: tuple[str, ...]
    prefer: tuple[str, ...] = ()
    keys: tuple[tuple[str, ...], ...] = ()
    fixture_model_id: str | None = None
    fixture_kwargs: Mapping[str, Any] = field(default_factory=dict)


SMOKE_CASES: tuple[SmokeCase, ...] = (
    SmokeCase("nano-banana-pro", "gemini", ("gemini",), ("edit_a",), (("GEMINI_API_KEY", "GOOGLE_API_KEY"),)),
    SmokeCase("gpt-image-2.5", "openai", ("openai",), ("edit_a",), (("OPENAI_API_KEY",),)),
    SmokeCase("flux-3-image", "bfl", ("bfl",), ("_a",), (("BFL_API_KEY",),)),
    SmokeCase(
        "seedream-5-pro", "atlas", ("atlas_image",), ("_a",), ATLAS_KEY, "bytedance/seedream-5-pro/edit"
    ),
    SmokeCase("seedance-2.5-i2v", "atlas", ("atlas_video",), ("seedance_i2v_a",), ATLAS_KEY),
    SmokeCase("seedance-2.5-r2v", "atlas", ("atlas_video",), ("r2v",), ATLAS_KEY),
    SmokeCase("kling-o3-4k", "atlas", ("atlas_video",), ("kling_o3",), ATLAS_KEY),
    SmokeCase("kling-v3-pro", "atlas", ("atlas_video",), ("kling_v3",), ATLAS_KEY),
    SmokeCase(
        "veo-3.1",
        "vertex",
        ("vertex",),
        ("veo_a",),
        (("VERTEX_ACCESS_TOKEN",), ("GOOGLE_CLOUD_PROJECT",)),
        fixture_kwargs={"project": "plate-test"},
    ),
    SmokeCase("seedance-2.5-i2v", "byteplus", ("byteplus",), ("i2v_a",), (("ARK_API_KEY",),)),
    SmokeCase("seedance-2.5-i2v", "wavespeed", ("wavespeed",), ("i2v_a",), (("WAVESPEED_API_KEY",),)),
)
SMOKE_CASE_BY_ROUTE = {(c.engine_id, c.provider): c for c in SMOKE_CASES}
FIXTURE_ENV_EXTRA = {"GOOGLE_CLOUD_PROJECT": "plate-test"}


class ReplayTransport(httpx.MockTransport):
    """Answers from the committed recordings by (method, host, path) and ignores the body, so the smoke can send
    its own image and still get the vendor's recorded answer. An unrecorded request raises `UnrecordedRequest`:
    nothing falls through to a socket. (The contract tests, not this, pin the exact bytes of each request.)"""

    def __init__(self, folders: Sequence[str], prefer: Sequence[str] = ()):
        from urllib.parse import parse_qs

        from plate_engines.adapters.transport import (
            CONTRACT_DIR,
            FixtureTransport,
            UnrecordedRequest,
            load_recordings,
        )

        self._parse_qs, self._build, self._unrecorded = parse_qs, FixtureTransport._build, UnrecordedRequest
        self.recordings = [r for f in folders for r in load_recordings(Path(CONTRACT_DIR) / f)]
        self.prefer = tuple(prefer)
        self.served: list[tuple[str, str, str, str]] = []
        self._count: dict[str, int] = {}
        super().__init__(self._answer)

    def _rank(self, rec: Any) -> tuple[int, bool, str]:
        name = rec.name.lower()
        pref = next((i for i, token in enumerate(self.prefer) if token in name), len(self.prefer))
        return pref, any(word in name for word in UNHAPPY), name

    def _answer(self, request: httpx.Request) -> httpx.Response:
        method, host, path = request.method.upper(), (request.url.host or "").lower(), request.url.path or "/"
        query = self._parse_qs(request.url.query.decode("ascii"), keep_blank_values=True)
        found = [
            r
            for r in self.recordings
            if (r.method, r.host, r.path) == (method, host, path) and (r.query is None or r.query == query)
        ]
        if not found:
            raise self._unrecorded(
                f"no recording for {method} {host}{path}; the smoke never touches the network"
            )
        rec = min(found, key=self._rank)
        n = self._count.get(rec.name, 0)
        self._count[rec.name] = n + 1
        self.served.append((method, host, path, rec.name))
        return self._build(rec.responses[min(n, len(rec.responses) - 1)])


def default_transport(case: SmokeCase, manifest: Any, route: Any, live: bool) -> httpx.BaseTransport:
    """Recorded fixtures, or the real network only when `--live` was asked for and PLATE_LIVE=1 says so."""
    if live:
        from plate_engines.adapters.transport import LiveTransport

        return LiveTransport()
    return ReplayTransport(case.contract, case.prefer)


@contextmanager
def fixture_env(case: SmokeCase) -> Iterator[None]:
    """Placeholder credentials for the adapter's key lookups while a fixture call runs (restored after)."""
    names = {group[0]: f"fixture-{group[0].lower()}" for group in case.keys}
    names.update({k: v for k, v in FIXTURE_ENV_EXTRA.items() if k in names})
    saved = {k: os.environ.get(k) for k in names}
    os.environ.update(names)
    try:
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def smoke_png() -> bytes:
    """The smoke image: the committed Blender fixture room when the repo has it, else a gradient (client
    imagery never enters the repo or a smoke)."""
    import cv2
    import numpy as np

    room = REPO_ROOT / "fixtures" / "packets" / "room_a" / "display.tif"
    if room.is_file():
        from plate_pixels import imageio

        rgb = np.clip(np.rint(imageio.read(room).rgb * 255.0), 0, 255).astype(np.uint8)
    else:
        ramp = np.linspace(0, 255, 640, dtype=np.uint8)[None, :, None]
        rgb = np.repeat(np.repeat(ramp, 360, axis=0), 3, axis=2)
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(rgb[..., ::-1]))
    if not ok:
        raise CliExit(EXIT_ERROR, "could not encode the smoke image")
    return bytes(buf)


def smoke_inputs(manifest: Any, png: bytes, still: Path) -> dict[str, Any]:
    """The smallest legal inputs for the engine: its plate-in image (or first frame) and a prompt. Never text-only."""
    names = {f.name: f for f in manifest.inputs}
    if manifest.kind in ("video_i2v", "video_r2v", "video_startend", "local_video"):
        ref = {"path": str(still)}
        if "reference_images" in names and names["reference_images"].required:
            return {"reference_images": [{**ref, "role": "first_frame"}], "prompt": SMOKE_PROMPT}
        key = "start_image" if "start_image" in names else "image"
        return {key: ref, "prompt": SMOKE_PROMPT}
    inputs: dict[str, Any] = {"image": {"bytes": png, "name": "smoke.png", "role": "ai_layer"}}
    if "prompt" in names:
        inputs["prompt"] = SMOKE_PROMPT
    return inputs


def _tier_rank(value: Any) -> int:
    """480P < 720P < 1080P < 1K < 2K < 4K: pixels-ish, enough to pick the smallest tier."""
    digits = int(re.sub(r"\D", "", str(value)) or 0)
    return digits * 1000 if str(value).upper().endswith("K") else digits


def min_cost_params(manifest: Any) -> dict[str, Any]:
    """The cheapest legal request: the shortest clip, the smallest tier of a resolution the engine already
    defaults to (an engine with no default, like Kling O3 4K, keeps its model's own), 1K for an image_size
    and the low quality setting where there is one."""
    params: dict[str, Any] = {}
    for p in manifest.params:
        tiers = [v for v in (p.enum or []) if re.match(r"^\d+[KkPp]?$", str(v))] if p.type == "enum" else []
        if p.name in ("duration", "duration_s") and p.range:
            params[p.name] = int(p.range[0]) if p.type == "int" else float(p.range[0])
        elif p.name == "resolution" and tiers and p.default is not None:
            params[p.name] = min(tiers, key=_tier_rank)
        elif p.name == "image_size" and tiers:
            params[p.name] = min(tiers, key=_tier_rank)
        elif p.name == "quality" and p.type == "enum" and "low" in (p.enum or []):
            params[p.name] = "low"
    return params


def adapter_for(manifest: Any, route: Any) -> type | None:
    """The concrete adapter class that speaks `manifest` on `route` (abstract bases are skipped)."""
    import importlib

    from .pipeline import ADAPTER_MODULES

    for name in ADAPTER_MODULES:
        try:
            module = importlib.import_module(f"plate_engines.adapters.{name}")
        except Exception:  # an adapter that cannot import is an adapter that is not there
            continue
        for obj in vars(module).values():
            if not isinstance(obj, type) or obj.__module__ != module.__name__ or inspect.isabstract(obj):
                continue
            ids = getattr(obj, "SUPPORTED_ENGINES", None) or getattr(obj, "ENGINE_IDS", None) or ()
            if manifest.id in ids and getattr(obj, "PROVIDER", None) == route.provider:
                return obj
    return None


@dataclass
class SmokeRow:
    engine_id: str
    route_id: str
    status: str  # ok | skipped | failed | stopped
    detail: str = ""
    custody: str | None = None
    price_usd: float | None = None
    job: str | None = None
    model: str | None = None
    result_bytes: int | None = None

    def line(self) -> str:
        head = f"{self.engine_id} via {self.route_id}: {self.status}"
        bits = []
        if self.price_usd is not None:
            bits.append(f"quote {usd(self.price_usd)}")
        if self.custody:
            bits.append(f"custody {self.custody}")
        if self.result_bytes is not None:
            bits.append(f"{self.result_bytes} bytes")
        return head + (f" ({self.detail})" if self.detail else "") + (f" [{', '.join(bits)}]" if bits else "")


def _drive(
    adapter: Any, inputs: dict[str, Any], params: dict[str, Any], live: bool, sleep: Callable[[float], None]
) -> tuple[str, int | None, str]:
    """submit -> poll until done -> fetch. Returns (job id, result bytes or None, note). A result URL the
    recordings have no download for is a note, not a failure (the submit and the poll are what they pin)."""
    from plate_engines.adapters.base import Staged
    from plate_engines.adapters.transport import UnrecordedRequest

    job = adapter.submit(Staged(inputs=inputs, params=adapter.validate_params(params), size=None))
    kind = "video" if adapter.manifest.kind.startswith(("video", "local_video")) else "image"
    deadline = time.monotonic() + SMOKE_TIMEOUT_S
    status = None
    for _ in range(SMOKE_MAX_POLLS if not live else 10_000):
        status = adapter.poll(job)
        if status.state in ("done", "failed", "refused"):
            break
        if live:
            if time.monotonic() > deadline:
                raise TimeoutError(f"job {job} did not finish within {SMOKE_TIMEOUT_S / 60:.0f} minutes")
            sleep(SMOKE_POLL_S[kind])
    if status is None or status.state != "done":
        raise RuntimeError(
            f"job {job} ended {getattr(status, 'state', 'unfinished')}: {getattr(status, 'message', '')}"
        )
    if not status.outputs:
        raise RuntimeError(f"job {job} is done but names no output")
    try:
        data = adapter.fetch(status.outputs[0])
    except UnrecordedRequest:
        return job, None, "result download not recorded"
    if not data:
        raise RuntimeError(f"job {job}: the download was empty")
    return job, len(data), ""


def cmd_smoke(
    session: Session,
    args: argparse.Namespace,
    transport_factory: Callable[[SmokeCase, Any, Any, bool], httpx.BaseTransport] | None = None,
) -> int:
    from plate_engines.loader import load_manifests

    live = bool(args.live)
    if live:
        if session.env.get("PLATE_LIVE") != "1":
            raise CliExit(EXIT_REFUSED, "smoke --live sends real requests: set PLATE_LIVE=1 to allow it")
        if args.budget_usd is None or not args.budget_usd > 0:
            raise CliExit(EXIT_REFUSED, "smoke --live needs an explicit --budget-usd greater than 0")
    elif args.budget_usd is not None:
        session.warn("--budget-usd only matters with --live; this is the fixture smoke")
    budget = float(args.budget_usd) if live else None
    manifests = load_manifests()
    wanted = [e.strip() for e in (args.engines or "").split(",") if e.strip()]
    unknown = [e for e in wanted if e not in manifests]
    if unknown:
        raise CliExit(EXIT_USAGE, f"unknown engine(s) {unknown}; the manifests define {sorted(manifests)}")
    factory = transport_factory or default_transport
    png = smoke_png()
    rows: list[SmokeRow] = []
    running = 0.0
    stopped = False
    mode = "live" if live else "fixture"
    session.say(
        f"{mode} smoke"
        + (f": budget {usd(budget)}, PLATE_LIVE=1" if live else ": recorded vendor fixtures, no network")
    )
    with tempfile.TemporaryDirectory(prefix="plate-smoke-") as tmp_name:
        tmp = Path(tmp_name)
        still = tmp / "smoke_still.png"
        still.write_bytes(png)
        for manifest in sorted(manifests.values(), key=lambda m: m.id):
            if wanted and manifest.id not in wanted:
                continue
            for route in manifest.routes:
                if route.status != "active":
                    continue
                row = SmokeRow(manifest.id, route.route_id, "skipped")
                rows.append(row)
                case = SMOKE_CASE_BY_ROUTE.get((manifest.id, route.provider))
                cls = adapter_for(manifest, route)
                if stopped:
                    row.detail = "not run: the budget was reached"
                elif route.provider == "local" or route.upload_visibility == "local":
                    row.detail = "local route: a GPU model with a stub, nothing to send"
                elif cls is None:
                    row.detail = "no adapter speaks this route"
                elif case is None:
                    row.detail = "no recorded minimum-cost call for this adapter yet"
                    if manifest.model_id is None and route.model_slug is None:
                        row.detail += "; the manifest has no vendor model id yet (flag unverified_p0)"
                elif live and (missing := [g for g in case.keys if not any(session.env.get(v) for v in g)]):
                    row.detail = "set " + " and ".join("/".join(g) for g in missing)
                elif not live and manifest.model_id is None and case.fixture_model_id is None:
                    row.detail = "the manifest has no vendor model id yet (flag unverified_p0)"
                else:
                    stopped = _smoke_one(
                        session,
                        row,
                        case,
                        cls,
                        manifest,
                        route,
                        png,
                        still,
                        tmp,
                        live,
                        budget,
                        running,
                        factory,
                    )
                    if row.status == "ok" and live and row.price_usd is not None:
                        running += row.price_usd
                    continue  # _smoke_one printed its own row
                session.say(row.line())
    ok = sum(1 for r in rows if r.status == "ok")
    failed = sum(1 for r in rows if r.status == "failed")
    if args.out:
        write_json_atomic(
            Path(args.out),
            {
                "mode": mode,
                "budget_usd": budget,
                "quoted_total_usd": round(running, 6),
                "rows": [r.__dict__ for r in rows],
            },
            indent=1,
        )
    if live:
        session.say(
            f"live smoke: {ok} adapters ok, {failed} failed; quoted total {usd(running)} of {usd(budget)}"
        )
        if stopped:
            return EXIT_REFUSED
        return EXIT_ERROR if failed else EXIT_OK
    session.say(f"fixture smoke: {ok} adapters ok" + (f", {failed} failed" if failed else ""))
    return EXIT_ERROR if failed else EXIT_OK


def _smoke_one(
    session: Session,
    row: SmokeRow,
    case: SmokeCase,
    cls: type,
    manifest: Any,
    route: Any,
    png: bytes,
    still: Path,
    tmp: Path,
    live: bool,
    budget: float | None,
    running: float,
    factory: Callable[[SmokeCase, Any, Any, bool], httpx.BaseTransport],
) -> bool:
    """One adapter route: quote, (live: print custody and check the budget), submit, poll, fetch. Fills `row`,
    prints it, and returns True when the budget stopped the smoke."""
    from plate_engines.adapters.image_common import ModelIdUnknown

    use = (
        manifest.model_copy(update={"model_id": case.fixture_model_id})
        if (not live and case.fixture_model_id)
        else manifest
    )
    kwargs: dict[str, Any] = {} if live else dict(case.fixture_kwargs)
    if "staging_dir" in inspect.signature(cls).parameters:
        kwargs["staging_dir"] = tmp / f"stage-{manifest.id}-{route.route_id}"
    try:
        with contextlib.nullcontext() if live else fixture_env(case):
            adapter = cls(use, route, factory(case, manifest, route, live), **kwargs)
            inputs = smoke_inputs(use, png, still)
            params = min_cost_params(use)
            if live and hasattr(adapter, "model_name"):
                adapter.model_name()  # raises ModelIdUnknown: a live call never sends a placeholder
            quote = adapter.quote(inputs, params)
            row.custody, row.price_usd, row.model = quote.custody_grade, quote.price_usd, quote.model_version
            if live:
                assert budget is not None
                session.say(
                    f"{manifest.id} via {route.route_id}: custody {quote.custody_grade} "
                    f"(declared {route.custody_grade}, {route.custody_status}, uploads {route.upload_visibility}), "
                    f"quote {usd(quote.price_usd)}, running {usd(running)} -> {usd(running + quote.price_usd)} of {usd(budget)}"
                )
                if running + quote.price_usd > budget + 1e-9:
                    row.status, row.detail = (
                        "stopped",
                        "this call would take the running total past the budget",
                    )
                    session.say(f"  stopping: {row.detail}")
                    return True
                session.say("  sending ...")
            job, size, note = _drive(adapter, inputs, params, live, session.sleep)
            row.job, row.result_bytes, row.status = job, size, "ok"
            row.detail = note
    except ModelIdUnknown:  # nothing was sent: the vendor model id still has to be confirmed
        row.status = "skipped"
        row.detail = "no vendor model id yet (flag unverified_p0): confirm it, then set route.model_slug"
    except Exception as e:  # one adapter's failure is a line in the report, not the end of the smoke
        row.status, row.detail = "failed", f"{type(e).__name__}: {str(e)[:200]}"
    session.say(row.line())
    return False


# ---------------------------------------------------------------------------- parser and entry point


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error; here 2 means a gate failure, so a usage error is 4."""

    def error(self, message: str) -> Any:
        self.print_usage(sys.stderr)
        raise CliExit(EXIT_USAGE, f"{self.prog}: {message}")


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="plate", description="PLATE command line: run, gate, export, reindex, gc, pack, smoke.")
    p.add_argument("--version", action="version", version=f"plate {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command", parser_class=_Parser)

    r = sub.add_parser("run", help="quote, approve and run a recipe on a packet")
    r.add_argument("recipe", help="a recipe id (night_relight_changelist) or a recipe .yaml file")
    r.add_argument("--project", required=True, help="the project folder (made from the packet if it is new)")
    r.add_argument(
        "--packet", required=True, help="a packet folder to ingest, or the id of an ingested packet"
    )
    r.add_argument("--engine", help="engine id, instead of the recipe's")
    r.add_argument("--n", type=int, help="best-of-N under one quote (default: the recipe's)")
    r.add_argument(
        "--approve", choices=("auto", "human"), default="human", help="human waits for y (default)"
    )
    r.add_argument("--envelope", type=float, metavar="USD", help="hard spending ceiling for this run")
    r.add_argument("--shot-class", choices=SHOT_CLASSES, help="what kind of shot this is (the router check)")
    r.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="an engine parameter")
    r.add_argument("--nda", action="store_true", help="a new project is NDA (C0 and C1 routes only)")
    r.add_argument(
        "--allow-grades", metavar="C0,C1,C2", help="grades a new project may use (default C0,C1,C2)"
    )

    g = sub.add_parser("gate", help="score a candidate against its plate")
    g.add_argument("plate")
    g.add_argument("candidate")
    g.add_argument("--packet", help="a packet folder: adds the G-buffer, material and invention gates")
    g.add_argument("--out", help="also write the report JSON here")
    g.add_argument("--engine", help="engine label for the report")

    e = sub.add_parser(
        "export", help="write a run's gated result as tiff16, png16 or exr, with its gate report"
    )
    e.add_argument("run_id")
    e.add_argument("--to", required=True, help="the output folder")
    e.add_argument("--format", choices=tuple(EXPORT_FORMATS), default="tiff16")
    e.add_argument(
        "--override-reason", help=f"why a GATE_FAIL take ships anyway ({MIN_OVERRIDE_REASON}+ characters)"
    )
    e.add_argument("--name", help="file name stem (default: the run id)")
    e.add_argument("--project", help="the project folder, if this machine's index does not know the run")

    x = sub.add_parser("reindex", help="rebuild this machine's index of a project from its folder")
    x.add_argument("project")
    x.add_argument("--verify", action="store_true", help="re-hash every asset")

    c = sub.add_parser("gc", help="delete CAS blobs nothing references")
    c.add_argument("project")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--min-age-hours", type=float, default=GC_GRACE_HOURS, help="keep newer blobs (default 1)")

    k = sub.add_parser("pack", help="zip a project folder (blobs over 50 MB left out unless --full)")
    k.add_argument("project")
    k.add_argument("--out", required=True, help="the .zip to write (never overwritten)")
    k.add_argument("--full", action="store_true")
    k.add_argument("--max-blob-mb", type=float, default=PACK_BLOB_LIMIT_MB)

    s = sub.add_parser(
        "smoke", help="every adapter's minimum-cost call: recorded fixtures, or --live with a budget"
    )
    s.add_argument("--live", action="store_true", help="real requests: needs PLATE_LIVE=1 and --budget-usd")
    s.add_argument("--budget-usd", type=float, help="stop before the running total passes this")
    s.add_argument("--engines", help="comma-separated engine ids (default: all)")
    s.add_argument("--out", help="also write the report JSON here")
    return p


COMMANDS: dict[str, Callable[[Session, argparse.Namespace], int]] = {
    "run": cmd_run,
    "gate": cmd_gate,
    "export": cmd_export,
    "reindex": cmd_reindex,
    "gc": cmd_gc,
    "pack": cmd_pack,
}
# Errors whose code is not 1, in order: the first matching class wins.
ERROR_CODES: tuple[tuple[type[BaseException], int], ...] = (
    (CustodyViolation, EXIT_REFUSED),
    (AutoApprovalDenied, EXIT_REFUSED),
    (EnvelopeExceeded, EXIT_REFUSED),
    (EnvelopeRequired, EXIT_REFUSED),
    (PacketInvalid, EXIT_USAGE),
    (PacketConflict, EXIT_USAGE),
    (RecipeError, EXIT_USAGE),
    (ProjectError, EXIT_USAGE),
    (FanOutError, EXIT_USAGE),
    (FileNotFoundError, EXIT_USAGE),
)
PIPELINE_RUNTIME_CODES = {"packet_changed", "packet_not_ready", "unpriceable"}


def _soften_stream(stream: Any) -> None:
    """Never die on a character the console cannot print."""
    with contextlib.suppress(AttributeError, ValueError, OSError):
        stream.reconfigure(errors="replace")


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    ask: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    transport_factory: Callable[[SmokeCase, Any, Any, bool], httpx.BaseTransport] | None = None,
) -> int:
    out, err = stdout or sys.stdout, stderr or sys.stderr
    if stdout is None:
        _soften_stream(out)
    if stderr is None:
        _soften_stream(err)

    def read(prompt: str) -> str:
        try:
            return input(prompt)
        except EOFError:
            return ""

    session = Session(
        env=os.environ if env is None else env,
        out=out,
        err=err,
        ask=ask or read,
        sleep=sleep or time.sleep,
    )
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
    except CliExit as e:
        print(e.message, file=err)
        return e.code
    except SystemExit as e:  # --help and --version
        return e.code if isinstance(e.code, int) else EXIT_OK
    try:
        if args.cmd == "smoke":
            return cmd_smoke(session, args, transport_factory)
        return COMMANDS[args.cmd](session, args)
    except CliExit as e:
        if e.message:
            print(f"plate {args.cmd}: {e.message}", file=err)
        return e.code
    except KeyboardInterrupt:
        print(f"plate {args.cmd}: interrupted (a run in flight resumes with the daemon)", file=err)
        return 130
    except PipelineError as e:
        print(f"plate {args.cmd}: {e}", file=err)
        return EXIT_ERROR if e.code in PIPELINE_RUNTIME_CODES else EXIT_USAGE
    except (QuoteExpired, QuoteNotFound, IllegalTransition, DuplicateRequest, ProjectLocked) as e:
        print(f"plate {args.cmd}: {e}", file=err)
        return EXIT_ERROR
    except Exception as e:
        if session.env.get("PLATE_DEBUG") == "1":
            raise
        for cls, code in ERROR_CODES:
            if isinstance(e, cls):
                print(f"plate {args.cmd}: {e}", file=err)
                return code
        print(f"plate {args.cmd}: {type(e).__name__}: {e} (PLATE_DEBUG=1 shows the traceback)", file=err)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
