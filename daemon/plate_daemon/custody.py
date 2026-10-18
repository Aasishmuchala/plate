"""Custody grades as code: which routes a project may use, and the vendor matrix the docs show.

Grades (BUILD_PLAN decision 9, `docs/custody.md` section 1): C0 local only, C1 first-party API with written
no-training and zero-retention terms, C2 a named reseller whose terms are held, D unknown terms or public
media. `Grade` orders by strictness, `C0 < C1 < C2 < D`. Pending counts as D: `effective_grade` is the
declared grade only when `custody_status == "confirmed"`, and a route that uploads to public URLs is D
whatever it declares (the same rule as `plate_engines.router.effective_grade`).

A run's allowed grades are the intersection of the project's custody and the packet's custody floor (the
stricter wins, schema `policy.custody`), and anything NDA is capped at `{C0, C1}`. A project with no custody
set is C0: nothing leaves (custody.md section 5). `CustodyViolation` is raised before any request is built.

The LLM route (the Anthropic API) has no engine manifest; its grade, status and terms hash live in
`custody_routes.yaml` beside this module, so promoting it to C1 is an edit there and nothing else changes.
Until then it is effectively D and NDA projects run with no Claude composer and no Claude judge (Decision 5a).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

ROUTES_YAML = Path(__file__).resolve().parent / "custody_routes.yaml"
LLM_UNAVAILABLE = "unavailable: custody"
STATUSES = ("confirmed", "pending_written_terms")
_TERMS_HASH = re.compile(r"^([0-9a-f]{64}|unknown)$")

__all__ = [
    "ALL_GRADES",
    "CustodyViolation",
    "Grade",
    "LLM_UNAVAILABLE",
    "LlmRoute",
    "NDA_GRADES",
    "effective_grade",
    "filter_routes",
    "llm_allowed",
    "llm_route",
    "matrix",
    "matrix_markdown",
    "project_allowed",
    "require_route",
    "route_allowed",
]


class Grade(StrEnum):
    """A custody grade. Compares by strictness: C0 (strictest) < C1 < C2 < D."""

    C0 = "C0"
    C1 = "C1"
    C2 = "C2"
    D = "D"

    @property
    def rank(self) -> int:
        return _ORDER.index(self)

    @classmethod
    def parse(cls, value: Grade | str) -> Grade:
        try:
            return cls(value)
        except ValueError:
            raise ValueError(
                f"unknown custody grade {value!r}; expected one of {[g.value for g in cls]}"
            ) from None

    def _other(self, other: object) -> Grade:
        if isinstance(other, Grade):
            return other
        if isinstance(other, str):
            return Grade.parse(other)
        raise TypeError(f"cannot compare a custody grade with {type(other).__name__}")

    def __lt__(self, other: object) -> bool:
        return self.rank < self._other(other).rank

    def __le__(self, other: object) -> bool:
        return self.rank <= self._other(other).rank

    def __gt__(self, other: object) -> bool:
        return self.rank > self._other(other).rank

    def __ge__(self, other: object) -> bool:
        return self.rank >= self._other(other).rank


_ORDER: tuple[Grade, ...] = (Grade.C0, Grade.C1, Grade.C2, Grade.D)
ALL_GRADES: frozenset[Grade] = frozenset(_ORDER)
NDA_GRADES: frozenset[Grade] = frozenset({Grade.C0, Grade.C1})


class CustodyViolation(Exception):
    """A route's effective grade is outside what the run allows. Raised before anything is sent."""

    code = "custody"

    def __init__(
        self,
        message: str,
        *,
        route_id: str | None = None,
        grade: Grade | None = None,
        allowed: Iterable[Grade] = (),
    ) -> None:
        super().__init__(f"custody: {message}")
        self.route_id = route_id
        self.grade = grade
        self.allowed = frozenset(allowed)


# ---------------------------------------------------------------------------- allowed grades


def _grades(values: Iterable[Grade | str]) -> frozenset[Grade]:
    return frozenset(Grade.parse(v) for v in values)


def _custody_side(custody: Any) -> tuple[bool, frozenset[Grade]] | None:
    """(nda, allowed) for one side of the intersection; None = no constraint from this side.

    Accepts the `{nda, allowed_grades}` object (project.json or packet `policy.custody`), an object with a
    `custody` attribute or key (a `Project` or a whole project doc), or a bare iterable of grades."""
    if custody is None:
        return None
    if not isinstance(custody, Mapping) and hasattr(custody, "custody"):
        custody = custody.custody
    if isinstance(custody, Mapping) and "custody" in custody and "nda" not in custody:
        custody = custody["custody"]
    if custody is None:
        return None
    if isinstance(custody, Mapping):
        nda = custody.get("nda", False)
        if not isinstance(nda, bool):
            raise ValueError(f"custody.nda must be true or false, got {nda!r}")
        raw = custody.get("allowed_grades")
        allowed = (NDA_GRADES if nda else ALL_GRADES) if raw is None else _grades(raw)
        return nda, allowed
    if isinstance(custody, (str, bytes)):
        raise ValueError(f"custody must be an object or a list of grades, got {custody!r}")
    return False, _grades(custody)


def project_allowed(project_custody: Any, packet_policy_custody: Any = None) -> set[Grade]:
    """The grades a run may use: project ∩ packet (the stricter wins), capped at {C0, C1} when either is NDA.

    A missing project custody means "not set yet", which is C0 (nothing leaves the box); a missing packet
    custody adds no constraint. The result can be empty: then nothing runs at all."""
    project = _custody_side(project_custody)
    if project is None:
        project = (False, frozenset({Grade.C0}))
    nda, allowed = project
    packet = _custody_side(packet_policy_custody)
    if packet is not None:
        nda = nda or packet[0]
        allowed = allowed & packet[1]
    if nda:
        allowed = allowed & NDA_GRADES
    return set(allowed)


# ---------------------------------------------------------------------------- routes


def _field(route: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(route, Mapping):
            if name in route:
                return route[name]
        elif hasattr(route, name):
            return getattr(route, name)
    return default


def effective_grade(route: Any) -> Grade:
    """The grade the router and egress act on: declared when confirmed, else D; public uploads are D.

    `route` is a `plate_engines.models.Route`, an `LlmRoute`, or a mapping with `custody_grade`/`grade` and
    `custody_status`/`status`."""
    declared = Grade.parse(_field(route, "custody_grade", "grade"))
    if _field(route, "upload_visibility") == "public":
        return Grade.D
    if _field(route, "custody_status", "status") != "confirmed":
        return Grade.D
    return declared


def _route_id(route: Any) -> str:
    return str(_field(route, "route_id", "provider", default="?"))


def route_allowed(route: Any, allowed: Iterable[Grade | str]) -> bool:
    return effective_grade(route) in _grades(allowed)


def require_route(route: Any, allowed: Iterable[Grade | str], *, what: str = "") -> Grade:
    """Return the route's effective grade, or raise `CustodyViolation` if the run does not allow it."""
    allowed_set = _grades(allowed)
    grade = effective_grade(route)
    if grade not in allowed_set:
        declared = _field(route, "custody_grade", "grade")
        status = _field(route, "custody_status", "status")
        why = (
            f"declared {declared}, {status} (pending counts as D)"
            if grade != declared
            else f"grade {grade.value}"
        )
        subject = f"{what} via " if what else ""
        shown = sorted(g.value for g in allowed_set) or ["nothing"]
        raise CustodyViolation(
            f"{subject}route {_route_id(route)!r} is effectively {grade.value} ({why}); this run allows {shown}",
            route_id=_route_id(route),
            grade=grade,
            allowed=allowed_set,
        )
    return grade


def filter_routes(manifest: Any, allowed: Iterable[Grade | str]) -> list[Any]:
    """The manifest's routes whose effective grade the run allows (custody only; health and price are the
    router's business)."""
    allowed_set = _grades(allowed)
    return [r for r in manifest.routes if effective_grade(r) in allowed_set]


# ---------------------------------------------------------------------------- the LLM route


@dataclass(frozen=True)
class LlmRoute:
    """A route with no engine manifest (the Anthropic API, or a web-app vendor) in the shape egress expects."""

    route_id: str
    vendor: str
    provider: str
    grade: Grade
    status: str
    terms_hash: str = "unknown"
    upload_visibility: str = "unknown"
    allowed_hosts: tuple[str, ...] = ()
    note: str = ""
    models: Mapping[str, str] = field(default_factory=dict)

    @property
    def custody_grade(self) -> str:
        return self.grade.value

    @property
    def custody_status(self) -> str:
        return self.status

    @property
    def effective(self) -> Grade:
        return effective_grade(self)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"{path}: unreadable custody routes file ({e})") from e
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise ValueError(f"{path}: expected a mapping with schema: 1")
    return doc


def _route_from(entry: Any, where: str) -> LlmRoute:
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected a mapping")
    for key in ("route_id", "vendor", "provider", "grade", "status"):
        if not entry.get(key):
            raise ValueError(f"{where}: {key} is required")
    grade = Grade.parse(entry["grade"])
    status = entry["status"]
    if status not in STATUSES:
        raise ValueError(f"{where}: status must be one of {list(STATUSES)}, got {status!r}")
    terms_hash = str(entry.get("terms_hash", "unknown"))
    if not _TERMS_HASH.match(terms_hash):
        raise ValueError(f"{where}: terms_hash must be a sha256 hex digest or 'unknown'")
    if status == "confirmed" and grade in (Grade.C1, Grade.C2) and terms_hash == "unknown":
        raise ValueError(
            f"{where}: a confirmed {grade.value} needs the terms_hash of the filed written terms"
        )
    visibility = str(entry.get("upload_visibility", "unknown"))
    if grade in (Grade.C1, Grade.C2) and visibility != "private":
        raise ValueError(f"{where}: {grade.value} requires upload_visibility 'private'")
    hosts = entry.get("allowed_hosts") or []
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h for h in hosts):
        raise ValueError(f"{where}: allowed_hosts must be a list of host names")
    return LlmRoute(
        route_id=str(entry["route_id"]),
        vendor=str(entry["vendor"]),
        provider=str(entry["provider"]),
        grade=grade,
        status=status,
        terms_hash=terms_hash,
        upload_visibility=visibility,
        allowed_hosts=tuple(h.lower() for h in hosts),
        note=str(entry.get("note", "")),
        models=dict(entry.get("models") or {}),
    )


def llm_route(path: Path | str = ROUTES_YAML) -> LlmRoute:
    """The Anthropic API route as `custody_routes.yaml` states it today (read on every call)."""
    return _route_from(_load_yaml(Path(path)).get("llm"), f"{path}: llm")


def llm_allowed(allowed: Iterable[Grade | str], path: Path | str = ROUTES_YAML) -> bool:
    """True when a run with these grades may call the composer and judge. False means `LLM_UNAVAILABLE`."""
    return route_allowed(llm_route(path), allowed)


# ---------------------------------------------------------------------------- the matrix for docs

VENDOR_NAMES = {
    "atlas": "Atlas (AtlasCloud)",
    "openai": "OpenAI",
    "gemini": "Google Gemini API",
    "vertex": "Google Vertex AI",
    "bfl": "Black Forest Labs",
    "magnific": "Magnific (Freepik)",
    "byteplus": "BytePlus",
    "wavespeed": "WaveSpeed",
    "runway": "Runway (Dev API)",
    "local": "Local (render box)",
}


def _row(
    vendor: str,
    provider: str,
    route_ids: list[str],
    engines: list[str],
    declared: str,
    status: str,
    effective: Grade,
    visibility: str,
    note: str = "",
    never: bool = False,
) -> dict[str, Any]:
    return {
        "vendor": vendor,
        "provider": provider,
        "route_ids": route_ids,
        "engines": engines,
        "declared_grade": declared,
        "custody_status": status,
        "effective_grade": effective.value,
        "upload_visibility": visibility,
        "nda_allowed": False if never else effective in NDA_GRADES,
        "never": never,
        "note": note,
    }


def matrix(
    manifests: Mapping[str, Any] | None = None, path: Path | str = ROUTES_YAML
) -> list[dict[str, Any]]:
    """One row per (provider, declared grade, status, visibility) across the engine manifests, then the LLM
    route, the consumer login (never) and the web-app vendors. `manifests` defaults to the shipped ones."""
    if manifests is None:
        from plate_engines.loader import load_manifests

        manifests = load_manifests()
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for manifest in manifests.values():
        for r in manifest.routes:
            key = (r.provider, r.custody_grade, r.custody_status, r.upload_visibility)
            g = groups.setdefault(
                key, {"route_ids": [], "engines": [], "notes": [], "effective": effective_grade(r)}
            )
            if r.route_id not in g["route_ids"]:
                g["route_ids"].append(r.route_id)
            g["engines"].append(manifest.id)
            if r.same_model_fallback_of:
                g["notes"].append(f"same-model fallback of {r.same_model_fallback_of}")
    rows = []
    for (provider, declared, status, visibility), g in sorted(groups.items()):
        note = "; ".join(dict.fromkeys(g["notes"]))
        if declared != g["effective"].value:
            pending = "pending written terms" if status != "confirmed" else "public uploads"
            note = "; ".join(x for x in (f"{declared} candidate, {pending}: treated as D", note) if x)
        rows.append(
            _row(
                VENDOR_NAMES.get(provider, provider),
                provider,
                sorted(g["route_ids"]),
                sorted(g["engines"]),
                declared,
                status,
                g["effective"],
                visibility,
                note,
            )
        )
    doc = _load_yaml(Path(path))
    llm = _route_from(doc.get("llm"), f"{path}: llm")
    rows.append(
        _row(
            llm.vendor,
            llm.provider,
            [llm.route_id],
            sorted(llm.models.values()),
            llm.grade.value,
            llm.status,
            llm.effective,
            llm.upload_visibility,
            llm.note,
        )
    )
    for i, entry in enumerate(doc.get("never") or []):
        if not isinstance(entry, dict) or not entry.get("route_id"):
            raise ValueError(f"{path}: never[{i}] needs a route_id")
        rows.append(
            _row(
                str(entry.get("vendor", entry["route_id"])),
                str(entry.get("provider", "")),
                [str(entry["route_id"])],
                [],
                "D",
                "never",
                Grade.D,
                "unknown",
                str(entry.get("note", "")),
                never=True,
            )
        )
    for i, entry in enumerate(doc.get("web_vendors") or []):
        v = _route_from(entry, f"{path}: web_vendors[{i}]")
        rows.append(
            _row(
                v.vendor,
                v.provider,
                [v.route_id],
                [],
                v.grade.value,
                v.status,
                v.effective,
                v.upload_visibility,
                v.note,
            )
        )
    return rows


def matrix_markdown(rows: list[dict[str, Any]] | None = None) -> str:
    """The matrix as a Markdown table (for docs and the custody panel)."""
    rows = matrix() if rows is None else rows
    head = "| vendor | routes | engines | declared | status | effective | NDA | note |"
    lines = [head, "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        nda = "never" if r["never"] else ("yes" if r["nda_allowed"] else "no")
        lines.append(
            f"| {r['vendor']} | {', '.join(r['route_ids'])} | {', '.join(r['engines']) or '-'} | "
            f"{r['declared_grade']} | {r['custody_status']} | {r['effective_grade']} | {nda} | {r['note']} |"
        )
    return "\n".join(lines) + "\n"
