"""How 3ds Max is colour-managed right now: OCIO, legacy gamma, or unknown (unknown blocks Send).

The structured version of MaxGaffer's spike S/#19 (scripts/onbox_spikes.py): Max 2024+ exposes the
colour pipeline as rt.ColorPipelineMgr. Its mode, OCIO config, display and view are read through
candidate property names (TODO-verify every one, ONBOX.md step 1 and docs/colour.md); the manifest
needs PACKET.md 6's colour block, and a packet whose mode cannot be established is never sent.

The OCIO config hash is a BLAKE3 hash block, computed by the daemon (bridge.hashing) over the config
file's bytes sent as a payload: the daemon's path form only hashes files inside the packets folders,
and the config lives elsewhere. A built-in config (an ocio:// URI) has no file; its versioned URI
is hashed instead and config_hash_source says so. A config file over the in-memory hash limit
(hashing.PAYLOAD_LIMIT) stops the Send here, before anything renders, with PAYLOAD_TOO_LARGE. A
daemon that rejects the token raises transport.Unauthorized; any other hashing failure leaves the
config unhashed (and the mode unknown).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import transport
from .hashing import PAYLOAD_LIMIT, PAYLOAD_TOO_LARGE, hash_payload_bytes, payload_limit
from .scene import rt_attr

MANAGER = "ColorPipelineMgr"  # TODO-verify: the Max 2024+ colour pipeline interface
MODE_NAMES = ("Mode", "mode", "ColorPipelineMode")  # TODO-verify
CONFIG_PATH_NAMES = ("OCIOConfigPath", "ocioConfigPath", "ConfigPath", "configPath")  # TODO-verify
DISPLAY_NAMES = ("displayName", "DisplayName", "defaultDisplay", "display")  # TODO-verify
VIEW_NAMES = ("viewName", "ViewName", "defaultView", "view")  # TODO-verify
LOOK_NAMES = ("lookName", "LookName", "look")  # TODO-verify
RENDERING_SPACE_NAMES = ("renderingSpace", "RenderingSpace", "workingSpace", "WorkingSpace")  # TODO-verify
GAMMA_ENABLED_NAMES = ("gammaEnabled", "GammaEnabled")  # TODO-verify
INPUT_GAMMA_NAMES = ("inputGamma", "InputGamma", "fileInGamma")  # TODO-verify
OUTPUT_GAMMA_NAMES = ("outputGamma", "OutputGamma", "fileOutGamma")  # TODO-verify
RAW_NAMES = tuple(
    dict.fromkeys(
        MODE_NAMES
        + CONFIG_PATH_NAMES
        + DISPLAY_NAMES
        + VIEW_NAMES
        + LOOK_NAMES
        + RENDERING_SPACE_NAMES
        + GAMMA_ENABLED_NAMES
        + INPUT_GAMMA_NAMES
        + OUTPUT_GAMMA_NAMES
        + ("outputConversion",)
    )
)
BUILTIN_PREFIX = "ocio://"
CONFIG_PAYLOAD_LIMIT = PAYLOAD_LIMIT  # a config file is hashed in memory: hashing.PAYLOAD_LIMIT


def _read(mgr: Any, names: tuple[str, ...]) -> tuple[str | None, Any]:
    for name in names:
        try:
            value = getattr(mgr, name)
        except Exception:
            continue
        if value is not None:
            return name, value
    return None, None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lstrip("#")
    return text or None


def _mode_of(raw_mode: Any) -> str:
    """ocio | legacy_gamma | unknown from the manager's mode value (an enum name such as
    #OCIO_Default, #OCIO_Custom, #Gamma, #Unmanaged: TODO-verify the exact names)."""
    text = (_text(raw_mode) or "").lower()
    if "ocio" in text:
        return "ocio"
    if "gamma" in text or "legacy" in text:
        return "legacy_gamma"
    return "unknown"


def _working_space(text: str | None) -> str | None:
    if not text:
        return None
    low = text.lower().replace(" ", "")
    if "acescg" in low or "ap1" in low:
        return "acescg"
    if "rec.709" in low or "rec709" in low or "srgb" in low or "linear" in low:
        return "lin_rec709"
    return None


def _config_name(path_or_uri: str) -> str:
    if path_or_uri.startswith(BUILTIN_PREFIX):
        return path_or_uri[len(BUILTIN_PREFIX) :] or path_or_uri
    stem = Path(path_or_uri.replace("\\", "/")).stem
    return stem or path_or_uri


def _config_bytes(path_or_uri: str) -> tuple[bytes | None, str | None]:
    """(bytes to hash, source): the config file's bytes, the URI's for a built-in config, else None."""
    if path_or_uri.startswith(BUILTIN_PREFIX):
        return path_or_uri.encode("utf-8"), "builtin_uri"
    try:
        path = Path(path_or_uri)
        if path.is_file() and path.stat().st_size <= _config_limit():
            return path.read_bytes(), "file"
    except OSError:
        pass
    return None, None


def _config_limit() -> int:
    return min(CONFIG_PAYLOAD_LIMIT, payload_limit())


def _oversize(path_or_uri: str) -> int | None:
    """The size of a config file too big to hash in memory, None otherwise."""
    if path_or_uri.startswith(BUILTIN_PREFIX):
        return None
    try:
        path = Path(path_or_uri)
        size = path.stat().st_size if path.is_file() else None
    except OSError:
        return None
    return size if size is not None and size > _config_limit() else None


def detect_colour_pipeline(config: dict[str, Any] | None = None, hasher: Any = None) -> dict[str, Any]:
    """{mode: ocio|legacy_gamma|unknown, reason, config_path, config_name, config_hash (a BLAKE3
    block or None), config_hash_source, display, view, look, working_space, legacy_gamma, raw: {...}}.

    config is plate_max's config.json (token, daemon_url) for the daemon hash; hasher(bytes) -> 64-hex
    replaces the daemon (tests). Without either the hash is None. mode unknown blocks Send (reason
    says why); ocio without a display, a view or a hashable config is unknown too, because the
    manifest's ocio block needs all three."""
    mgr = rt_attr(MANAGER)
    out: dict[str, Any] = {
        "mode": "unknown",
        "reason": None,
        "config_path": None,
        "config_name": None,
        "config_hash": None,
        "config_hash_source": None,
        "display": None,
        "view": None,
        "look": None,
        "working_space": None,
        "legacy_gamma": None,
        "raw": {},
    }
    if mgr is None:
        out["reason"] = f"no rt.{MANAGER}: the colour mode cannot be read on this Max (S/#19)"
        return out
    raw: dict[str, str] = {}
    for name in RAW_NAMES:
        try:
            value = getattr(mgr, name)
        except Exception:
            continue
        if value is not None:
            raw[name] = str(value)[:200]
    out["raw"] = raw

    _, mode_value = _read(mgr, MODE_NAMES)
    mode = _mode_of(mode_value)
    out["working_space"] = _working_space(_text(_read(mgr, RENDERING_SPACE_NAMES)[1]))
    if mode == "unknown":
        out["reason"] = (
            f"colour mode {(_text(mode_value) or 'unreadable')!r} is neither OCIO nor gamma: "
            "set Max to OCIO or the gamma workflow before sending"
        )
        return out

    if mode == "legacy_gamma":
        _, gamma = _read(mgr, OUTPUT_GAMMA_NAMES)
        if gamma is None:
            _, gamma = _read(mgr, INPUT_GAMMA_NAMES)
        try:
            out["legacy_gamma"] = float(gamma) if gamma is not None else None
        except (TypeError, ValueError):
            out["legacy_gamma"] = None
        if out["legacy_gamma"] is None or out["legacy_gamma"] <= 0:
            out["reason"] = "gamma workflow, but the display gamma is unreadable"
            return out
        out["mode"] = "legacy_gamma"
        if out["working_space"] is None:
            out["working_space"] = "lin_rec709"
        return out

    path_or_uri = _text(_read(mgr, CONFIG_PATH_NAMES)[1])
    out["config_path"] = path_or_uri
    out["display"] = _text(_read(mgr, DISPLAY_NAMES)[1])
    out["view"] = _text(_read(mgr, VIEW_NAMES)[1])
    out["look"] = _text(_read(mgr, LOOK_NAMES)[1])
    if path_or_uri:
        out["config_name"] = _config_name(path_or_uri)
        data, source = _config_bytes(path_or_uri)
        if data is not None:
            out["config_hash"] = _hash(data, config, hasher)
            out["config_hash_source"] = source if out["config_hash"] else None
        elif (size := _oversize(path_or_uri)) is not None:
            out["reason"] = (
                f"{PAYLOAD_TOO_LARGE}: the OCIO config {path_or_uri} is {size} bytes, over the "
                f"{_config_limit()}-byte cap for in-memory hashing, so its BLAKE3 cannot be taken"
            )
            return out
    missing = [k for k in ("config_path", "display", "view", "config_hash") if not out[k]]
    if missing:
        out["reason"] = (
            f"OCIO mode, but {', '.join(missing)} unreadable: the manifest's ocio block needs them"
        )
        return out
    out["mode"] = "ocio"
    return out


def _hash(data: bytes, config: dict[str, Any] | None, hasher: Any) -> dict[str, str] | None:
    if hasher is not None:
        hex_value = hasher(data)
    elif config is not None:
        try:
            hex_value = hash_payload_bytes({"ocio_config": data}, config)["ocio_config"]["hex"]
        except transport.Unauthorized:
            raise  # a bad token is the preflight's to report, never an "unreadable" config
        except Exception:
            return None
    else:
        return None
    if not isinstance(hex_value, str) or not re.fullmatch(r"[0-9a-f]{64}", hex_value):
        return None
    return {"algo": "blake3", "hex": hex_value}


def blocks_send(detected: dict[str, Any]) -> str | None:
    """Why this detection must stop a Send, or None when the colour mode is known."""
    if detected.get("mode") in ("ocio", "legacy_gamma"):
        return None
    return detected.get("reason") or "colour mode unknown"


def manifest_colour(
    detected: dict[str, Any],
    *,
    vfb_corrections_baked: bool,
    vfb_layers: list[str] | None = None,
    display_encoding: dict[str, str] | None = None,
    working_space: str | None = None,
) -> dict[str, Any]:
    """The manifest's colour block (PACKET.md 6) for a tier A packet from detect_colour_pipeline().
    Raises ValueError when the mode is unknown (Send must stop before this) or the working space
    cannot be told."""
    reason = blocks_send(detected)
    if reason:
        raise ValueError(reason)
    space = working_space or detected.get("working_space")
    if space not in ("lin_rec709", "acescg"):
        raise ValueError(f"working space {space!r} unknown: pass working_space='lin_rec709' or 'acescg'")
    block: dict[str, Any] = {
        "max_colour_mode": detected["mode"],
        "working_space": space,
        "master": "display_tiff16",
        "vfb_corrections_baked": bool(vfb_corrections_baked),
        "display_encoding": dict(
            display_encoding or {"primaries": "rec709", "transfer": "srgb", "icc": "embedded"}
        ),
    }
    if detected["mode"] == "ocio":
        ocio = {
            "config_name": detected["config_name"],
            "config_hash": dict(detected["config_hash"]),
            "display": detected["display"],
            "view": detected["view"],
        }
        if detected.get("look"):
            ocio["look"] = detected["look"]
        block["ocio"] = ocio
    else:
        block["legacy_gamma"] = float(detected["legacy_gamma"])
    if vfb_layers:
        block["vfb_layers"] = list(vfb_layers)
    return block
