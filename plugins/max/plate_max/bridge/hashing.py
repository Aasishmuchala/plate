"""BLAKE3 for the packet, computed by the loopback daemon (BUILD_PLAN decision 5b).

Max's Python has no blake3 and plate_max is stdlib-only, while the schema keeps hash.algo
"blake3". So every hash the manifest carries comes from POST /api/v0/hash: files by path (the
daemon confines paths to its packets folders and streams them), in-memory bytes as base64 payloads
(the scene digest, the view payload, the legend and camera_frames sidecars, legend node records,
the OCIO config). The daemon rehashes everything again on ingest, so a wrong answer here can only
make a packet fail ingest, never pass it.

  hash_packet_files(folder, config)       {rel: {hex, bytes}} for every file the packet references
  hash_payload_bytes({name: bytes}, cfg)  {name: {hex, bytes}}, batched under the daemon's 4 MiB cap
  hash_scene / hash_view                  the manifest's scene_hash and view_hash blocks
  sidecar_hashes(legend=, frames=)        id_legend.json / camera_frames.json refs before writing
  with_daemon_digests(build, config)      runs a digest_fn consumer (legend.build_id_legend) on
                                          daemon BLAKE3 with one request instead of one per node
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

from .. import transport
from ..digest import DIGEST_VERSION, SCENE_DIGEST_VERSION, scene_hash_payload
from ..packet import legend_bytes

T = TypeVar("T")

MANIFEST_NAME = "manifest.json"
# Names never hashed when the folder is walked: the manifest itself and temp files mid-write.
SKIP_SUFFIXES = (".tmp", ".partial")
PLACEHOLDER_HEX = "0" * 64


class HashAnswerError(transport.TransportError):
    """The daemon's answer does not fit the request (a missing entry, a size that disagrees with
    the file on disk)."""


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def referenced_files(manifest: Mapping[str, Any]) -> list[str]:
    """Packet-relative paths of every file a manifest references: file refs, and every frame of a
    sequence (pattern % n for first..last). Sequence hash listings are excluded (they are not hashed
    in the manifest; plate_spec checks the frames through them)."""
    rels: list[str] = []
    for ref in manifest.get("files", {}).values():
        if ref.get("kind") == "file":
            rels.append(str(ref["path"]))
        elif ref.get("kind") == "sequence":
            rels.extend(str(ref["pattern"]) % n for n in range(int(ref["first"]), int(ref["last"]) + 1))
    return sorted(dict.fromkeys(rels))


def folder_files(folder: Path | str) -> list[str]:
    """Every file under folder except manifest.json and temp files, as sorted POSIX relative paths."""
    root = Path(folder)
    return sorted(
        _rel(root, p)
        for p in root.rglob("*")
        if p.is_file() and p.name != MANIFEST_NAME and not p.name.endswith(SKIP_SUFFIXES)
    )


def hash_packet_files(
    folder: Path | str,
    config: dict[str, Any],
    rels: Sequence[str] | None = None,
    timeout_s: float = transport.HASH_TIMEOUT_S,
) -> dict[str, dict[str, Any]]:
    """{rel: {"hex", "bytes"}} for the packet's files, hashed by the daemon (transport.hash_files).

    rels defaults to every file the folder's manifest.json references when one exists, else every
    file in the folder (the usual case: hash after the render, before build_manifest). The folder
    must sit inside the daemon's packets folders (403 otherwise: transport.HashRefused). Each answer's
    size is checked against the file on disk."""
    root = Path(folder)
    if rels is None:
        manifest_path = root / MANIFEST_NAME
        if manifest_path.is_file():
            rels = referenced_files(json.loads(manifest_path.read_text(encoding="utf-8-sig")))
        else:
            rels = folder_files(root)
    rels = list(rels)
    if not rels:
        return {}
    paths = [root.joinpath(*rel.split("/")) for rel in rels]
    answer = transport.hash_files(paths, config, timeout_s=timeout_s)
    out: dict[str, dict[str, Any]] = {}
    for rel, path in zip(rels, paths, strict=True):
        entry = answer.get(str(path))
        if not isinstance(entry, dict) or not entry.get("hex"):
            raise HashAnswerError(f"the daemon returned no hash for {rel}")
        size = path.stat().st_size
        reported = entry.get("bytes")
        if reported is not None and int(reported) != size:
            raise HashAnswerError(f"{rel}: the daemon hashed {reported} bytes, the file has {size}")
        out[rel] = {"hex": str(entry["hex"]), "bytes": size}
    return out


def hash_payload_bytes(
    payloads: Mapping[str, bytes],
    config: dict[str, Any],
    timeout_s: float = transport.HASH_TIMEOUT_S,
) -> dict[str, dict[str, Any]]:
    """{name: {"hex", "bytes"}} for in-memory bytes, in as few requests as the daemon's payload cap
    (transport.HASH_PAYLOAD_LIMIT) allows. One payload over the cap raises ValueError: hash it as a
    file inside the packet folder instead."""
    out: dict[str, dict[str, Any]] = {}
    batch: dict[str, bytes] = {}
    size = 0
    limit = transport.HASH_PAYLOAD_LIMIT
    for name, data in payloads.items():
        if len(data) > limit:
            raise ValueError(f"payload {name!r} is {len(data)} bytes, over the daemon's {limit}-byte cap")
        if batch and size + len(data) > limit:
            out.update(_payload_batch(batch, config, timeout_s))
            batch, size = {}, 0
        batch[name] = bytes(data)
        size += len(data)
    if batch:
        out.update(_payload_batch(batch, config, timeout_s))
    return out


def _payload_batch(
    batch: dict[str, bytes], config: dict[str, Any], timeout_s: float
) -> dict[str, dict[str, Any]]:
    answer = transport.hash_payloads(batch, config, timeout_s=timeout_s)
    return {name: {"hex": str(answer[name]["hex"]), "bytes": len(data)} for name, data in batch.items()}


def hash_scene(scene_digest: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """The manifest's scene_hash block: BLAKE3 of digest.scene_hash_payload plus its digest_version."""
    entry = hash_payload_bytes({"scene_digest": scene_hash_payload(scene_digest)}, config)["scene_digest"]
    version = scene_digest.get("digest_version") or (
        SCENE_DIGEST_VERSION if scene_digest.get("lines") is not None else DIGEST_VERSION
    )
    return {"algo": "blake3", "hex": entry["hex"], "digest_version": str(version)}


def hash_view(view_payload: bytes, config: dict[str, Any]) -> dict[str, str]:
    """The manifest's view_hash block: BLAKE3 of packet.view_hash_payload(...)."""
    entry = hash_payload_bytes({"view": view_payload}, config)["view"]
    return {"algo": "blake3", "hex": entry["hex"]}


def sidecar_hashes(
    config: dict[str, Any],
    legend: dict[str, Any] | None = None,
    frames_bytes: bytes | None = None,
    legend_path: str = "id_legend.json",
    frames_path: str = "camera_frames.json",
) -> dict[str, dict[str, Any]]:
    """{rel: {hex, bytes}} for the JSON sidecars write_packet will write, hashed before they exist:
    the legend's exact bytes (packet.legend_bytes) and camera_frames' (packet.camera_frames_bytes)."""
    payloads: dict[str, bytes] = {}
    if legend is not None:
        payloads[legend_path] = legend_bytes(legend)
    if frames_bytes is not None:
        payloads[frames_path] = frames_bytes
    return hash_payload_bytes(payloads, config) if payloads else {}


def with_daemon_digests(build: Callable[[Callable[[bytes], str]], T], config: dict[str, Any]) -> T:
    """build(digest_fn) twice: once with a recorder that collects every payload digest_fn is asked
    for, then, after one hash request for all of them, with the daemon's answers. build must be
    deterministic (legend.build_id_legend is, given a fixed packet_id), e.g.

        legend = with_daemon_digests(lambda fn: build_id_legend(nodes, pid, digest_fn=fn), cfg)"""
    seen: dict[bytes, str] = {}

    def record(data: bytes) -> str:
        seen.setdefault(bytes(data), f"p{len(seen)}")
        return PLACEHOLDER_HEX

    build(record)
    answer = hash_payload_bytes({name: data for data, name in seen.items()}, config) if seen else {}
    table = {data: answer[name]["hex"] for data, name in seen.items()}

    def lookup(data: bytes) -> str:
        try:
            return table[bytes(data)]
        except KeyError:
            raise HashAnswerError("build asked for a digest it did not ask for on the first pass") from None

    return build(lookup)


def sequence_listing(pattern: str, first: int, last: int, hashes: Mapping[str, Mapping[str, Any]]) -> bytes:
    """The sequence hashes file (PACKET.md 4.1): one "<hex>  <rel>" line per frame, first..last,
    from a hash_packet_files answer. Raises KeyError naming the first frame that was not hashed."""
    lines = []
    for n in range(int(first), int(last) + 1):
        rel = pattern % n
        if rel not in hashes:
            raise KeyError(f"frame {n} ({rel}) was not hashed")
        lines.append(f"{hashes[rel]['hex']}  {rel}")
    return ("\n".join(lines) + "\n").encode("utf-8")
