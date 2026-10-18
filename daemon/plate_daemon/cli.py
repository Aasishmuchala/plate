"""`plate`: run / gate / export / reindex / gc / pack / smoke over the daemon's own modules (router plan T34), and
review / import / deliver / timer / spend over the wave 8 libraries (T78); cards / estimate / doctor / recipes and
the batch forms of run, export, review and spend are wave 9 (T103).

    plate run <recipe.yaml|id> --project <dir> (--packet <id|dir> | --packets a,b,c) [--engine ID] [--n N]
              [--approve auto|human] [--envelope USD] [--shot-class C] [--param K=V]
              [--nda] [--allow-grades C0,C1,C2]            (the last two only when the project is new)
    plate gate <plate> <candidate> [--packet DIR] [--out report.json]
    plate export <run_id> --to <dir> --format tiff16|png16|exr [--override-reason TEXT] [--name N] [--project DIR]
    plate export --runs r1,r2 --to <dir> [--format F] [--name-pattern P] [--project P]
    plate export --project P --accepted --to <dir> [--format F] [--name-pattern P]
    plate reindex <project> [--verify]
    plate gc <project> [--dry-run] [--min-age-hours H]
    plate pack <project> --out <zip> [--full] [--max-blob-mb MB]
    plate smoke [--live --budget-usd N --engines a,b] [--out report.json]
    plate review <project> (--runs r1,r2 | --all-exportable) [--watermark REVIEW] [--out dir] [--max-width PX]
                 [--title T] [--reply-to ADDR]
    plate import <file> --project <dir|id> --packet <id|dir> [--node ID] [--source higgsfield|runway|manual]
                 [--cost-usd X] [--note TEXT]
    plate deliver <run_id> --format prores4444|hevc|exr_sequence|av1_retag|fcpxml --to <dir>
                  [--name N] [--override-reason TEXT] [--project DIR]
    plate timer start|stop <deliverable_id> [--project P]     plate timer today [--project P]
    plate spend (<project> | --all-projects) [--since ISO] [--by engine|day|week]
    plate cards [--project P] [--task T] [--shot-class C]
    plate estimate --engine ID --task T --shot-class C (--count N | --seconds S) [--project P] [--param K=V]
    plate doctor
    plate recipes list [--project P]
    plate recipes save <recipe.yaml> --scope studio|project [--project P] [--overwrite]

`<project>` is a project folder or a project id this machine knows.

Run it as `python -m plate_daemon.cli`, as the `plate` console script (`[project.scripts]`, installed by a
`uv sync`), or through the shims `scripts/plate` and `scripts\\plate.cmd`. Nothing here talks to a daemon: the
pipeline runs in this process on the same per-machine index (`PLATE_HOME`) and the same project folders, so a
run made here shows up in the UI and the ledger like any other.

Exit codes: 0 ok, 1 an error that is not one of the next three (a failed run, a locked project, a bug),
2 a gate failure (`run` or `import` ended GATE_FAIL, `gate` scored `fail`, `export` refused a failed run),
3 a custody, budget, approval or delivery refusal (nothing was sent; `review` and `deliver` refuse a previs
take and a GATE_FAIL take without an override, `import` refuses a file from a vendor the project's custody
does not allow), 4 usage (bad arguments, an unknown run, a recipe that cannot run, and `timer` finding no daemon).

`review`, `import` and `deliver` are the daemon's `POST /projects/{id}/review`, `POST /projects/{id}/import` and
`POST /runs/{id}/deliver` run in this process (`review_bundle.export_review`, `imports.import_file`,
`routes.delivery.deliver_run`: the same guards, so a previs take or a GATE_FAIL take without an override is
refused here as it is there). `import` files a take made outside PLATE (Higgsfield, Runway, by hand) as a run:
it is gated against the packet, ledgered and exportable like any other, and prints its run id and verdict.
`timer` is the one command that talks to a running daemon (`POST /timer`, `GET /timer/today`, urllib, the
bearer token from the install's config, loopback addresses only; `PLATE_DAEMON_URL` or `PLATE_PORT` says
where). `spend` prints `Ledger.summarize` of the project's `runs.jsonl` per engine.

Wave 9 (T103), all of it over modules that already exist:

- `run --packets a,b,c` lays one chain per packet with `graph_build.chain_for_packet` (one column each), then
  quotes, asks and runs each packet under the same `--approve` rules; one `--envelope` is a ceiling for the whole
  command. It prints a line per packet with the run id and the verdict, and exits 2 if any run ended GATE_FAIL.
  A packet that is refused (custody, a cap, a no) or cannot be run is a line of its own and the others go on.
- `cards` is `GET /engines/cards` and `estimate` prices one cell: the engine's quote for `--count` outputs (or
  `--seconds` of video) divided by the cell's Wilson lower bound; with no card it says so and divides by 1.
  Without `--project` both read the ledgers of every project this machine knows.
- `export --runs` / `--accepted` is the batch export (`routes.export_batch.export_many`): each run goes through the
  single export's guards and one that cannot leave never stops the rest. `review --all-exportable` takes every
  DONE (or overridden GATE_FAIL) still of the project that the review guard lets through.
- `spend --by day|week` and `--all-projects` fold the ledgers with `routes.spend.spend_report`; the `cost per
  accepted` column shows once the window holds an accepted take (before that the table is the T78 one).
- `doctor` only looks (it always exits 0): git HEAD against origin/main, the Max startup script against the
  installed copy (config.json `startup_dir`, `PLATE_MAX_STARTUP_DIR`, else the default Max 2026 profile folder),
  the built UI, ffmpeg and ffprobe, the pixels worker and the token. `recipes` lists and saves runnable recipes.
- `smoke` knows the Magnific Precision v2 route as a live-only case (a 128 px PNG, an image expected back): the
  vendor's address and model id are unconfirmed and nothing is recorded, so the fixture smoke skips it with that
  reason and it does not count among the adapters ok.

`run` follows the doctrine rules in code: the packet's plate is the only starting point (no text-to-image), the
engine never sees more than egress prepared, and the quote is printed and approved before a byte leaves.
`--approve human` (the default) waits for `y`; `--approve auto` goes through the same cap, daily and unattended
checks as the daemon's auto approval and exits 3 when one says no; `--envelope USD` makes the spend a hard
ceiling either way. `--n N` runs a best-of-N group under one quote. A vendor engine needs `PLATE_LIVE=1` (without
it the transport is recorded fixtures and could only fail); `PLATE_FAKE_ENGINE=1` swaps in the daemon's own
`engine_fake.FakeEngine(echo=True)`, which hands back the plate egress prepared (a JSON object sets its flags:
`echo`, `shift_px`, `size`, `behaviour`, ...; unlike the daemon's reading, `echo` is on unless the object says
`false`, so a bare `{"shift_px": 6}` still scores real pixels). Recipes that make video (`motion_croppair`) are run from the
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
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

import httpx

from . import __version__, canvas_rules, graph_build, recipes
from .cas import CAS
from .config import PROJECT_ID_RE, ConfigError, Settings, host_port
from .custody import CustodyViolation
from .engine_fake import FALSE_WORDS, FLAG_NAMES, TRUE_WORDS, FakeEngine
from .export import FORMATS as EXPORT_FORMATS
from .export import MIN_OVERRIDE_REASON, NotExportable, export
from .fsutil import replace_with_retry, temp_sibling, write_json_atomic
from .imports import import_file
from .index import Index, RebuildError
from .ingest import PacketConflict, PacketInvalid, begin_ingest, finish_ingest, prepare_ingest
from .ledger import Ledger, LedgerError
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
from .review_bundle import DEFAULT_WATERMARK, MAX_WIDTH_LIMIT
from .states import RunState
from .timeutil import iso, parse_iso, utcnow
from .worker import FanOutError, fan_out

__all__ = [
    "CliExit",
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


def fake_engine_from_env(env: Mapping[str, str]) -> FakeEngine | None:
    """`PLATE_FAKE_ENGINE`: unset, 0 or false means the real engines; 1 (or true, yes, on) the daemon's
    `FakeEngine(echo=True)`, which hands back the PNG egress sent it so the gates score real pixels; a JSON
    object its flags (`echo: false` with a `size` equal to the plate is its noise, which is how a GATE_FAIL run
    is made). `echo` is on unless the object says otherwise, so `{"shift_px": 6}` is a six-pixel camera drift."""
    raw = (env.get("PLATE_FAKE_ENGINE") or "").strip()
    if raw.lower() in FALSE_WORDS:
        return None
    flags: dict[str, Any] = {}
    if raw.lower() not in TRUE_WORDS:
        try:
            flags = json.loads(raw)
        except ValueError as e:
            raise CliExit(EXIT_USAGE, f"PLATE_FAKE_ENGINE must be 1 or a JSON object of flags ({e})") from e
        if not isinstance(flags, dict):
            raise CliExit(EXIT_USAGE, "PLATE_FAKE_ENGINE must be 1 or a JSON object of flags")
    unknown = sorted(set(flags) - set(FLAG_NAMES))
    if unknown:
        raise CliExit(
            EXIT_USAGE, f"PLATE_FAKE_ENGINE: unknown flag(s) {unknown}; known: {sorted(FLAG_NAMES)}"
        )
    flags.setdefault("echo", True)
    try:
        return FakeEngine(**flags)
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


def _engine_choice(session: Session, requested: str, fake: FakeEngine | None) -> str:
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


def _check_same_project(prepared: Any, project: Project) -> None:
    if prepared.project_id != project.project_id:
        raise CliExit(
            EXIT_USAGE,
            f"packet {prepared.packet_id} belongs to project {prepared.project_id!r}, "
            f"but {project.root} is project {project.project_id!r}",
        )


def _open_project(session: Session, args: argparse.Namespace, prepared: Any | None) -> Project:
    """The project folder: the one that exists, else a new one made from the packet's project id."""
    project = _existing_project(session, args.project)
    if project is None:
        if prepared is None:
            raise CliExit(
                EXIT_USAGE,
                f"{args.project}: no project here; pass --packet <folder> (or --packets) to create one",
            )
        project = _create_project(session, Path(args.project).resolve(), prepared.project_id, args)
    if prepared is not None:
        _check_same_project(prepared, project)
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


def _packet_summary(pv: PacketView, packet_id: str, project_id: str) -> dict[str, Any]:
    """What `graph_build` reads of `GET /packets/{id}/summary`: the packet's tier and camera, and its legend
    entries with `editable` / `locked` resolved by the packet's policy (the route's own resolution)."""
    manifest = pv.manifest
    editable, locked = set(pv.editable_ids()), set(pv.locked_ids())
    entries = [
        {
            "id": e.get("id"),
            "kind": e.get("kind"),
            "name": e.get("name"),
            "class": e.get("class"),
            "editable": e.get("id") in editable,
            "locked": e.get("id") in locked,
        }
        for e in ((pv.legend or {}).get("entries") or [])
        if isinstance(e, dict)
    ]
    return {
        "packet_id": packet_id,
        "project_id": project_id,
        "shot_id": manifest.get("shot_id"),
        "tier": pv.tier,
        "camera": manifest.get("camera"),
        "legend": {"entries": entries},
    }


def _chain_template(recipe: RunnableRecipe, mask: dict[str, Any] | None) -> dict[str, Any]:
    """The chain `plate run` lays for one packet, as a `graph_build` template: Plate -> (Mask) -> Edit. The Edit
    takes the plate as its base image and the Mask, when there is one, as its mask."""
    nodes: list[dict[str, Any]] = [{"suffix": "plate", "kind": "Plate"}]
    edit_inputs = {"image": "$plate"}
    if mask is not None:
        entry: dict[str, Any] = {"suffix": "mask", "kind": "Mask", "inputs": {"source": "$packet"}}
        classes = [str(c) for c in (recipe.mask.get("editable_classes") or [])]
        if "params" in mask and classes:
            entry["params"] = {"classes": classes}  # graph_build picks the ids; _set_mask pins them
        nodes.append(entry)
        edit_inputs["mask"] = "mask"
    nodes.append(
        {
            "suffix": "edit",
            "kind": "Edit",
            "label": recipe.title,
            "params": {"region_crop": (recipe.mask or {}).get("region") == "mask_bbox"},
            "inputs": edit_inputs,
        }
    )
    return {"id": "cli_run", "title": recipe.title, "set": False, "nodes": nodes}


def _fields(node: dict[str, Any]) -> dict[str, Any]:
    """The dict a node's own fields live in: `data` for a node the UI saved, the node itself for one built here."""
    data = node.get("data")
    return data if isinstance(data, dict) else node


def _set_mask(node: dict[str, Any], mask: dict[str, Any]) -> None:
    """Put the Mask node's fields (`params.ids` or `outputHash`) on `node`, keeping what else it carries."""
    fields = _fields(node)
    params = dict(fields.get("params") or {})
    if "params" in mask:
        params["ids"] = list(mask["params"]["ids"])
        fields.pop("outputHash", None)
    else:
        params.pop("ids", None)
        fields["outputHash"] = mask["outputHash"]
    if params:
        fields["params"] = params
    else:
        fields.pop("params", None)


def _existing_chain(
    graph: dict[str, Any], packet_id: str, recipe: RunnableRecipe, wants_mask: bool
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """The Edit (and its Mask) an earlier `plate run` of this recipe left on this packet's Plate: a re-run is a
    new attempt of the same node, not a second chain. The Edit is told by its label (the recipe's title) and
    its Plate; a chain whose Mask is there or not differently from what this run needs is not reused."""
    g = canvas_rules.as_graph(graph)
    raw = {n.get("id"): n for n in graph["nodes"] if isinstance(n, dict)}
    kinds = {n["id"]: n.get("kind") for n in g.nodes}
    for node in g.nodes:
        if node.get("kind") != "Edit" or node.get("label") != recipe.title:
            continue
        if not any(p.get("packetId") == packet_id for p in canvas_rules.upstream_plates(g, node["id"])):
            continue
        masks = [
            e["source"] for e in g.edges if e["target"] == node["id"] and kinds.get(e["source"]) == "Mask"
        ]
        if bool(masks) == wants_mask:
            return raw[node["id"]], (raw[masks[0]] if masks else None)
    return None


def _graph_for(
    project: Project,
    packet_id: str,
    pv: PacketView,
    recipe: RunnableRecipe,
    engine_id: str,
    mask: dict[str, Any] | None,
) -> str:
    """Add the packet's chain (Plate once, an optional Mask and the recipe's Edit) with
    `graph_build.chain_for_packet`, one column right of what the graph holds; returns the Edit id. A recipe
    already run on this packet reuses its Edit and Mask, so the run is another attempt of the same node.
    `mask` is the Mask node's fields (`params.ids` or `outputHash`); None leaves the packet's policy to decide."""
    graph = json.loads(json.dumps(project.graph))
    found = _existing_chain(graph, packet_id, recipe, mask is not None)
    if found is not None:
        edit, mask_node = found
        fields = _fields(edit)
        fields["params"] = {
            **(fields.get("params") or {}),
            "engine": engine_id,
            "region_crop": (recipe.mask or {}).get("region") == "mask_bbox",
        }
        if mask is not None and mask_node is not None:
            _set_mask(mask_node, mask)
        edit_id = str(edit["id"])
    else:
        before = {str(n.get("id")) for n in graph["nodes"] if isinstance(n, dict)}
        try:
            graph = graph_build.chain_for_packet(
                graph,
                _packet_summary(pv, packet_id, project.project_id),
                template=_chain_template(recipe, mask),
                engine_id=engine_id,
                column=graph_build.next_column(graph),
            )
        except graph_build.GraphBuildError as e:
            raise CliExit(EXIT_USAGE, f"cannot lay the chain for {packet_id}: {e}") from e
        added = [n for n in graph["nodes"] if str(n.get("id")) not in before]
        edit = next(n for n in added if n.get("kind") == "Edit")
        if mask is not None:
            _set_mask(next(n for n in added if n.get("kind") == "Mask"), mask)
        edit_id = str(edit["id"])
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


@dataclass
class _PacketOutcome:
    """What one packet of a `plate run` came to: the runs it started, or why it started none."""

    ref: str
    packet_id: str | None = None
    states: dict[str, RunState] = field(default_factory=dict)
    error: str | None = None
    code: int | None = None


# What ends one packet of a `--packets` run without ending the others: a refusal (custody, a cap, a no), a packet
# that cannot be ingested or quoted, a run that cannot be recorded. Anything else is a bug and stops the command.
PACKET_ERRORS: tuple[type[BaseException], ...] = (
    CliExit,
    CustodyViolation,
    DuplicateRequest,
    EnvelopeRequired,  # EnvelopeExceeded and AutoApprovalDenied are subclasses
    IllegalTransition,
    PacketConflict,
    PacketInvalid,
    PipelineError,
    ProjectLocked,
    QuoteExpired,
    QuoteNotFound,
)


def _packet_refs(args: argparse.Namespace) -> list[str]:
    """`--packet X` or `--packets a,b,c` as a list of ids and folders (a repeat is run once)."""
    if bool(args.packet) == bool(args.packets):
        raise CliExit(EXIT_USAGE, "give --packet <id|folder>, or --packets a,b,c (not both)")
    raw = [args.packet] if args.packet else args.packets.split(",")
    refs = list(dict.fromkeys(r.strip() for r in raw if r.strip()))
    if not refs:
        raise CliExit(EXIT_USAGE, "--packets needs at least one packet id or folder")
    return refs


def _prepare_packet(ref: str) -> Any | None:
    """The validated packet folder `ref` names, or None when `ref` is the id of an ingested packet."""
    if not Path(ref).is_dir():
        return None
    try:
        return prepare_ingest(Path(ref).resolve())
    except PacketInvalid as e:
        raise CliExit(EXIT_USAGE, f"packet {ref}: {'; '.join(e.errors[:5])}") from e


@dataclass
class _RunPlan:
    """What every packet of one `plate run` shares."""

    args: argparse.Namespace
    pipeline: Pipeline
    project: Project
    recipe: RunnableRecipe
    engine_id: str
    shot_class: str
    n: int
    prompt: str
    params: Mapping[str, Any]
    multi: bool
    # the one envelope of a command that named --envelope, made with the first quote and used by every packet
    envelope_id: str | None = None


def _run_packet(
    session: Session, plan: _RunPlan, ref: str, prepared: Any | None, outcome: _PacketOutcome
) -> None:
    """One packet: ingest (or find) it, lay its chain, quote, ask, run. Fills `outcome.states`; a refusal or a
    problem is raised."""
    args, pipeline, project, recipe = plan.args, plan.pipeline, plan.project, plan.recipe
    engine_id, n = plan.engine_id, plan.n
    pid = project.project_id
    packet_id = _packet_in_project(session, pipeline, project, ref, prepared)
    outcome.packet_id = packet_id
    pv = pipeline.packet(pid, packet_id)
    _router_check(session, recipe, pv, plan.shot_class)

    mask: dict[str, Any] | None = None
    ids = _mask_ids(recipe, pv)
    if ids is not None:
        mask = {"params": {"ids": ids}}
        session.say(f"mask: {', '.join(recipe.mask['editable_classes'])} -> {len(ids)} ids")
    elif not recipe.mask.get("lock_rest", True):
        mask = {"outputHash": _full_frame_mask_key(pv, session.cas_for(pid))}
        session.say("mask: the whole frame (the packet's locked ids stay locked)")
    edit_id = _graph_for(project, packet_id, pv, recipe, engine_id, mask)

    body = {
        "engine_id": engine_id,
        "packet_id": packet_id,
        "prompt": plan.prompt,
        "params": dict(plan.params),
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
        envelope_id = plan.envelope_id
        if args.envelope is not None and envelope_id is None:
            envelope_id = plan.envelope_id = create_envelope(
                conn,
                owner="user:cli",
                max_total_usd=args.envelope,
                max_per_run_usd=args.envelope,
                ttl_s=ENVELOPE_TTL_S,
                now=utcnow(),
            ).id
            session.index.remember(envelope_id, pid)
            what = "the whole command" if plan.multi else "this run"
            session.say(f"  envelope {envelope_id}: at most {usd(args.envelope)} for {what}")
        try:
            if not _approved(session, args.approve, quote.price_usd * n, n):
                raise CliExit(EXIT_REFUSED, "not approved: nothing was sent")
            approved = pipeline.approve(conn, pid, run.id, args.approve, envelope_id, reason="plate run")
        except PACKET_ERRORS:
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
    outcome.states = {r.id: pipeline.execute(r.id, pid) for r in approved}


def cmd_run(session: Session, args: argparse.Namespace) -> int:
    recipe = _load_recipe(args.recipe)
    refs = _packet_refs(args)
    fake = fake_engine_from_env(session.env)
    engine_id = _engine_choice(session, args.engine or recipe.engine, fake)
    n = args.n if args.n is not None else int(recipe.params.get("n") or 1)
    if n < 1:
        raise CliExit(EXIT_USAGE, "--n must be at least 1")
    if args.envelope is not None and args.envelope <= 0:
        raise CliExit(EXIT_USAGE, "--envelope must be a positive USD amount")
    shot_class = args.shot_class or recipe.shot_class
    pipeline = session.pipeline(fake)

    # every packet folder is read, and agrees on its project, before a project is made or a byte is sent
    prepared = [_prepare_packet(ref) for ref in refs]
    project = _open_project(session, args, next((p for p in prepared if p is not None), None))
    for p in prepared:
        if p is not None:
            _check_same_project(p, project)
    pid = project.project_id

    sheet = ProjectSheet.from_dict((project.doc.get("settings") or {}).get("project_sheet"))
    prompt = recipes.assemble_runnable(recipe, sheet)
    left = recipes.unfilled_count(prompt)
    if left:
        session.warn(
            f"{left} [TOKEN](s) in the prompt are unfilled: set settings.project_sheet in project.json"
        )
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

    plan = _RunPlan(
        args, pipeline, project, recipe, engine_id, shot_class, n, prompt, params, multi=len(refs) > 1
    )
    multi = plan.multi
    outcomes: list[_PacketOutcome] = []
    for ref, prep in zip(refs, prepared, strict=True):
        outcome = _PacketOutcome(ref)
        outcomes.append(outcome)
        try:
            _run_packet(session, plan, ref, prep, outcome)
        except PACKET_ERRORS as e:
            if not multi:
                raise
            outcome.error, outcome.code = str(e) or type(e).__name__, _code_for(e)
            session.warn(f"packet {outcome.packet_id or ref}: {outcome.error}")
    if not multi:
        return _report_runs(session, pid, outcomes[0].states)
    return _report_packets(session, pid, outcomes)


def _describe_runs(
    session: Session, pid: str, states: Mapping[str, RunState]
) -> list[tuple[str, RunState, str]]:
    """(run id, state, the one line that says how it ended) for each run, in order."""
    out: list[tuple[str, RunState, str]] = []
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
            out.append((r.id, r.state, line))
    return out


def _report_runs(session: Session, pid: str, states: dict[str, RunState]) -> int:
    done: list[str] = []
    gate_fail: list[str] = []
    for rid, state, line in _describe_runs(session, pid, states):
        session.say(line)
        if state is RunState.DONE:
            done.append(rid)
        elif state is RunState.GATE_FAIL:
            gate_fail.append(rid)
    if done:
        session.say(f"next: plate export {done[0]} --to <folder> --format tiff16")
        return EXIT_OK
    if gate_fail:
        session.say(
            f'the gates failed; `plate export {gate_fail[0]} --override-reason "..."` ships it on the record'
        )
        return EXIT_GATE_FAIL
    return EXIT_ERROR


def _report_packets(session: Session, pid: str, outcomes: Sequence[_PacketOutcome]) -> int:
    """One line per run, led by its packet (a packet that did not run gets a line saying why). Exit 2 when any run
    ended GATE_FAIL, else 3 for a refusal, 4 for a packet that could not start, 1 for a run that failed."""
    done: list[str] = []
    gate_fail: list[str] = []
    codes: list[int] = []
    for o in outcomes:
        label = o.packet_id or o.ref
        if o.error is not None:
            session.say(f"packet {label}: not run ({o.error})")
            codes.append(o.code or EXIT_ERROR)
            continue
        for rid, state, line in _describe_runs(session, pid, o.states):
            session.say(f"packet {label}: {line}")
            if state is RunState.DONE:
                done.append(rid)
            elif state is RunState.GATE_FAIL:
                gate_fail.append(rid)
            else:
                codes.append(EXIT_ERROR)
    if done:
        session.say(f"next: plate export --runs {','.join(done)} --to <folder> --format tiff16")
    if gate_fail:
        session.say(
            f'the gates failed on {len(gate_fail)} run(s); `plate export {gate_fail[0]} --override-reason "..."` '
            "ships one on the record"
        )
        return EXIT_GATE_FAIL
    return next((c for c in (EXIT_REFUSED, EXIT_USAGE, EXIT_ERROR) if c in codes), EXIT_OK)


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
    if sum([bool(args.run_id), bool(args.runs), bool(args.accepted)]) != 1:
        raise CliExit(
            EXIT_USAGE,
            "give one run id, or --runs r1,r2, or --accepted (with --project): exactly one of them",
        )
    if args.run_id:
        if args.name_pattern:
            raise CliExit(
                EXIT_USAGE, "--name-pattern names a batch (--runs, --accepted); --name names one file"
            )
        return _export_one(session, args)
    return _export_batch(session, args)


def _export_one(session: Session, args: argparse.Namespace) -> int:
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


def _fresh_index(session: Session, project: Project) -> None:
    """Rebuild this machine's index of the project from its folder when it has none yet (the accepts and
    overrides a batch reads live there; the rebuild replays them from the ledger)."""
    if not session.index.db_path(project.project_id).is_file():
        with held(project.root):
            session.index.rebuild_from_project(project.root)


def _export_batch(session: Session, args: argparse.Namespace) -> int:
    from .routes.export_batch import DEFAULT_PATTERN, export_many

    if args.override_reason:
        raise CliExit(
            EXIT_USAGE,
            "a batch export has no typed override: record one per run first (`plate export <run_id> "
            "--override-reason ...`), then export the batch",
        )
    if args.name:
        raise CliExit(EXIT_USAGE, "--name names one file; a batch is named with --name-pattern")
    run_ids: list[str] | None = None
    if args.accepted:
        if not args.project:
            raise CliExit(
                EXIT_USAGE, "--accepted needs --project <dir|id>: the accepted runs of which project?"
            )
        project = _load_project(session, args.project)
        _fresh_index(session, project)
        pid = project.project_id
    else:
        run_ids = list(dict.fromkeys(r.strip() for r in args.runs.split(",") if r.strip()))
        if not run_ids:
            raise CliExit(EXIT_USAGE, "--runs needs at least one run id (r1,r2)")
        if args.project:
            project = _load_project(session, args.project)
            _runs_indexed(session, project, run_ids)
            pid = project.project_id
        else:
            homes = {rid: session.index.locate_run(rid) for rid in run_ids}
            lost = next((rid for rid, home in homes.items() if home is None), None)
            if lost is not None:
                raise CliExit(
                    EXIT_USAGE,
                    f"run {lost} is not in this machine's index; name its folder with --project <dir>",
                )
            if len(set(homes.values())) != 1:
                raise CliExit(
                    EXIT_USAGE,
                    f"these runs belong to {len(set(homes.values()))} projects: export one project at a time",
                )
            pid = str(next(iter(homes.values())))
    result = export_many(
        session.pipeline(),
        pid,
        out_dir=Path(args.to).resolve(),
        run_ids=run_ids,
        accepted=bool(args.accepted),
        fmt=args.format,
        name_pattern=args.name_pattern or DEFAULT_PATTERN,
        by="human",
    )
    for row in result["exported"]:
        session.say(f"exported {row['path']}")
        session.say(f"gate report {row['sidecar']}")
    for row in result["refused"]:
        code = f" ({row['code']})" if row.get("code") else ""
        session.warn(f"not exported: run {row['run_id']}: {row['error']}{code}: {row.get('detail', '')}")
    exported, refused = len(result["exported"]), len(result["refused"])
    if args.accepted and not exported and not refused:
        session.say(f"{pid}: no accepted runs yet, nothing exported")
    else:
        session.say(f"exported {exported} run(s) to {Path(args.to).resolve()}; {refused} refused")
    if any(row.get("code") == "GATE_FAIL" for row in result["refused"]):
        return EXIT_GATE_FAIL
    return EXIT_ERROR if refused else EXIT_OK


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
    only: the live smoke is where it gets confirmed. A `live_only` case has no recording (the vendor's answer
    is not established yet); the fixture smoke skips it and says why."""

    engine_id: str
    provider: str
    contract: tuple[str, ...]
    prefer: tuple[str, ...] = ()
    keys: tuple[tuple[str, ...], ...] = ()
    fixture_model_id: str | None = None
    fixture_kwargs: Mapping[str, Any] = field(default_factory=dict)
    live_only: str | None = None  # why there is no recorded answer: the case is skipped on fixtures
    image_px: int | None = None  # send a PNG this many pixels square instead of the fixture room (a tiny one)


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
    SmokeCase(
        "magnific-precision-v2",
        "magnific",
        (),
        keys=(("MAGNIFIC_API_KEY", "FREEPIK_API_KEY"),),
        live_only="the vendor's address and model id are unconfirmed (flag unverified_p0), nothing is recorded",
        image_px=128,
    ),
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


def smoke_png(square_px: int | None = None) -> bytes:
    """The smoke image: the committed Blender fixture room when the repo has it, else a gradient (client
    imagery never enters the repo or a smoke). `square_px` shrinks it to a square that size, for an engine
    that is billed by the pixel."""
    import cv2
    import numpy as np

    room = REPO_ROOT / "fixtures" / "packets" / "room_a" / "display.tif"
    if room.is_file():
        from plate_pixels import imageio

        rgb = np.clip(np.rint(imageio.read(room).rgb * 255.0), 0, 255).astype(np.uint8)
    else:
        ramp = np.linspace(0, 255, 640, dtype=np.uint8)[None, :, None]
        rgb = np.repeat(np.repeat(ramp, 360, axis=0), 3, axis=2)
    if square_px is not None:
        rgb = cv2.resize(rgb, (square_px, square_px), interpolation=cv2.INTER_AREA)
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
                elif not live and case.live_only:
                    row.detail = f"live only: {case.live_only}"
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
            transport = factory(case, manifest, route, live)
            if case.live_only and isinstance(
                transport, ReplayTransport
            ):  # a recording stands in: it has no answer
                row.status, row.detail = "skipped", f"live only: {case.live_only}"
                session.say(row.line())
                return False
            adapter = cls(use, route, transport, **kwargs)
            inputs = smoke_inputs(use, smoke_png(case.image_px) if case.image_px else png, still)
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


# ---------------------------------------------------------------------------- plate review / import / deliver

# What `POST /runs/{id}/deliver` accepts (`routes.delivery.FORMATS`; a test keeps the two equal).
DELIVER_FORMATS = ("prores4444", "hevc", "exr_sequence", "av1_retag", "fcpxml")
IMPORT_SOURCES = ("higgsfield", "runway", "manual")


def _load_project(session: Session, ref: str) -> Project:
    """The project at a folder or id, registered in this machine's index."""
    root = resolve_project(session, ref)
    try:
        project = Project.load(root)
    except (ProjectError, FileNotFoundError) as e:
        raise CliExit(EXIT_USAGE, f"{ref}: {e}") from e
    session.index.set_project_root(project.project_id, project.root, project.custody)
    return project


def _runs_indexed(session: Session, project: Project, run_ids: Sequence[str]) -> None:
    """Every run id is a run of the project; the index is rebuilt from the folder once if one is missing."""

    def missing() -> list[str]:
        with session.index.project(project.project_id) as conn:
            return [
                r for r in run_ids if not conn.execute("SELECT 1 FROM runs WHERE id = ?", (r,)).fetchone()
            ]

    if missing():
        with held(project.root):
            session.index.rebuild_from_project(project.root)
    lost = missing()
    if lost:
        raise CliExit(EXIT_USAGE, f"run {lost[0]} is not a run of project {project.project_id}")


def _exportable_takes(session: Session, project: Project, ctx: Any) -> tuple[list[str], dict[str, Any]]:
    """The runs a review bundle can show, oldest first, with their sources: every DONE (or overridden GATE_FAIL)
    run of the project that the review guard lets through and that has a still in the CAS. A run that is
    exportable but cannot be shown (previs, a video take, no output) is skipped with the reason printed."""
    from .review_bundle import ReviewError
    from .routes import ApiError
    from .routes.review import check_reviewable, take_source
    from .routes.runs import run_view

    pid = project.project_id
    _fresh_index(session, project)
    ids: list[str] = []
    sources: dict[str, Any] = {}
    with session.index.project(pid) as conn:
        for row in conn.execute("SELECT id FROM runs ORDER BY created_utc, id").fetchall():
            rid = row[0]
            if not run_view(conn, rid)["exportable"]:
                continue
            run = get_run(conn, rid)
            manifest = ctx.pipeline.manifests.get(run.route.engine_id)
            if manifest is not None and str(manifest.kind).startswith(("video", "local_video")):
                session.say(f"skipped {rid}: video takes are not in review bundles yet (stills only)")
                continue
            try:
                check_reviewable(conn, run, project.root)
                sources[rid] = take_source(ctx, run)
            except ApiError as e:
                session.say(f"skipped {rid}: not reviewable ({e.body.get('code') or e.body.get('error')})")
            except ReviewError as e:
                session.say(f"skipped {rid}: {e}")
            else:
                ids.append(rid)
    return ids, sources


def cmd_review(session: Session, args: argparse.Namespace) -> int:
    from .review_bundle import ReviewError, export_review
    from .routes import ApiError
    from .routes.review import check_reviewable, take_source

    if args.all_exportable == bool(args.runs):
        raise CliExit(EXIT_USAGE, "give --runs r1,r2, or --all-exportable (not both)")
    project = _load_project(session, args.project)
    pid = project.project_id
    ctx = SimpleNamespace(cas_for=session.cas_for, pipeline=session.pipeline())  # all `take_source` reads
    sources: dict[str, Any] = {}
    if args.all_exportable:
        run_ids, sources = _exportable_takes(session, project, ctx)
        if not run_ids:
            raise CliExit(EXIT_USAGE, f"{pid} has no exportable run a review bundle can show yet")
    else:
        run_ids = list(dict.fromkeys(r.strip() for r in args.runs.split(",") if r.strip()))
        if not run_ids:
            raise CliExit(EXIT_USAGE, "--runs needs at least one run id (r1,r2)")
        _runs_indexed(session, project, run_ids)
    out_dir = Path(args.out).resolve() if args.out else project.review_dir
    options: dict[str, Any] = {"reply_to": args.reply_to}
    if args.title is not None:
        options["title"] = args.title
    try:
        with session.index.project(pid) as conn:
            for rid in run_ids:
                if rid in sources:
                    continue  # --all-exportable already checked it
                run = get_run(conn, rid)
                check_reviewable(
                    conn, run, project.root
                )  # the delivery guard: previs and unforgiven GATE_FAIL
                sources[rid] = take_source(ctx, run)
        zip_path = export_review(
            project, run_ids, out_dir, args.watermark, args.max_width, sources=sources, **options
        )
    except ApiError as e:
        body = e.body
        raise CliExit(
            EXIT_REFUSED,
            f"run {body.get('run_id')} is not reviewable ({body.get('code') or body.get('error')}): "
            f"{body.get('detail') or e}",
        ) from e
    except ReviewError as e:
        raise CliExit(EXIT_USAGE, str(e)) from e
    except ValueError as e:  # watermark, max width, title, reply-to
        raise CliExit(EXIT_USAGE, str(e)) from e
    session.say(
        f"review bundle {zip_path} ({len(run_ids)} take(s), {zip_path.stat().st_size / 1e6:.2f} MB, "
        f"watermark {args.watermark})"
    )
    return EXIT_OK


def cmd_import(session: Session, args: argparse.Namespace) -> int:
    file = Path(args.file)
    if not file.is_file():
        raise CliExit(EXIT_USAGE, f"{file} is not a file")
    project = _load_project(session, args.project)
    pid = project.project_id
    prepared = None
    if Path(args.packet).is_dir():
        try:
            prepared = prepare_ingest(Path(args.packet).resolve())
        except PacketInvalid as e:
            raise CliExit(EXIT_USAGE, f"packet {args.packet}: {'; '.join(e.errors[:5])}") from e
        if prepared.project_id != pid:
            raise CliExit(
                EXIT_USAGE,
                f"packet {prepared.packet_id} belongs to project {prepared.project_id!r}, "
                f"but {project.root} is project {pid!r}",
            )
    pipeline = session.pipeline()
    packet_id = _packet_in_project(session, pipeline, project, args.packet, prepared)
    run = import_file(
        pipeline,
        pid,
        packet_id,
        file.resolve(),
        source=args.source,
        node_id=args.node,
        cost_usd=args.cost_usd,
        note=args.note,
        by="human",
    )
    session.say(f"imported {file.name} ({args.source}, declared {usd(args.cost_usd)}) as run {run.id}")
    return _report_runs(session, pid, {run.id: run.state})


def cmd_deliver(session: Session, args: argparse.Namespace) -> int:
    from .delivery.guard import DeliveryError, DeliveryRefused, ToolMissing
    from .lineage import LineageError
    from .routes.delivery import deliver_run

    pid, _project_dir = _locate(session, args.run_id, args.project)
    pipeline = session.pipeline()
    try:
        done = deliver_run(
            pipeline,
            args.run_id,
            args.format,
            Path(args.to).resolve(),
            name=args.name,
            override_reason=args.override_reason,
            by="human",
        )
    except DeliveryRefused as e:
        raise CliExit(EXIT_REFUSED, f"not delivered: {e}") from e
    except ToolMissing as e:
        raise CliExit(EXIT_ERROR, f"not delivered: {e}") from e
    except FileExistsError as e:
        raise CliExit(EXIT_USAGE, f"not delivered: {e} (deliver never overwrites)") from e
    except (DeliveryError, LineageError) as e:
        raise CliExit(EXIT_USAGE, f"not delivered: {e}") from e
    session.say(f"delivered {done['path']} ({done['format']})")
    session.say(f"gate report {done['sidecar']}")
    if args.override_reason:
        session.say(f"override on the record: {args.override_reason}")
    return EXIT_OK


# ---------------------------------------------------------------------------- plate timer


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DAEMON_TIMEOUT_S = 15.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the bearer token somewhere the install never named: refuse it."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def daemon_base(session: Session) -> str:
    """The running daemon's address: `PLATE_DAEMON_URL`, else the loopback address in the settings
    (`PLATE_PORT`, `PLATE_BIND`). The bearer token is only ever sent to a loopback host."""
    raw = (
        session.env.get("PLATE_DAEMON_URL")
        or f"http://{host_port(session.settings.bind_host, session.settings.port)}"
    )
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "http" or (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
        raise CliExit(
            EXIT_USAGE,
            f"the daemon address {raw!r} is not http on 127.0.0.1 or localhost: the CLI sends its token "
            "to a loopback daemon only",
        )
    return f"{parts.scheme}://{parts.netloc}"


def _error_text(raw: bytes) -> str:
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return raw.decode("utf-8", "replace")[:300]
    if isinstance(body, dict):
        text = " ".join(str(body[k]) for k in ("error", "detail") if body.get(k))
        if body.get("errors"):
            text = (text + " " + "; ".join(str(e) for e in body["errors"])).strip()
        return text or json.dumps(body)[:300]
    return str(body)[:300]


def daemon_call(
    session: Session,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    query: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One authenticated call to the running daemon. Exit 4 when nothing answers; 1 when it answers with an error."""
    base = daemon_base(session)
    url = f"{base}/api/v0{path}" + (f"?{urllib.parse.urlencode(dict(query))}" if query else "")
    data = None if body is None else json.dumps(dict(body)).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {session.settings.token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)  # loopback: no proxy
    try:
        with opener.open(req, timeout=DAEMON_TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        text = _error_text(e.read())
        hint = " (the token in this install's config.json is not the daemon's)" if e.code == 401 else ""
        raise CliExit(EXIT_ERROR, f"the daemon at {base} answered {e.code}: {text}{hint}") from e
    except OSError as e:  # refused, unreachable, timed out: nothing is listening
        reason = getattr(e, "reason", e)
        raise CliExit(
            EXIT_USAGE,
            f"no daemon answered at {base} ({reason}): start it with the start-daemon script in scripts/, "
            "or name it with PLATE_DAEMON_URL / PLATE_PORT",
        ) from e
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise CliExit(EXIT_ERROR, f"the daemon at {base} answered something that is not JSON") from e
    if not isinstance(doc, dict):
        raise CliExit(EXIT_ERROR, f"the daemon at {base} answered {type(doc).__name__}, not an object")
    return doc


def _project_id_of(ref: str) -> str:
    """A project id from a project folder or an id."""
    root = Path(ref)
    if (root / "project.json").is_file():
        try:
            return Project.load(root).project_id
        except (ProjectError, FileNotFoundError) as e:
            raise CliExit(EXIT_USAGE, f"{ref}: {e}") from e
    return ref


def cmd_timer(session: Session, args: argparse.Namespace) -> int:
    pid = _project_id_of(args.project) if args.project else None
    if args.timer_cmd == "today":
        doc = daemon_call(session, "GET", "/timer/today", query={"project_id": pid} if pid else None)
        rows = doc.get("rows") or []
        total = (doc.get("totals") or {}).get("minutes", 0)
        session.say(f"human minutes {doc.get('day')}" + (f" in {pid}" if pid else "") + f": {total} min")
        for row in rows:
            session.say(f"  {row.get('deliverable_id')}: {row.get('minutes')} min")
        running = doc.get("running")
        if isinstance(running, Mapping):
            session.say(f"running: {running.get('deliverable_id')} since {running.get('started_at')}")
        return EXIT_OK
    body: dict[str, Any] = {"deliverable_id": args.deliverable_id, "action": args.timer_cmd}
    if pid:
        body["project_id"] = pid
    doc = daemon_call(session, "POST", "/timer", body)
    if args.timer_cmd == "start":
        if doc.get("already_running"):
            session.say(f"timer already running: {doc.get('deliverable_id')} since {doc.get('started_at')}")
        else:
            session.say(f"timer started: {doc.get('deliverable_id')} at {doc.get('started_at')}")
    else:
        session.say(f"timer stopped: {doc.get('deliverable_id')}, {doc.get('minutes')} min recorded")
    return EXIT_OK


# ---------------------------------------------------------------------------- plate spend


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], left: int = 1) -> list[str]:
    """The first `left` columns left-aligned (names), the rest right-aligned (numbers), two spaces between."""
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]

    def line(cells: Sequence[str]) -> str:
        parts = [
            str(c).ljust(w) if i < left else str(c).rjust(w)
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        ]
        return "  ".join(parts).rstrip()

    return [line(header), *(line(r) for r in rows)]


def _namespaced(pid: str, rec: Mapping[str, Any]) -> dict[str, Any]:
    """A ledger record with its run id (and gate report path) led by the project, so two projects' runs can
    never fold into one when their ledgers are read together."""
    out = dict(rec)
    for key in ("run_id", "gate_report"):
        if isinstance(out.get(key), str) and out[key]:
            out[key] = f"{pid}/{out[key]}"
    return out


def _project_ledgers(session: Session, project_ref: str | None) -> list[tuple[str, Path]]:
    """(project id, folder) of the named project, or of every project this machine knows."""
    if project_ref is not None:
        root = resolve_project(session, project_ref)
        return [(Project.load(root).project_id, root)]
    return [(h["project_id"], Path(h["root"])) for h in session.pipeline().project_list()]


def _ledger_records(session: Session, project_ref: str | None) -> list[dict[str, Any]]:
    """The ledger records of one project as they are, or of every project with run ids namespaced."""
    out: list[dict[str, Any]] = []
    for pid, root in _project_ledgers(session, project_ref):
        try:
            records = Ledger(root).records()
        except (LedgerError, OSError) as e:
            if project_ref is not None:
                raise
            session.warn(f"{pid}: ledger not read ({e})")
            continue
        out.extend(records if project_ref is not None else (_namespaced(pid, r) for r in records))
    return out


def _spend_per_accepted(billed: float, quoted: float, accepted: int) -> str:
    """The billed figure (the quoted one while nothing is billed yet) over the accepted takes."""
    spent = billed if billed > 0 else quoted
    return f"{spent / accepted:.4f}" if accepted else "-"


def cmd_spend(session: Session, args: argparse.Namespace) -> int:
    from .routes.spend import spend_report

    if bool(args.project) == bool(args.all_projects):
        raise CliExit(EXIT_USAGE, "give a project (folder or id), or --all-projects")
    try:
        since = parse_iso(args.since)
    except ValueError as e:  # a --since that is not an ISO date or time
        raise CliExit(EXIT_USAGE, f"--since {args.since!r}: {e}") from e
    if args.all_projects:
        title = f"spend for all projects ({len(_project_ledgers(session, None))})"
        records = _ledger_records(session, None)
    else:
        title = f"spend for {Project.load(resolve_project(session, args.project)).project_id}"
        records = _ledger_records(session, args.project)
    report = spend_report(records, args.by, since)
    show_cpa = report["accepted"] > 0  # the T78 table until there is something accepted to divide by

    def money(quoted: float, billed: float, accepted: int, per_accepted: str) -> list[str]:
        cells = [f"{quoted:.4f}", f"{billed:.4f}", str(accepted)]
        return [*cells, per_accepted] if show_cpa else cells

    total = money(
        report["spent_usd_quoted"],
        report["spent_usd_billed"],
        report["accepted"],
        _spend_per_accepted(report["spent_usd_billed"], report["spent_usd_quoted"], report["accepted"]),
    )
    if args.by == "engine":
        header, left = ["engine", "runs", "quoted USD", "billed USD", "accepted"], 1
        rows = [
            [
                engine,
                str(b["generations"]),
                *money(
                    b["spent_usd_quoted"],
                    b["spent_usd_billed"],
                    b["accepted"],
                    _spend_per_accepted(b["spent_usd_billed"], b["spent_usd_quoted"], b["accepted"]),
                ),
            ]
            for engine, b in report["by_engine"].items()
        ]
        rows.append(["total", str(report["generations"]), *total])
    else:
        header, left = [args.by, "engine", "runs", "quoted USD", "billed USD", "accepted"], 2
        rows = [
            [
                s["bucket"],
                s["engine_id"],
                str(s["runs"]),
                *money(
                    s["quoted_usd"],
                    s["billed_usd"],
                    s["accepted"],
                    "-" if s["cost_per_accepted"] is None else f"{s['cost_per_accepted']:.4f}",
                ),
            ]
            for s in report["series"]
        ]
        rows.append(["total", "", str(report["generations"]), *total])
    if show_cpa:
        header.append("per accepted USD")
    session.say(
        title
        + (f" since {args.since}" if args.since else "")
        + (f" by {args.by}" if args.by != "engine" else "")
    )
    for text in _table(header, rows, left):
        session.say(text)
    # An imported take never reaches `submitted`, so `summarize` does not count it; what was declared is shown.
    declared = [
        r
        for r in records
        if r.get("kind") == "quoted"
        and isinstance(r.get("data"), Mapping)
        and r["data"].get("imported") is True
        and (since is None or ((at := parse_iso(r.get("at"))) is not None and at >= since))
    ]
    if declared:
        spent = sum(float(r.get("cost_usd_quoted") or 0) for r in declared)
        session.say(f"not in the table: {len(declared)} imported take(s), {usd(spent)} declared")
    return EXIT_OK


# ---------------------------------------------------------------------------- plate cards / estimate


def _cards(session: Session, project_ref: str | None) -> list[Any]:
    """The engine cards folded from the ledger of one project, or of every project this machine knows."""
    from plate_engines.cards import from_ledger

    from .routes.engines import _verdicts

    records = _ledger_records(session, project_ref)
    return from_ledger(records, _verdicts(records))


def _scope(session: Session, project_ref: str | None) -> str:
    if project_ref is None:
        return "all projects"
    return Project.load(resolve_project(session, project_ref)).project_id


def cmd_cards(session: Session, args: argparse.Namespace) -> int:
    cards = [
        c
        for c in _cards(session, args.project)
        if (args.task is None or c.task == args.task)
        and (args.shot_class is None or c.shot_class == args.shot_class)
    ]
    session.say(f"engine cards for {_scope(session, args.project)}")
    rows = [
        [
            c.engine_id,
            c.task,
            c.shot_class or "-",
            str(c.n),
            str(c.passes),
            f"{c.wilson_lower_95:.4f}",
            f"{c.mean_cost:.4f}" if c.mean_cost is not None else "-",
            "yes" if c.steering() else "no",
        ]
        for c in cards
    ]
    header = ["engine", "task", "shot_class", "n", "pass", "lower95", "mean_usd", "steering"]
    for text in _table(header, rows, 3):
        session.say(text)
    if not rows:
        session.say("no cards yet: a card needs runs with a gate verdict (pass or fail) in the ledger")
    return EXIT_OK


def cmd_estimate(session: Session, args: argparse.Namespace) -> int:
    from .quotes import DURATION_PARAMS, FAKE_PRICE_USD, build_quote

    if (args.count is not None and args.count < 1) or (args.seconds is not None and not args.seconds > 0):
        raise CliExit(EXIT_USAGE, "--count must be at least 1 and --seconds more than 0")
    params = _parse_params(args.param)
    manifest = session.pipeline().manifests.get(args.engine)
    if args.engine == FAKE_ENGINE_ID:
        if args.seconds is not None:
            raise CliExit(EXIT_USAGE, f"{args.engine} is priced per output: use --count")
        unit, custody, status = FAKE_PRICE_USD, "C0", "fake"
    elif manifest is None:
        known = sorted(session.pipeline().manifests)
        raise CliExit(EXIT_USAGE, f"unknown engine {args.engine!r}; the manifests define {known}")
    else:
        if args.seconds is not None:
            if manifest.price.model != "per_second":
                unit_word = {"per_image": "image", "per_megapixel": "megapixel"}.get(
                    manifest.price.model, "request"
                )
                raise CliExit(
                    EXIT_USAGE, f"{args.engine} is priced per {unit_word}, not per second: use --count"
                )
            name = next((p.name for p in manifest.params if p.name in DURATION_PARAMS), DURATION_PARAMS[0])
            params[name] = args.seconds
        try:
            quote = build_quote(manifest, None, {}, params)
        except ValueError as e:
            hint = " (it is priced per second: use --seconds)" if manifest.price.model == "per_second" else ""
            raise CliExit(EXIT_USAGE, f"cannot price {args.engine}: {e}{hint}") from e
        unit, custody, status = quote.price_usd, quote.custody_grade, quote.price_status
    count = 1 if args.seconds is not None else args.count
    total = unit * count
    each = f"{usd(unit)} for {args.seconds:g} s" if args.seconds is not None else f"{usd(unit)} each"
    cell = (args.engine, args.task, args.shot_class)
    card = next((c for c in _cards(session, args.project) if c.key == cell), None)
    session.say(f"estimate for {args.engine}: task {args.task}, shot class {args.shot_class}")
    session.say(
        f"  quote {each}"
        + (f"; {count} x = {usd(total)}" if count > 1 else "")
        + f" (custody {custody}, price {status})"
    )
    if card is None:
        session.say("  no card for this cell: dividing by 1")
        rate = 1.0
    else:
        steer = "steering" if card.steering() else "not steering yet, fewer than 20 runs"
        session.say(f"  card: n {card.n}, {card.passes} passed, lower95 {card.wilson_lower_95:.4f} ({steer})")
        rate = card.wilson_lower_95
    if rate <= 0:
        session.say("  expected USD per accepted output: unbounded (the card's lower bound is 0)")
        return EXIT_OK
    session.say(f"  expected USD per accepted output: {unit / rate:.4f}")
    if count > 1:
        session.say(f"  expected USD for {count} accepted outputs: {total / rate:.4f}")
    return EXIT_OK


# ---------------------------------------------------------------------------- plate doctor

# the Max profile folder the installer targets by default (docs/windows.md)
DOCTOR_MAX_PROFILE = "2026 - 64bit"


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _git_out(*argv: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _doctor_git() -> tuple[str, str]:
    head = _git_out("rev-parse", "HEAD")
    if head is None:
        return "warn", "git was not found on PATH"
    if head.returncode != 0:
        return "info", "this is not a git checkout"
    mine = head.stdout.strip()
    remote = _git_out("rev-parse", "--verify", "--quiet", "origin/main")
    if remote is None or remote.returncode != 0 or not remote.stdout.strip():
        return "info", f"HEAD {mine[:8]}; no remote (origin/main is not known here)"
    theirs = remote.stdout.strip()
    if theirs == mine:
        return "ok", f"HEAD {mine[:8]} is origin/main"
    counts = _git_out("rev-list", "--left-right", "--count", "HEAD...origin/main")
    ahead = behind = "?"
    if counts is not None and counts.returncode == 0 and len(counts.stdout.split()) == 2:
        ahead, behind = counts.stdout.split()
    return "warn", f"HEAD {mine[:8]} is not origin/main {theirs[:8]} (ahead {ahead}, behind {behind})"


def _installed_startup(env: Mapping[str, str], cfg: Mapping[str, Any]) -> Path | None:
    """Where the installer put the Max startup script: config.json `startup_dir`, else `PLATE_MAX_STARTUP_DIR`,
    else the default Max profile folder under %LOCALAPPDATA% (None when this machine has none)."""
    folder = cfg.get("startup_dir") or env.get("PLATE_MAX_STARTUP_DIR")
    if not folder and env.get("LOCALAPPDATA"):
        folder = (
            Path(env["LOCALAPPDATA"])
            / "Autodesk"
            / "3dsMax"
            / DOCTOR_MAX_PROFILE
            / "ENU"
            / "scripts"
            / "startup"
        )
    return Path(str(folder)) / "plate_startup.py" if folder else None


def _doctor_startup(env: Mapping[str, str], cfg: Mapping[str, Any]) -> tuple[str, str]:
    source = REPO_ROOT / "plugins" / "max" / "startup" / "plate_startup.py"
    if not source.is_file():
        return "warn", f"{source} is missing from this checkout"
    target = _installed_startup(env, cfg)
    if target is None or not target.is_file():
        return "info", "not installed (plugins/max/scripts/install.ps1 puts it in Max's startup folder)"
    if sha256(source.read_bytes()).digest() == sha256(target.read_bytes()).digest():
        return "ok", f"same as the installed copy ({target})"
    return "warn", f"differs from the installed copy {target}: run plugins/max/scripts/install.ps1 again"


def _doctor_ui(env: Mapping[str, str]) -> tuple[str, str]:
    dist = Path(env["PLATE_UI_DIST"]) if env.get("PLATE_UI_DIST") else REPO_ROOT / "ui" / "dist"
    index = dist / "index.html"
    if index.is_file():
        return "ok", f"{index} is there"
    return "warn", f"{index} is missing: build the UI (npm run build in ui/; scripts/bootstrap does it)"


def _doctor_tool(finder: Callable[[], str]) -> tuple[str, str]:
    from .delivery.guard import ToolMissing

    try:
        return "ok", finder()
    except ToolMissing as e:
        return "warn", str(e)


def _doctor_token(paths: Any, cfg: Mapping[str, Any]) -> tuple[str, str, str | None]:
    """(status, text, the token or None); the token itself is never printed."""
    if paths.token_file is not None:
        try:
            token = paths.token_file.read_text(encoding="utf-8-sig").strip()
        except (OSError, ValueError):
            return "warn", f"PLATE_TOKEN_FILE {paths.token_file} cannot be read", None
        if not token:
            return "warn", f"PLATE_TOKEN_FILE {paths.token_file} is empty", None
        return "ok", f"PLATE_TOKEN_FILE {paths.token_file}", token
    if cfg.get("token"):
        return "ok", f"config.json {paths.config_path}", str(cfg["token"])
    if paths.config_path.is_file():
        return "warn", f"{paths.config_path} holds no token (the daemon adds one on its next start)", None
    return "info", f"no {paths.config_path} yet (the daemon makes one on its first start)", None


def _doctor_pixels(env: Mapping[str, str], paths: Any, token: str | None) -> tuple[str, str]:
    from .pixels_client import DEFAULT_PORT, INPROC_ENV, PORT_ENV, PixelsClient

    if not token:
        return "info", "not checked: there is no token to ask with yet"
    client = PixelsClient(
        SimpleNamespace(token=token, home=paths.home),  # type: ignore[arg-type]  # only .token and .home are read
        inproc=env.get(INPROC_ENV) == "1",
        port=int(env.get(PORT_ENV) or DEFAULT_PORT),
    )
    mode = client.status()
    if mode == "unavailable":
        return "warn", (
            f"unavailable: nothing answers on 127.0.0.1:{client.port} "
            f"(python -m plate_pixels, or set {INPROC_ENV}=1)"
        )
    return "ok", mode if mode == "inproc" else f"{mode} (127.0.0.1:{client.port})"


def cmd_doctor(session: Session, args: argparse.Namespace) -> int:
    """Look, never change anything, never fail: every check that cannot run says so in its row."""
    from . import config as plate_config
    from .delivery import tools

    paths = plate_config.Paths.from_env(session.env)
    cfg = _read_json_object(paths.config_path)
    token_row = _doctor_token(paths, cfg)
    checks: list[tuple[str, Callable[[], tuple[str, str]]]] = [
        ("git", _doctor_git),
        ("max startup script", lambda: _doctor_startup(session.env, cfg)),
        ("ui build", lambda: _doctor_ui(session.env)),
        ("ffmpeg", lambda: _doctor_tool(tools.ffmpeg_path)),
        ("ffprobe", lambda: _doctor_tool(tools.ffprobe_path)),
        ("pixels worker", lambda: _doctor_pixels(session.env, paths, token_row[2])),
        ("token", lambda: token_row[:2]),
    ]
    rows: list[list[str]] = []
    for name, check in checks:
        try:
            status, text = check()
        except Exception as e:  # a check that cannot run is a row, not a crash
            status, text = "warn", f"check failed: {type(e).__name__}: {e}"
        rows.append([name, f"{status:<4}  {text}"])
    for text in _table(["check", "result"], rows, 2):
        session.say(text)
    return EXIT_OK


# ---------------------------------------------------------------------------- plate recipes


@contextmanager
def _plate_home(session: Session) -> Iterator[None]:
    """`recipes` finds the studio folder through the process environment: point it at this session's home."""
    home = str(session.settings.home)
    old = os.environ.get("PLATE_HOME")
    if old == home:
        yield
        return
    os.environ["PLATE_HOME"] = home
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("PLATE_HOME", None)
        else:
            os.environ["PLATE_HOME"] = old


def cmd_recipes(session: Session, args: argparse.Namespace) -> int:
    project_dir = resolve_project(session, args.project) if args.project else None
    with _plate_home(session):
        if args.recipes_cmd == "list":
            entries = recipes.runnable_entries(project_dir)
            rows = [[r.id, r.kind, r.engine, r.shot_class or "-", r.origin, r.title] for r in entries]
            for text in _table(["id", "kind", "engine", "shot_class", "origin", "title"], rows, 6):
                session.say(text)
            return EXIT_OK
        return _save_recipe(session, args, project_dir)


def _save_recipe(session: Session, args: argparse.Namespace, project_dir: Path | None) -> int:
    import yaml

    if args.scope == "project" and project_dir is None:
        raise CliExit(EXIT_USAGE, "--scope project needs --project <dir|id>: whose recipe folder?")
    file = Path(args.yaml)
    try:
        doc = yaml.safe_load(file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as e:
        raise CliExit(EXIT_USAGE, f"{file}: cannot read it ({e})") from e
    except yaml.YAMLError as e:
        raise CliExit(EXIT_USAGE, f"{file}: not valid YAML ({e})") from e
    recipe = recipes.parse_runnable(doc, args.scope)  # RecipeError -> exit 4, as for a file on disk
    manifests = session.pipeline().manifests
    if recipe.engine not in manifests:
        raise CliExit(
            EXIT_USAGE, f"engine {recipe.engine!r} is not an engine manifest; known: {sorted(manifests)}"
        )
    path = recipes.save_runnable(doc, args.scope, project_dir, overwrite=args.overwrite)
    session.say(f"saved recipe {recipe.id} ({args.scope}) at {path}")
    return EXIT_OK


# ---------------------------------------------------------------------------- parser and entry point


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error; here 2 means a gate failure, so a usage error is 4."""

    def error(self, message: str) -> Any:
        self.print_usage(sys.stderr)
        raise CliExit(EXIT_USAGE, f"{self.prog}: {message}")


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="plate",
        description="PLATE command line: run, gate, export, reindex, gc, pack, smoke, review, import, deliver, timer, "
        "spend, cards, estimate, doctor, recipes.",
    )
    p.add_argument("--version", action="version", version=f"plate {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command", parser_class=_Parser)

    r = sub.add_parser("run", help="quote, approve and run a recipe on a packet")
    r.add_argument("recipe", help="a recipe id (night_relight_changelist) or a recipe .yaml file")
    r.add_argument("--project", required=True, help="the project folder (made from the packet if it is new)")
    r.add_argument("--packet", help="a packet folder to ingest, or the id of an ingested packet")
    r.add_argument(
        "--packets",
        metavar="a,b,c",
        help="several packets (folders or ids) of the project, one chain each, quoted and run in turn",
    )
    r.add_argument("--engine", help="engine id, instead of the recipe's")
    r.add_argument("--n", type=int, help="best-of-N under one quote (default: the recipe's)")
    r.add_argument(
        "--approve", choices=("auto", "human"), default="human", help="human waits for y (default)"
    )
    r.add_argument(
        "--envelope",
        type=float,
        metavar="USD",
        help="hard spending ceiling for this command (all its packets)",
    )
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
        "export",
        help="write a run's (or a batch of runs') gated result as tiff16, png16 or exr, with its gate report",
    )
    e.add_argument("run_id", nargs="?", help="one run (or use --runs / --accepted for a batch)")
    e.add_argument(
        "--runs", metavar="r1,r2", help="a batch: these runs, each through the single export's guards"
    )
    e.add_argument("--accepted", action="store_true", help="a batch: every accepted run of --project")
    e.add_argument("--name-pattern", metavar="PATTERN", help="batch file names (default {node}_{run8})")
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

    v = sub.add_parser("review", help="a client review bundle (static HTML A/B wipe) of finished takes")
    v.add_argument("project")
    v.add_argument("--runs", help="comma-separated run ids")
    v.add_argument(
        "--all-exportable",
        action="store_true",
        help="every DONE (or overridden) still the review guard allows",
    )
    v.add_argument("--watermark", default=DEFAULT_WATERMARK, help="text across every proxy (default REVIEW)")
    v.add_argument("--out", help="output folder (default: the project's review/ folder)")
    v.add_argument(
        "--max-width", type=int, default=MAX_WIDTH_LIMIT, help="widest proxy in pixels (at most 2048)"
    )
    v.add_argument("--title", help="title of the review page")
    v.add_argument("--reply-to", help="where the client's comment form sends its answer")

    i = sub.add_parser("import", help="file a take made outside PLATE as a gated, ledgered run")
    i.add_argument("file")
    i.add_argument(
        "--project", required=True, help="an existing project (folder or id), e.g. made by `plate run`"
    )
    i.add_argument(
        "--packet", required=True, help="the id of an ingested packet, or a packet folder to ingest"
    )
    i.add_argument("--node", help="the graph node the take belongs to")
    i.add_argument("--source", choices=IMPORT_SOURCES, default="manual", help="where the file came from")
    i.add_argument("--cost-usd", type=float, default=0.0, help="what the take cost, as declared (default 0)")
    i.add_argument("--note", help="a note kept with the run")

    d = sub.add_parser(
        "deliver", help="encode a run's take for delivery (ProRes 4444, HEVC, EXR, AV1 re-tag)"
    )
    d.add_argument("run_id")
    d.add_argument("--format", required=True, choices=DELIVER_FORMATS)
    d.add_argument("--to", required=True, help="the output folder")
    d.add_argument("--name", help="file name stem (default: the run id)")
    d.add_argument(
        "--override-reason", help=f"why a GATE_FAIL take ships anyway ({MIN_OVERRIDE_REASON}+ characters)"
    )
    d.add_argument("--project", help="the project folder, if this machine's index does not know the run")

    t = sub.add_parser("timer", help="the human-minute timer of the running daemon")
    tsub = t.add_subparsers(dest="timer_cmd", required=True, metavar="start|stop|today", parser_class=_Parser)
    for word, text in (
        ("start", "start the timer on a deliverable"),
        ("stop", "stop it and record the minutes"),
    ):
        tw = tsub.add_parser(word, help=text)
        tw.add_argument("deliverable_id")
        tw.add_argument("--project", help="the project (folder or id) the minutes belong to")
    td = tsub.add_parser("today", help="today's recorded minutes per deliverable")
    td.add_argument("--project", help="only this project (folder or id)")

    y = sub.add_parser("spend", help="a project's spend per engine, day or week, from its ledger")
    y.add_argument("project", nargs="?", help="the project (folder or id)")
    y.add_argument("--all-projects", action="store_true", help="every project this machine knows")
    y.add_argument("--by", choices=("engine", "day", "week"), default="engine", help="how to bucket the rows")
    y.add_argument("--since", metavar="ISO", help="only records from this date or time (2026-10-01)")

    cd = sub.add_parser(
        "cards", help="engine cards: pass rate with its Wilson bound per engine, task, shot class"
    )
    cd.add_argument("--project", help="one project (folder or id); default: every project on this machine")
    cd.add_argument("--task", help="only this task")
    cd.add_argument("--shot-class", choices=SHOT_CLASSES, help="only this shot class")

    es = sub.add_parser(
        "estimate", help="expected USD per accepted output: the quote over the card's lower bound"
    )
    es.add_argument("--engine", required=True, help="engine id")
    es.add_argument("--task", required=True, help="the task (relight, people, ...)")
    es.add_argument("--shot-class", required=True, choices=SHOT_CLASSES)
    how = es.add_mutually_exclusive_group(required=True)
    how.add_argument("--count", type=int, metavar="N", help="how many outputs (images)")
    how.add_argument("--seconds", type=float, metavar="S", help="how many seconds of video")
    es.add_argument("--project", help="read the cards of this project (folder or id); default: every project")
    es.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="a priced parameter")

    sub.add_parser(
        "doctor", help="check the install: git, Max startup script, UI build, ffmpeg, pixels worker, token"
    )

    rc = sub.add_parser("recipes", help="list and save runnable recipes")
    rsub = rc.add_subparsers(dest="recipes_cmd", required=True, metavar="list|save", parser_class=_Parser)
    rl = rsub.add_parser("list", help="shipped, studio and (with --project) project recipes")
    rl.add_argument("--project", help="also this project's own recipes (folder or id)")
    rs = rsub.add_parser("save", help="validate a recipe .yaml and save it to the studio or a project")
    rs.add_argument("yaml", help="the recipe file")
    rs.add_argument("--scope", required=True, choices=("studio", "project"))
    rs.add_argument("--project", help="the project (folder or id): the folder of --scope project")
    rs.add_argument("--overwrite", action="store_true", help="replace a recipe of the same id")
    return p


COMMANDS: dict[str, Callable[[Session, argparse.Namespace], int]] = {
    "run": cmd_run,
    "gate": cmd_gate,
    "export": cmd_export,
    "reindex": cmd_reindex,
    "gc": cmd_gc,
    "pack": cmd_pack,
    "review": cmd_review,
    "import": cmd_import,
    "deliver": cmd_deliver,
    "timer": cmd_timer,
    "spend": cmd_spend,
    "cards": cmd_cards,
    "estimate": cmd_estimate,
    "doctor": cmd_doctor,
    "recipes": cmd_recipes,
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


def _code_for(e: BaseException) -> int:
    """The exit code an error ends the command with (what `main` answers; a `--packets` run uses it per packet)."""
    if isinstance(e, CliExit):
        return e.code
    if isinstance(e, PipelineError):
        return EXIT_ERROR if e.code in PIPELINE_RUNTIME_CODES else EXIT_USAGE
    if isinstance(e, (QuoteExpired, QuoteNotFound, IllegalTransition, DuplicateRequest, ProjectLocked)):
        return EXIT_ERROR
    return next((code for cls, code in ERROR_CODES if isinstance(e, cls)), EXIT_ERROR)


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
