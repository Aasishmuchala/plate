"""What AI may touch: class rules, the editable/locked split and the Send-blocking lint.

Doctrine (CLAUDE.md rule 3, spec/PACKET.md section 7): locked by default. An ID is editable only
when its layer starts with an editable prefix and nothing marks it architecture, glazing or
signage; everything else, and everything unlabelled, stays locked. The class rules here are the
same list written into the manifest's policy.class_rules, so plate_spec.validate_legend can re-check
every class_source=rule entry against them. Stdlib only.
"""

from __future__ import annotations

from typing import Any

EDITABLE_PREFIXES = ("VEG_", "SKY", "PEOPLE", "WATER", "ENT_", "GRASS")
NEVER_EDITABLE_TAGS = ("architecture", "glazing", "signage")
# plate_spec.NEVER_EDITABLE: classes no AI edit may touch ("other" = unclassified).
NEVER_EDITABLE_CLASSES = ("architecture", "glazing", "signage", "other")

DEFAULT_MAX_UNLABELLED_SHARE = 0.05
SCHEMA_MAX_UNLABELLED_SHARE = 0.25
RULES_VERSION = "1"


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


def lint(
    policy: dict[str, Any],
    coverage: dict[str, float],
    max_unlabelled_share: float = DEFAULT_MAX_UNLABELLED_SHARE,
) -> list[str]:
    """Blocking messages (an empty list means Send may proceed). coverage maps an id to the share
    of the frame it covers at rank 0."""
    problems: list[str] = []
    share = sum(float(coverage.get(i, 0.0)) for i in policy.get("unlabelled_ids", []))
    if share > max_unlabelled_share:
        problems.append(
            f"unlabelled IDs cover {share:.1%} of the frame, over the {max_unlabelled_share:.1%} limit: "
            "put those nodes on a labelled layer (VEG_, ARCH_, ...) before sending"
        )
    both = set(policy.get("locked_ids", [])) & set(policy.get("editable_ids", []))
    if both:
        problems.append(f"ids both locked and editable: {sorted(both)}")
    unlabelled_editable = set(policy.get("unlabelled_ids", [])) & set(policy.get("editable_ids", []))
    if unlabelled_editable:
        problems.append(f"unlabelled ids marked editable: {sorted(unlabelled_editable)}")
    return problems
