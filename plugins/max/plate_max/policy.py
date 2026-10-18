"""What AI may touch: class rules, the editable/locked split and the Send-blocking lint.

Doctrine (CLAUDE.md rule 3, spec/PACKET.md section 7): locked by default. An ID is editable only
when its layer starts with an editable prefix and nothing marks it architecture, glazing or
signage; everything else, and everything unlabelled, stays locked. The class rules here are the
same list written into the manifest's policy.class_rules, so plate_spec.validate_legend can re-check
every class_source=rule entry against them. lint() is the Send gate: the policy checks (unlabelled
share, locked/editable overlap) plus the scene checks that must pass before a render is worth
starting (scaled, shifted or hidden camera, a non-finite camera pose, no V-Ray, node names that
break file names). With level_and_crop (decision 58) a two-point camera that bridge.camera_level can
send as a level pinhole is a SHIFTED_CAMERA warning instead of a blocking problem.

lint_scene() adds what only the open scene can tell (decision 81), all before any render: two
geometry nodes sharing a name (DUPLICATE_NODE_NAME: Cryptomatte hashes the name, so they would share
one id and one class, and whichever node came last would decide whether locked architecture is
editable), two cameras sharing a name (DUPLICATE_CAMERA_NAME: the Send would render the first one
under both), a camera name that cannot become a shot id (BAD_SHOT_SLUG) and lens or exposure values
that are not positive finite numbers (BAD_LENS). Stdlib only.
"""

from __future__ import annotations

import math
from typing import Any

from . import digest
from .bridge import camera_level
from .rt import runtime

EDITABLE_PREFIXES = ("VEG_", "SKY", "PEOPLE", "WATER", "ENT_", "GRASS")
NEVER_EDITABLE_TAGS = ("architecture", "glazing", "signage")
# plate_spec.NEVER_EDITABLE: classes no AI edit may touch ("other" = unclassified).
NEVER_EDITABLE_CLASSES = ("architecture", "glazing", "signage", "other")

DEFAULT_MAX_UNLABELLED_SHARE = 0.05
SCHEMA_MAX_UNLABELLED_SHARE = 0.25
RULES_VERSION = "2"  # 2: GRND_ joins HARD_ as a ground prefix

# Decision 43, the sender-side planar rule: an object of one of these classes covering at least
# PLANAR_MIN_SHARE of the frame is marked planar in the legend (the invention-flag gate scans those;
# plate_gates.invention.PLANAR_CLASSES is the same set). The node user property plate_planar
# (autotag.planar_property) overrides it either way.
PLANAR_CLASSES = ("architecture", "ground", "glazing")
PLANAR_MIN_SHARE = 0.005


def _layer_rule(pattern: str, cls: str) -> dict[str, str]:
    return {"source": "layer", "match": "prefix", "pattern": pattern, "class": cls}


# First match wins (the order is part of the manifest). Every EDITABLE_PREFIXES entry maps to a
# class outside NEVER_EDITABLE_CLASSES; test_policy.py pins that.
CLASS_RULES: tuple[dict[str, str], ...] = (
    _layer_rule("ARCH_", "architecture"),
    _layer_rule("GLZ_", "glazing"),
    _layer_rule("GLAZING", "glazing"),
    _layer_rule("SIGN_", "signage"),
    _layer_rule("HARD_", "ground"),
    _layer_rule("GRND_", "ground"),
    _layer_rule("VEG_", "vegetation"),
    _layer_rule("GRASS", "vegetation"),
    _layer_rule("SKY", "sky"),
    _layer_rule("PEOPLE", "people"),
    _layer_rule("WATER", "water"),
    _layer_rule("ENT_", "furniture"),
    _layer_rule("VEH_", "vehicle"),
)


def class_rules() -> list[dict[str, str]]:
    return [dict(r) for r in CLASS_RULES]


def classify_object(layer: str | None, tags: list[str] | tuple[str, ...] | None) -> tuple[str, str]:
    """(class, class_source) for one node. A never-editable tag wins over the layer rules; a node on
    no layer (or Max's default layer "0") or matching no rule is unlabelled (class "other")."""
    rule_class = None
    if layer and layer != "0":
        for rule in CLASS_RULES:
            if rule["source"] == "layer" and layer.startswith(rule["pattern"]):
                rule_class = rule["class"]
                break
    lowered = {str(t).lower() for t in (tags or ())}
    for tag in NEVER_EDITABLE_TAGS:
        if tag in lowered:
            return tag, ("rule" if rule_class == tag else "manual")
    if rule_class:
        return rule_class, "rule"
    return "other", "unlabelled"


def _object_editable(entry: dict[str, Any]) -> bool:
    layer = entry.get("layer") or ""
    tags = {str(t).lower() for t in entry.get("tags", [])}
    return (
        layer.startswith(EDITABLE_PREFIXES)
        and not tags & set(NEVER_EDITABLE_TAGS)
        and entry.get("class") not in NEVER_EDITABLE_CLASSES
        and entry.get("class_source") != "unlabelled"
    )


def derive_policy(legend: dict[str, Any]) -> dict[str, Any]:
    """Split every legend id into locked or editable. An object is editable per the rule above; a
    material is editable only when every object that uses it is, so a material shared with the
    facade stays locked. Returns locked_ids, editable_ids, unlabelled_ids (a subset of locked) and
    the class_rules the legend was classified with."""
    entries = legend["entries"]
    objects = [e for e in entries if e["kind"] == "object"]
    editable_objects = {e["id"] for e in objects if _object_editable(e)}
    locked: list[str] = []
    editable: list[str] = []
    for e in entries:
        if e["kind"] == "object":
            ok = e["id"] in editable_objects
        elif e["kind"] == "material":
            users = [o for o in objects if o.get("material") == e["name"]]
            ok = bool(users) and all(o["id"] in editable_objects for o in users)
        else:
            ok = False  # segments are tier C; the Max plugin never edits them
        (editable if ok else locked).append(e["id"])
    unlabelled = [e["id"] for e in entries if e.get("class_source") == "unlabelled"]
    return {
        "locked_ids": locked,
        "editable_ids": editable,
        "unlabelled_ids": unlabelled,
        "class_rules": class_rules(),
    }


# Lint problem codes: the stable names the dock shows and the tests assert.
UNLABELLED_SHARE = "UNLABELLED_SHARE"
LOCKED_AND_EDITABLE = "LOCKED_AND_EDITABLE"
UNLABELLED_EDITABLE = "UNLABELLED_EDITABLE"
SCALED_CAMERA = "SCALED_CAMERA"
SHIFTED_CAMERA = "SHIFTED_CAMERA"
NO_VRAY = "NO_VRAY"
HIDDEN_CAMERA = "HIDDEN_CAMERA"
BAD_NAME_CHARS = "BAD_NAME_CHARS"
DUPLICATE_NODE_NAME = "DUPLICATE_NODE_NAME"
DUPLICATE_CAMERA_NAME = "DUPLICATE_CAMERA_NAME"
BAD_SHOT_SLUG = "BAD_SHOT_SLUG"
BAD_LENS = "BAD_LENS"
BAD_POSE = "BAD_POSE"

# Characters that would break a file name or a path made from a node name (":" is the Windows
# drive/stream separator and the one the generated file names must never contain).
BAD_NAME_CHAR_SET = ":/\\"


def _problem(code: str, node: str | None, detail: str) -> dict[str, Any]:
    return {"code": code, "node": node, "detail": detail}


class LintReport(list):
    """What lint() returns. As a list it is the blocking messages (strings), exactly as before: empty
    means Send may proceed. It also carries passed, unlabelled_share (None when no coverage was
    given), problems, a list of {code, node, detail}, and warnings in the same shape (what does not
    block: a camera Send levels and crops); report["passed"] style reads work too. as_dict() lists
    "warnings" only when there are some."""

    def __init__(
        self,
        problems: list[dict[str, Any]],
        unlabelled_share: float | None,
        warnings: list[dict[str, Any]] | None = None,
    ):
        super().__init__(p["detail"] for p in problems)
        self.problems = problems
        self.unlabelled_share = unlabelled_share
        self.warnings = list(warnings or [])

    @property
    def passed(self) -> bool:
        return not self.problems

    @property
    def codes(self) -> list[str]:
        return [p["code"] for p in self.problems]

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "passed": self.passed,
            "unlabelled_share": self.unlabelled_share,
            "problems": [dict(p) for p in self.problems],
        }
        if self.warnings:
            out["warnings"] = [dict(w) for w in self.warnings]
        return out

    def __getitem__(self, key):  # type: ignore[override]
        if key == "warnings":
            return [dict(w) for w in self.warnings]
        if isinstance(key, str):
            return self.as_dict()[key]
        return super().__getitem__(key)


def lens_problems(cam: dict[str, Any]) -> list[str]:
    """Why a camera_digest() cannot be a v0.1 pinhole (PACKET.md 3.3); the same test as
    packet._refuse_unsupported_lens, so Send is blocked before the render and not after it."""
    why = []
    shift = cam.get("lens_shift") or {}
    if shift.get("horizontal") or shift.get("vertical"):
        why.append("lens shift")
    tilt = cam.get("tilt") or {}
    if tilt.get("vertical") or tilt.get("horizontal") or tilt.get("auto"):
        why.append("vertical/horizontal tilt")
    distortion = cam.get("distortion") or {}
    if distortion.get("amount") or distortion.get("type"):
        why.append("lens distortion")
    return why


def level_plan(cam: dict[str, Any]) -> tuple[camera_level.LevelPlan | None, str]:
    """camera_level.assess() at the digest's own render size: (plan, "") when Send can level and
    crop the camera, (None, why) when it stays refused."""
    resolution = cam.get("resolution")
    if not resolution:
        return None, "the render size is unreadable"
    return camera_level.assess(cam, int(resolution[0]), int(resolution[1]))


# Max Matrix3 rows as the artist knows them; world_from_camera[i][j] is row j + 1, axis i.
_MAX_ROWS = ("X axis (row1)", "Y axis (row2)", "Z axis (row3)", "position (row4)")


def pose_problems(cam: dict[str, Any]) -> list[dict[str, Any]]:
    """BAD_POSE when the camera_digest()'s world_from_camera holds NaN or infinity (a broken scripted
    controller, say): the detail names the transform rows. None (unreadable) is not judged here."""
    m = cam.get("world_from_camera")
    if m is None:
        return []
    bad = digest.non_finite_entries(m)
    if not bad:
        return []
    name = str(cam.get("name"))
    where = sorted(
        {_MAX_ROWS[j] + " " + "xyz"[i] for i, j in bad if i < 3 and j < 4}
        | {f"entry [{i}][{j}]" for i, j in bad if i >= 3 or j >= 4}
    )
    return [
        _problem(
            BAD_POSE,
            name,
            f"camera {name}: {digest.NON_FINITE_POSE} (NaN or infinity in the objecttransform "
            f"{', '.join(where)}): fix the camera's transform or its animation controller before sending",
        )
    ]


def camera_checks(
    cam: dict[str, Any], hidden: bool = False, level_and_crop: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(problems, warnings) for one camera_digest(): BAD_POSE, SCALED_CAMERA, SHIFTED_CAMERA,
    HIDDEN_CAMERA. A pose with a non-finite value is BAD_POSE (never also SCALED_CAMERA); a finite
    pose that is not a proper rotation is SCALED_CAMERA. With level_and_crop, a shifted or tilted
    camera that camera_level can plan is a SHIFTED_CAMERA warning (Send renders the level pinhole and
    crops it); one it cannot stays a problem, saying why."""
    name = str(cam.get("name"))
    out: list[dict[str, Any]] = pose_problems(cam)
    warnings: list[dict[str, Any]] = []
    if cam.get("world_from_camera") is not None and not out:
        problem = digest.rotation_error(cam["world_from_camera"])
        if problem:
            out.append(_problem(SCALED_CAMERA, name, f"camera {name}: {problem}"))
    why = lens_problems(cam)
    level, reason = level_plan(cam) if why and level_and_crop else (None, "")
    if level is not None:
        warnings.append(
            _problem(
                SHIFTED_CAMERA,
                name,
                f"camera {name} uses {', '.join(why)}: Send renders it as a level pinhole at "
                f"{level.width}x{level.render_height} and crops the {level.width}x{level.height} frame at "
                f"y {level.safe_y} ({level.note.split(';')[0]})",
            )
        )
    elif why:
        tail = f"; level-and-crop does not apply: {reason}" if reason else ""
        out.append(
            _problem(
                SHIFTED_CAMERA,
                name,
                f"camera {name} uses {', '.join(why)}: Shot Packet v0.1 is unshifted, untilted and "
                f"undistorted only (spec/PACKET.md 3.3); zero them before sending{tail}",
            )
        )
    if hidden:
        out.append(
            _problem(HIDDEN_CAMERA, name, f"camera {name} is hidden (or on a hidden layer): unhide it")
        )
    return out, warnings


def camera_problems(
    cam: dict[str, Any], hidden: bool = False, level_and_crop: bool = False
) -> list[dict[str, Any]]:
    """The blocking half of camera_checks()."""
    return camera_checks(cam, hidden, level_and_crop)[0]


def renderer_problems(renderer: str | None) -> list[dict[str, Any]]:
    """NO_VRAY unless the current renderer's class name is a V-Ray one ("V_Ray_7_hotfix_3", "V_Ray_GPU_7")."""
    if renderer and str(renderer).lower().replace("_", "").startswith("vray"):
        return []
    shown = repr(str(renderer)) if renderer else "unreadable"
    return [
        _problem(
            NO_VRAY, None, f"the current renderer is {shown}, not V-Ray: set V-Ray as the production renderer"
        )
    ]


def name_problems(names: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    """BAD_NAME_CHARS for every name holding ":" or a path separator (it ends up in file names)."""
    out = []
    for name in names:
        bad = sorted({c for c in str(name) if c in BAD_NAME_CHAR_SET})
        if bad:
            out.append(
                _problem(
                    BAD_NAME_CHARS,
                    str(name),
                    f"node name {str(name)!r} contains {' '.join(repr(c) for c in bad)}: rename it "
                    "(it becomes part of file names)",
                )
            )
    return out


def _layer_text(layer: Any) -> str:
    return repr(str(layer)) if layer is not None else "no layer"


def duplicate_node_text(name: str) -> str:
    """The words legend.build_id_legend raises for two visible nodes named name, and the start of
    DUPLICATE_NODE_NAME's detail: one text for the same refusal."""
    return f"duplicate visible node name {name!r}"


def duplicate_node_problems(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """DUPLICATE_NODE_NAME for every name two or more geometry nodes share (digest.scene_nodes()
    records, hidden ones included: V-Ray can render hidden geometry). The detail starts with the text
    legend.build_id_legend raises for it and lists the nodes' layers."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        groups.setdefault(str(node["name"]), []).append(node)
    out = []
    for name, group in groups.items():
        if len(group) < 2:
            continue
        layers = ", ".join(_layer_text(n.get("layer")) for n in group)
        out.append(
            _problem(
                DUPLICATE_NODE_NAME,
                name,
                f"{duplicate_node_text(name)}: {len(group)} geometry nodes share it (layers "
                f"{layers}); Cryptomatte gives them one id and one class, so rename them apart before "
                "sending",
            )
        )
    return out


def duplicate_camera_problems(names: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    """DUPLICATE_CAMERA_NAME for every name two or more camera nodes share: the Send finds a camera by
    name, so it would render the first one under both."""
    counts: dict[str, int] = {}
    for name in names:
        counts[str(name)] = counts.get(str(name), 0) + 1
    return [
        _problem(
            DUPLICATE_CAMERA_NAME,
            name,
            f"{count} cameras are named {name!r}: Send finds a camera by its name and would render the "
            "first one for both; rename them apart",
        )
        for name, count in counts.items()
        if count > 1
    ]


def shot_slug_problems(camera_name: str) -> list[dict[str, Any]]:
    """BAD_SHOT_SLUG when packet.slugify(camera_name) raises (no ASCII letter or digit at all) or
    yields a slug that is not the whole name's: a letter or digit outside a-z, A-Z, 0-9 would be
    dropped ('客户A' would be shot 'a'). Symbols, spaces and emoji are separators and do not count."""
    from . import packet  # packet imports this module

    name = str(camera_name)
    try:
        slug = packet.slugify(name)
    except packet.ManifestError:
        return [
            _problem(
                BAD_SHOT_SLUG,
                name,
                f"camera name {name!r} has no ASCII letter or digit, so it cannot become a shot id: "
                "rename the camera (for example CAM_front)",
            )
        ]
    dropped = sorted({c for c in name if c.isalnum() and not c.isascii()})
    if dropped:
        return [
            _problem(
                BAD_SHOT_SLUG,
                name,
                f"camera name {name!r} has letters or digits outside a-z and 0-9 ({' '.join(dropped)}): "
                f"its shot id would be {slug!r}, not the camera's name; rename it with ASCII letters "
                "and digits",
            )
        ]
    return []


# (camera_digest field, what the artist calls it, read_from key naming the Max property).
LENS_FIELDS = (
    ("focal_mm", "focal length", "focal_mm"),
    ("film_gate_mm", "film gate", "film_gate_mm"),
    ("zoom_factor", "zoom factor", "zoom_factor"),
    ("f_number", "f-number", "f_number"),
    ("exposure.iso", "ISO", "exposure.iso"),
    ("exposure.shutter_s", "shutter", "exposure.shutter_s"),
)


def _lens_value(cam: dict[str, Any], field: str) -> Any:
    value: Any = cam
    for key in field.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _number_text(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return repr(value)


def lens_value_problems(cam: dict[str, Any]) -> list[dict[str, Any]]:
    """BAD_LENS for every lens or exposure value of a camera_digest() that was read but is not a
    positive finite number (focal length, film gate, zoom, f-number, ISO, shutter): the detail names
    the camera property it came from. An unreadable value (None) is the digest's warning, not this."""
    name = str(cam.get("name"))
    read_from = cam.get("read_from") or {}
    out = []
    for field, label, source in LENS_FIELDS:
        value = _lens_value(cam, field)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = math.nan
        if math.isfinite(number) and number > 0:
            continue
        prop = read_from.get(source) or field
        typed = number
        if field == "exposure.shutter_s" and prop in digest.SHUTTER_SPEED_NAMES and not math.isnan(number):
            typed = 1.0 / number if number else math.inf  # V-Ray stores 1/seconds: the number typed
        why = "not a finite number" if not math.isfinite(typed) else "not positive"
        shown = _number_text(typed) if not math.isnan(number) else _number_text(value)
        out.append(
            _problem(
                BAD_LENS,
                name,
                f"camera {name}: {label} {prop} is {shown}, {why}: set the camera's {prop} to a "
                "positive number before sending",
            )
        )
    return out


def camera_names(rt: Any) -> list[str]:
    """The names of the scene's camera nodes (rt.cameras), camera targets left out."""
    try:
        nodes = list(getattr(rt, "cameras", None) or [])
    except Exception:
        return []
    out = []
    for node in nodes:
        try:
            if str(rt.classOf(node)).lower() == "targetobject":
                continue
        except Exception:
            pass
        out.append(str(getattr(node, "name", "")))
    return out


def lint(
    policy: dict[str, Any] | None,
    coverage: dict[str, float] | None,
    max_unlabelled_share: float = DEFAULT_MAX_UNLABELLED_SHARE,
    *,
    camera: dict[str, Any] | None = None,
    camera_hidden: bool = False,
    node_names: list[str] | tuple[str, ...] | None = None,
    renderer: str | None = None,
    level_and_crop: bool = False,
) -> LintReport:
    """Blocking problems (an empty report means Send may proceed). coverage maps an id to the share
    of the frame it covers at rank 0. The scene checks run only for what is passed: camera (a
    camera_digest(); camera_hidden says whether the node is hidden), node_names, renderer (the
    current renderer's class name; None skips the V-Ray check, "" means unreadable). level_and_crop
    (the recipe's, decision 58) turns a camera Send can level and crop into a warning."""
    problems: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    share: float | None = None
    if policy is not None:
        coverage = coverage or {}
        share = sum(float(coverage.get(i, 0.0)) for i in policy.get("unlabelled_ids", []))
        if share > max_unlabelled_share:
            problems.append(
                _problem(
                    UNLABELLED_SHARE,
                    None,
                    f"unlabelled IDs cover {share:.1%} of the frame, over the {max_unlabelled_share:.1%} limit: "
                    "put those nodes on a labelled layer (VEG_, ARCH_, ...) before sending",
                )
            )
        both = set(policy.get("locked_ids", [])) & set(policy.get("editable_ids", []))
        if both:
            problems.append(
                _problem(LOCKED_AND_EDITABLE, None, f"ids both locked and editable: {sorted(both)}")
            )
        unlabelled_editable = set(policy.get("unlabelled_ids", [])) & set(policy.get("editable_ids", []))
        if unlabelled_editable:
            problems.append(
                _problem(
                    UNLABELLED_EDITABLE,
                    None,
                    f"unlabelled ids marked editable: {sorted(unlabelled_editable)}",
                )
            )
    if renderer is not None:
        problems += renderer_problems(renderer)
    if camera is not None:
        blocking, warned = camera_checks(camera, camera_hidden, level_and_crop)
        problems += blocking
        warnings += warned
    if node_names:
        problems += name_problems(node_names)
    return LintReport(problems, share, warnings)


def lint_scene(
    camera_name: str,
    policy: dict[str, Any] | None = None,
    coverage: dict[str, float] | None = None,
    max_unlabelled_share: float = DEFAULT_MAX_UNLABELLED_SHARE,
    level_and_crop: bool = False,
) -> LintReport:
    """lint() over the open scene: reads the camera, its hidden state, the renderer and the node
    names through pymxs. Without policy and coverage (before any render exists) it runs the scene
    checks only; the unlabelled share needs the render's Cryptomatte coverage. Before lint() it runs
    the checks only the scene can answer: DUPLICATE_NODE_NAME over digest.scene_nodes(),
    DUPLICATE_CAMERA_NAME over rt.cameras, and BAD_SHOT_SLUG and BAD_LENS on the camera_digest()
    (BAD_POSE comes from lint()'s camera checks); their problems come first in the report.
    level_and_crop: see lint() (off by default: a camera is refused unless the caller levels it).
    Raises digest.CameraNotFound for an unknown camera."""
    rt = runtime()
    cam = digest.camera_digest(camera_name)
    node = rt.getNodeByName(camera_name, exact=True)
    layer = getattr(node, "layer", None)
    hidden = bool(getattr(node, "isHidden", False)) or bool(getattr(layer, "ishidden", False))
    try:
        renderer = str(rt.classOf(rt.renderers.current))
    except Exception:
        renderer = ""
    nodes = digest.scene_nodes()
    first = (
        duplicate_node_problems(nodes)
        + duplicate_camera_problems(camera_names(rt))
        + shot_slug_problems(camera_name)
        + lens_value_problems(cam)
    )
    names = [camera_name] + [str(n["name"]) for n in nodes]
    report = lint(
        policy,
        coverage,
        max_unlabelled_share,
        camera=cam,
        camera_hidden=hidden,
        node_names=names,
        renderer=renderer,
        level_and_crop=level_and_crop,
    )
    if not first:
        return report
    return LintReport(first + report.problems, report.unlabelled_share, report.warnings)
