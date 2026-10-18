"""Recipes: Lensbook's archviz doctrine as data, plus the three runnable P1 recipes (task T33).

Library (ported verbatim from ~/Desktop/lensbook/lib/data.ts and assemble.ts):

* `arch_stills.yaml`: the Scene Lock foundation `af-a` and stills `a01` to `a11`.
* `arch_shots.yaml`: shots `as01` to `as14` with their motion `risk`, and `ARCH_PREFIX`, the Style Prefix every
  `prefixed` shot gets once the Project Sheet has filled it.
* `risk_ladder.yaml`: the move risk ladder (T43's Move policy keys on `RiskRow.key` and `.risk`).
* `repairs.yaml`: repair codes A1 to A5 and F2 to F12, vegetation tells T1 to T10, material tells M1 to M8.
* `project_sheet.yaml`: the 14 Project Sheet fields and the 17 token rules behind `fill_tokens`.

Runnable (`RunnableRecipe`, what `plate run <recipe.yaml>` takes) come from three folders, a later one
shadowing an earlier id: `shipped` (this package: the three P1 recipes and Lensbook's a01 to a10 promoted to
runnable), `studio` (`<app-data>/recipes/`) and `project` (`<project>/recipes/`). `save_runnable` writes the
second and third; the shipped folder is never written. The schema is in README.md.

Nothing here talks to a vendor or a model: the composer (`plate_daemon.llm.composer`) calls `assemble` for its
template-only path and for the text it hands the model.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..config import app_home
from ..fsutil import write_bytes_atomic

__all__ = [
    "DEFAULT_NEGATIVE_PROMPT",
    "KINDS",
    "ORIGINS",
    "RECIPES_DIR",
    "RISKS",
    "Recipe",
    "RecipeError",
    "RecipeExists",
    "RepairRow",
    "RiskRow",
    "RunnableRecipe",
    "ProjectSheet",
    "TokenRule",
    "arch_prefix",
    "assemble",
    "assemble_runnable",
    "fill_tokens",
    "get_recipe",
    "is_recipe_id",
    "list_runnable",
    "load_runnable",
    "mat_tells",
    "parse_runnable",
    "project_recipes_dir",
    "repair",
    "repairs",
    "risk_for",
    "risk_ladder",
    "runnable_entries",
    "save_runnable",
    "shots",
    "stills",
    "studio_dir",
    "token_rules",
    "unfilled_count",
    "unfilled_tokens",
    "veg_tells",
]

log = logging.getLogger("plate_daemon")

RECIPES_DIR = Path(__file__).resolve().parent
ORIGINS = ("shipped", "studio", "project")  # the order of the folders; a later one shadows an earlier id
RISKS = ("none", "low", "med", "high", "highest")
KINDS = ("still_edit", "still_rephotograph", "motion_croppair")
CROPPAIR_RULES = {  # croppair.direction -> the zoom pair it needs (T94)
    "in": "1.0 <= z0 < z1 <= 3.0",
    "out": "1.0 <= z1 < z0 <= 3.0",
    "locked": "1.0 <= z0 == z1 <= 3.0",
}
REGIONS = ("none", "mask_bbox")
SHOT_CLASSES = ("wide", "medium", "close", "detail")
# Used when a recipe carries no negative prompt of its own (the Scene Lock, as a list of things not to do).
DEFAULT_NEGATIVE_PROMPT = (
    "changed architecture, redrawn geometry, altered perspective, re-framing, added objects, removed objects, "
    "restyled materials, text, logos, captions"
)
_BRACKET = re.compile(r"\[([^\]]+)\]")
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
# A recipe id is a file name; these are device names on Windows whatever the extension.
_RESERVED_IDS = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)
_CAS_HASH = re.compile(r"^[0-9a-f]{16,128}$")


class RecipeError(ValueError):
    """A recipe file or lookup that does not fit the schema."""


class RecipeExists(RecipeError):
    """`save_runnable` found the id already taken (in this folder or another) and `overwrite` was off."""


# ---------------------------------------------------------------------------- loading


def _load(name: str) -> dict[str, Any]:
    path = RECIPES_DIR / name
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise RecipeError(f"{path}: unreadable recipe file ({e})") from e
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise RecipeError(f"{path}: expected a mapping with schema: 1")
    return doc


def _text(entry: Mapping[str, Any], key: str, where: str, *, required: bool = True) -> str | None:
    v = entry.get(key)
    if v is None:
        if required:
            raise RecipeError(f"{where}: {key} is required")
        return None
    if not isinstance(v, str) or (required and not v.strip()):
        raise RecipeError(f"{where}: {key} must be a non-empty string")
    return v


# ---------------------------------------------------------------------------- the library


@dataclass(frozen=True)
class Recipe:
    """One Lensbook recipe. `prefixed` shots get the filled Style Prefix on top (see `assemble`)."""

    id: str
    num: str
    title: str
    use: str
    prompt: str
    dial: str | None = None
    risk: str | None = None
    foundation: bool = False
    prefixed: bool = False
    negative_prompt: str | None = None


@dataclass(frozen=True)
class RepairRow:
    code: str
    symptom: str
    fix: str


@dataclass(frozen=True)
class RiskRow:
    """A move on the risk ladder. `key` is the move id T43 uses, `risk` one of `RISKS`, `repair_codes` index
    `repairs()`."""

    key: str
    move: str
    risk: str
    label: str
    invents: str
    repair_codes: tuple[str, ...] = ()


def _recipe_from(entry: Any, where: str) -> Recipe:
    if not isinstance(entry, dict):
        raise RecipeError(f"{where}: expected a mapping")
    risk = entry.get("risk")
    if risk is not None and risk not in ("low", "med", "high"):
        raise RecipeError(f"{where}: risk must be low, med or high, got {risk!r}")
    return Recipe(
        id=str(_text(entry, "id", where)),
        num=str(entry.get("num", "")),
        title=str(_text(entry, "title", where)),
        use=str(_text(entry, "use", where)),
        prompt=str(_text(entry, "prompt", where)),
        dial=_text(entry, "dial", where, required=False),
        risk=risk,
        foundation=bool(entry.get("foundation", False)),
        prefixed=bool(entry.get("prefixed", False)),
        negative_prompt=_text(entry, "negative_prompt", where, required=False),
    )


@lru_cache(maxsize=1)
def stills() -> tuple[Recipe, ...]:
    """`af-a` (the Scene Lock) then `a01` to `a11`."""
    doc = _load("arch_stills.yaml")
    return tuple(_recipe_from(e, f"arch_stills.yaml: recipes[{i}]") for i, e in enumerate(doc["recipes"]))


@lru_cache(maxsize=1)
def shots() -> tuple[Recipe, ...]:
    """`as01` to `as14`; every one is `prefixed` and carries a `risk`."""
    doc = _load("arch_shots.yaml")
    return tuple(_recipe_from(e, f"arch_shots.yaml: recipes[{i}]") for i, e in enumerate(doc["recipes"]))


@lru_cache(maxsize=1)
def arch_prefix() -> str:
    """ARCH_PREFIX, the 10-line Style Prefix with its [SLOTS] still unfilled."""
    return str(_text(_load("arch_shots.yaml"), "prefix", "arch_shots.yaml"))


def get_recipe(recipe_id: str) -> Recipe:
    """A library recipe by id (`a01`, `as02`, `af-a`)."""
    for r in (*stills(), *shots()):
        if r.id == recipe_id:
            return r
    raise RecipeError(f"no library recipe {recipe_id!r}")


@lru_cache(maxsize=1)
def risk_ladder() -> tuple[RiskRow, ...]:
    doc = _load("risk_ladder.yaml")
    rows = []
    for i, e in enumerate(doc["moves"]):
        where = f"risk_ladder.yaml: moves[{i}]"
        if e.get("risk") not in RISKS:
            raise RecipeError(f"{where}: risk must be one of {list(RISKS)}, got {e.get('risk')!r}")
        rows.append(
            RiskRow(
                key=str(_text(e, "key", where)),
                move=str(_text(e, "move", where)),
                risk=e["risk"],
                label=str(_text(e, "label", where)),
                invents=str(_text(e, "invents", where)),
                repair_codes=tuple(str(c) for c in e.get("repair_codes") or ()),
            )
        )
    return tuple(rows)


def risk_for(move_key: str) -> RiskRow:
    for row in risk_ladder():
        if row.key == move_key:
            return row
    raise RecipeError(f"no move {move_key!r} on the risk ladder; known: {[r.key for r in risk_ladder()]}")


def _rows(section: str) -> tuple[RepairRow, ...]:
    entries = _load("repairs.yaml").get(section) or []
    return tuple(
        RepairRow(
            str(_text(e, "code", f"repairs.yaml: {section}[{i}]")),
            str(_text(e, "symptom", f"repairs.yaml: {section}[{i}]")),
            str(_text(e, "fix", f"repairs.yaml: {section}[{i}]")),
        )
        for i, e in enumerate(entries)
    )


@lru_cache(maxsize=1)
def repairs() -> tuple[RepairRow, ...]:
    """ARCH_REPAIRS: A1 to A5 and F2 to F12."""
    return _rows("repairs")


@lru_cache(maxsize=1)
def veg_tells() -> tuple[RepairRow, ...]:
    return _rows("veg_tells")


@lru_cache(maxsize=1)
def mat_tells() -> tuple[RepairRow, ...]:
    return _rows("mat_tells")


def repair(code: str) -> RepairRow:
    """A repair, vegetation tell or material tell by its code (`A1`, `F6`, `T3`, `M5`)."""
    for row in (*repairs(), *veg_tells(), *mat_tells()):
        if row.code == code:
            return row
    raise RecipeError(f"no repair or tell with code {code!r}")


# ---------------------------------------------------------------------------- the Project Sheet


@dataclass(frozen=True)
class ProjectSheet:
    """The per-film Project Sheet (Lensbook's 14 fields). Everything is optional: an unfilled field leaves its
    [TOKEN] visible. Defaults are Lensbook's EMPTY_SHEET."""

    name: str = ""
    location: str = ""
    format: str = "4K architectural film, 16:9"
    light_logic: str = ""
    palette_dominant: str = ""
    palette_secondary: str = ""
    palette_accent: str = ""
    lens_character: str = ""
    depth_register: str = "deep legible for exteriors"
    species: str = ""
    materials: str = ""
    climate: str = ""
    audio_bed: str = ""
    scale_note: str = "door height 2.1m"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ProjectSheet:
        """Accepts the snake_case field names or Lensbook's camelCase ones; an unknown key is an error."""
        if not data:
            return cls()
        by_lensbook = {s["lensbook_key"]: s["key"] for s in _sheet_doc()["fields"]}
        known = {f.name for f in fields(cls)}
        values: dict[str, str] = {}
        unknown = []
        for k, v in data.items():
            name = by_lensbook.get(k, k)
            if name not in known:
                unknown.append(k)
            elif v is not None:
                if not isinstance(v, str):
                    raise RecipeError(f"project sheet field {k!r} must be text, got {type(v).__name__}")
                values[name] = v
        if unknown:
            raise RecipeError(f"unknown project sheet field(s) {sorted(unknown)}; known: {sorted(known)}")
        return cls(**values)

    def as_dict(self) -> dict[str, str]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class TokenRule:
    pattern: re.Pattern[str]
    field: str


@lru_cache(maxsize=1)
def _sheet_doc() -> dict[str, Any]:
    doc = _load("project_sheet.yaml")
    names = [s["key"] for s in doc["fields"]]
    have = [f.name for f in fields(ProjectSheet)]
    if names != have:
        raise RecipeError(f"project_sheet.yaml fields {names} do not match ProjectSheet {have}")
    for s in doc["fields"]:
        if getattr(ProjectSheet(), s["key"]) != s["default"]:
            raise RecipeError(f"project_sheet.yaml: default of {s['key']!r} differs from ProjectSheet")
    return doc


@lru_cache(maxsize=1)
def token_rules() -> tuple[TokenRule, ...]:
    """The 17 token rules, in Lensbook's order (first match with a filled sheet field wins)."""
    known = {f.name for f in fields(ProjectSheet)}
    rules = []
    for i, t in enumerate(_sheet_doc()["tokens"]):
        if t.get("field") not in known:
            raise RecipeError(f"project_sheet.yaml: tokens[{i}] names unknown field {t.get('field')!r}")
        rules.append(TokenRule(re.compile(str(t["pattern"]), re.IGNORECASE), t["field"]))
    return tuple(rules)


def fill_tokens(text: str, sheet: ProjectSheet, overrides: Mapping[str, str] | None = None) -> str:
    """Replace known [TOKENS] with Project Sheet values; leave the rest for manual fill or the composer.

    `overrides` is keyed by the literal bracketed token (`"[2700K]"`) and wins over the sheet when non-blank.
    A rule whose sheet field is blank does not stop later rules from matching (Lensbook behaviour)."""
    overrides = overrides or {}

    def sub(m: re.Match[str]) -> str:
        whole, inner = m.group(0), m.group(1).strip()
        over = overrides.get(whole)
        if over and over.strip():
            return over
        for rule in token_rules():
            if rule.pattern.search(inner):
                value = getattr(sheet, rule.field)
                if value and value.strip():
                    return value
        return whole

    return _BRACKET.sub(sub, text)


def unfilled_count(text: str) -> int:
    """How many [TOKENS] the text still carries."""
    return len(_BRACKET.findall(text))


def unfilled_tokens(text: str) -> list[str]:
    """The [TOKEN] texts the text still carries, each once, in order of appearance (what an override is keyed by)."""
    return list(dict.fromkeys(m.group(0) for m in _BRACKET.finditer(text)))


def assemble(recipe: Any, sheet: ProjectSheet, overrides: Mapping[str, str] | None = None) -> str:
    """Copy-ready text for a recipe: `prefixed` recipes get the filled Style Prefix on top. Works for a library
    `Recipe` and a `RunnableRecipe` (anything with `.prompt` and `.prefixed`)."""
    body = fill_tokens(recipe.prompt, sheet, overrides)
    if not getattr(recipe, "prefixed", False):
        return body
    return f"{fill_tokens(arch_prefix(), sheet, overrides)}\n\n{body}"


# ---------------------------------------------------------------------------- runnable recipes


@dataclass(frozen=True)
class RunnableRecipe:
    """A recipe `plate run` can execute: which engine, what the mask allows, how the router checks it.

    `overrides` are the recipe's own token defaults (literal "[TOKEN]" keys) so it runs without a composer;
    `library` names the Lensbook recipe whose doctrine it applies. `origin` is derived from the folder the file
    was found in (never written), `group` heads the recipe in a grouped select, `thumbnail` is a CAS hash and
    `task` the engine-card task (`plate_engines.tasks`) the recipe's runs belong to (None = the node decides)."""

    id: str
    title: str
    kind: str
    engine: str
    prompt: str
    library: str | None = None
    shot_class: str = "wide"
    mask: Mapping[str, Any] = field(default_factory=dict)
    router_check: Mapping[str, Any] | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    croppair: Mapping[str, Any] | None = None
    motion: Mapping[str, Any] | None = None
    overrides: Mapping[str, str] = field(default_factory=dict)
    negative_prompt: str | None = None
    prefixed: bool = False
    source: str = ""
    origin: str = "shipped"
    group: str | None = None
    thumbnail: str | None = None
    task: str | None = None


def _mapping(
    entry: Mapping[str, Any], key: str, where: str, *, required: bool = False
) -> dict[str, Any] | None:
    v = entry.get(key)
    if v is None:
        if required:
            raise RecipeError(f"{where}: {key} is required")
        return None
    if not isinstance(v, dict):
        raise RecipeError(f"{where}: {key} must be a mapping")
    return dict(v)


def _runnable_from(doc: Any, where: str, origin: str = "shipped") -> RunnableRecipe:
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise RecipeError(f"{where}: expected a mapping with schema: 1")
    rid = str(_text(doc, "id", where))
    if not _ID.match(rid):
        raise RecipeError(f"{where}: id {rid!r} must be lower_snake_case")
    if rid in _RESERVED_IDS:
        raise RecipeError(f"{where}: id {rid!r} is a reserved device name on Windows; pick another")
    kind = doc.get("kind")
    if kind not in KINDS:
        raise RecipeError(f"{where}: kind must be one of {list(KINDS)}, got {kind!r}")
    shot_class = doc.get("shot_class", "wide")
    if shot_class not in SHOT_CLASSES:
        raise RecipeError(f"{where}: shot_class must be one of {list(SHOT_CLASSES)}, got {shot_class!r}")
    library = doc.get("library")
    if library is not None:
        get_recipe(str(library))  # a library id that does not exist is a typo, not a preference
    mask = _mapping(doc, "mask", where) or {}
    editable = mask.get("editable_classes", [])
    if not isinstance(editable, list) or not all(isinstance(c, str) for c in editable):
        raise RecipeError(f"{where}: mask.editable_classes must be a list of class names")
    region = mask.get("region", "none")
    if region not in REGIONS:
        raise RecipeError(f"{where}: mask.region must be one of {list(REGIONS)}, got {region!r}")
    croppair = _mapping(doc, "croppair", where)
    if kind == "motion_croppair":
        if croppair is None:
            raise RecipeError(f"{where}: a motion_croppair recipe needs a croppair block")
        z0, z1 = croppair.get("z0"), croppair.get("z1")
        # T94: `direction` is the zoom's: in (a push-in, the default), out (a pull-back) or locked (one zoom: a held
        # frame, or a slider drift with `center_end`). A pair that does not match its direction is a typo.
        direction = croppair.get("direction", "in")
        if direction not in CROPPAIR_RULES:
            raise RecipeError(
                f"{where}: croppair.direction must be one of {list(CROPPAIR_RULES)}, got {direction!r}"
            )
        ok = isinstance(z0, int | float) and isinstance(z1, int | float) and 1.0 <= min(z0, z1)
        ok = ok and max(z0, z1) <= 3.0
        ok = ok and {"in": z0 < z1, "out": z0 > z1, "locked": z0 == z1}[direction]
        if not ok:
            raise RecipeError(
                f"{where}: croppair needs {CROPPAIR_RULES[direction]} ({direction}), got {z0!r}, {z1!r}"
            )
    overrides = _mapping(doc, "overrides", where) or {}
    bad = [
        k for k, v in overrides.items() if not (k.startswith("[") and k.endswith("]") and isinstance(v, str))
    ]
    if bad:
        raise RecipeError(f"{where}: overrides keys must be literal [TOKEN] text with a text value: {bad}")
    group = _text(doc, "group", where, required=False)
    thumbnail = _text(doc, "thumbnail", where, required=False)
    if thumbnail is not None and not _CAS_HASH.match(thumbnail):
        raise RecipeError(f"{where}: thumbnail must be a CAS hash (lower-case hex), got {thumbnail!r}")
    task = _text(doc, "task", where, required=False)
    if task is not None:
        from plate_engines.tasks import TASKS

        if task not in TASKS:
            raise RecipeError(f"{where}: task must be one of {sorted(TASKS)}, got {task!r}")
    return RunnableRecipe(
        id=rid,
        title=str(_text(doc, "title", where)),
        kind=kind,
        engine=str(_text(doc, "engine", where)),
        prompt=str(_text(doc, "prompt", where)),
        library=str(library) if library is not None else None,
        shot_class=shot_class,
        mask={**mask, "editable_classes": list(editable), "region": region},
        router_check=_mapping(doc, "router_check", where),
        params=_mapping(doc, "params", where) or {},
        croppair=croppair,
        motion=_mapping(doc, "motion", where),
        overrides=dict(overrides),
        negative_prompt=_text(doc, "negative_prompt", where, required=False),
        prefixed=bool(doc.get("prefixed", False)),
        source=where,
        origin=origin,
        group=group,
        thumbnail=thumbnail,
        task=task,
    )


def is_recipe_id(name: str) -> bool:
    """True for a string that can name a runnable recipe (and so a file `<id>.yaml`): lower_snake_case, not a
    Windows device name. A caller that takes an id from outside checks this before `load_runnable`, which would
    treat anything ending in `.yaml` as a file path."""
    return bool(_ID.match(name)) and name not in _RESERVED_IDS


def parse_runnable(doc: Any, origin: str = "project") -> RunnableRecipe:
    """Validate a schema-1 recipe doc (what `save_runnable` checks first) without writing anything."""
    if not isinstance(doc, Mapping):
        raise RecipeError("a recipe must be a mapping")
    return _runnable_from({k: v for k, v in doc.items() if k != "origin"}, "recipe", origin)


def studio_dir() -> Path:
    """`<app-data>/recipes`: the studio's own recipes (`PLATE_HOME` moves it, as it moves the rest of app data)."""
    return app_home() / "recipes"


def project_recipes_dir(project_dir: Path | str) -> Path:
    """`<project>/recipes`: recipes that travel with one project."""
    return Path(project_dir) / "recipes"


def _folders(project_dir: Path | str | None) -> list[tuple[str, Path]]:
    folders = [("shipped", RECIPES_DIR), ("studio", studio_dir())]
    if project_dir is not None:
        folders.append(("project", project_recipes_dir(project_dir)))
    return folders


_DOCS: dict[str, tuple[int, int, Any]] = {}
_DOCS_LOCK = threading.Lock()


def _read_doc(path: Path) -> Any:
    """`yaml.safe_load` of a recipe file, kept until the file's mtime or size changes: listing the recipes
    parses every file in three folders and loading one asks for the list again, so without this a catalogue
    request is quadratic in the number of recipes. Callers must not mutate what they get back."""
    st = path.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    key = str(path)
    with _DOCS_LOCK:
        hit = _DOCS.get(key)
    if hit is not None and hit[:2] == stamp:
        return hit[2]
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    with _DOCS_LOCK:
        _DOCS[key] = (*stamp, doc)
    return doc


def _scan(origin: str, folder: Path) -> dict[str, Path]:
    """{id: path} of the runnable recipe files in `folder` (a `kind:` key from `KINDS`; the id is the file
    stem, which is also how `load_runnable` finds it). A file that cannot be read is left out."""
    found: dict[str, Path] = {}
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.yaml")):
        try:
            doc = _read_doc(path)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if not (isinstance(doc, dict) and doc.get("kind") in KINDS):
            continue
        if str(doc.get("id", path.stem)) != path.stem:
            log.warning(
                "%s recipe %s: id %r differs from the file name; left out", origin, path, doc.get("id")
            )
            continue
        found[path.stem] = path
    return found


def _index(project_dir: Path | str | None) -> dict[str, tuple[str, Path]]:
    """{id: (origin, path)}: shipped, then studio, then project; a later folder shadows an earlier id but the
    id keeps the position it first appeared at."""
    out: dict[str, tuple[str, Path]] = {}
    for origin, folder in _folders(project_dir):
        for rid, path in _scan(origin, folder).items():
            out[rid] = (origin, path)
    return out


def list_runnable(project_dir: Path | str | None = None) -> list[str]:
    """Ids of the runnable recipes: shipped, studio and (with `project_dir`) that project's own."""
    return list(_index(project_dir))


def _origin_of(path: Path) -> str:
    """Which folder a recipe loaded by file path belongs to; a file anywhere else is the artist's own, so it
    counts as project."""
    here = path.resolve().parent
    for origin, folder in (("shipped", RECIPES_DIR), ("studio", studio_dir())):
        if here == folder.resolve():
            return origin
    return "project"


def load_runnable(name_or_path: str | Path, project_dir: Path | str | None = None) -> RunnableRecipe:
    """A runnable recipe by id (`night_relight_changelist`: project folder, then studio, then shipped) or by
    file path (`plate run my.yaml`)."""
    p = Path(name_or_path)
    if p.suffix in (".yaml", ".yml") or p.is_file():
        origin = _origin_of(p)
    else:
        name = str(name_or_path)
        hit = _index(project_dir).get(name) if is_recipe_id(name) else None
        if hit is None:
            raise RecipeError(f"no runnable recipe {name!r}; known: {list_runnable(project_dir)}")
        origin, p = hit
    try:
        doc = copy.deepcopy(_read_doc(p))
    except (OSError, UnicodeDecodeError) as e:
        raise RecipeError(f"{p}: cannot read recipe ({e})") from e
    except yaml.YAMLError as e:
        raise RecipeError(f"{p}: not valid YAML ({e})") from e
    return _runnable_from(doc, p.name, origin)


def runnable_entries(project_dir: Path | str | None = None) -> list[RunnableRecipe]:
    """Every runnable recipe in `list_runnable` order. One that fails to load is logged and left out."""
    out = []
    for rid in list_runnable(project_dir):
        try:
            out.append(load_runnable(rid, project_dir))
        except RecipeError as e:
            log.warning("recipe %s left out: %s", rid, e)
    return out


class _Dumper(yaml.SafeDumper):
    """safe_dump that writes a multi-line string (a prompt) as a block scalar, so a saved recipe reads like the
    shipped ones and diffs line by line."""


def _str_repr(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None)


_Dumper.add_representer(str, _str_repr)


def save_runnable(
    doc: Mapping[str, Any],
    scope: str,
    project_dir: Path | str | None = None,
    overwrite: bool = False,
) -> Path:
    """Write a schema-1 recipe to the studio folder (`scope="studio"`) or the project's (`scope="project"`,
    needs `project_dir`) and return its path. The doc is validated first, exactly as a file on disk would be.
    An id that already exists in any folder is refused (`RecipeExists`) unless `overwrite`; the shipped folder
    is never written, so a shipped id can only be shadowed."""
    if scope not in ("studio", "project"):
        raise RecipeError(f"scope must be studio or project, got {scope!r}")
    if scope == "project" and project_dir is None:
        raise RecipeError("a project recipe needs its project folder")
    recipe = parse_runnable(doc, scope)
    body = {k: v for k, v in doc.items() if k != "origin"}  # origin is derived from the folder, never written
    folder = studio_dir() if scope == "studio" else project_recipes_dir(project_dir)  # type: ignore[arg-type]
    held = _index(project_dir).get(recipe.id)
    if held is not None and not overwrite:
        raise RecipeExists(
            f"recipe {recipe.id!r} already exists ({held[0]}: {held[1]}); pass overwrite to replace it"
        )
    target = folder / f"{recipe.id}.yaml"
    text = yaml.dump(body, Dumper=_Dumper, sort_keys=False, allow_unicode=True)
    write_bytes_atomic(target, text.encode("utf-8"))
    with _DOCS_LOCK:
        _DOCS.pop(str(target), None)  # a rewrite inside one mtime tick must not be read back stale
    return target


def assemble_runnable(
    recipe: RunnableRecipe, sheet: ProjectSheet, overrides: Mapping[str, str] | None = None
) -> str:
    """Template-only text for a runnable recipe: its own token defaults, then the caller's overrides, then the
    Project Sheet."""
    return assemble(recipe, sheet, {**recipe.overrides, **(overrides or {})})
