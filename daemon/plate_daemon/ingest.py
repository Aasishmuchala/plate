"""Packet ingest by reference (spec/PACKET.md sections 1 and 9).

`begin_ingest` validates the folder with `plate_spec.validate_packet_dir(path, check_files=True)` (the
only validator: decision 5), hashes `manifest.json` with BLAKE3 and decides: new packet -> insert a row
with status `ingesting`; same `packet_id` and same manifest hash -> `known` (idempotent re-send); same id,
different hash -> `PacketConflict` (HTTP 409); validation errors -> `PacketInvalid` (HTTP 422).
`finish_ingest` then re-hashes every file the manifest references, records `(packet_id, relpath, size,
blake3)` in `blobs` (copying into the CAS only when `cas_copy`), and appends `ingest.started`, one
`ingest.file` per file and `ingest.done` (or `ingest.failed`) to the packet's append-only event log.
The packet folder (possibly on the NAS) stays the source of truth; the index and the CAS are a
rebuildable cache. Hashing runs outside transactions so a large packet never holds the write lock.

Two P1 hooks then run before a packet counts as ingested (PACKET.md 4.3 and 7). The sender's coverage and
policy are claims: `recompute_coverage_from_cryptomatte` re-reads the object and material Cryptomatte from
the EXR (tier A) and rejects a legend that omits a visible id, and `reapply_project_policy` re-applies the
project's own class rules and lint threshold from `project.json` (`settings.policy`), the stricter result
winning. Packets are immutable, so nothing is written back to the packet folder: corrections and the applied
policy ride on the `ingest.done` event (`data.notes`, `data.policy`), which `effective_policy` reads back.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import plate_spec

from .cas import CAS, hash_bytes, hash_file
from .config import check_project_id
from .db import dumps, loads, tx
from .fsutil import read_json
from .timeutil import iso, utcnow


class PacketInvalid(Exception):
    def __init__(self, errors: list[str]):
        self.errors = list(errors) or ["packet invalid"]
        super().__init__("; ".join(self.errors[:5]))


class PacketConflict(Exception):
    def __init__(self, packet_id: str, known_hash: str, new_hash: str):
        self.packet_id, self.known_hash, self.new_hash = packet_id, known_hash, new_hash
        super().__init__(
            f"{packet_id} is already known with manifest blake3 {known_hash[:16]}..., this one is {new_hash[:16]}...; "
            "a changed packet needs a new packet_id (and `supersedes`)"
        )


class PacketNotFound(KeyError):
    pass


@dataclass(frozen=True)
class PreparedPacket:
    path: Path
    manifest: dict[str, Any]
    manifest_blake3: str

    @property
    def packet_id(self) -> str:
        return self.manifest["packet_id"]

    @property
    def project_id(self) -> str:
        return self.manifest["project_id"]


def prepare_ingest(path: Path | str) -> PreparedPacket:
    """Validate a packet folder and hash its manifest. Raises PacketInvalid."""
    path = Path(path)
    if not path.is_absolute():
        raise PacketInvalid([f"packet_path must be an absolute folder path, got {str(path)!r}"])
    if not path.is_dir():
        raise PacketInvalid([f"packet folder not found: {path}"])
    mpath = path / "manifest.json"
    before = mpath.read_bytes() if mpath.is_file() else None
    errors = plate_spec.validate_packet_dir(path, check_files=True)
    if errors:
        raise PacketInvalid(errors)
    after = mpath.read_bytes()
    if before != after:
        raise PacketInvalid(["manifest.json changed while it was being validated; re-send the packet"])
    manifest = json.loads(after.decode("utf-8-sig"))
    return PreparedPacket(path=path, manifest=manifest, manifest_blake3=hash_bytes(after))


def _scene_hash(manifest: dict[str, Any]) -> str | None:
    sh = manifest.get("scene_hash")
    return sh.get("hex") if isinstance(sh, dict) else None


def begin_ingest(
    conn: sqlite3.Connection,
    path: Path | str,
    *,
    sender: dict[str, Any] | None = None,
    prepared: PreparedPacket | None = None,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Returns ("ingesting" | "known", record). A same-hash re-send of a packet whose earlier ingest
    failed is accepted again as "ingesting" so the sender can retry after fixing the files."""
    prepared = prepared or prepare_ingest(path)
    m = prepared.manifest
    pid = prepared.packet_id
    sender = sender or {}
    with tx(conn):
        row = conn.execute(
            "SELECT manifest_blake3, status FROM packets WHERE packet_id = ?", (pid,)
        ).fetchone()
        if row is not None:
            if row["manifest_blake3"] != prepared.manifest_blake3:
                raise PacketConflict(pid, row["manifest_blake3"], prepared.manifest_blake3)
            if row["status"] != "failed":
                return "known", get_packet(conn, pid)
            conn.execute(
                "UPDATE packets SET status = 'ingesting', error = NULL, path = ? WHERE packet_id = ?",
                (str(path), pid),
            )
            return "ingesting", get_packet(conn, pid)
        conn.execute(
            """INSERT INTO packets (packet_id, project_id, shot_id, kind, tier, packet_version, scene_hash, path,
                                    manifest_blake3, status, sender_name, sender_version, received_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ingesting', ?, ?, ?)""",
            (
                pid,
                m["project_id"],
                m["shot_id"],
                m["kind"],
                m["tier"],
                m["packet_version"],
                _scene_hash(m),
                str(path),
                prepared.manifest_blake3,
                sender.get("name"),
                sender.get("version"),
                iso(now or utcnow()),
            ),
        )
    return "ingesting", get_packet(conn, pid)


def referenced_files(manifest: dict[str, Any], root: Path) -> list[tuple[str, str, str | None, int | None]]:
    """(role, relpath, expected blake3 or None, expected bytes or None) for every file the manifest
    references: plain files, each sequence's hashes file and every frame it lists."""
    out: list[tuple[str, str, str | None, int | None]] = []
    for role, ref in manifest["files"].items():
        if ref["kind"] == "file":
            out.append((role, ref["path"], ref["hash"]["hex"], ref["bytes"]))
            continue
        out.append((role, ref["hashes"], None, None))
        text = (root / ref["hashes"]).read_text(encoding="utf-8-sig")
        for line in text.splitlines():
            if line.strip():
                hexd, rel = line.split(None, 1)
                out.append((role, rel.strip(), hexd, None))
    return out


def _event(conn: sqlite3.Connection, packet_id: str, typ: str, data: dict[str, Any]) -> None:
    with tx(conn):
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM packet_events WHERE packet_id = ?", (packet_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO packet_events (packet_id, seq, type, data, at_utc) VALUES (?, ?, ?, ?, ?)",
            (packet_id, seq, typ, dumps(data), iso(utcnow())),
        )


def _fail(conn: sqlite3.Connection, packet_id: str, errors: list[str]) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE packets SET status = 'failed', error = ? WHERE packet_id = ?",
            ("; ".join(errors)[:2000], packet_id),
        )
        _event(conn, packet_id, "ingest.failed", {"errors": errors})


# ---------------------------------------------------------------------------- P1 hooks (PACKET.md 4.3, 7)

PRESENT_MIN_COVERAGE = 0.005  # a rank counts as showing an id when its coverage exceeds this
VISIBLE_MIN_SHARE = 0.005  # an id covering at least 0.5 % of the frame must be in the legend
SHARE_TOLERANCE = 0.001  # a legend pixel_share further than this from the EXR's is replaced
MAX_NOTES = 20  # per kind: a real scene can have dozens of sub-floor slivers
PLACEHOLDER_MAGIC = b"PLATE-PLACEHOLDER"  # what plate_spec.materialize_example writes in place of pixels


def _load_legend(root: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    ref = (manifest.get("files") or {}).get("id_legend")
    if not isinstance(ref, dict) or ref.get("kind") != "file":
        return None
    path = root / ref["path"]
    if not path.is_file():
        return None
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    return doc if isinstance(doc, dict) else None


def _hex_of(entry_id: Any, prefix: str) -> int | None:
    if not isinstance(entry_id, str) or not entry_id.startswith(prefix + ":"):
        return None
    try:
        return int(entry_id[2:], 16)
    except ValueError:
        return None


def _rank0_shares(ranks: Any, cov: Any) -> dict[int, float]:
    """id -> rank-0 coverage summed over the frame, as a fraction of its pixels (PACKET.md 4.3 `pixel_share`)."""
    import numpy as np

    r0 = np.ascontiguousarray(ranks[..., 0]).ravel()
    c0 = np.ascontiguousarray(cov[..., 0]).ravel().astype(np.float64)
    uniq, inv = np.unique(r0, return_inverse=True)
    sums = np.bincount(inv.ravel(), weights=c0, minlength=len(uniq)) / max(r0.size, 1)
    return {int(u): float(v) for u, v in zip(uniq, sums, strict=True)}


def recompute_coverage_from_cryptomatte(
    root: Path,
    manifest: dict[str, Any],
    *,
    legend: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> list[str]:
    """Tier A only: re-read the object (and material) Cryptomatte from the EXR and check the legend against it.
    Returns the errors; a legend that omits an id covering >= 0.5 % of the frame is one. Each legend entry's
    `pixel_share` is recomputed (rank-0 coverage over the frame) and replaced in `legend` when it differs by
    more than 0.001; that is a note, not an error. `legend` is the loaded id_legend.json to patch in place
    (loaded from the packet when None); `notes` collects the corrections and sub-floor ids."""
    notes = notes if notes is not None else []
    files = manifest.get("files") or {}
    exr_ref = files.get("beauty_exr")
    if (
        manifest.get("tier") != "A"
        or not isinstance(exr_ref, dict)
        or exr_ref.get("kind") != "file"
        or (manifest.get("id_legend") or {}).get("source") != "cryptomatte"
    ):
        return []
    legend = legend if legend is not None else _load_legend(root, manifest)
    if legend is None:
        return ["id_legend.json is missing or not a JSON object"]

    import numpy as np

    from plate_pixels import exr as exr_io

    exr_path = root / exr_ref["path"]
    with open(exr_path, "rb") as fh:
        if fh.read(len(PLACEHOLDER_MAGIC)) == PLACEHOLDER_MAGIC:
            # A transport-test payload (spec materialize_example): no pixels, so nothing downstream can read
            # an id mask from it either. A real EXR never starts with this.
            notes.append(f"{exr_ref['path']} is a placeholder payload: legend coverage not recomputed")
            return []
    entries = [e for e in legend.get("entries") or [] if isinstance(e, dict)]
    errors: list[str] = []
    for which, prefix in (("object", "o"), ("material", "m")):
        try:
            ranks, cov = exr_io.read_cryptomatte(exr_path, manifest, which)
        except exr_io.MissingChannel as e:
            if which == "object":
                errors.append(f"cannot recompute legend coverage: {e}")
            continue  # a packet without a material Cryptomatte has no m: entries to check
        except (RuntimeError, OSError) as e:  # OpenEXR raises RuntimeError for a file it cannot read
            return [f"cannot recompute legend coverage: {exr_ref['path']} is not a readable EXR ({e})"]
        n_pixels = ranks.shape[0] * ranks.shape[1]
        shares = _rank0_shares(ranks, cov)
        known = set()
        for entry in entries:
            rid = _hex_of(entry.get("id"), prefix)
            if rid is None:
                continue
            known.add(rid)
            new = round(shares.get(rid, 0.0), 4)
            old = entry.get("pixel_share")
            if not isinstance(old, (int, float)) or abs(new - old) > SHARE_TOLERANCE:
                entry["pixel_share"] = new
                notes.append(f"pixel_share of {entry['id']} recomputed from the Cryptomatte: {old} -> {new}")
        if which != "object":
            continue
        omitted, sliver = [], []
        for rid in sorted(exr_io.present_ids(ranks, cov, min_coverage=PRESENT_MIN_COVERAGE) - known):
            area = float(np.where(ranks == np.uint32(rid), cov, 0.0).sum(dtype=np.float64)) / n_pixels
            (omitted if area >= VISIBLE_MIN_SHARE else sliver).append((rid, area))
        errors.extend(f"legend omits visible id o:{rid:08x} ({area * 100:.2f}%)" for rid, area in omitted)
        notes.extend(
            f"legend omits id o:{rid:08x} ({area * 100:.2f}%), below the {VISIBLE_MIN_SHARE * 100:g}% floor"
            for rid, area in sliver[:MAX_NOTES]
        )
        if len(sliver) > MAX_NOTES:
            notes.append(
                f"{len(sliver) - MAX_NOTES} more ids below the visibility floor are not in the legend"
            )
    return errors


def _project_json_path(
    conn: sqlite3.Connection, project_id: str, project_root: Path | str | None
) -> Path | None:
    """The project's project.json: the explicit root, else the folder registered in the index, else
    `<home>/projects/<id>` beside the connection's `<home>/index/<id>.sqlite`."""
    if project_root is not None:
        return Path(project_root) / "project.json"
    try:
        row = conn.execute("SELECT value FROM project_meta WHERE key = 'root'").fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        return Path(loads(row[0])) / "project.json"
    try:
        check_project_id(project_id)
    except ValueError:
        return None
    for r in conn.execute("PRAGMA database_list"):
        if r[1] == "main" and r[2]:
            return Path(r[2]).parent.parent / "projects" / project_id / "project.json"
    return None


def _project_policy(doc: Any) -> tuple[list[str], float | None] | str | None:
    """(locked_classes, lint_threshold) from a project.json document; None when it sets no policy; a
    string (the error) when `settings.policy` is malformed, so a typo never silently loosens a rule."""
    settings = doc.get("settings") if isinstance(doc, dict) else None
    policy = settings.get("policy") if isinstance(settings, dict) else None
    if policy is None:
        return None
    if not isinstance(policy, dict):
        return "project settings.policy must be an object {locked_classes, lint_threshold}"
    classes = policy.get("locked_classes", [])
    if not isinstance(classes, list) or not all(isinstance(c, str) for c in classes):
        return "project settings.policy.locked_classes must be a list of class names"
    threshold = policy.get("lint_threshold")
    if threshold is not None and (
        isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1
    ):
        return "project settings.policy.lint_threshold must be a number between 0 and 1"
    return list(classes), None if threshold is None else float(threshold)


def reapply_project_policy(
    conn: sqlite3.Connection,
    root: Path,
    manifest: dict[str, Any],
    *,
    legend: dict[str, Any] | None = None,
    notes: list[str] | None = None,
    applied: dict[str, Any] | None = None,
    project_root: Path | str | None = None,
) -> list[str]:
    """Re-apply the project's own rules (`project.json` `settings.policy`: `{locked_classes, lint_threshold}`)
    to the packet; the stricter of the project's and the sender's result wins (PACKET.md section 7). A legend
    id of a locked class that the sender made editable moves from `policy.editable_ids` to `locked_ids`, and an
    unlabelled share over the threshold is an error. `manifest` is updated in memory; the packet on disk is
    never rewritten. `applied` (when given) receives the effective policy for the `ingest.done` event.
    No project.json, or no `settings.policy` in it, is a no-op."""
    notes = notes if notes is not None else []
    project_json = _project_json_path(conn, str(manifest.get("project_id")), project_root)
    if project_json is None or not project_json.is_file():
        return []
    try:
        parsed = _project_policy(read_json(project_json))
    except (OSError, ValueError) as e:
        return [f"{project_json.name} is unreadable, so the project's policy cannot be applied: {e}"]
    if parsed is None:
        return []
    if isinstance(parsed, str):
        return [parsed]
    locked_classes, lint_threshold = parsed

    legend = legend if legend is not None else (_load_legend(root, manifest) or {})
    entries = [e for e in legend.get("entries") or [] if isinstance(e, dict)]
    class_of = {str(e.get("id")): e.get("class") for e in entries}
    policy = manifest.setdefault("policy", {})
    editable = [str(i) for i in policy.get("editable_ids") or []]
    locked = [str(i) for i in policy.get("locked_ids") or []]
    demoted = [i for i in editable if class_of.get(i) in locked_classes]
    if demoted:
        editable = [i for i in editable if i not in demoted]
        locked += [i for i in demoted if i not in locked]
        notes.extend(f"policy: {i} locked by project class rule" for i in demoted)
        policy["editable_ids"], policy["locked_ids"] = editable, locked

    errors: list[str] = []
    unlabelled = 0.0
    if lint_threshold is not None:
        claimed = ((manifest.get("id_legend") or {}).get("coverage") or {}).get("unlabelled_share") or 0.0
        derived = sum(
            float(e.get("pixel_share") or 0.0)
            for e in entries
            if e.get("class_source") == "unlabelled" and str(e.get("id")).startswith(("o:", "s:"))
        )
        unlabelled = max(float(claimed), derived)
        if unlabelled > lint_threshold:
            errors.append(
                f"unlabelled share {unlabelled:.4f} exceeds the project's lint threshold {lint_threshold:g}"
            )
    if applied is not None:
        applied.update(
            {
                "source": "project.json settings.policy",
                "locked_classes": locked_classes,
                "lint_threshold": lint_threshold,
                "unlabelled_share": unlabelled if lint_threshold is not None else None,
                "demoted_ids": demoted,
                "editable_ids": editable,
                "locked_ids": locked,
            }
        )
    return errors


def effective_policy(conn: sqlite3.Connection, packet_id: str) -> dict[str, Any] | None:
    """The project policy `reapply_project_policy` applied when this packet was ingested (the `policy` of its
    `ingest.done` event), or None when the project set none. Packets are immutable, so this is where the
    stricter result lives."""
    row = conn.execute(
        "SELECT data FROM packet_events WHERE packet_id = ? AND type = 'ingest.done' ORDER BY seq DESC LIMIT 1",
        (packet_id,),
    ).fetchone()
    policy = loads(row[0]).get("policy") if row else None
    return policy if isinstance(policy, dict) else None


def apply_effective_policy(manifest: dict[str, Any], applied: dict[str, Any] | None) -> dict[str, Any]:
    """A copy of `manifest` whose `policy.editable_ids` / `locked_ids` are the effective ones (what a
    `PacketView` built on it reads). `applied=None` returns the manifest unchanged."""
    if not applied:
        return manifest
    out = {**manifest, "policy": {**(manifest.get("policy") or {})}}
    out["policy"]["editable_ids"] = list(applied.get("editable_ids") or [])
    out["policy"]["locked_ids"] = list(applied.get("locked_ids") or [])
    return out


def finish_ingest(
    conn: sqlite3.Connection,
    cas: CAS,
    packet_id: str,
    *,
    cas_copy: bool = False,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """Hash and index every referenced file, then run the two P1 hooks. Never raises for a bad packet: it
    records `ingest.failed`. `project_root` names the project folder when it is not the one the index has
    registered (or `<home>/projects/<id>`)."""
    rec = get_packet(conn, packet_id)
    root = Path(rec["path"])
    try:
        mbytes = (root / "manifest.json").read_bytes()
        if hash_bytes(mbytes) != rec["manifest_blake3"]:
            _fail(conn, packet_id, ["manifest.json changed after the packet was accepted; re-send it"])
            return get_packet(conn, packet_id)
        manifest = json.loads(mbytes.decode("utf-8-sig"))
        files = referenced_files(manifest, root)
        _event(conn, packet_id, "ingest.started", {"files": len(files), "cas_copy": cas_copy})
        total = 0
        for role, rel, want_hash, want_size in files:
            p = root / rel
            digest, size = hash_file(p)
            if want_hash is not None and digest != want_hash:
                _fail(conn, packet_id, [f"{rel}: blake3 changed since validation"])
                return get_packet(conn, packet_id)
            if want_size is not None and size != want_size:
                _fail(conn, packet_id, [f"{rel}: size {size} != manifest bytes {want_size}"])
                return get_packet(conn, packet_id)
            in_cas = 0
            if cas_copy:
                if cas.put_file(p) != digest:
                    _fail(conn, packet_id, [f"{rel}: changed while being copied into the CAS"])
                    return get_packet(conn, packet_id)
                in_cas = 1
            total += size
            with tx(conn):
                conn.execute(
                    """INSERT OR REPLACE INTO blobs (packet_id, relpath, role, size, blake3, in_cas)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (packet_id, rel, role, size, digest, in_cas),
                )
                _event(
                    conn,
                    packet_id,
                    "ingest.file",
                    {"relpath": rel, "role": role, "bytes": size, "blake3": digest},
                )
        notes: list[str] = []
        applied: dict[str, Any] = {}
        legend = _load_legend(root, manifest)
        problems = recompute_coverage_from_cryptomatte(
            root, manifest, legend=legend, notes=notes
        ) + reapply_project_policy(
            conn,
            root,
            manifest,
            legend=legend,
            notes=notes,
            applied=applied,
            project_root=project_root,
        )
        if problems:
            _fail(conn, packet_id, problems)
            return get_packet(conn, packet_id)
        with tx(conn):
            conn.execute(
                "UPDATE packets SET status = 'ingested', ingested_utc = ?, error = NULL WHERE packet_id = ?",
                (iso(utcnow()), packet_id),
            )
            done: dict[str, Any] = {"files": len(files), "bytes": total}
            if notes:
                done["notes"] = notes
            if applied:
                done["policy"] = applied
            _event(conn, packet_id, "ingest.done", done)
    except Exception as e:  # a vanished share or unreadable file: record it, never hang the event stream
        _fail(conn, packet_id, [f"{type(e).__name__}: {e}"])
    return get_packet(conn, packet_id)


def get_packet(conn: sqlite3.Connection, packet_id: str, *, with_blobs: bool = True) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM packets WHERE packet_id = ?", (packet_id,)).fetchone()
    if row is None:
        raise PacketNotFound(packet_id)
    rec = dict(row)
    if with_blobs:
        rec["blobs"] = [
            dict(r)
            for r in conn.execute(
                "SELECT relpath, role, size, blake3, in_cas FROM blobs WHERE packet_id = ? ORDER BY relpath",
                (packet_id,),
            )
        ]
    return rec


def packet_events(conn: sqlite3.Connection, packet_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT seq, type, data, at_utc FROM packet_events WHERE packet_id = ? AND seq > ? ORDER BY seq",
        (packet_id, after_seq),
    ).fetchall()
    return [
        {
            "packet_id": packet_id,
            "seq": r["seq"],
            "type": r["type"],
            "data": loads(r["data"]),
            "at_utc": r["at_utc"],
        }
        for r in rows
    ]


def pending_ingests(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT packet_id FROM packets WHERE status = 'ingesting' ORDER BY received_utc"
        )
    ]
