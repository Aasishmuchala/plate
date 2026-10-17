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
from .db import dumps, loads, tx
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


def recompute_coverage_from_cryptomatte(root: Path, manifest: dict[str, Any]) -> list[str]:
    """TODO(P1): recompute `id_legend.coverage` from the EXR's Cryptomatte layers (needs OpenEXR on the
    box) and return an error when the legend omits a visible ID or the shares disagree. The sender's
    coverage is a claim until this runs. No-op today."""
    return []


def reapply_project_policy(conn: sqlite3.Connection, root: Path, manifest: dict[str, Any]) -> list[str]:
    """TODO(P1): re-apply the project's own class rules and lint threshold to the legend; the stricter of
    the project's and the sender's result wins (PACKET.md section 7). No-op today."""
    return []


def finish_ingest(
    conn: sqlite3.Connection, cas: CAS, packet_id: str, *, cas_copy: bool = False
) -> dict[str, Any]:
    """Hash and index every referenced file. Never raises for a bad packet: it records `ingest.failed`."""
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
        problems = recompute_coverage_from_cryptomatte(root, manifest) + reapply_project_policy(
            conn, root, manifest
        )
        if problems:
            _fail(conn, packet_id, problems)
            return get_packet(conn, packet_id)
        with tx(conn):
            conn.execute(
                "UPDATE packets SET status = 'ingested', ingested_utc = ?, error = NULL WHERE packet_id = ?",
                (iso(utcnow()), packet_id),
            )
            _event(conn, packet_id, "ingest.done", {"files": len(files), "bytes": total})
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
