"""BLAKE3 for the Max plugin, which has no blake3 in its stdlib-only Python (Decision 5b).

`POST /api/v0/hash` hashes files the plugin has just written and in-memory payloads (scene and view
digests) so the manifest it writes carries `hash.algo: blake3`; the daemon rehashes on ingest anyway.
A file is hashed only when its path is absolute, lies inside one of `Settings.packets_dirs` (lexically
after collapsing `..`, and again after resolving the real path, via `Path.is_relative_to`), no component
below that root is a symlink or junction, and it is a regular file opened with `O_NOFOLLOW` where the OS
has it. Anything else is `HashRefused` (HTTP 403); a missing file is `FileNotFoundError` (404). Files are
streamed in 1 MiB chunks; payloads are limited to `MAX_PAYLOAD_BYTES` in total.
"""

from __future__ import annotations

import base64
import binascii
import os
import stat
from collections.abc import Iterable
from pathlib import Path

from blake3 import blake3

CHUNK = 1 << 20
MAX_PAYLOAD_BYTES = 4 << 20
MAX_NAME_LEN = 200
ALGO = "blake3"


class HashRefused(PermissionError):
    pass


class BadHashRequest(ValueError):
    pass


class PayloadTooLarge(BadHashRequest):
    pass


def _is_link(p: Path) -> bool:
    return p.is_symlink() or p.is_junction()


def contained_path(raw: str, roots: Iterable[Path]) -> Path:
    """The real file a requested path names, provided it is inside one of `roots`. Raises HashRefused."""
    if not isinstance(raw, str) or not raw:
        raise BadHashRequest("paths must be non-empty strings")
    p = Path(raw)
    if not p.is_absolute():
        raise BadHashRequest(f"{raw!r} is not an absolute path")
    lexical = Path(os.path.normpath(p))
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        real_root = root.resolve()
        for base in (Path(os.path.normpath(root.absolute())), real_root):
            if lexical.is_relative_to(base):
                rel = lexical.relative_to(base)
                break
        else:
            continue
        if not rel.parts:
            raise HashRefused(f"{raw}: a packets folder itself is not a file")
        cur = real_root
        for part in rel.parts:
            cur = cur / part
            if _is_link(cur):
                raise HashRefused(f"{raw}: {cur} is a symlink; the daemon never follows links")
        if not cur.exists():
            raise FileNotFoundError(raw)
        if not cur.resolve(strict=True).is_relative_to(real_root):
            raise HashRefused(f"{raw}: resolves outside the packets folders")
        return cur
    raise HashRefused(f"{raw}: outside the packets folders")


def hash_contained_file(path: Path) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as e:  # ELOOP: the last component became a link after the check
        raise HashRefused(f"{path}: cannot open without following links ({e})") from e
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise HashRefused(f"{path}: not a regular file")
        h = blake3()
        n = 0
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
            n += len(chunk)
    return {"algo": ALGO, "hex": h.hexdigest(), "bytes": n}


def hash_paths(paths: Iterable[str], roots: Iterable[Path]) -> dict[str, dict[str, object]]:
    roots = list(roots)
    return {raw: hash_contained_file(contained_path(raw, roots)) for raw in paths}


def hash_payloads(payloads: dict[str, str]) -> dict[str, dict[str, object]]:
    decoded: dict[str, bytes] = {}
    total = 0
    for name, b64 in payloads.items():
        if not name or len(name) > MAX_NAME_LEN:
            raise BadHashRequest(f"payload names must be 1-{MAX_NAME_LEN} characters")
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise BadHashRequest(f"payload {name!r} is not valid base64") from e
        total += len(data)
        if total > MAX_PAYLOAD_BYTES:
            raise PayloadTooLarge(f"payloads exceed {MAX_PAYLOAD_BYTES} bytes in total")
        decoded[name] = data
    return {
        name: {"algo": ALGO, "hex": blake3(d).hexdigest(), "bytes": len(d)} for name, d in decoded.items()
    }
