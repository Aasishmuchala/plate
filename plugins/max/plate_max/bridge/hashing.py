"""BLAKE3 for the packet, computed by the loopback daemon (BUILD_PLAN decision 5b).

Max's Python has no blake3 and plate_max is stdlib-only, while the schema keeps hash.algo
"blake3". So every hash the manifest carries comes from POST /api/v0/hash: files by path (the
daemon confines paths to its packets folders and streams them), in-memory bytes as base64 payloads
(the view payload, legend node records, the OCIO config). The daemon rehashes everything again on
ingest, so a wrong answer here can only make a packet fail ingest, never pass it.

Big payloads go by path. A payload is sent in memory only up to PAYLOAD_LIMIT (1 MiB; one over it
is PayloadTooLarge, code PAYLOAD_TOO_LARGE, naming it and its size). The scene digest of a big scene
and the legend of a dense frame pass that easily, so given the packet folder they are written into it
first and hashed as files: scene_digest.txt (file role x_scene_digest, the canonical digest text),
id_legend.json and camera_frames.json (the exact bytes write_packet writes). An oversize view payload
is written to a temp file in the folder, hashed and removed.

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
from ..fsutil import atomic_write_bytes
from ..packet import file_ref_from_hashes, legend_bytes

T = TypeVar("T")

MANIFEST_NAME = "manifest.json"
# Names never hashed when the folder is walked: the manifest itself and temp files mid-write.
SKIP_SUFFIXES = (".tmp", ".partial")
PLACEHOLDER_HEX = "0" * 64
PAYLOAD_LIMIT = 1024 * 1024  # in-memory hashing stops here; bigger bytes are hashed as a file
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
SCENE_DIGEST_NAME = "scene_digest.txt"
SCENE_DIGEST_ROLE = "x_scene_digest"
# The schema's file_ref.format enum has no text value (exr, tiff, png, jpeg, json); "json" is the
# nearest. The file is the canonical digest text, not JSON: readers go by the role, never the format.
SCENE_DIGEST_FORMAT = "json"
VIEW_PAYLOAD_TEMP = "view_payload.tmp"


class HashAnswerError(transport.TransportError):
    """The daemon's answer does not fit the request (a missing entry, a size that disagrees with
    the file on disk)."""


class PayloadTooLarge(ValueError):
    """An in-memory payload over the limit (code PAYLOAD_TOO_LARGE): write it into the packet
    folder and hash it by path instead."""

    code = PAYLOAD_TOO_LARGE

    def __init__(self, name: str, size: int, limit: int):
        super().__init__(
            f"{PAYLOAD_TOO_LARGE}: payload {name!r} is {size} bytes, over the {limit}-byte cap for "
            "in-memory hashing: hash it as a file inside the packet folder"
        )
        self.name = name
        self.size = int(size)
        self.limit = int(limit)


def payload_limit() -> int:
    """The largest payload hashed in memory: PAYLOAD_LIMIT, or the daemon's per-request cap when
    that is smaller."""
    return min(PAYLOAD_LIMIT, transport.HASH_PAYLOAD_LIMIT)


def check_payload(name: str, size: int) -> None:
    """PayloadTooLarge when a payload of size bytes may not be hashed in memory."""
    limit = payload_limit()
    if int(size) > limit:
        raise PayloadTooLarge(name, int(size), limit)


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
    (transport.HASH_PAYLOAD_LIMIT) allows. Every payload is checked before anything is sent: one over
    payload_limit() raises PayloadTooLarge (a ValueError): hash it as a file inside the packet
    folder instead."""
    for name, data in payloads.items():
        check_payload(name, len(data))
    out: dict[str, dict[str, Any]] = {}
    batch: dict[str, bytes] = {}
    size = 0
    limit = transport.HASH_PAYLOAD_LIMIT
    for name, data in payloads.items():
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


def hash_file_bytes(
    folder: Path | str, rel: str, data: bytes, config: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Write data to folder/rel (temp then replace) and hash it by path: {rel: {hex, bytes}}. The
    folder must sit inside the daemon's packets folders."""
    return hash_files_bytes(folder, {rel: data}, config)


def hash_files_bytes(
    folder: Path | str, files: Mapping[str, bytes], config: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """hash_file_bytes for several files, in one request."""
    root = Path(folder)
    for rel, data in files.items():
        atomic_write_bytes(root.joinpath(*rel.split("/")), bytes(data))
    return hash_packet_files(root, config, rels=list(files)) if files else {}


def _scene_block(scene_digest: dict[str, Any], hex_value: str) -> dict[str, Any]:
    version = scene_digest.get("digest_version") or (
        SCENE_DIGEST_VERSION if scene_digest.get("lines") is not None else DIGEST_VERSION
    )
    return {"algo": "blake3", "hex": str(hex_value), "digest_version": str(version)}


def hash_scene(
    scene_digest: dict[str, Any],
    config: dict[str, Any],
    folder: Path | str | None = None,
    hashes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The manifest's scene_hash block: BLAKE3 of digest.scene_hash_payload plus its digest_version.

    With the packet folder the canonical digest text is written into it as scene_digest.txt (file
    role x_scene_digest, see scene_digest_ref) and hashed by path, whatever its size; its hash entry
    lands in hashes when given (the manifest then references the file). Without a folder the bytes go
    as a payload, which PayloadTooLarge refuses over payload_limit()."""
    payload = scene_hash_payload(scene_digest)
    if folder is not None:
        entry = hash_file_bytes(folder, SCENE_DIGEST_NAME, payload, config)[SCENE_DIGEST_NAME]
        if hashes is not None:
            hashes[SCENE_DIGEST_NAME] = entry
        return _scene_block(scene_digest, entry["hex"])
    entry = hash_payload_bytes({"scene_digest": payload}, config)["scene_digest"]
    return _scene_block(scene_digest, entry["hex"])


def scene_digest_ref(hashes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The files.x_scene_digest entry for scene_digest.txt from a hash_scene(folder=, hashes=) answer."""
    return file_ref_from_hashes(SCENE_DIGEST_NAME, dict(hashes), SCENE_DIGEST_FORMAT)


def hash_view(
    view_payload: bytes, config: dict[str, Any], folder: Path | str | None = None
) -> dict[str, str]:
    """The manifest's view_hash block: BLAKE3 of packet.view_hash_payload(...). A payload over
    payload_limit() (a frame with tens of thousands of visible ids) is hashed by path through a temp
    file in the packet folder when one is given (the file is removed afterwards), else
    PayloadTooLarge."""
    if len(view_payload) <= payload_limit() or folder is None:
        entry = hash_payload_bytes({"view": view_payload}, config)["view"]
        return {"algo": "blake3", "hex": entry["hex"]}
    temp = Path(folder) / VIEW_PAYLOAD_TEMP
    try:
        entry = hash_file_bytes(folder, VIEW_PAYLOAD_TEMP, view_payload, config)[VIEW_PAYLOAD_TEMP]
    finally:
        try:
            temp.unlink()
        except OSError:  # a scanner holding it on Windows: a .tmp is never part of the packet
            pass
    return {"algo": "blake3", "hex": entry["hex"]}


def sidecar_hashes(
    config: dict[str, Any],
    legend: dict[str, Any] | None = None,
    frames_bytes: bytes | None = None,
    legend_path: str = "id_legend.json",
    frames_path: str = "camera_frames.json",
    folder: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """{rel: {hex, bytes}} for the JSON sidecars write_packet writes: the legend's exact bytes
    (packet.legend_bytes) and camera_frames' (packet.camera_frames_bytes). With the packet folder
    they are written into it now (write_packet rewrites the same bytes) and hashed by path, so a
    dense legend never meets the payload limit; without one they go as payloads."""
    files: dict[str, bytes] = {}
    if legend is not None:
        files[legend_path] = legend_bytes(legend)
    if frames_bytes is not None:
        files[frames_path] = bytes(frames_bytes)
    if not files:
        return {}
    if folder is not None:
        return hash_files_bytes(folder, files, config)
    return hash_payload_bytes(files, config)


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
