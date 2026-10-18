"""Graph templates: a chain of canvas nodes an artist lays down for a packet in one step (router plan T95).

A template is a YAML file in one of two folders, a later one shadowing an earlier id: `shipped` (this package:
`edit_chain`, `night_chain`, `set_chain`) and `project` (`<project>/templates/`). The schema:

    schema: 1
    id: edit_chain                 # the file stem; lower case letters, digits and _
    title: Edit chain              # what the artist reads
    set: false                     # true: after every packet's chain, one Set node fed by every new Edit
    nodes:                         # in build order; the first listed Plate is row 0 of the chain
      - suffix: plate              # the name `inputs` of a later node refers to
        kind: Plate                # a canvas_rules.NODE_KINDS entry
      - suffix: mask
        kind: Mask
        label: Mask                # optional, default the kind
        params: {classes: [sky]}   # optional; a Mask's classes pick its ids from the packet
        inputs: {source: "$packet"}   # input port -> "$plate" | "$packet" | the suffix of an earlier node

`$plate` and `$packet` are the packet's Plate node (made once per packet, see `graph_build`) and its `image` and
`packet` outputs; a suffix is the first output of that template node. Loading checks the shape, that every
input port exists on its kind, that every ref points at an earlier node (so a template has no cycle) and that
the output's type is one the input takes (`canvas_rules.PORTS`, the mirror of the UI's `ports.ts`), so a bad
project template is refused when it is read, not when the artist applies it.

`list(project_dir=None)` returns every template (the dicts `load` returns, each with its `origin`); `load(id,
project_dir=None)` one. A project template that does not parse is left out of the list with a warning and is
an error from `load`. (`list` shadows the builtin in this module on purpose, as the router plan names it, so
the module reaches the builtin as `builtins.list`.)
"""

from __future__ import annotations

import builtins
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .. import canvas_rules

__all__ = [
    "ORIGINS",
    "TEMPLATES_DIR",
    "TemplateError",
    "TemplateNotFound",
    "list",
    "list_templates",
    "load",
    "parse",
    "project_templates_dir",
]

log = logging.getLogger("plate_daemon")

TEMPLATES_DIR = Path(__file__).resolve().parent
ORIGINS = ("shipped", "project")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SUFFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PLATE_REFS = {"$plate": "image", "$packet": "packet"}
# Kinds a template cannot lay down: a Reference needs an image of its own and a Frame is a note on the canvas.
NOT_TEMPLATABLE = frozenset({"Reference", "Frame"})


class TemplateError(ValueError):
    """A template file that cannot be used."""


class TemplateNotFound(TemplateError):
    """No template has that id."""


def project_templates_dir(project_dir: Path | str) -> Path:
    """`<project>/templates`: templates that travel with one project."""
    return Path(project_dir) / "templates"


def _ports(kind: str, side: str) -> tuple[Any, ...]:
    return (canvas_rules.PORTS.get(kind) or {}).get(side, ())


def parse(doc: Any, *, where: str = "template", stem: str | None = None) -> dict[str, Any]:
    """A template document checked and normalised: `{id, title, nodes, set}` (every node has `suffix`, `kind`,
    `label`, `params`, `inputs`). Raises `TemplateError` naming the first problem."""
    if not isinstance(doc, Mapping) or doc.get("schema") != 1:
        raise TemplateError(f"{where}: expected a mapping with schema: 1")
    tid = doc.get("id")
    if not isinstance(tid, str) or not ID_RE.match(tid):
        raise TemplateError(f"{where}: id must be lower case letters, digits and _ (got {tid!r})")
    if stem is not None and tid != stem:
        raise TemplateError(f"{where}: id {tid!r} must be the file name {stem!r}")
    title = doc.get("title")
    if not isinstance(title, str) or not title.strip():
        raise TemplateError(f"{where}: title is required")
    flag = doc.get("set", False)
    if not isinstance(flag, bool):
        raise TemplateError(f"{where}: set must be true or false")
    raw = doc.get("nodes")
    if not isinstance(raw, builtins.list) or not raw:
        raise TemplateError(f"{where}: nodes must be a list with at least one node")
    nodes: builtins.list[dict[str, Any]] = []
    by_suffix: dict[str, str] = {}
    for i, entry in enumerate(raw):
        at = f"{where}: nodes[{i}]"
        if not isinstance(entry, Mapping):
            raise TemplateError(f"{at} must be a mapping")
        suffix, kind = entry.get("suffix"), entry.get("kind")
        if not isinstance(suffix, str) or not SUFFIX_RE.match(suffix):
            raise TemplateError(f"{at}: suffix must be lower case letters, digits and _ (got {suffix!r})")
        if suffix in by_suffix:
            raise TemplateError(f"{at}: suffix {suffix!r} appears twice")
        if kind not in canvas_rules.NODE_KINDS or kind in NOT_TEMPLATABLE:
            raise TemplateError(f"{at}: kind {kind!r} is not a node a template can lay down")
        if kind == "Plate" and "Plate" in by_suffix.values():
            raise TemplateError(f"{at}: a template has one Plate")
        label = entry.get("label", kind)
        if not isinstance(label, str) or not label:
            raise TemplateError(f"{at}: label must be a string")
        params = entry.get("params") or {}
        if not isinstance(params, Mapping):
            raise TemplateError(f"{at}: params must be a mapping")
        classes = params.get("classes")
        if classes is not None and not (
            isinstance(classes, builtins.list) and all(isinstance(c, str) and c for c in classes)
        ):
            raise TemplateError(f"{at}: params.classes must be a list of class names")
        inputs = entry.get("inputs") or {}
        if not isinstance(inputs, Mapping):
            raise TemplateError(f"{at}: inputs must be a mapping of port to ref")
        for port, ref in inputs.items():
            _check_input(at, str(kind), str(port), ref, by_suffix)
        by_suffix[suffix] = str(kind)
        nodes.append(
            {
                "suffix": suffix,
                "kind": kind,
                "label": label,
                "params": _plain(params),
                "inputs": {str(p): str(r) for p, r in inputs.items()},
            }
        )
    return {"id": tid, "title": title.strip(), "nodes": nodes, "set": flag}


def _check_input(at: str, kind: str, port: str, ref: Any, earlier: Mapping[str, str]) -> None:
    target = next((p for p in _ports(kind, "inputs") if p[0] == port), None)
    if target is None:
        raise TemplateError(f"{at}: a {kind} node has no input {port!r}")
    if not isinstance(ref, str) or not ref:
        raise TemplateError(f"{at}: input {port!r} needs a ref ($plate, $packet or an earlier suffix)")
    if ref in PLATE_REFS:
        handle, source_kind = PLATE_REFS[ref], "Plate"
    elif ref in earlier:
        source_kind = earlier[ref]
        outputs = _ports(source_kind, "outputs")
        if not outputs:
            raise TemplateError(f"{at}: input {port!r} takes {ref!r}, a {source_kind} node with no output")
        handle = outputs[0][0]
    else:
        raise TemplateError(
            f"{at}: input {port!r} names {ref!r}, which is not $plate, $packet or an earlier node"
        )
    out = next((p for p in _ports(source_kind, "outputs") if p[0] == handle), None)
    if out is None or out[1] not in target[2]:
        carried = out[1] if out is not None else "nothing"
        raise TemplateError(
            f"{at}: {kind} input {port!r} takes {'/'.join(target[2])}, but {ref!r} carries {carried}"
        )


def _plain(value: Any) -> Any:
    """Nested mappings and lists as plain dicts and lists, so a caller may change what it gets back."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (builtins.list, tuple)):
        return [_plain(v) for v in value]
    return value


def _read(path: Path, origin: str) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise TemplateError(f"{path}: unreadable template file ({e})") from e
    template = parse(doc, where=str(path), stem=path.stem)
    template["origin"] = origin
    return template


def _folders(project_dir: Path | str | None) -> builtins.list[tuple[str, Path]]:
    folders = [("shipped", TEMPLATES_DIR)]
    if project_dir is not None:
        folders.append(("project", project_templates_dir(project_dir)))
    return folders


def _files(project_dir: Path | str | None) -> dict[str, tuple[str, Path]]:
    """{id: (origin, path)}: the template files in every folder, a later folder shadowing an earlier id."""
    found: dict[str, tuple[str, Path]] = {}
    for origin, folder in _folders(project_dir):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.yaml")):
            found[path.stem] = (origin, path)
    return found


def list_templates(project_dir: Path | str | None = None) -> builtins.list[dict[str, Any]]:
    """Every template, sorted by id, as `load` returns them (with `origin`)."""
    out = []
    for tid, (origin, path) in sorted(_files(project_dir).items()):
        try:
            out.append(_read(path, origin))
        except TemplateError as e:
            if origin == "shipped":
                raise
            log.warning("template %s left out: %s", tid, e)
    return out


def load(template_id: str, project_dir: Path | str | None = None) -> dict[str, Any]:
    """The template with that id (`origin` says which folder it came from)."""
    hit = _files(project_dir).get(template_id) if isinstance(template_id, str) else None
    if hit is None:
        raise TemplateNotFound(f"no template {template_id!r}")
    return _read(hit[1], hit[0])


# The name the router plan gives it: `templates.list(project_dir)`.
list = list_templates  # noqa: A001
