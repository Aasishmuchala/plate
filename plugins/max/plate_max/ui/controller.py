"""What the dock talks to: a Controller protocol, the live implementation over plate_max, and a
FakeController for tests and the offscreen preview. Standard library only (no Qt), so it imports and
tests without PySide6.

Threading contract (copied from MaxGaffer's dock): every pymxs touch runs on Max's main thread; slow
pure I/O (the daemon health call) goes through Controller.io, which the dock replaces with a
QThread-plus-local-event-loop runner so Max stays responsive. Controller methods never raise for an
expected failure; they return it as data (ok False, problems, reason) for the dock to show.

Action contract with the plate_max.actions modules: the live controller calls them only when the
module exists, so a build without them simply has those buttons disabled (and Light stays disabled,
its tooltip saying MAXGAFFER_MISSING, while MaxGaffer is not installed in this Max).
  actions.send_packet.run(camera_names, out_root, daemon_base, options) -> list[result]
  actions.batch_cameras.run(recipes, out_root, daemon_base, options) -> list[result]
  actions.receive.load_result(path, mode) -> (ok, message)
  actions.send_motion.run(camera, hero_packet_id=, f0=, f1=, out_root=, daemon_base=, crossings=,
                          options=) -> result (hero: the dock's last packet of the camera, or the user's pick)
  actions.light.run([camera], out_root, daemon_base, options) -> [result]: light.match against
                          options.reference_path, apply, rerender_packet superseding the old packet
  actions.scout.run(out_root, daemon_base, options) -> [result]: the scout poses as temporary
                          cameras with previews (options.keep leaves them in the scene)
options is an ActionOptions (a dict that also reads as attributes): recipes {camera: recipe},
progress callable(done, total, message), backup False, project_id (the scene's project binding),
custody ({nda, allowed_grades} of that project once its custody is set) and supersedes ({camera:
the dock's last packet} when "supersedes last" is ticked). A result is a dict or an object with
camera, ok, packet_id, errors, and maybe status_code, events_url, ingest, code, warnings (passed
through by result_dict). A recipe is the CameraRecipe field set: camera, engine_id, preset,
safe_frame {x,y,w,h} | None, overscan_aspect | None, editable_classes, extra_elements; it reaches the
actions as a plate_max.recipes.CameraRecipe when that module exists, else as a plain dict of those
fields. engine_id and preset are hints for the artist only: they never leave Max (the canvas's Make
bar picks the engine), which is why the dock labels them "hint (not sent)".

Project binding: the project a scene sends into is the dock's Project combo (GET /api/v0/projects),
kept in <scene>.plate.json next to the .max file under "project_id"; without one, config.json's
project_id, then the .max file's stem as a slug. The project's custody (from the same answer) rides
along with every send; a project the daemon does not know yet is created by the first packet with
custody unset, which runs C0 (local) engines only until someone sets it.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from .. import config as config_mod
from .. import packet, policy, recipes, transport
from ..config import DEFAULT_DAEMON_URL, load_config, packets_dir
from ..fsutil import atomic_write_json

Progress = Callable[[int, int, str], None]

CAP_SEND_PACKET = "send_packet"
CAP_BATCH = "batch_cameras"
CAP_RECEIVE = "receive"
CAP_MOTION = "send_motion"
CAP_LIGHT = "light"
CAP_SCOUT = "scout"
ALL_CAPABILITIES = (CAP_SEND_PACKET, CAP_BATCH, CAP_RECEIVE, CAP_MOTION, CAP_LIGHT, CAP_SCOUT)

RECEIVE_MODES = ("vfb_history", "viewport_background")
MAXGAFFER_MISSING = "MAXGAFFER_MISSING"  # actions.light's code for a Max without MaxGaffer
CUSTODY_UNSET = "custody unset (C0 only)"
SCENE_PROJECT_KEY = "project_id"  # the key of <scene>.plate.json the project binding is kept under
PROJECTS_PATH = "/api/v0/projects"
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")  # the schema's slug ($defs/slug)
HERO_SCAN_LIMIT = 20
# Result fields result_dict passes through when an action sets them (T75's send answers among them).
PASS_THROUGH = ("status_code", "events_url", "ingest", "code", "warnings", "preview", "label")

# Image engines the recipe editor offers; mirrors engines/plate_engines/manifests (image kinds). The
# daemon's router is the authority on what actually runs; this is only the menu.
ENGINE_IDS = (
    "nano-banana-2.1",
    "nano-banana-pro",
    "gpt-image-2.5",
    "seedream-5-pro",
    "flux-3-image",
    "qwen-image-edit-2509",
)
# The three Lensbook-derived recipes of the first CLI (BUILD_PLAN P1); free text is allowed too.
PRESETS = ("night-relight", "sunburst-rephotograph", "crop-pair-pushin")
OVERSCAN_ASPECTS = ("16:9", "3:2", "4:3", "1:1")


def editable_class_choices() -> list[str]:
    """The classes a recipe may mark editable: every class the policy rules produce, minus the ones
    no AI edit may ever touch (doctrine: locked by default)."""
    seen: list[str] = []
    for rule in policy.CLASS_RULES:
        cls = rule["class"]
        if cls not in policy.NEVER_EDITABLE_CLASSES and cls not in seen:
            seen.append(cls)
    return seen


def default_editable_classes() -> list[str]:
    """Classes whose layer prefix the policy treats as editable (VEG_, SKY, PEOPLE, WATER, ENT_, GRASS)."""
    out: list[str] = []
    for rule in policy.CLASS_RULES:
        cls = rule["class"]
        starts = str(rule.get("pattern", "")).startswith(tuple(policy.EDITABLE_PREFIXES))
        if starts and cls not in policy.NEVER_EDITABLE_CLASSES and cls not in out:
            out.append(cls)
    return out


def default_recipe(camera: str, engine_id: str = ENGINE_IDS[0]) -> dict[str, Any]:
    return {
        "camera": camera,
        "engine_id": engine_id,
        "preset": "",
        "safe_frame": None,
        "overscan_aspect": None,
        "editable_classes": default_editable_classes(),
        "extra_elements": [],
    }


class ActionOptions(dict):
    """The options argument of an action: a dict (options["progress"]) that also reads as attributes
    (options.progress), because the actions are written independently of this dock. An unknown key reads
    as None; the keys the dock sets are recipes, progress and backup."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return self.get(name)


def result_dict(raw: Any, camera: str = "") -> dict[str, Any]:
    """One action result (dict or object) as {camera, ok, packet_id, errors}, plus each PASS_THROUGH
    field the result sets (status_code, events_url and ingest of a send; for Light they are read
    from its re-render, result.send)."""

    def get(name: str, default: Any = None) -> Any:
        if isinstance(raw, dict):
            return raw.get(name, default)
        return getattr(raw, name, default)

    errors = get("errors", None) or []
    if isinstance(errors, str):
        errors = [errors]
    out: dict[str, Any] = {
        "camera": str(get("camera", camera) or camera),
        "ok": bool(get("ok", False)),
        "packet_id": get("packet_id", None),
        "errors": [str(e) for e in errors],
    }
    send = get("send", None)
    for key in PASS_THROUGH:
        value = get(key, None)
        if value is None and isinstance(send, dict):
            value = send.get(key)
        if value is None or value == "" or value == []:
            continue
        out[key] = [str(w) for w in value] if key == "warnings" else value
    return out


def result_message(name: str, result: Mapping[str, Any]) -> tuple[str, bool]:
    """The dock's log line for one result_dict, and whether it is an error: "Sent, ingesting" (202),
    "Sent, ingested", "Already sent" (200: the daemon knew the packet), "Ingest failed: <why>", a
    scout preview, or "<camera> FAILED: <errors>"."""
    camera = result.get("camera") or "?"
    packet_id = result.get("packet_id") or "?"
    ingest = str(result.get("ingest") or "")
    status = result.get("status_code")
    if ingest.startswith("failed"):
        why = (
            ingest[len("failed") :].lstrip(": ") or "; ".join(result.get("errors") or []) or "no reason given"
        )
        return f"{name}: {camera}: Ingest failed: {why}", True
    if not result.get("ok"):
        why = "; ".join(result.get("errors") or []) or "no reason given"
        return f"{name}: {camera} FAILED: {why}", True
    if result.get("preview"):
        tail = f" (packet {packet_id})" if result.get("packet_id") else ""
        return f"{name}: {camera} preview {result['preview']}{tail}", False
    if status == 200:
        return f"{name}: {camera}: Already sent (packet {packet_id})", False
    if ingest == "ingested":
        return f"{name}: {camera}: Sent, ingested (packet {packet_id})", False
    if status == 202 or ingest == "ingesting":
        return f"{name}: {camera}: Sent, ingesting (packet {packet_id})", False
    return f"{name}: {camera} sent (packet {packet_id})", False


def custody_text(custody: Mapping[str, Any] | None) -> str:
    """A project's custody in a few words: "custody unset", "NDA · C0, C1", "C0, C1, C2, D"."""
    if not custody or custody.get("unset"):
        return "custody unset"
    grades = ", ".join(str(g) for g in custody.get("allowed_grades") or []) or "no grades"
    return f"NDA · {grades}" if custody.get("nda") else grades


def packet_custody(custody: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The custody floor a packet carries (the schema's {nda, allowed_grades}) for a project whose
    custody is set; None for an unset or unknown one (a C0 floor in the packet would outlive the
    project's custody being set later)."""
    if not isinstance(custody, Mapping) or custody.get("unset") or not isinstance(custody.get("nda"), bool):
        return None
    out: dict[str, Any] = {"nda": bool(custody["nda"])}
    grades = custody.get("allowed_grades")
    if isinstance(grades, list) and grades:
        out["allowed_grades"] = [str(g) for g in grades]
    return out


# --------------------------------------------------------------------------- the scene's project


def project_slug(text: Any) -> str:
    """A project id from what the artist typed or the daemon listed: an id the schema accepts is
    kept as it is (underscores too), anything else is lower-cased with runs of other characters
    turned into "-". Raises ValueError when nothing usable is left."""
    raw = str(text or "").strip()
    if PROJECT_ID_RE.match(raw):
        return raw
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).strip("-_")[:64].strip("-_")
    if not PROJECT_ID_RE.match(slug):
        raise ValueError(f"{raw!r} is not a project id (lower-case letters, digits, - and _)")
    return slug


def scene_project_id(scene: str | Path | None) -> str | None:
    """The project_id <scene>.plate.json binds the scene to, or None (no sidecar, unsaved scene)."""
    path = recipes.sidecar_path(scene)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    value = data.get(SCENE_PROJECT_KEY) if isinstance(data, dict) else None
    try:
        return project_slug(value) if isinstance(value, str) and value.strip() else None
    except ValueError:
        return None


def save_scene_project_id(project_id: str, scene: str | Path | None) -> Path | None:
    """Write project_id into <scene>.plate.json, keeping every other key (the recipes). None for an
    unsaved scene: there is nowhere next to it to write."""
    path = recipes.sidecar_path(scene)
    if path is None:
        return None
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            existing = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            existing = {}
    return atomic_write_json(path, {**existing, SCENE_PROJECT_KEY: project_slug(project_id)})


def stem_project_id(scene: str | Path | None) -> str:
    """The .max file's stem as a slug (send_packet's own default), "untitled" for an unsaved scene."""
    name = Path(str(scene)).stem if scene else ""
    if not name:
        return "untitled"
    try:
        return packet.slugify(name)
    except packet.ManifestError:
        return "untitled"


def resolve_project_id(scene: str | Path | None, cfg: Mapping[str, Any] | None = None) -> str:
    """The scene's project: <scene>.plate.json, else config.json's project_id, else the stem slug."""
    bound = scene_project_id(scene)
    if bound:
        return bound
    configured = config_mod.project_id(dict(cfg) if cfg is not None else None)
    if configured:
        try:
            return project_slug(configured)
        except ValueError:
            pass
    return stem_project_id(scene)


class Controller(Protocol):
    """Everything the dock needs. cameras(), lint() and the actions touch pymxs and must be called on
    Max's main thread; daemon_status() and projects() are pure I/O and the dock runs them through io."""

    io: Callable[[Callable[[], Any]], Any]

    def capabilities(self) -> set[str]: ...
    def unavailable(self) -> dict[str, str]: ...  # {capability: why its button is disabled}
    def cameras(self) -> list[dict[str, Any]]: ...  # {"name", "class", "hidden"}
    def engines(self) -> list[str]: ...
    def presets(self) -> list[str]: ...
    def overscan_aspects(self) -> list[str]: ...
    def classes(self) -> list[str]: ...
    def recipe(self, camera: str) -> dict[str, Any]: ...
    def set_recipe(self, camera: str, recipe: dict[str, Any]) -> None: ...
    def daemon_status(self) -> dict[str, Any]: ...  # {"reachable", "ok", "daemon_version", "reason", "url"}
    def projects(self) -> list[dict[str, Any]]: ...  # GET /api/v0/projects heads
    def project_id(self) -> str: ...
    def set_project(self, project_id: str) -> Path | None: ...  # where it was saved (None: unsaved scene)
    def project_custody(self, project_id: str | None = None) -> dict[str, Any] | None: ...
    def last_packet(self, camera: str) -> str | None: ...
    def hero_candidates(self, camera: str) -> list[str]: ...
    def set_supersede_last(self, on: bool) -> None: ...
    def make_poller(self, **hooks: Any) -> Any: ...  # a plate_max.jobs.Poller, or None
    def lint(self, camera: str) -> dict[str, Any]: ...  # LintReport.as_dict()
    def send_packet(self, cameras: list[str], progress: Progress) -> list[dict[str, Any]]: ...
    def batch_cameras(self, cameras: list[str], progress: Progress) -> list[dict[str, Any]]: ...
    def receive(self, path: str, mode: str) -> tuple[bool, str]: ...
    def send_motion(
        self, camera: str, progress: Progress, hero_packet_id: str | None = None
    ) -> list[dict[str, Any]]: ...
    def light(
        self, camera: str, progress: Progress, reference_path: str | None = None
    ) -> list[dict[str, Any]]: ...
    def scout(self, progress: Progress, keep: bool = False) -> list[dict[str, Any]]: ...


def _direct(fn: Callable[[], Any]) -> Any:
    return fn()


def _frame(value: Any) -> int | None:
    """A pymxs time value (or a plain number) as a whole frame number."""
    raw = getattr(value, "frame", value)
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return None


def _failed(camera: str, why: str, code: str | None = None) -> dict[str, Any]:
    out = result_dict({"camera": camera, "ok": False, "errors": [why]}, camera)
    if code:
        out["code"] = code
    return out


class LiveController:
    """The Controller over the open Max scene (through plate_max.rt), the loopback daemon and the
    plate_max.actions modules. A capability whose action module is missing stays out of
    capabilities(), and so does Light while MaxGaffer is not installed: the dock keeps those
    buttons disabled and shows unavailable()'s reason as the tooltip."""

    _ACTION_MODULES = {
        CAP_SEND_PACKET: "plate_max.actions.send_packet",
        CAP_BATCH: "plate_max.actions.batch_cameras",
        CAP_RECEIVE: "plate_max.actions.receive",
        CAP_MOTION: "plate_max.actions.send_motion",
        CAP_LIGHT: "plate_max.actions.light",
        CAP_SCOUT: "plate_max.actions.scout",
    }

    def __init__(self, io: Callable[[Callable[[], Any]], Any] | None = None):
        self.io = io or _direct
        self._recipes: dict[str, dict[str, Any]] = {}
        self._last: dict[str, str] = {}  # camera -> the newest still packet this dock sent or relit
        self._last_motion: dict[str, str] = {}  # camera -> the newest motion packet
        self._projects: dict[str, dict[str, Any]] = {}  # GET /projects heads, by project id
        self._project: str | None = None  # the binding, resolved for _project_scene
        self._project_scene: str | None = None
        self.supersede_last = False

    # ---- capabilities
    @staticmethod
    def _module_exists(name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    def _light_ready(self) -> tuple[bool, str]:
        """actions.light.available(): (False, "MAXGAFFER_MISSING: ...") without MaxGaffer."""
        try:
            ok, why = self._action(CAP_LIGHT).available()
        except Exception as exc:  # noqa: BLE001 a broken light module is a disabled button, not a crash
            return False, f"{MAXGAFFER_MISSING}: {type(exc).__name__}: {exc}"
        return bool(ok), str(why or "")

    def capabilities(self) -> set[str]:
        caps = {cap for cap, mod in self._ACTION_MODULES.items() if self._module_exists(mod)}
        if CAP_LIGHT in caps and not self._light_ready()[0]:
            caps.discard(CAP_LIGHT)
        return caps

    def unavailable(self) -> dict[str, str]:
        out = {
            cap: f"{cap} is not in this build"
            for cap, mod in self._ACTION_MODULES.items()
            if not self._module_exists(mod)
        }
        if CAP_LIGHT not in out:
            ok, why = self._light_ready()
            if not ok:
                out[CAP_LIGHT] = why or MAXGAFFER_MISSING
        return out

    def _action(self, cap: str) -> Any:
        return importlib.import_module(self._ACTION_MODULES[cap])

    # ---- scene
    def cameras(self) -> list[dict[str, Any]]:
        from ..rt import runtime

        rt = runtime()
        out = []
        for node in list(getattr(rt, "cameras", [])):
            layer = getattr(node, "layer", None)
            hidden = bool(getattr(node, "isHidden", False)) or bool(getattr(layer, "ishidden", False))
            try:
                cls = str(rt.classOf(node))
            except Exception:  # noqa: BLE001
                cls = ""
            out.append({"name": str(node.name), "class": cls, "hidden": hidden})
        return sorted(out, key=lambda c: c["name"].lower())

    def lint(self, camera: str) -> dict[str, Any]:
        try:
            return policy.lint_scene(camera).as_dict()
        except Exception as exc:  # noqa: BLE001 a missing camera or odd scene is a lint failure, not a crash
            return {
                "passed": False,
                "unlabelled_share": None,
                "problems": [
                    {"code": "LINT_ERROR", "node": camera, "detail": f"{type(exc).__name__}: {exc}"}
                ],
            }

    @staticmethod
    def scene_path() -> str:
        from ..bridge.scene import scene_path

        try:
            return scene_path()
        except Exception:  # noqa: BLE001 no scene readable: an unsaved one
            return ""

    def motion_range(self) -> tuple[int | None, int | None]:
        """The scene's animation range (rt.animationRange) as (f0, f1) frame numbers."""
        from ..bridge.scene import rt_attr

        anim = rt_attr("animationRange")
        return _frame(getattr(anim, "start", None)), _frame(getattr(anim, "end", None))

    # ---- recipe editor menus
    def engines(self) -> list[str]:
        return list(ENGINE_IDS)

    def presets(self) -> list[str]:
        return list(PRESETS)

    def overscan_aspects(self) -> list[str]:
        return list(OVERSCAN_ASPECTS)

    def classes(self) -> list[str]:
        return editable_class_choices()

    def recipe(self, camera: str) -> dict[str, Any]:
        if camera not in self._recipes:
            self._recipes[camera] = default_recipe(camera)
        return dict(self._recipes[camera])

    def set_recipe(self, camera: str, recipe: dict[str, Any]) -> None:
        self._recipes[camera] = {
            **self.recipe(camera),
            **recipe,
            "camera": camera,
        }  # a partial update keeps the rest

    # ---- daemon
    def daemon_status(self) -> dict[str, Any]:
        url = str(load_config().get("daemon_url") or DEFAULT_DAEMON_URL)
        try:
            health = transport.handshake(url)
        except transport.TransportError as exc:
            return {"reachable": False, "ok": False, "daemon_version": None, "reason": str(exc), "url": url}
        ok, reason = transport.compatible(health)
        problems = [str(p) for p in health.get("problems") or []]
        if not ok and problems:  # handshake() already says why (packet version, ok=false): more specific
            reason = "; ".join(problems)
        elif not ok and not reason:
            reason = "daemon not compatible"
        return {
            "reachable": True,
            "ok": ok,
            "daemon_version": health.get("daemon_version"),
            "reason": reason,
            "url": url,
        }

    # ---- project binding
    def projects(self) -> list[dict[str, Any]]:
        """GET /api/v0/projects: the daemon's projects ({project_id, custody, ...}), remembered for
        the custody of each send. Raises transport.TransportError (the dock shows it)."""
        answer = transport.get_json(PROJECTS_PATH, load_config())
        heads = (
            [p for p in answer if isinstance(p, dict) and p.get("project_id")]
            if isinstance(answer, list)
            else []
        )
        self._projects = {str(p["project_id"]): dict(p) for p in heads}
        return [dict(p) for p in heads]

    def project_id(self) -> str:
        """The open scene's project: the dock's choice, else <scene>.plate.json's, else config.json's
        project_id, else the .max file's stem (re-read whenever another scene is open)."""
        scene = self.scene_path()
        if self._project is None or self._project_scene != scene:
            self._project = resolve_project_id(scene, load_config())
            self._project_scene = scene
        return self._project

    def set_project(self, project_id: str) -> Path | None:
        """Bind the open scene to project_id and keep it in <scene>.plate.json. Returns that file, or
        None for an unsaved scene (the binding then lasts until another scene opens). Raises
        ValueError for text that is not a project id."""
        pid = project_slug(project_id)
        scene = self.scene_path()
        self._project, self._project_scene = pid, scene
        return save_scene_project_id(pid, scene)

    def project_custody(self, project_id: str | None = None) -> dict[str, Any] | None:
        """The project's custody from the last projects() answer; None while the daemon does not know
        the project (its first packet creates it with custody unset)."""
        head = self._projects.get(project_id or self.project_id())
        custody = head.get("custody") if head else None
        return dict(custody) if isinstance(custody, dict) else None

    def last_packet(self, camera: str) -> str | None:
        return self._last.get(camera)

    def note_packet(self, camera: str, packet_id: str) -> None:
        """A still packet of camera reached the daemon (the job poller's relight reports here too)."""
        if camera and packet_id:
            self._last[camera] = str(packet_id)

    def hero_candidates(self, camera: str) -> list[str]:
        """Still packets of camera in the packets folder that belong to the scene's project, newest
        first: the hero still Send Motion offers when this dock has sent none of the camera yet."""
        root = packets_dir(load_config())
        project = self.project_id()
        found: list[str] = []
        for manifest_path in root.glob("*/manifest.json") if root.is_dir() else []:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(manifest, dict) or manifest.get("kind") != "still":
                continue
            if (manifest.get("camera") or {}).get("name") != camera:
                continue
            if manifest.get("project_id") not in (None, project):
                continue
            pid = manifest.get("packet_id")
            if isinstance(pid, str) and pid:
                found.append(pid)
        found.sort(reverse=True)  # packet ids are time-sortable ULIDs
        last = self._last.get(camera)
        ordered = ([last] if last else []) + [p for p in found if p != last]
        return ordered[:HERO_SCAN_LIMIT]

    def set_supersede_last(self, on: bool) -> None:
        self.supersede_last = bool(on)

    def make_poller(self, **hooks: Any) -> Any:
        """The dock's job poller (plate_max.jobs.Poller) over this controller: config.json read at
        each poll, the daemon calls through self.io. hooks: log, busy, on_start, on_finish."""
        from .. import jobs

        return jobs.Poller(None, self, interval_s=jobs.DEFAULT_INTERVAL_S, **hooks)

    def job_options(
        self, cameras: Iterable[str], project_id: Any, progress: Progress | None = None
    ) -> ActionOptions:
        """The send options of a canvas job's packets: the job's project (and its custody when known),
        and only the recipes the artist edited in this dock (the scene sidecar's apply otherwise)."""
        names = [str(c) for c in cameras]
        opts = ActionOptions(
            recipes={c: self._recipe_object(c) for c in names if c in self._recipes},
            progress=progress,
            backup=False,
        )
        if project_id:
            opts["project_id"] = str(project_id)
            custody = packet_custody(self.project_custody(str(project_id)))
            if custody:
                opts["custody"] = custody
        return opts

    # ---- actions
    _RECIPES_MODULE = "plate_max.recipes"  # written by the Send/Batch/Receive task; optional here

    def _recipe_object(self, camera: str) -> Any:
        """The camera's recipe as the actions' own CameraRecipe when that module exists, else the dict."""
        rec = self.recipe(camera)
        if not self._module_exists(self._RECIPES_MODULE):
            return rec
        try:
            return importlib.import_module(self._RECIPES_MODULE).CameraRecipe(**rec)
        except Exception:  # noqa: BLE001 a recipe type that does not take these fields: hand over the dict
            return rec

    def _options(self, cameras: Iterable[str], progress: Progress, motion: bool = False) -> ActionOptions:
        """recipes, progress, backup, project_id; custody once the project's is set; supersedes
        when "supersedes last" is ticked and the camera's last packet is known ({camera: id} for a
        still, the one id for Send Motion, whose last packet is the camera's last motion packet)."""
        names = list(cameras)
        opts = ActionOptions(
            recipes={c: self._recipe_object(c) for c in names},
            progress=progress,
            backup=False,
            project_id=self.project_id(),
        )
        custody = packet_custody(self.project_custody())
        if custody:
            opts["custody"] = custody
        if self.supersede_last:
            last = self._last_motion if motion else self._last
            known = {c: last[c] for c in names if last.get(c)}
            if known and motion:
                opts["supersedes"] = next(iter(known.values()))
            elif known:
                opts["supersedes"] = known
        return opts

    def _remember(self, results: list[dict[str, Any]], motion: bool = False) -> list[dict[str, Any]]:
        book = self._last_motion if motion else self._last
        for r in results:
            if r.get("ok") and r.get("packet_id") and r.get("camera"):
                book[r["camera"]] = str(r["packet_id"])
        return results

    def _base(self) -> tuple[dict[str, Any], Path, str]:
        cfg = load_config()
        return cfg, packets_dir(cfg), str(cfg.get("daemon_url") or DEFAULT_DAEMON_URL)

    def _run(self, cap: str, cameras: list[str], progress: Progress) -> list[dict[str, Any]]:
        if cap not in self.capabilities():
            return [result_dict({"ok": False, "errors": [f"{cap} is not available in this build"]})]
        _cfg, out_root, daemon_base = self._base()
        runner = self._action(cap).run
        subject = cameras if cap == CAP_SEND_PACKET else [self._recipe_object(c) for c in cameras]
        results = runner(subject, out_root, daemon_base, self._options(cameras, progress))
        return self._remember([result_dict(r) for r in results])

    def send_packet(self, cameras: list[str], progress: Progress) -> list[dict[str, Any]]:
        return self._run(CAP_SEND_PACKET, cameras, progress)

    def batch_cameras(self, cameras: list[str], progress: Progress) -> list[dict[str, Any]]:
        return self._run(CAP_BATCH, cameras, progress)

    def receive(self, path: str, mode: str) -> tuple[bool, str]:
        if CAP_RECEIVE not in self.capabilities():
            return False, "Receive is not available in this build"
        ok, message = self._action(CAP_RECEIVE).load_result(path, mode)
        return bool(ok), str(message)

    def send_motion(
        self,
        camera: str,
        progress: Progress,
        hero_packet_id: str | None = None,
        frames: tuple[int, int] | None = None,
        crossings: Iterable[dict[str, Any]] = (),
    ) -> list[dict[str, Any]]:
        """Send camera's move over the scene's animation range (or frames) as a motion packet of the
        hero still hero_packet_id (default: the dock's last packet of the camera)."""
        if CAP_MOTION not in self.capabilities():
            return [_failed(camera, "Send Motion is not available in this build")]
        hero = hero_packet_id or self._last.get(camera)
        if not hero:
            return [
                _failed(camera, f"no hero still for {camera}: Send Packet first, or pick its hero packet")
            ]
        f0, f1 = frames if frames is not None else self.motion_range()
        if f0 is None or f1 is None:
            return [_failed(camera, "the scene's animation range could not be read")]
        _cfg, out_root, daemon_base = self._base()
        raw = self._action(CAP_MOTION).run(
            camera,
            hero_packet_id=str(hero),
            f0=int(f0),
            f1=int(f1),
            out_root=out_root,
            daemon_base=daemon_base,
            crossings=list(crossings or ()),
            options=self._options([camera], progress, motion=True),
        )
        return self._remember([result_dict(raw, camera)], motion=True)

    def light(
        self,
        camera: str,
        progress: Progress,
        reference_path: str | None = None,
        locks: Iterable[str] | None = None,
        deep: bool = False,
    ) -> list[dict[str, Any]]:
        """MaxGaffer's match of camera to reference_path, the best state applied, and a re-rendered
        packet that supersedes the camera's last one (actions.light.run: light.match + apply +
        rerender_packet). MAXGAFFER_MISSING without MaxGaffer."""
        if CAP_LIGHT not in self.capabilities():
            why = self.unavailable().get(CAP_LIGHT) or "Light is not available in this build"
            return [_failed(camera, why, MAXGAFFER_MISSING if MAXGAFFER_MISSING in why else None)]
        if not reference_path:
            return [_failed(camera, "Light needs a reference picture to match", "NO_REFERENCE")]
        _cfg, out_root, daemon_base = self._base()
        opts = self._options([camera], progress)
        opts["reference_path"] = str(reference_path)
        opts["deep"] = bool(deep)
        if locks:
            opts["locks"] = sorted({str(k) for k in locks})
        if self._last.get(camera):  # a relight always supersedes; the dock knows which packet
            opts["supersedes"] = {camera: self._last[camera]}
        results = self._action(CAP_LIGHT).run([camera], out_root, daemon_base, opts)
        return self._remember([result_dict(r, camera) for r in results])

    def scout(self, progress: Progress, keep: bool = False) -> list[dict[str, Any]]:
        """The scout poses as temporary cameras with low-res previews (actions.scout.run); keep leaves
        the cameras in the scene. No packets: a scout pose becomes a plate only when it is sent."""
        if CAP_SCOUT not in self.capabilities():
            return [_failed("", "Scout is not available in this build")]
        _cfg, out_root, daemon_base = self._base()
        opts = self._options([], progress)
        opts["keep"] = bool(keep)
        results = self._action(CAP_SCOUT).run(out_root, daemon_base, opts)
        return [result_dict(r) for r in results]


class FakeController:
    """A scripted Controller: real recipe plumbing, canned scene and daemon answers, every call
    recorded in .calls as (name, ...). Used by tests/test_ui_dock.py and scripts/ui_preview.py.
    It makes no job poller unless .poller is set (a dock under test never polls a real daemon)."""

    def __init__(
        self,
        cameras: list[str] | None = None,
        capabilities: Iterable[str] = (CAP_SEND_PACKET, CAP_BATCH, CAP_RECEIVE),
    ):
        names = cameras if cameras is not None else ["CAM_main", "CAM_terrace", "CAM_lobby"]
        self._cameras = [{"name": n, "class": "VRayPhysicalCamera", "hidden": False} for n in names]
        self._capabilities = set(capabilities)
        self.calls: list[tuple[Any, ...]] = []
        self.io: Callable[[Callable[[], Any]], Any] = _direct
        self.status: dict[str, Any] = {
            "reachable": True,
            "ok": True,
            "daemon_version": "0.1.0",
            "reason": "",
            "url": "http://127.0.0.1:4610",
        }
        self.lint_reports: dict[str, dict[str, Any]] = {}
        self.results: list[dict[str, Any]] | None = None
        self.raise_on: str | None = None
        self.receive_result: tuple[bool, str] = (True, "loaded into the VFB history")
        self._recipes: dict[str, dict[str, Any]] = {}
        self.on_action: Callable[[str], None] | None = None
        self.unavailable_reasons: dict[str, str] = {}
        self.project_list: list[dict[str, Any]] = [
            {"project_id": "clubhouse", "custody": {"nda": False, "allowed_grades": ["C0", "C1", "C2"]}},
            {"project_id": "tula-wot", "custody": {"nda": True, "allowed_grades": ["C0", "C1"]}},
        ]
        self.projects_error: Exception | None = None
        self.bound_project = "clubhouse"
        self.saved_to: Path | None = None  # what set_project() reports as the sidecar it wrote
        self.last_packets: dict[str, str] = {}
        self.heroes: dict[str, list[str]] = {}
        self.supersede_last = False
        self.poller: Any = None
        self.poller_hooks: dict[str, Any] = {}

    def capabilities(self) -> set[str]:
        return set(self._capabilities)

    def unavailable(self) -> dict[str, str]:
        return {cap: why for cap, why in self.unavailable_reasons.items() if cap not in self._capabilities}

    def cameras(self) -> list[dict[str, Any]]:
        self.calls.append(("cameras",))
        return [dict(c) for c in self._cameras]

    def engines(self) -> list[str]:
        return list(ENGINE_IDS)

    def presets(self) -> list[str]:
        return list(PRESETS)

    def overscan_aspects(self) -> list[str]:
        return list(OVERSCAN_ASPECTS)

    def classes(self) -> list[str]:
        return editable_class_choices()

    def recipe(self, camera: str) -> dict[str, Any]:
        if camera not in self._recipes:
            self._recipes[camera] = default_recipe(camera)
        return dict(self._recipes[camera])

    def set_recipe(self, camera: str, recipe: dict[str, Any]) -> None:
        self.calls.append(("set_recipe", camera, dict(recipe)))
        self._recipes[camera] = {
            **self.recipe(camera),
            **recipe,
            "camera": camera,
        }  # a partial update keeps the rest

    def daemon_status(self) -> dict[str, Any]:
        self.calls.append(("daemon_status",))
        return dict(self.status)

    def projects(self) -> list[dict[str, Any]]:
        self.calls.append(("projects",))
        if self.projects_error is not None:
            raise self.projects_error
        return [dict(p) for p in self.project_list]

    def project_id(self) -> str:
        return self.bound_project

    def set_project(self, project_id: str) -> Path | None:
        pid = project_slug(project_id)
        self.calls.append(("set_project", pid))
        self.bound_project = pid
        return self.saved_to

    def project_custody(self, project_id: str | None = None) -> dict[str, Any] | None:
        wanted = project_id or self.bound_project
        for head in self.project_list:
            if head.get("project_id") == wanted and isinstance(head.get("custody"), dict):
                return dict(head["custody"])
        return None

    def last_packet(self, camera: str) -> str | None:
        return self.last_packets.get(camera)

    def note_packet(self, camera: str, packet_id: str) -> None:
        self.last_packets[camera] = str(packet_id)

    def hero_candidates(self, camera: str) -> list[str]:
        return list(self.heroes.get(camera, []))

    def set_supersede_last(self, on: bool) -> None:
        self.calls.append(("set_supersede_last", bool(on)))
        self.supersede_last = bool(on)

    def make_poller(self, **hooks: Any) -> Any:
        self.poller_hooks = dict(hooks)
        return self.poller

    def lint(self, camera: str) -> dict[str, Any]:
        self.calls.append(("lint", camera))
        return self.lint_reports.get(camera, {"passed": True, "unlabelled_share": 0.04, "problems": []})

    def _act(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
        if self.on_action:
            self.on_action(name)
        if self.raise_on == name:
            raise RuntimeError(f"{name} blew up")

    def _ok_results(self, cameras: list[str]) -> list[dict[str, Any]]:
        if self.results is not None:
            return self.results
        out = [
            {"camera": c, "ok": True, "packet_id": f"01FAKE{n:03d}", "errors": []}
            for n, c in enumerate(cameras)
        ]
        for r in out:
            self.last_packets[r["camera"]] = r["packet_id"]
        return out

    def send_packet(self, cameras: list[str], progress: Progress) -> list[dict[str, Any]]:
        self._act("send_packet", list(cameras))
        for i, cam in enumerate(cameras, 1):
            progress(i, len(cameras), f"sent {cam}")
        return self._ok_results(cameras)

    def batch_cameras(self, cameras: list[str], progress: Progress) -> list[dict[str, Any]]:
        self._act("batch_cameras", list(cameras))
        for i, cam in enumerate(cameras, 1):
            progress(i, len(cameras), f"batch {cam}")
        return self._ok_results(cameras)

    def receive(self, path: str, mode: str) -> tuple[bool, str]:
        self._act("receive", path, mode)
        return self.receive_result

    def send_motion(
        self, camera: str, progress: Progress, hero_packet_id: str | None = None
    ) -> list[dict[str, Any]]:
        self._act("send_motion", camera, hero_packet_id)
        if self.results is not None:
            return self.results
        return [{"camera": camera, "ok": True, "packet_id": "01FAKEMOTION", "errors": []}]

    def light(
        self, camera: str, progress: Progress, reference_path: str | None = None
    ) -> list[dict[str, Any]]:
        self._act("light", camera, reference_path)
        return self._ok_results([camera])

    def scout(self, progress: Progress, keep: bool = False) -> list[dict[str, Any]]:
        self._act("scout", keep)
        return []
