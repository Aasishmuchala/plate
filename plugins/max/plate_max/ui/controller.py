"""What the dock talks to: a Controller protocol, the live implementation over plate_max, and a
FakeController for tests and the offscreen preview. Standard library only (no Qt), so it imports and
tests without PySide6.

Threading contract (copied from MaxGaffer's dock): every pymxs touch runs on Max's main thread; slow
pure I/O (the daemon health call) goes through Controller.io, which the dock replaces with a
QThread-plus-local-event-loop runner so Max stays responsive. Controller methods never raise for an
expected failure; they return it as data (ok False, problems, reason) for the dock to show.

Action contract with the plate_max.actions modules (written by the Send/Batch/Receive task): the
live controller calls them only when the module exists, so a build without them simply has those
buttons disabled.
  actions.send_packet.run(camera_names, out_root, daemon_base, options) -> list[result]
  actions.batch_cameras.run(recipes, out_root, daemon_base, options) -> list[result]
  actions.receive.load_result(path, mode) -> (ok, message)
options is an ActionOptions (a dict that also reads as attributes): recipes {camera: recipe},
progress callable(done, total, message), backup False. A result is a dict or an object with camera,
ok, packet_id, errors. A recipe is the CameraRecipe field set: camera, engine_id, preset, safe_frame
{x,y,w,h} | None, overscan_aspect | None, editable_classes, extra_elements; it reaches the actions as a
plate_max.recipes.CameraRecipe when that module exists, else as a plain dict of those fields.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from .. import policy, transport
from ..config import DEFAULT_DAEMON_URL, load_config, packets_dir

Progress = Callable[[int, int, str], None]

CAP_SEND_PACKET = "send_packet"
CAP_BATCH = "batch_cameras"
CAP_RECEIVE = "receive"
CAP_MOTION = "send_motion"
CAP_LIGHT = "light"
CAP_SCOUT = "scout"
ALL_CAPABILITIES = (CAP_SEND_PACKET, CAP_BATCH, CAP_RECEIVE, CAP_MOTION, CAP_LIGHT, CAP_SCOUT)

RECEIVE_MODES = ("vfb_history", "viewport_background")

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
    """One action result (dict or object) as {camera, ok, packet_id, errors}."""

    def get(name: str, default: Any = None) -> Any:
        if isinstance(raw, dict):
            return raw.get(name, default)
        return getattr(raw, name, default)

    errors = get("errors", None) or []
    if isinstance(errors, str):
        errors = [errors]
    return {
        "camera": str(get("camera", camera) or camera),
        "ok": bool(get("ok", False)),
        "packet_id": get("packet_id", None),
        "errors": [str(e) for e in errors],
    }


class Controller(Protocol):
    """Everything the dock needs. cameras() and lint() touch pymxs and must be called on Max's main
    thread; daemon_status() is pure I/O and the dock runs it through io."""

    io: Callable[[Callable[[], Any]], Any]

    def capabilities(self) -> set[str]: ...
    def cameras(self) -> list[dict[str, Any]]: ...  # {"name", "class", "hidden"}
    def engines(self) -> list[str]: ...
    def presets(self) -> list[str]: ...
    def overscan_aspects(self) -> list[str]: ...
    def classes(self) -> list[str]: ...
    def recipe(self, camera: str) -> dict[str, Any]: ...
    def set_recipe(self, camera: str, recipe: dict[str, Any]) -> None: ...
    def daemon_status(self) -> dict[str, Any]: ...  # {"reachable", "ok", "daemon_version", "reason", "url"}
    def lint(self, camera: str) -> dict[str, Any]: ...  # LintReport.as_dict()
    def send_packet(self, cameras: list[str], progress: Progress) -> list[dict[str, Any]]: ...
    def batch_cameras(self, cameras: list[str], progress: Progress) -> list[dict[str, Any]]: ...
    def receive(self, path: str, mode: str) -> tuple[bool, str]: ...
    def send_motion(self, camera: str, progress: Progress) -> list[dict[str, Any]]: ...
    def light(self, camera: str, progress: Progress) -> list[dict[str, Any]]: ...
    def scout(self, progress: Progress) -> list[dict[str, Any]]: ...


def _direct(fn: Callable[[], Any]) -> Any:
    return fn()


class LiveController:
    """The Controller over the open Max scene (through plate_max.rt), the loopback daemon and the
    plate_max.actions modules. Motion, Light and Scout stay out of capabilities() until their
    actions are wired, so the dock keeps those buttons disabled."""

    _ACTION_MODULES = {
        CAP_SEND_PACKET: "plate_max.actions.send_packet",
        CAP_BATCH: "plate_max.actions.batch_cameras",
        CAP_RECEIVE: "plate_max.actions.receive",
    }

    def __init__(self, io: Callable[[Callable[[], Any]], Any] | None = None):
        self.io = io or _direct
        self._recipes: dict[str, dict[str, Any]] = {}

    # ---- capabilities
    @staticmethod
    def _module_exists(name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    def capabilities(self) -> set[str]:
        return {cap for cap, mod in self._ACTION_MODULES.items() if self._module_exists(mod)}

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

    def _options(self, cameras: Iterable[str], progress: Progress) -> ActionOptions:
        recipes = {c: self._recipe_object(c) for c in cameras}
        return ActionOptions(recipes=recipes, progress=progress, backup=False)

    def _run(self, cap: str, cameras: list[str], progress: Progress) -> list[dict[str, Any]]:
        if cap not in self.capabilities():
            return [result_dict({"ok": False, "errors": [f"{cap} is not available in this build"]})]
        cfg = load_config()
        runner = self._action(cap).run
        subject = cameras if cap == CAP_SEND_PACKET else [self._recipe_object(c) for c in cameras]
        daemon_base = cfg.get("daemon_url") or DEFAULT_DAEMON_URL
        results = runner(subject, packets_dir(cfg), daemon_base, self._options(cameras, progress))
        return [result_dict(r) for r in results]

    def send_packet(self, cameras: list[str], progress: Progress) -> list[dict[str, Any]]:
        return self._run(CAP_SEND_PACKET, cameras, progress)

    def batch_cameras(self, cameras: list[str], progress: Progress) -> list[dict[str, Any]]:
        return self._run(CAP_BATCH, cameras, progress)

    def receive(self, path: str, mode: str) -> tuple[bool, str]:
        if CAP_RECEIVE not in self.capabilities():
            return False, "Receive is not available in this build"
        ok, message = self._action(CAP_RECEIVE).load_result(path, mode)
        return bool(ok), str(message)

    def send_motion(self, camera: str, progress: Progress) -> list[dict[str, Any]]:
        raise NotImplementedError("Send Motion is not wired yet")

    def light(self, camera: str, progress: Progress) -> list[dict[str, Any]]:
        raise NotImplementedError("Light is not wired yet")

    def scout(self, progress: Progress) -> list[dict[str, Any]]:
        raise NotImplementedError("Scout is not wired yet")


class FakeController:
    """A scripted Controller: real recipe plumbing, canned scene and daemon answers, every call
    recorded in .calls as (name, ...). Used by tests/test_ui_dock.py and scripts/ui_preview.py."""

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

    def capabilities(self) -> set[str]:
        return set(self._capabilities)

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
        return [
            {"camera": c, "ok": True, "packet_id": f"01FAKE{n:03d}", "errors": []}
            for n, c in enumerate(cameras)
        ]

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

    def send_motion(self, camera: str, progress: Progress) -> list[dict[str, Any]]:
        self._act("send_motion", camera)
        return self._ok_results([camera])

    def light(self, camera: str, progress: Progress) -> list[dict[str, Any]]:
        self._act("light", camera)
        return self._ok_results([camera])

    def scout(self, progress: Progress) -> list[dict[str, Any]]:
        self._act("scout")
        return []
