"""Load and validate the engine manifests in engines/plate_engines/manifests/*.yaml.

New for the PLATE monorepo. One file per engine, named <id>.yaml. Duplicate ids, a file whose name does not
match its id, unparsable YAML and any pydantic validation error raise ManifestError naming the file.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import EngineManifest

DEFAULT_MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"


class ManifestError(ValueError):
    """A manifest file is unreadable, invalid, duplicated or misnamed."""


def _one_line(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def load_manifests(dir: Path = DEFAULT_MANIFEST_DIR) -> dict[str, EngineManifest]:
    """Return {engine_id: EngineManifest} for every *.yaml in dir, sorted by file name.

    A file whose name starts with "_" is an overlay (prices.refresh writes _prices.yaml), not a manifest: skipped.
    """
    out: dict[str, EngineManifest] = {}
    origin: dict[str, str] = {}
    for path in sorted(Path(dir).glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            raise ManifestError(f"{path.name}: unreadable YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ManifestError(f"{path.name}: top level must be a mapping")
        try:
            manifest = EngineManifest.model_validate(raw)
        except ValidationError as exc:
            raise ManifestError(f"{path.name}: {_one_line(exc)}") from exc
        if manifest.id in out:
            raise ManifestError(f"{path.name}: duplicate id {manifest.id!r} (also in {origin[manifest.id]})")
        if path.stem != manifest.id:
            raise ManifestError(f"{path.name}: file name must be <id>.yaml, id is {manifest.id!r}")
        out[manifest.id] = manifest
        origin[manifest.id] = path.name
    return out
