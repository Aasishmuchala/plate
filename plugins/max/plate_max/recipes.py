"""Per-camera recipes: what each camera's packet is for and how it is rendered, kept next to the scene.

A CameraRecipe is the dock's recipe editor in data: the engine and preset the shot is meant for, the
deliverable safe frame, the engine aspect to overscan to, the classes AI may edit in this shot and the
opt-in render elements. Recipes live in <scene>.plate.json next to the .max file (MaxGaffer's
<scene>.maxgaffer.json pattern: human-readable, diff-able, survives a Max crash, never bloats the
scene), UTF-8 JSON written temp-then-os.replace (fsutil). An unsaved scene has no sidecar. A scene whose
name leaves no room for ".plate.json" next to it (a file name past 255 bytes, or a path past Windows'
259 characters) keeps its sidecar under <app data>/sidecars/<hash of the scene path>.plate.json
instead (sidecar_note says so for the dock's log): long names bind and send like any other.

Doctrine (locked by default): editable_classes can only narrow what the policy rules already make
editable; a class no AI edit may touch (architecture, glazing, signage, other) is refused here.
Stdlib only.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config, policy
from .bridge.elements import OPTIN_CLASSES
from .fsutil import atomic_write_json

FORMAT_VERSION = 1
SIDECAR_SUFFIX = ".plate.json"
PROJECT_KEY = "project_id"  # the key the scene's project binding is kept under (ui.controller)
DEFAULT_ENGINE_ID = "nano-banana-2.1"
ASPECT_RE = re.compile(r"^([1-9][0-9]{0,4}):([1-9][0-9]{0,4})$")
NAME_SHOWN = 80  # a sidecar file name longer than this is elided in a message
NAME_MAX_BYTES = 255  # one file name, UTF-8 bytes (NTFS counts 255 UTF-16 units; bytes is the stricter)
WINDOWS_MAX_PATH = 259  # MAX_PATH less the terminating NUL; the plugin does not rely on long-path opt-in


def platform_is_windows() -> bool:
    """os.name == "nt", behind a hook so a test can pretend without breaking pathlib."""
    return os.name == "nt"


def __getattr__(name: str) -> Any:
    """SIDECAR_FALLBACK_DIR (<app data>/sidecars) is resolved on each use, not at import: PLATE_HOME
    may change after the module loads (tests, the installer)."""
    if name == "SIDECAR_FALLBACK_DIR":
        return config.app_data_dir() / "sidecars"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def sidecar_fallback_dir() -> Path:
    """Where sidecars of scenes with too long a name live (a test may set SIDECAR_FALLBACK_DIR)."""
    override = globals().get("SIDECAR_FALLBACK_DIR")
    return Path(override) if override is not None else config.app_data_dir() / "sidecars"


def fits_filesystem(path: Path) -> bool:
    """Whether path can be created here: its file name within 255 UTF-8 bytes and, on Windows, the
    whole path within WINDOWS_MAX_PATH characters."""
    path = Path(path)
    if len(path.name.encode("utf-8")) > NAME_MAX_BYTES:
        return False
    return not platform_is_windows() or len(str(path)) <= WINDOWS_MAX_PATH


def scene_key(scene: str | Path) -> str:
    """16 hex characters naming a scene by its resolved path: the fallback sidecar's file name and the
    fallback backup's (so the two can be matched by eye)."""
    path = Path(str(scene))
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        path = path.absolute()
    return hashlib.blake2b(str(path).encode("utf-8"), digest_size=8).hexdigest()


class RecipeError(ValueError):
    """A recipe that must not be used as written (the message says which field and why)."""


def _shown(name: str) -> str:
    return name if len(name) <= NAME_SHOWN else f"{name[:40]}...{name[-30:]}"


class SidecarUnreadable(ValueError):
    """<scene>.plate.json exists but cannot be trusted: it is not UTF-8 JSON holding an object, or its
    project_id is not a string. Nothing may be written over it and the scene must not fall back to
    another project silently; the message tells the artist to fix or delete the file."""

    def __init__(self, path: Path | str, reason: str, message: str | None = None):
        self.path = Path(path)
        self.reason = str(reason)
        super().__init__(
            message
            or f"{_shown(self.path.name)} is unreadable ({self.reason}): fix or delete it before sending"
        )


class SidecarUnwritable(SidecarUnreadable):
    """The sidecar cannot be written (a file name past what the file system allows, a read-only
    folder): the OSError as a message the artist can act on, never a bare OSError."""

    def __init__(self, path: Path | str, reason: str):
        name = _shown(Path(path).name)
        super().__init__(
            path,
            reason,
            f"{name} cannot be written ({reason}): shorten the scene's file name or save the scene elsewhere",
        )


def default_editable_classes() -> list[str]:
    """The classes the policy's editable layer prefixes map to (vegetation, sky, people, water,
    furniture): the same default the dock's recipe editor starts from."""
    out: list[str] = []
    for rule in policy.CLASS_RULES:
        cls = rule["class"]
        if (
            str(rule.get("pattern", "")).startswith(policy.EDITABLE_PREFIXES)
            and cls not in policy.NEVER_EDITABLE_CLASSES
            and cls not in out
        ):
            out.append(cls)
    return out


def parse_aspect(text: str) -> tuple[int, int]:
    """ "16:9" -> (16, 9). Raises RecipeError for anything else."""
    match = ASPECT_RE.match(str(text).strip())
    if not match:
        raise RecipeError(f"overscan_aspect {text!r} is not W:H (for example 16:9)")
    return int(match.group(1)), int(match.group(2))


def _rect(value: Any) -> dict[str, int] | None:
    """{x, y, w, h} from {x, y, w|width, h|height} or (x, y, w, h); None stays None."""
    if value is None:
        return None
    try:
        if isinstance(value, (list, tuple)):
            x, y, w, h = value
        elif isinstance(value, dict):
            x, y = value.get("x", 0), value.get("y", 0)
            w = value["w"] if "w" in value else value["width"]
            h = value["h"] if "h" in value else value["height"]
        else:
            raise TypeError(type(value).__name__)
        rect = {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
    except (KeyError, TypeError, ValueError) as exc:
        raise RecipeError(f"safe_frame {value!r} is not {{x, y, w, h}}") from exc
    if rect["x"] < 0 or rect["y"] < 0 or rect["w"] < 1 or rect["h"] < 1:
        raise RecipeError(f"safe_frame {rect} needs x, y >= 0 and w, h >= 1")
    return rect


@dataclass
class CameraRecipe:
    camera: str
    engine_id: str = DEFAULT_ENGINE_ID
    preset: str = ""
    safe_frame: dict[str, int] | None = None  # {x, y, w, h} in the scene's render pixels
    overscan_aspect: str | None = None  # e.g. "16:9": the engine aspect the frame is padded to
    editable_classes: list[str] = field(default_factory=default_editable_classes)
    extra_elements: list[str] = field(default_factory=list)  # V-Ray classes, bridge.elements.OPTIN_CLASSES

    def __post_init__(self) -> None:
        if not isinstance(self.camera, str) or not self.camera:
            raise RecipeError("a recipe needs the camera name")
        self.engine_id = str(self.engine_id or "")
        self.preset = str(self.preset or "")
        self.safe_frame = _rect(self.safe_frame)
        if self.overscan_aspect in ("", None):
            self.overscan_aspect = None
        else:
            a, b = parse_aspect(str(self.overscan_aspect))
            self.overscan_aspect = f"{a}:{b}"
        classes = [str(c) for c in (self.editable_classes or [])]
        never = sorted(set(classes) & set(policy.NEVER_EDITABLE_CLASSES))
        if never:
            raise RecipeError(
                f"editable_classes {never}: architecture, glazing, signage and unclassified ids are never "
                "AI-editable"
            )
        self.editable_classes = list(dict.fromkeys(classes))
        extras = [str(e) for e in (self.extra_elements or [])]
        unknown = sorted(set(extras) - set(OPTIN_CLASSES))
        if unknown:
            raise RecipeError(f"extra_elements {unknown}: opt-ins are {', '.join(OPTIN_CLASSES)}")
        self.extra_elements = list(dict.fromkeys(extras))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any], camera: str | None = None) -> CameraRecipe:
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        if camera is not None:
            known["camera"] = camera
        return cls(**known)

    @classmethod
    def coerce(cls, value: Any, camera: str | None = None) -> CameraRecipe:
        """A CameraRecipe from a CameraRecipe, a dict of its fields, or any object carrying them
        (the dock hands either over). camera fills or overrides the camera name."""
        if isinstance(value, cls):
            return (
                value if camera is None or value.camera == camera else cls.from_dict(value.to_dict(), camera)
            )
        if isinstance(value, dict):
            return cls.from_dict(value, camera)
        if value is None:
            if camera is None:
                raise RecipeError("no recipe and no camera name")
            return cls(camera=camera)
        fields = {k: getattr(value, k) for k in cls.__dataclass_fields__ if hasattr(value, k)}
        return cls.from_dict(fields, camera)

    def digest_recipe(self, safe_frame: dict[str, int] | None, overscan: dict[str, str] | None) -> dict:
        """What digest.camera_digest(recipe=) reads: the safe frame and overscan block of the frame
        actually rendered (computed by Send from this recipe and the render size)."""
        out: dict[str, Any] = {}
        if safe_frame is not None:
            out["safe_frame"] = dict(safe_frame)
        if overscan is not None:
            out["overscan"] = dict(overscan)
        return out


# --------------------------------------------------------------------------- the sidecar


def natural_sidecar_path(scene: str | Path | None) -> Path | None:
    """foo.max -> foo.plate.json next to it, whether or not the file system would take that name
    (None for an unsaved scene)."""
    if not scene:
        return None
    path = Path(str(scene))
    if not path.name:
        return None
    stem = path.with_suffix("") if path.suffix.lower() == ".max" else path
    return stem.with_name(stem.name + SIDECAR_SUFFIX)


def sidecar_path(scene: str | Path | None) -> Path | None:
    """foo.max -> foo.plate.json next to it (None for an unsaved scene). When that name does not fit
    the file system (fits_filesystem) the sidecar is <app data>/sidecars/<scene_key>.plate.json, so
    load_recipes, save_recipes and the project binding work for any scene name."""
    natural = natural_sidecar_path(scene)
    if natural is None or fits_filesystem(natural):
        return natural
    return sidecar_fallback_dir() / f"{scene_key(str(scene))}{SIDECAR_SUFFIX}"


def sidecar_note(scene: str | Path | None) -> str | None:
    """A line for the dock's log when the scene's sidecar is not beside it (None when it is, or the
    scene is unsaved)."""
    natural = natural_sidecar_path(scene)
    if natural is None or fits_filesystem(natural):
        return None
    return f"sidecar stored under {sidecar_fallback_dir()} (the scene name is too long for one beside it)"


def _current_scene() -> str:
    from .bridge.scene import scene_path

    return scene_path()


def _why_not_json(text: str, exc: Exception) -> str:
    """Why text that failed to parse is not a sidecar, in words an artist can act on."""
    if not text.strip():
        return "the file is empty"
    if any(line.startswith(("<<<<<<<", ">>>>>>>")) for line in text.splitlines()):
        return "unresolved merge conflict markers"
    return f"not valid JSON: {exc}"


def read_sidecar(path: Path, check_project: bool = True) -> dict[str, Any] | None:
    """The sidecar's JSON object, None when there is no file (absent is not unreadable: a scene that
    was never bound has none). Raises SidecarUnreadable for a file that exists but is not UTF-8 JSON
    holding an object (an empty file, a trailing comma, UTF-16, conflict markers, a list), is a
    directory or cannot be opened, or (check_project) whose project_id is present and not a string.
    A file name past what the file system allows cannot exist, so it reads as absent."""
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            return None
        raise SidecarUnreadable(path, f"cannot be read: {exc.strerror or exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        utf16 = raw.startswith((b"\xff\xfe", b"\xfe\xff"))
        raise SidecarUnreadable(path, "UTF-16 text: save it as UTF-8" if utf16 else "not UTF-8 text") from exc
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise SidecarUnreadable(path, _why_not_json(text, exc)) from exc
    if not isinstance(data, dict):
        raise SidecarUnreadable(path, f"holds a JSON {type(data).__name__}, not an object")
    if check_project and PROJECT_KEY in data and not isinstance(data[PROJECT_KEY], str):
        raise SidecarUnreadable(path, f"{PROJECT_KEY} is not a string")
    return data


def write_sidecar(path: Path, data: dict[str, Any]) -> Path:
    """atomic_write_json with the OSError (a file name too long for the file system, a read-only
    folder) as SidecarUnwritable. Callers read_sidecar first, so an unreadable file is never replaced."""
    try:
        return atomic_write_json(path, data)
    except OSError as exc:
        raise SidecarUnwritable(path, exc.strerror or str(exc)) from exc


def load_recipes(scene: str | Path | None = None) -> dict[str, CameraRecipe]:
    """{camera: CameraRecipe} from the scene's sidecar (scene default: the open .max). A missing
    sidecar is no recipes; a malformed entry is skipped (the camera then gets the default recipe); an
    unreadable sidecar is no recipes too (the dock refuses to send before it gets here)."""
    path = sidecar_path(_current_scene() if scene is None else scene)
    if path is None:
        return {}
    try:
        data = read_sidecar(path, check_project=False)
    except SidecarUnreadable:
        return {}
    cameras = data.get("cameras") if isinstance(data, dict) else None
    out: dict[str, CameraRecipe] = {}
    for name, entry in (cameras or {}).items():
        if not isinstance(entry, dict):
            continue
        try:
            out[str(name)] = CameraRecipe.from_dict(entry, str(name))
        except (RecipeError, TypeError):
            continue
    return out


def save_recipes(recipes: dict[str, Any], scene: str | Path | None = None) -> Path:
    """Write every recipe into the scene's sidecar (keys other than "cameras" are kept). Raises
    RecipeError for an unsaved scene: there is nowhere next to it to write; SidecarUnreadable (nothing
    written) when the sidecar exists but is unreadable, SidecarUnwritable when it cannot be written."""
    path = sidecar_path(_current_scene() if scene is None else scene)
    if path is None:
        raise RecipeError("the scene has never been saved: save the .max first, recipes live next to it")
    existing = read_sidecar(path) or {}
    cameras = {name: CameraRecipe.coerce(recipe, name).to_dict() for name, recipe in sorted(recipes.items())}
    return write_sidecar(path, {**existing, "format_version": FORMAT_VERSION, "cameras": cameras})


def get_recipe(camera: str, scene: str | Path | None = None) -> CameraRecipe:
    """The camera's saved recipe, else the default one."""
    return load_recipes(scene).get(camera) or CameraRecipe(camera=camera)


def set_recipe(recipe: Any, scene: str | Path | None = None) -> Path:
    """Save one camera's recipe into the sidecar, keeping the others."""
    rec = CameraRecipe.coerce(recipe)
    recipes: dict[str, Any] = dict(load_recipes(scene))
    recipes[rec.camera] = rec
    return save_recipes(recipes, scene)
