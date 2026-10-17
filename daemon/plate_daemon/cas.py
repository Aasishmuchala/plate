"""Content-addressed store keyed by BLAKE3 hex, under `<home>/cas`.

Objects live at `objects/<key[:2]>/<key[2:]>` (short, colon-free paths: a 60-character home stays far
below MAX_PATH). Writes go to `tmp/<uuid>` first and are moved into place with `os.replace` retried with
backoff (Windows and Defender hold new files briefly). Putting the same bytes twice is a no-op returning
the same key. The CAS is a rebuildable cache: packets are ingested by reference and only copied in when
`PLATE_CAS_COPY=1`; engine results are always copied in at once because result URLs expire.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import BinaryIO

from blake3 import blake3

from .fsutil import replace_with_retry

KEY_RE = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 1 << 20


class CASError(RuntimeError):
    pass


def hash_bytes(b: bytes) -> str:
    return blake3(b).hexdigest()


def hash_file(path: Path) -> tuple[str, int]:
    """(blake3 hex, size) of a file, streamed in 1 MiB chunks."""
    h = blake3()
    n = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


class CAS:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.tmp = self.root / "tmp"

    def path_for(self, key: str) -> Path:
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise CASError(f"not a blake3 hex key: {key!r}")
        return self.objects / key[:2] / key[2:]

    def has(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def _commit(self, tmp: Path, key: str) -> str:
        dest = self.path_for(key)
        if dest.is_file():
            tmp.unlink(missing_ok=True)
            return key
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            replace_with_retry(tmp, dest)
        except PermissionError:
            # A concurrent put of the same bytes won the race and a reader holds it open: same content.
            if dest.is_file():
                tmp.unlink(missing_ok=True)
                return key
            raise
        return key

    def _new_tmp(self) -> Path:
        self.tmp.mkdir(parents=True, exist_ok=True)
        return self.tmp / uuid.uuid4().hex

    def put_bytes(self, b: bytes) -> str:
        key = hash_bytes(b)
        if self.has(key):
            return key
        tmp = self._new_tmp()
        try:
            with open(tmp, "wb") as fh:
                fh.write(b)
            return self._commit(tmp, key)
        finally:
            tmp.unlink(missing_ok=True)

    def put_file(self, path: Path) -> str:
        """Copy a file in (hashing while copying, so a file changing mid-copy cannot be mis-keyed)."""
        tmp = self._new_tmp()
        try:
            h = blake3()
            with open(path, "rb") as src, open(tmp, "wb") as dst:
                for chunk in iter(lambda: src.read(CHUNK), b""):
                    h.update(chunk)
                    dst.write(chunk)
            return self._commit(tmp, h.hexdigest())
        finally:
            tmp.unlink(missing_ok=True)

    def open(self, key: str, mode: str = "rb") -> BinaryIO:
        """Read-only binary handle on an object (objects are immutable; `mode` must be "rb")."""
        if mode != "rb":
            raise CASError("CAS objects are immutable: open them with mode='rb'")
        p = self.path_for(key)
        if not p.is_file():
            raise CASError(f"missing object {key}")
        return open(p, "rb")

    def read_bytes(self, key: str) -> bytes:
        p = self.path_for(key)
        if not p.is_file():
            raise CASError(f"missing object {key}")
        return p.read_bytes()

    def verify(self, key: str) -> bool:
        p = self.path_for(key)
        if not p.is_file():
            return False
        return hash_file(p)[0] == key
