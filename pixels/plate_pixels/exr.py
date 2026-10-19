"""Manifest-driven EXR IO (OpenEXR 3.x ``File`` API).

One reader for every host: ``read_elements`` locates each element's channels by the packet manifest's
``layer`` + ``channels`` across all parts of the file, so a V-Ray single-part multilayer EXR, a
Blender-style one-part-per-layer EXR and the repacked fixture packets all go through the same code
(router plan decision 2).

Channel naming rule (shared by reader and writer, :func:`channel_name`): the root layer ``RGBA`` (or an
empty layer) uses the bare channel (``R``); a channel that already contains a dot is the full name
(``CryptoObject00.R``); anything else is ``<layer>.<channel>`` (``VRayZDepth.Z``).

Cryptomatte: each EXR layer ``CryptoObjectNN`` holds two ranks as R/G = (id, coverage) and B/A = (id, coverage);
the ids are the murmur3 hash bit pattern stored in a float32, so they are read back through a ``uint32`` view.

Every reader opens (and so decodes) the whole file once per call. ``read_z_and_cryptomatte`` gives the depth and a
Cryptomatte from one open, for the render-it router's inputs (router plan T152), which otherwise cost two.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import OpenEXR

from .imageio import atomic_target

__all__ = [
    "MissingChannel",
    "channel_name",
    "element_key",
    "id_mask",
    "present_ids",
    "read_cryptomatte",
    "read_elements",
    "read_z_and_cryptomatte",
    "write_multilayer",
]

ROOT_LAYER = "RGBA"
CRYPTO_ELEMENTS = {"object": "cryptomatte_object", "material": "cryptomatte_material"}


class MissingChannel(LookupError):
    """A channel the manifest promises is not in the file (or the element is not in the manifest)."""

    def __init__(self, element: str, channel: str):
        super().__init__(f"element {element!r}: channel {channel!r} not found in the EXR")
        self.element = element
        self.channel = channel


# --------------------------------------------------------------------------- naming


def channel_name(layer: str, channel: str) -> str:
    """EXR channel name for a manifest ``layer`` + ``channels[i]`` pair (see the module docstring)."""
    if layer in ("", ROOT_LAYER) or "." in channel:
        return channel
    return f"{layer}.{channel}"


def element_key(entry: Mapping[str, Any]) -> str:
    """Key of a manifest element in the ``read_elements`` result. ``light_select`` repeats once per light,
    so it is keyed ``light_select[<light_name>]`` (safe in a file name); every other element is its own name."""
    name = entry["element"]
    if name == "light_select" and entry.get("light_name"):
        return f"light_select[{entry['light_name']}]"
    return name


def _manifest_elements(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    try:
        return list(manifest["files"]["beauty_exr"]["elements"])
    except (KeyError, TypeError) as exc:
        raise ValueError("manifest has no files.beauty_exr.elements") from exc


# --------------------------------------------------------------------------- reading


def _channel_index(exr: Any) -> dict[str, np.ndarray]:
    """{exact channel name: 2-D array} across all parts. In a multi-part file a bare channel is also
    reachable as ``<part name>.<channel>`` (Blender's layout); an exact name always wins."""
    index: dict[str, np.ndarray] = {}
    aliases: dict[str, np.ndarray] = {}
    for part in exr.parts:
        pname = part.name() if callable(part.name) else part.name
        for cname, ch in part.channels.items():
            index[cname] = ch.pixels
            if pname and "." not in cname:
                aliases.setdefault(f"{pname}.{cname}", ch.pixels)
    for k, v in aliases.items():
        index.setdefault(k, v)
    return index


def read_elements(
    path: str | Path, manifest: Mapping[str, Any], elements: Iterable[str] | None = None
) -> dict[str, np.ndarray]:
    """Read manifest elements as float32 arrays shaped (H, W, C) in the manifest's channel order.

    ``elements`` selects a subset by key (see :func:`element_key`; plain element names also select every
    ``light_select``). Raises :class:`MissingChannel` for a missing channel or an unknown element.
    """
    entries = _manifest_elements(manifest)
    wanted: set[str] | None = None if elements is None else set(elements)
    chosen = [e for e in entries if wanted is None or element_key(e) in wanted or e["element"] in wanted]
    if wanted is not None:
        known = {element_key(e) for e in entries} | {e["element"] for e in entries}
        for name in sorted(wanted - known):
            raise MissingChannel(name, "<element not in manifest>")
    exr = OpenEXR.File(str(path), separate_channels=True)
    index = _channel_index(exr)
    out: dict[str, np.ndarray] = {}
    for entry in chosen:
        planes = []
        for ch in entry["channels"]:
            full = channel_name(entry["layer"], ch)
            if full not in index:
                raise MissingChannel(entry["element"], full)
            planes.append(index[full])
        out[element_key(entry)] = np.stack(planes, axis=-1).astype(np.float32, copy=False)
    return out


def read_cryptomatte(
    path: str | Path, manifest: Mapping[str, Any], which: str = "object"
) -> tuple[np.ndarray, np.ndarray]:
    """Decode a Cryptomatte layer into ``(ranks, coverage)``.

    ``ranks`` is uint32 (H, W, levels) holding the murmur3 id bits, ``coverage`` float32 (H, W, levels).
    Levels are interleaved the way the format stores them: layer 00 R/G is rank 0, B/A is rank 1, layer 01
    R/G is rank 2, and so on, so rank 0 is always the dominant id. Unused ranks are id 0, coverage 0.
    """
    element = _crypto_element(which)
    return _ranks_coverage(element, read_elements(path, manifest, [element])[element])


def read_z_and_cryptomatte(
    path: str | Path, manifest: Mapping[str, Any], which: str = "object"
) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray]]:
    """``(z, (ranks, coverage))`` from one open of the file: the ``zdepth`` element as float32 (H, W, 1) and the
    ``which`` Cryptomatte exactly as :func:`read_cryptomatte` decodes it. Raises :class:`MissingChannel` like
    the readers it stands for."""
    element = _crypto_element(which)
    got = read_elements(path, manifest, ["zdepth", element])
    return got["zdepth"], _ranks_coverage(element, got[element])


def _crypto_element(which: str) -> str:
    if which not in CRYPTO_ELEMENTS:
        raise ValueError(f"which must be 'object' or 'material', not {which!r}")
    return CRYPTO_ELEMENTS[which]


def _ranks_coverage(element: str, planes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(H, W, 4 * layers) Cryptomatte planes as (uint32 ranks, float32 coverage), rank 0 first."""
    if planes.shape[2] % 4:
        raise MissingChannel(element, "<channel count is not a multiple of 4 (R,G,B,A per layer)>")
    ids = np.ascontiguousarray(planes[..., 0::2]).view(np.uint32)  # R, B of each layer
    cov = np.ascontiguousarray(planes[..., 1::2])  # G, A of each layer
    return ids, cov


def _parse_id(hex_id: str | int) -> int:
    if isinstance(hex_id, (int, np.integer)):
        return int(hex_id) & 0xFFFFFFFF
    text = str(hex_id).strip().lower()
    if ":" in text:
        text = text.split(":", 1)[1]
    if text.startswith("0x"):
        text = text[2:]
    return int(text, 16)


def id_mask(ranks: np.ndarray, coverage: np.ndarray, hex_id: str | int, threshold: float = 0.5) -> np.ndarray:
    """Boolean mask of pixels where the summed coverage of ``hex_id`` (``o:3d8800df``, ``3d8800df`` or an int)
    over all ranks is at least ``threshold``."""
    target = np.uint32(_parse_id(hex_id))
    share = np.where(ranks == target, coverage, 0.0).sum(axis=2)
    return share >= threshold


def present_ids(ranks: np.ndarray, coverage: np.ndarray, min_coverage: float = 0.0) -> set[int]:
    """The set of id values with any coverage above ``min_coverage`` (id 0 means 'unused rank' and is omitted)."""
    live = (coverage > min_coverage) & (ranks != 0)
    return {int(v) for v in np.unique(ranks[live])}


# --------------------------------------------------------------------------- writing


def write_multilayer(
    path: str | Path,
    layers: Mapping[str, Mapping[str, np.ndarray]],
    dtypes: Mapping[str, str] | None = None,
    header_attrs: Mapping[str, Any] | None = None,
) -> None:
    """Write a single-part multilayer scanline EXR (ZIP compression).

    ``layers`` maps layer name to {channel: 2-D array}; channels are named by :func:`channel_name`
    (the layer ``RGBA`` writes bare ``R``, ``G``, ``B``, ``A``). ``dtypes`` maps layer name to ``'half'`` or
    ``'float'`` (default ``float``: Cryptomatte ids and depth must never be half). ``header_attrs`` are extra
    string/number header attributes (Cryptomatte ``cryptomatte/<key>/name`` entries and so on).
    """
    dtypes = dtypes or {}
    channels: dict[str, np.ndarray] = {}
    shape: tuple[int, ...] | None = None
    for layer, chans in layers.items():
        kind = dtypes.get(layer, "float")
        if kind not in ("half", "float"):
            raise ValueError(f"layer {layer!r}: dtype must be 'half' or 'float', not {kind!r}")
        np_dtype = np.float16 if kind == "half" else np.float32
        for cname, arr in chans.items():
            a = np.asarray(arr)
            if a.ndim == 3 and a.shape[2] == 1:
                a = a[..., 0]
            if a.ndim != 2:
                raise ValueError(f"{layer}.{cname}: expected a 2-D array, got {a.shape}")
            if shape is None:
                shape = a.shape
            elif a.shape != shape:
                raise ValueError(f"{layer}.{cname}: shape {a.shape} differs from {shape}")
            full = channel_name(layer, cname)
            if full in channels:
                raise ValueError(f"duplicate channel {full!r}")
            channels[full] = np.ascontiguousarray(a.astype(np_dtype, copy=False))
    if not channels:
        raise ValueError("no channels to write")
    header: dict[str, Any] = {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage}
    for key, value in (header_attrs or {}).items():
        header[str(key)] = value
    with atomic_target(path) as part:
        OpenEXR.File(header, channels).write(str(part))
