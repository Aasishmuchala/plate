"""Vendor schema diff: what was added, removed or changed between two recorded request schemas.

New for the PLATE monorepo (router plan T27; BUILD_PLAN section 9: vendors change their fields without notice, so
nightly jobs compare the schema a vendor serves today with the one the adapter was built from).

The schemas are JSON-Schema-shaped dicts (an OpenAPI request body works): `{"type": "object", "required":
[...], "properties": {name: {...}}}`, with nested objects under `properties` and array items under `items`.
A field is addressed by a dotted path: `duration`, `elements[].name` (an array's items get `[]`). Everything in a
field's schema except `properties`, `items`, `required` (the list), `description`, `title`, `examples`, `example`,
`$comment` and `x-*` keys is an attribute that is compared by value; `required` is read as a per-field flag.

    diff(old, new) -> {
        "added":   ["cfg_scale", ...],                       # paths present only in new
        "removed": ["image_tail", ...],                      # paths present only in old
        "changed": [{"path": "duration", "attr": "enum", "old": [...], "new": [...],
                     "added_values": [...], "removed_values": [...]}, ...],   # the last two on enum changes
        "alert":   "schema_change" | None,
        "reasons": ["removed required field 'image'", "changed enum on 'resolution'", ...],
    }

`alert` is "schema_change" when a field that was required is gone, or when any field's enum changed (values
added, removed, or an enum appearing or disappearing). Those are the changes that break an adapter silently;
everything else is reported in added / removed / changed without an alert. With `alerts=` the diff also appends a
`schema_change` alert to alerts.jsonl.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .health import AlertLog, as_alert_log

_SKIP_ATTRS = frozenset(
    {"properties", "items", "required", "description", "title", "examples", "example", "$comment"}
)
ALERT = "schema_change"


def _is_attr(name: str) -> bool:
    return name not in _SKIP_ATTRS and not name.startswith("x-")


def flatten(schema: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """{path: attributes} for every property of the schema, including a `required` bool on each."""
    out: dict[str, dict[str, Any]] = {}

    def walk(node: Mapping[str, Any], prefix: str) -> None:
        required = set(node.get("required") or []) if isinstance(node.get("required"), list) else set()
        props = node.get("properties")
        if isinstance(props, Mapping):
            for name, sub in props.items():
                path = f"{prefix}{name}"
                sub = sub if isinstance(sub, Mapping) else {}
                attrs = {k: v for k, v in sub.items() if _is_attr(k)}
                attrs["required"] = name in required
                out[path] = attrs
                walk(sub, f"{path}.")
        items = node.get("items")
        if isinstance(items, Mapping):
            base = prefix[:-1] if prefix.endswith(".") else prefix
            walk(items, f"{base}[].")

    walk(schema, "")
    return out


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _enum_delta(old: Any, new: Any) -> tuple[list[Any], list[Any]]:
    old_vals = old if isinstance(old, list) else []
    new_vals = new if isinstance(new, list) else []
    added = [v for v in new_vals if _canon(v) not in {_canon(x) for x in old_vals}]
    removed = [v for v in old_vals if _canon(v) not in {_canon(x) for x in new_vals}]
    return added, removed


def diff(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    *,
    alerts: AlertLog | str | Path | None = None,
    engine_id: str | None = None,
    route_id: str | None = None,
) -> dict[str, Any]:
    """Compare two schemas; see the module docstring for the result."""
    old, new = flatten(old_schema), flatten(new_schema)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed: list[dict[str, Any]] = []
    reasons: list[str] = []

    for path in sorted(set(old) & set(new)):
        for attr in sorted(set(old[path]) | set(new[path])):
            a, b = old[path].get(attr), new[path].get(attr)
            if _canon(a) == _canon(b):
                continue
            entry: dict[str, Any] = {"path": path, "attr": attr, "old": a, "new": b}
            if attr == "enum":
                entry["added_values"], entry["removed_values"] = _enum_delta(a, b)
                reasons.append(f"changed enum on {path!r}")
            changed.append(entry)

    for path in removed:
        if old[path].get("required"):
            reasons.append(f"removed required field {path!r}")

    result: dict[str, Any] = {
        "added": added,
        "removed": removed,
        "changed": changed,
        "alert": ALERT if reasons else None,
        "reasons": reasons,
    }
    log = as_alert_log(alerts)
    if log is not None and reasons:
        log.write(
            ALERT,
            "; ".join(reasons[:6]),
            severity="critical",
            engine_id=engine_id,
            route_id=route_id,
            data={"added": added, "removed": removed, "changed": changed},
        )
    return result
