"""What AI may touch: class rules, the editable/locked split and the Send-blocking lint.

Doctrine (CLAUDE.md rule 3, spec/PACKET.md section 7): locked by default. An ID is editable only
when its layer starts with an editable prefix and nothing marks it architecture, glazing or
signage; everything else, and everything unlabelled, stays locked. The class rules here are the
same list written into the manifest's policy.class_rules, so plate_spec.validate_legend can re-check
every class_source=rule entry against them. lint() is the Send gate: the policy checks (unlabelled
share, locked/editable overlap) plus the scene checks that must pass before a render is worth
starting (scaled, shifted or hidden camera, no V-Ray, node names that break file names). Stdlib only.
"""

from __future__ import annotations

from typing import Any

from . import digest
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

# Characters that would break a file name or a path made from a node name (":" is the Windows
# drive/stream separator and the one the generated file names must never contain).
BAD_NAME_CHAR_SET = ":/\\"


def _problem(code: str, node: str | None, detail: str) -> dict[str, Any]:
    return {"code": code, "node": node, "detail": detail}


class LintReport(list):
    """What lint() returns. As a list it is the blocking messages (strings), exactly as before: empty
    means Send may proceed. It also carries passed, unlabelled_share (None when no coverage was
    given) and problems, a list of {code, node, detail}; report["passed"] style reads work too."""

    def __init__(self, problems: list[dict[str, Any]], unlabelled_share: float | None):
        super().__init__(p["detail"] for p in problems)
        self.problems = problems
        self.unlabelled_share = unlabelled_share

    @property
    def passed(self) -> bool:
        return not self.problems

    @property
    def codes(self) -> list[str]:
        return [p["code"] for p in self.problems]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "unlabelled_share": self.unlabelled_share,
            "problems": [dict(p) for p in self.problems],
        }

    def __getitem__(self, key):  # type: ignore[override]
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


def camera_problems(cam: dict[str, Any], hidden: bool = False) -> list[dict[str, Any]]:
    """SCALED_CAMERA, SHIFTED_CAMERA and HIDDEN_CAMERA for one camera_digest()."""
    name = str(cam.get("name"))
    out: list[dict[str, Any]] = []
    if cam.get("world_from_camera") is not None:
        problem = digest.rotation_error(cam["world_from_camera"])
        if problem:
            out.append(_problem(SCALED_CAMERA, name, f"camera {name}: {problem}"))
    why = lens_problems(cam)
    if why:
        out.append(
            _problem(
                SHIFTED_CAMERA,
                name,
                f"camera {name} uses {', '.join(why)}: Shot Packet v0.1 is unshifted, untilted and "
                "undistorted only (spec/PACKET.md 3.3); zero them before sending",
            )
        )
    if hidden:
        out.append(
            _problem(HIDDEN_CAMERA, name, f"camera {name} is hidden (or on a hidden layer): unhide it")
        )
    return out


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


def lint(
    policy: dict[str, Any] | None,
    coverage: dict[str, float] | None,
    max_unlabelled_share: float = DEFAULT_MAX_UNLABELLED_SHARE,
    *,
    camera: dict[str, Any] | None = None,
    camera_hidden: bool = False,
    node_names: list[str] | tuple[str, ...] | None = None,
    renderer: str | None = None,
) -> LintReport:
    """Blocking problems (an empty report means Send may proceed). coverage maps an id to the share
    of the frame it covers at rank 0. The scene checks run only for what is passed: camera (a
    camera_digest(); camera_hidden says whether the node is hidden), node_names, renderer (the
    current renderer's class name; None skips the V-Ray check, "" means unreadable)."""
    problems: list[dict[str, Any]] = []
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
        problems += camera_problems(camera, camera_hidden)
    if node_names:
        problems += name_problems(node_names)
    return LintReport(problems, share)


def lint_scene(
    camera_name: str,
    policy: dict[str, Any] | None = None,
    coverage: dict[str, float] | None = None,
    max_unlabelled_share: float = DEFAULT_MAX_UNLABELLED_SHARE,
) -> LintReport:
    """lint() over the open scene: reads the camera, its hidden state, the renderer and the node
    names through pymxs. Without policy and coverage (before any render exists) it runs the scene
    checks only; the unlabelled share needs the render's Cryptomatte coverage. Raises
    digest.CameraNotFound for an unknown camera."""
    rt = runtime()
    cam = digest.camera_digest(camera_name)
    node = rt.getNodeByName(camera_name, exact=True)
    layer = getattr(node, "layer", None)
    hidden = bool(getattr(node, "isHidden", False)) or bool(getattr(layer, "ishidden", False))
    try:
        renderer = str(rt.classOf(rt.renderers.current))
    except Exception:
        renderer = ""
    names = [camera_name] + [str(n["name"]) for n in digest.scene_nodes()]
    return lint(
        policy,
        coverage,
        max_unlabelled_share,
        camera=cam,
        camera_hidden=hidden,
        node_names=names,
        renderer=renderer,
    )
