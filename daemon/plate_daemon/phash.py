"""Perceptual-hash index and drop-to-resolve (BUILD_PLAN decision 26).

A stripped JPEG or a screenshot dropped on the canvas carries no lineage, so the daemon also keeps a
perceptual hash for every stored image and finds the nearest one. `compute(path)` is `imagehash.phash`
with `hash_size=16` (a 256-bit hash, 64 hex characters) on a thumbnail that is 512 px wide at most
(never enlarged). EXR is tone-mapped (clip, sRGB encode) and 16-bit or float TIFF is scaled to 8 bits
first, so a finished EXR and its sRGB PNG hash alike. Absolute hash values are not part of the contract
(a median threshold can flip a bit between libm builds); distances are.

`PhashIndex` keeps `content_hash -> phash` in the project index's `assets.phash` column (the column
already exists, so there is no migration) or, with no index given, in memory. The index argument is:

    None                              in-memory (tests, one-off scans)
    sqlite3.Connection                an autocommit connection from `plate_daemon.db.connect`
    plate_daemon.index.Index          plus `project_id=`; a connection is opened for each call

`add` needs the asset to exist already (`Index.record_asset` or the CAS `on_put` hook): a phash belongs
to a stored object, and inventing an asset row would hide a missing `record_asset`. `nearest` scans the
table and compares by Hamming distance; the table is small (one row per stored image).

`resolve_drop(path, index)` answers "what is this file?" in the order lineage, then the exact BLAKE3
content hash, then the nearest perceptual hash within `max_distance` (12 of 256 bits by default), and
returns None when nothing matches. A lineage answer is the file's own claim (it can come from another
project): the caller compares `project_id` and checks the run exists before acting on it.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from . import lineage as _lineage
from .cas import check_key, hash_file
from .db import tx

__all__ = [
    "DEFAULT_MAX_DISTANCE",
    "HASH_SIZE",
    "PhashError",
    "PhashIndex",
    "compute",
    "distance",
    "resolve_drop",
]

HASH_SIZE = 16
HASH_HEX_CHARS = HASH_SIZE * HASH_SIZE // 4
THUMB_WIDTH = 512
DEFAULT_MAX_DISTANCE = 12
_HEX = frozenset("0123456789abcdef")


class PhashError(ValueError):
    pass


# ---------------------------------------------------------------------------- hashing


def _check_phash(h: Any) -> str:
    if not isinstance(h, str) or len(h) != HASH_HEX_CHARS or not _HEX.issuperset(h):
        raise PhashError(
            f"not a {HASH_SIZE}x{HASH_SIZE} perceptual hash ({HASH_HEX_CHARS} lower-case hex characters)"
        )
    return h


def distance(a: str, b: str) -> int:
    """Hamming distance in bits between two hashes."""
    return (int(_check_phash(a), 16) ^ int(_check_phash(b), 16)).bit_count()


def _to_uint8(arr: np.ndarray) -> np.ndarray:
    if arr.dtype == np.uint8:
        return arr
    if arr.dtype == np.uint16:
        return (arr >> 8).astype(np.uint8)
    a = np.nan_to_num(arr.astype(np.float32), nan=0.0, posinf=1.0, neginf=0.0)
    if np.issubdtype(arr.dtype, np.integer):  # 32-bit gray and the like: scale to the brightest pixel
        a = a / max(float(a.max()), 1.0)
    return (np.clip(a, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def _from_array(arr: np.ndarray) -> Image.Image:
    if arr.ndim == 3 and arr.shape[0] in (1, 3, 4) and arr.shape[-1] not in (1, 3, 4):
        arr = np.moveaxis(arr, 0, -1)  # planar -> interleaved
    arr = _to_uint8(arr)
    if arr.ndim == 3:
        arr = arr[..., 0] if arr.shape[2] < 3 else arr[..., :3]
    if arr.ndim != 2 and not (arr.ndim == 3 and arr.shape[2] == 3):
        raise PhashError(f"unsupported pixel layout {arr.shape}")
    return Image.fromarray(np.ascontiguousarray(arr))


def _exr_image(path: Path) -> Image.Image:
    import OpenEXR

    with OpenEXR.File(str(path), separate_channels=True) as exr:
        channels = exr.channels()
        names = [n for n in ("R", "G", "B") if n in channels]
        if len(names) == 3:
            lin = np.stack([channels[n].pixels for n in names], axis=-1).astype(np.float32)
        elif "Y" in channels:
            lin = channels["Y"].pixels.astype(np.float32)
        else:
            raise PhashError(f"{path.name}: no R, G, B or Y channels to hash")
    lin = np.clip(np.nan_to_num(lin, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)
    srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055)
    return _from_array(srgb)


def _tiff_image(path: Path) -> Image.Image:
    import tifffile

    try:
        return _from_array(tifffile.imread(str(path)))
    except Exception:  # an exotic TIFF tifffile will not decode: let Pillow try
        return _pil_image(path)


def _pil_image(path: Path) -> Image.Image:
    with Image.open(path) as im:
        im.load()
        if im.mode.startswith(("I", "F")):  # 16-bit and float gray
            return _from_array(np.asarray(im))
        return im.convert("RGB" if im.mode not in ("L", "RGB") else im.mode)


def _thumb(path: Path) -> Image.Image:
    ext = path.suffix.lower()
    try:
        if ext == ".exr":
            img = _exr_image(path)
        elif ext in (".tif", ".tiff"):
            img = _tiff_image(path)
        else:
            img = _pil_image(path)
    except PhashError:
        raise
    except Exception as e:  # unreadable, truncated, a decompression bomb, not an image
        raise PhashError(f"{path.name}: cannot read as an image ({type(e).__name__}: {e})") from e
    if img.width > THUMB_WIDTH:
        height = max(1, round(img.height * THUMB_WIDTH / img.width))
        img = img.resize((THUMB_WIDTH, height), Image.Resampling.LANCZOS)
    return img


def compute(path: str | Path) -> str:
    """The 64-hex-character perceptual hash of an image file. Raises PhashError for anything else."""
    import imagehash

    return str(imagehash.phash(_thumb(Path(path)), hash_size=HASH_SIZE))


# ---------------------------------------------------------------------------- the index


class PhashIndex:
    def __init__(self, index: sqlite3.Connection | Any | None = None, project_id: str | None = None):
        self._conn: sqlite3.Connection | None = None
        self._owner: Any = None
        self._project_id = project_id
        self._mem: dict[str, str] = {}
        self._lock = threading.Lock()
        if index is None:
            return
        if isinstance(index, sqlite3.Connection):
            self._conn = index
        elif hasattr(index, "project"):
            if not project_id:
                raise TypeError("PhashIndex(Index) needs project_id=")
            self._owner = index
        else:
            raise TypeError("PhashIndex takes None, a sqlite3.Connection or a plate_daemon.index.Index")

    def _connection(self) -> AbstractContextManager[sqlite3.Connection]:
        if self._conn is not None:
            return nullcontext(self._conn)
        return self._owner.project(self._project_id)

    @property
    def in_memory(self) -> bool:
        return self._conn is None and self._owner is None

    def add(self, phash: str, content_hash: str) -> None:
        """Record `phash` for a stored object. Re-adding replaces. KeyError when the asset is not recorded."""
        _check_phash(phash)
        check_key(content_hash)
        if self.in_memory:
            with self._lock:
                self._mem[content_hash] = phash
            return
        with self._connection() as conn, tx(conn):
            cur = conn.execute("UPDATE assets SET phash = ? WHERE hash = ?", (phash, content_hash))
            if cur.rowcount == 0:
                raise KeyError(f"asset {content_hash} is not recorded in the index; record_asset it first")

    def add_file(self, path: str | Path, content_hash: str) -> str:
        """Compute the hash of an image file and record it for `content_hash`; returns the phash."""
        h = compute(path)
        self.add(h, content_hash)
        return h

    def get(self, content_hash: str) -> str | None:
        check_key(content_hash)
        if self.in_memory:
            with self._lock:
                return self._mem.get(content_hash)
        with self._connection() as conn:
            row = conn.execute("SELECT phash FROM assets WHERE hash = ?", (content_hash,)).fetchone()
        return row[0] if row else None

    def _rows(self) -> list[tuple[str, str]]:
        if self.in_memory:
            with self._lock:
                return list(self._mem.items())
        with self._connection() as conn:
            rows = conn.execute("SELECT hash, phash FROM assets WHERE phash IS NOT NULL").fetchall()
        return [(r[0], r[1]) for r in rows]

    def nearest(self, phash: str, max_distance: int = DEFAULT_MAX_DISTANCE) -> list[tuple[str, int]]:
        """(content_hash, distance) within `max_distance` bits, closest first (ties by content hash)."""
        target = int(_check_phash(phash), 16)
        hits = []
        for content_hash, stored in self._rows():
            if len(stored) != HASH_HEX_CHARS or not _HEX.issuperset(stored):
                continue  # a row written by something else; not comparable
            d = (target ^ int(stored, 16)).bit_count()
            if d <= max_distance:
                hits.append((content_hash, d))
        hits.sort(key=lambda t: (t[1], t[0]))
        return hits

    def has_content(self, content_hash: str) -> bool:
        """Is this exact content stored? In memory: has a phash been added for it."""
        check_key(content_hash)
        if self.in_memory:
            with self._lock:
                return content_hash in self._mem
        with self._connection() as conn:
            return conn.execute("SELECT 1 FROM assets WHERE hash = ?", (content_hash,)).fetchone() is not None

    def run_for(self, content_hash: str) -> str | None:
        """The newest run whose result is this content, when the index can say (it cannot in memory)."""
        if self.in_memory:
            return None
        check_key(content_hash)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT id FROM runs WHERE result_key = ? ORDER BY created_utc DESC, id DESC LIMIT 1",
                (content_hash,),
            ).fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------- drop to resolve


def _with_run(result: dict[str, Any], index: PhashIndex) -> dict[str, Any]:
    run_id = index.run_for(result["content_hash"])
    if run_id:
        result["run_id"] = run_id
    return result


def resolve_drop(
    path: str | Path, index: PhashIndex, max_distance: int = DEFAULT_MAX_DISTANCE
) -> dict[str, Any] | None:
    """Identify a dropped file: `{by: 'lineage'|'content_hash'|'phash', content_hash, run_id?, distance?}`.

    lineage: the file's embedded record (`project_id`, `node_id` and `run_id` are added); content_hash: the
    BLAKE3 of the dropped bytes is a stored asset; phash: the closest stored image within `max_distance`
    bits (`content_hash` is the stored one, `distance` how far). None when nothing matches."""
    path = Path(path)
    dropped_hash, _ = hash_file(path)
    lin = _lineage.read(path)
    if lin is not None:
        return {
            "by": "lineage",
            "run_id": lin.run_id,
            "project_id": lin.project_id,
            "node_id": lin.node_id,
            "content_hash": dropped_hash,
        }
    if index.has_content(dropped_hash):
        return _with_run({"by": "content_hash", "content_hash": dropped_hash}, index)
    try:
        phash = compute(path)
    except PhashError:
        return None
    near = index.nearest(phash, max_distance)
    if not near:
        return None
    content_hash, d = near[0]
    return _with_run({"by": "phash", "content_hash": content_hash, "distance": d}, index)
