"""Retirement alerts: a pinned model id that the vendor no longer lists.

New for the PLATE monorepo (router plan T27; BUILD_PLAN section 9: "a retirement alert fires when a pinned id
disappears; fallback only to the same model elsewhere, re-quoted, never silent"). `check` compares what the
manifests pin with what the vendor catalogues list today and returns `retired_pin` alerts; with `alerts=` it also
appends them to alerts.jsonl. It never changes a manifest or a project: a person decides what replaces a model.

What counts as pinned: a manifest with `pinned: true`, or an engine a project pins (`pins`: a mapping of task
name to Pin / engine id / (engine, version), the same shapes `router.choose` takes). A pin that names a version
other than the manifest's current one also alerts: that version is no longer served.

A route is checked when it is active, its provider has a catalogue, and its model id is known (route.model_slug,
else manifest.model_id). A provider with no catalogue, or a route with no model id, cannot be judged and is left
out; it is listed in no alert. `catalogue` is a prices.Catalogue or a plain {provider: [model ids]} mapping. An
entry the catalogue marks available=false counts as gone.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .health import Alert, AlertLog, as_alert_log
from .models import EngineManifest
from .prices import Catalogue, as_catalogue
from .router import as_pin


def check(
    manifests: Mapping[str, EngineManifest],
    catalogue: Catalogue | Mapping[str, Any],
    *,
    pins: Mapping[str, Any] | None = None,
    alerts: AlertLog | str | Path | None = None,
) -> list[Alert]:
    """`retired_pin` alerts for pinned engines whose model id is gone or whose pinned version is not served."""
    cat = as_catalogue(catalogue)
    log = as_alert_log(alerts)
    pinned_by: dict[str, list[str]] = {}
    pinned_versions: dict[str, set[str]] = {}
    for task, value in (pins or {}).items():
        pin = as_pin(value)
        pinned_by.setdefault(pin.engine_id, []).append(f"project pin for {task}")
        if pin.version is not None:
            pinned_versions.setdefault(pin.engine_id, set()).add(pin.version)
    for m in manifests.values():
        if m.pinned:
            pinned_by.setdefault(m.id, []).append("manifest pinned: true")

    found: list[Alert] = []
    for engine_id in sorted(pinned_by):
        why = "; ".join(dict.fromkeys(pinned_by[engine_id]))
        m = manifests.get(engine_id)
        if m is None:
            found.append(
                Alert(
                    kind="retired_pin",
                    severity="critical",
                    engine_id=engine_id,
                    message=f"pinned engine {engine_id} has no manifest any more ({why})",
                    data={"pinned_by": why},
                )
            )
            continue
        for version in sorted(pinned_versions.get(engine_id, ())):
            if version != m.version:
                found.append(
                    Alert(
                        kind="retired_pin",
                        severity="critical",
                        engine_id=engine_id,
                        message=f"pinned version {version!r} of {engine_id} is not served; manifest has {m.version!r}",
                        data={"pinned_version": version, "manifest_version": m.version, "pinned_by": why},
                    )
                )
        for route in m.routes:
            model = route.model_slug or m.model_id
            if route.status != "active" or model is None or not cat.has_vendor(route.provider):
                continue
            if model in cat.model_ids(route.provider):
                continue
            found.append(
                Alert(
                    kind="retired_pin",
                    severity="critical",
                    engine_id=engine_id,
                    route_id=route.route_id,
                    message=f"{route.provider} no longer lists {model!r}, which {engine_id} pins ({why})",
                    data={"provider": route.provider, "model_id": model, "pinned_by": why},
                )
            )

    return [log.append(a) for a in found] if log is not None else found
