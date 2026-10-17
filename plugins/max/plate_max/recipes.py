"""Per-camera recipes: what each camera's packet is for and how it is rendered, kept next to the scene.

A CameraRecipe is the dock's recipe editor in data: the engine and preset the shot is meant for, the
deliverable safe frame, the engine aspect to overscan to, the classes AI may edit in this shot and the
opt-in render elements. Recipes live in <scene>.plate.json next to the .max file (MaxGaffer's
<scene>.maxgaffer.json pattern: human-readable, diff-able, survives a Max crash, never bloats the
scene), UTF-8 JSON written temp-then-os.replace (fsutil). An unsaved scene has no sidecar.

Doctrine (locked by default): editable_classes can only narrow what the policy rules already make
editable; a class no AI edit may touch (architecture, glazing, signage, other) is refused here.
Stdlib only.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import policy
from .bridge.elements import OPTIN_CLASSES
from .fsutil import atomic_write_json

FORMAT_VERSION = 1
SIDECAR_SUFFIX = ".plate.json"
DEFAULT_ENGINE_ID = "nano-banana-2.1"
ASPECT_RE = re.compile(r"^([1-9][0-9]{0,4}):([1-9][0-9]{0,4})$")


class RecipeError(ValueError):
    """A recipe that must not be used as written (the message says which field and why)."""


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


def sidecar_path(scene: str | Path | None) -> Path | None:
    """foo.max -> foo.plate.json next to it (None for an unsaved scene)."""
    if not scene:
        return None
    path = Path(str(scene))
    if not path.name:
        return None
    stem = path.with_suffix("") if path.suffix.lower() == ".max" else path
    return stem.with_name(stem.name + SIDECAR_SUFFIX)


def _current_scene() -> str:
    from .bridge.scene import scene_path

    return scene_path()


def load_recipes(scene: str | Path | None = None) -> dict[str, CameraRecipe]:
    """{camera: CameraRecipe} from the scene's sidecar (scene default: the open .max). A missing
    sidecar is no recipes; a malformed entry is skipped (the camera then gets the default recipe)."""
    path = sidecar_path(_current_scene() if scene is None else scene)
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
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
    RecipeError for an unsaved scene: there is nowhere next to it to write."""
    path = sidecar_path(_current_scene() if scene is None else scene)
    if path is None:
        raise RecipeError("the scene has never been saved: save the .max first, recipes live next to it")
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            existing = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            existing = {}
    cameras = {name: CameraRecipe.coerce(recipe, name).to_dict() for name, recipe in sorted(recipes.items())}
    return atomic_write_json(path, {**existing, "format_version": FORMAT_VERSION, "cameras": cameras})


def get_recipe(camera: str, scene: str | Path | None = None) -> CameraRecipe:
    """The camera's saved recipe, else the default one."""
    return load_recipes(scene).get(camera) or CameraRecipe(camera=camera)


def set_recipe(recipe: Any, scene: str | Path | None = None) -> Path:
    """Save one camera's recipe into the sidecar, keeping the others."""
    rec = CameraRecipe.coerce(recipe)
    recipes: dict[str, Any] = dict(load_recipes(scene))
    recipes[rec.camera] = rec
    return save_recipes(recipes, scene)
