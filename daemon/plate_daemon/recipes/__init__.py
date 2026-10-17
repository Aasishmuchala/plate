"""Recipes: Lensbook's archviz doctrine as data, plus the three runnable P1 recipes (task T33).

Library (ported verbatim from ~/Desktop/lensbook/lib/data.ts and assemble.ts):

* `arch_stills.yaml`: the Scene Lock foundation `af-a` and stills `a01` to `a11`.
* `arch_shots.yaml`: shots `as01` to `as14` with their motion `risk`, and `ARCH_PREFIX`, the Style Prefix every
  `prefixed` shot gets once the Project Sheet has filled it.
* `risk_ladder.yaml`: the move risk ladder (T43's Move policy keys on `RiskRow.key` and `.risk`).
* `repairs.yaml`: repair codes A1 to A5 and F2 to F12, vegetation tells T1 to T10, material tells M1 to M8.
* `project_sheet.yaml`: the 14 Project Sheet fields and the 17 token rules behind `fill_tokens`.

Runnable (`RunnableRecipe`, what `plate run <recipe.yaml>` takes): `night_relight_changelist`,
`sunburst_rephotograph_close`, `croppair_pushin`. The schema is in README.md.

Nothing here talks to a vendor or a model: the composer (`plate_daemon.llm.composer`) calls `assemble` for its
template-only path and for the text it hands the model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "DEFAULT_NEGATIVE_PROMPT",
    "RECIPES_DIR",
    "RISKS",
    "Recipe",
    "RecipeError",
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
    "list_runnable",
    "load_runnable",
    "mat_tells",
    "repair",
    "repairs",
    "risk_for",
    "risk_ladder",
    "shots",
    "stills",
    "token_rules",
    "unfilled_count",
    "veg_tells",
]

RECIPES_DIR = Path(__file__).resolve().parent
RISKS = ("none", "low", "med", "high", "highest")
KINDS = ("still_edit", "still_rephotograph", "motion_croppair")
REGIONS = ("none", "mask_bbox")
SHOT_CLASSES = ("wide", "medium", "close", "detail")
# Used when a recipe carries no negative prompt of its own (the Scene Lock, as a list of things not to do).
DEFAULT_NEGATIVE_PROMPT = (
    "changed architecture, redrawn geometry, altered perspective, re-framing, added objects, removed objects, "
    "restyled materials, text, logos, captions"
)
_BRACKET = re.compile(r"\[([^\]]+)\]")
_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class RecipeError(ValueError):
    """A recipe file or lookup that does not fit the schema."""


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
    `library` names the Lensbook recipe whose doctrine it applies."""

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


def _runnable_from(doc: Any, where: str) -> RunnableRecipe:
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise RecipeError(f"{where}: expected a mapping with schema: 1")
    rid = str(_text(doc, "id", where))
    if not _ID.match(rid):
        raise RecipeError(f"{where}: id {rid!r} must be lower_snake_case")
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
        if not (isinstance(z0, int | float) and isinstance(z1, int | float) and 1.0 <= z0 < z1 <= 3.0):
            raise RecipeError(f"{where}: croppair needs 1.0 <= z0 < z1 <= 3.0, got {z0!r}, {z1!r}")
    overrides = _mapping(doc, "overrides", where) or {}
    bad = [
        k for k, v in overrides.items() if not (k.startswith("[") and k.endswith("]") and isinstance(v, str))
    ]
    if bad:
        raise RecipeError(f"{where}: overrides keys must be literal [TOKEN] text with a text value: {bad}")
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
    )


def list_runnable() -> list[str]:
    """Ids of the runnable recipes shipped beside this module (the files with a `kind:` key)."""
    out = []
    for path in sorted(RECIPES_DIR.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(doc, dict) and doc.get("kind") in KINDS:
            out.append(str(doc.get("id", path.stem)))
    return out


def load_runnable(name_or_path: str | Path) -> RunnableRecipe:
    """A runnable recipe by id (`night_relight_changelist`) or by file path (`plate run my.yaml`)."""
    p = Path(name_or_path)
    if not (p.suffix in (".yaml", ".yml") or p.exists()):
        p = RECIPES_DIR / f"{name_or_path}.yaml"
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise RecipeError(f"{p}: cannot read recipe ({e})") from e
    except yaml.YAMLError as e:
        raise RecipeError(f"{p}: not valid YAML ({e})") from e
    return _runnable_from(doc, p.name)


def assemble_runnable(
    recipe: RunnableRecipe, sheet: ProjectSheet, overrides: Mapping[str, str] | None = None
) -> str:
    """Template-only text for a runnable recipe: its own token defaults, then the caller's overrides, then the
    Project Sheet."""
    return assemble(recipe, sheet, {**recipe.overrides, **(overrides or {})})
