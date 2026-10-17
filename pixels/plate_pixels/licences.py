"""Commercial-safe licence registry for every model the pixels worker may ever run, and the CI check.

``REGISTRY`` is the single list of models PLATE may touch: allowed entries are the only ones a backend can be
constructed for, blocked entries stay in the list so CI can prove they are refused. ``assert_commercial_safe``
runs in every backend's ``__init__``, so a non-commercial model can never be loaded by accident.

``python -m plate_pixels.licences --check`` is the CI half: it parses every backend module under
``pixels/plate_pixels`` (and the non-comment lines of ``pixels/requirements-cuda.txt``) and exits 1 when an
import or a string literal names a model whose ``commercial_ok`` is false. This file is exempt (it must name
them) and so are the tests (they name them to prove the refusal). Matching ignores case and punctuation, so
``unidepth``, ``UniDepth`` and ``uni-depth`` are one name; short names (``rvm``) must stand alone as a token.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


class LicenceError(Exception):
    """Raised when a model name is unknown or its licence is not commercial-safe."""


@dataclass(frozen=True)
class ModelLicence:
    name: str
    version: str
    weights_licence: str
    code_licence: str
    commercial_ok: bool
    note: str = ""
    confirm_licence_file: bool = (
        False  # read the licence file shipped in the weights repo before an NDA claim
    )
    weights_source: str = ""
    aliases: tuple[str, ...] = ()  # other spellings the CI check must also catch


def _ok(
    name: str,
    version: str,
    weights: str,
    code: str,
    source: str,
    note: str = "",
    confirm: bool = False,
    aliases: tuple[str, ...] = (),
) -> ModelLicence:
    return ModelLicence(name, version, weights, code, True, note, confirm, source, aliases)


def _blocked(
    name: str,
    version: str,
    weights: str,
    code: str,
    source: str,
    note: str,
    aliases: tuple[str, ...] = (),
) -> ModelLicence:
    return ModelLicence(name, version, weights, code, False, note, False, source, aliases)


REGISTRY: list[ModelLicence] = [
    # --- allowed (commercial_ok=True) ---
    _ok(
        "depth-anything-v2-small",
        "2024-06",
        "Apache-2.0",
        "Apache-2.0",
        "DepthAnything/Depth-Anything-V2-Small (Hugging Face)",
        "only the Small checkpoint is Apache-2.0; Base and Large are non-commercial",
        aliases=("depth_anything_v2_small",),
    ),
    _ok(
        "geocalib",
        "pinhole-2024",
        "CC-BY-4.0",
        "Apache-2.0",
        "cvg/GeoCalib (GitHub release weights)",
        "weights are CC-BY-4.0: keep the attribution; verify the weights licence before NDA use",
    ),
    _ok("sam2.1", "2.1", "Apache-2.0", "Apache-2.0", "facebook/sam2.1-hiera-* (Hugging Face)"),
    _ok(
        "raft",
        "raft_large",
        "BSD-3-Clause",
        "BSD-3-Clause",
        "torchvision.models.optical_flow (RAFT weights)",
    ),
    _ok(
        "opencv-dis",
        "opencv-5",
        "none",
        "Apache-2.0",
        "opencv-python-headless (DISOpticalFlow)",
        "the Mac fallback for RAFT: real on every machine, no weights",
        aliases=("opencv_dis",),
    ),
    _ok(
        "paddleocr",
        "pp-ocr",
        "Apache-2.0",
        "Apache-2.0",
        "PaddlePaddle/PaddleOCR (official model downloads)",
        "signage text-lock",
    ),
    _ok(
        "real-esrgan",
        "x4plus",
        "BSD-3-Clause",
        "BSD-3-Clause",
        "xinntao/Real-ESRGAN (GitHub release weights)",
        "AI layers only; never the plate",
        aliases=("real_esrgan", "realesrgan"),
    ),
    _ok(
        "lpips",
        "0.1",
        "BSD-2-Clause",
        "BSD-2-Clause",
        "richzhang/PerceptualSimilarity (bundled linear layers)",
    ),
    _ok(
        "open-clip",
        "per-checkpoint",
        "per-checkpoint",
        "MIT",
        "mlfoundations/open_clip (checkpoint chosen per use)",
        "code is MIT; confirm the licence of the chosen pretrained checkpoint",
        aliases=("open_clip", "openclip"),
    ),
    _ok(
        "qwen-image-edit-2509",
        "2509",
        "Apache-2.0",
        "Apache-2.0",
        "Qwen/Qwen-Image-Edit-2509 (Hugging Face)",
        "scheduled GPU windows only; confirm the licence file in the weights repo before any NDA claim",
        confirm=True,
        aliases=("qwen_image_edit_2509",),
    ),
    _ok(
        "wan2.2",
        "2.2",
        "Apache-2.0",
        "Apache-2.0",
        "Wan-AI/Wan2.2 VACE (Hugging Face)",
        "VACE previs only, scheduled GPU windows",
        aliases=("wan2.2-vace", "wan2.2_vace"),
    ),
    # --- blocked (commercial_ok=False) ---
    _blocked(
        "depth-anything-v2-base",
        "2024-06",
        "CC-BY-NC-4.0",
        "Apache-2.0",
        "DepthAnything/Depth-Anything-V2-Base (Hugging Face)",
        "non-commercial weights; use the Small checkpoint",
        aliases=("depth_anything_v2_base", "vitb"),
    ),
    _blocked(
        "depth-anything-v2-large",
        "2024-06",
        "CC-BY-NC-4.0",
        "Apache-2.0",
        "DepthAnything/Depth-Anything-V2-Large (Hugging Face)",
        "non-commercial weights; use the Small checkpoint",
        aliases=("depth_anything_v2_large", "vitl"),
    ),
    _blocked(
        "unidepth",
        "v2",
        "CC-BY-NC-4.0",
        "CC-BY-NC-4.0",
        "lpiccinelli-eth/UniDepth (GitHub, Hugging Face)",
        "non-commercial weights",
    ),
    _blocked(
        "superpoint",
        "v1",
        "Magic Leap research licence (non-commercial)",
        "Magic Leap research licence (non-commercial)",
        "magicleap/SuperPointPretrainedNetwork (GitHub)",
        "research-only licence",
    ),
    _blocked(
        "flux-dev",
        "1",
        "FLUX.1 [dev] Non-Commercial License",
        "Apache-2.0",
        "black-forest-labs/FLUX.1-dev (Hugging Face)",
        "non-commercial weights; outputs feed paid client work",
        aliases=("flux1_dev", "flux.1-dev", "flux1-dev"),
    ),
    _blocked(
        "matanyone",
        "1",
        "S-Lab License 1.0 (non-commercial)",
        "S-Lab License 1.0 (non-commercial)",
        "pq-yang/MatAnyone (GitHub, Hugging Face)",
        "off until cleared in writing",
    ),
    _blocked(
        "rvm",
        "1",
        "GPL-3.0",
        "GPL-3.0",
        "PeterL1n/RobustVideoMatting (GitHub)",
        "GPL: isolated in STAFFAGE only, never imported here",
        aliases=("robust_video_matting", "robustvideomatting"),
    ),
]

BY_NAME: dict[str, ModelLicence] = {entry.name: entry for entry in REGISTRY}
if len(BY_NAME) != len(REGISTRY):  # a duplicate would silently shadow a licence
    raise RuntimeError("licences.REGISTRY has a duplicate name")


def canonical(name: str) -> str:
    """Registry key for a spelling: lower case, ``_`` and spaces read as ``-`` (``real_esrgan`` -> ``real-esrgan``)."""
    return re.sub(r"[\s_]+", "-", name.strip().lower())


def _lookup(name: str) -> ModelLicence | None:
    key = canonical(name)
    entry = BY_NAME.get(key)
    if entry is not None:
        return entry
    for item in REGISTRY:
        if key in {canonical(a) for a in item.aliases}:
            return item
    return None


def get(name: str) -> ModelLicence | None:
    """The registry entry for a name or alias (blocked entries included), or ``None``."""
    return _lookup(name)


def licence_summary(entry: ModelLicence) -> str:
    """One licence string for a manifest's ``estimated`` block: ``Apache-2.0`` or ``X (code); Y (weights)``."""
    if entry.code_licence == entry.weights_licence:
        return entry.weights_licence
    return f"{entry.code_licence} (code); {entry.weights_licence} (weights)"


def assert_commercial_safe(name: str) -> ModelLicence:
    """Return the registry entry, or raise ``LicenceError`` for an unknown or non-commercial model."""
    entry = _lookup(name)
    if entry is None:
        raise LicenceError(
            f"{name}: unregistered is not commercial-safe - "
            "add it to the licence registry with a reviewed licence first"
        )
    if not entry.commercial_ok:
        raise LicenceError(f"{entry.name}: {entry.weights_licence} is not commercial-safe - {entry.note}")
    return entry


def commercial_models() -> list[str]:
    """Names of every model a backend may be constructed for, in registry order."""
    return [entry.name for entry in REGISTRY if entry.commercial_ok]


def blocked_models() -> list[str]:
    return [entry.name for entry in REGISTRY if not entry.commercial_ok]


# --------------------------------------------------------------------------- the CI check


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    model: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: names blocked model {self.model!r} in {self.text!r}"


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _tokens(text: str) -> str:
    return "-" + re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") + "-"


def _blocked_spellings() -> list[tuple[str, str]]:
    """(model name, spelling) for every blocked model: its name plus aliases."""
    return [(e.name, s) for e in REGISTRY if not e.commercial_ok for s in (e.name, *e.aliases)]


def names_blocked_model(text: str) -> str | None:
    """The blocked model ``text`` names, or ``None``. Spellings of 6+ characters match anywhere once
    punctuation is dropped; shorter ones (``rvm``) must be a whole token so ordinary words never trip."""
    compact, tokens = _compact(text), _tokens(text)
    for model, spelling in _blocked_spellings():
        needle = _compact(spelling)
        if len(needle) >= 6:
            if needle in compact:
                return model
        elif f"-{needle}-" in tokens:
            return model
    return None


def _literals(tree: ast.AST) -> Iterable[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, node.module or ""
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def scan_source(source: str, filename: str = "<source>") -> list[Violation]:
    found = []
    for line, text in _literals(ast.parse(source, filename=filename)):
        model = names_blocked_model(text)
        if model:
            found.append(Violation(filename, line, model, text.strip()[:60]))
    return found


def package_root() -> Path:
    return Path(__file__).resolve().parent


def scan(root: Path | None = None) -> tuple[list[Violation], int]:
    """Scan ``root`` (default: this package) for blocked names. Returns (violations, files scanned)."""
    root = Path(root) if root is not None else package_root()
    found: list[Violation] = []
    count = 0
    for path in sorted(root.rglob("*.py")):
        if path.name == "licences.py" or "__pycache__" in path.parts:
            continue
        count += 1
        rel = path.relative_to(root).as_posix()
        found += scan_source(path.read_text(encoding="utf-8"), rel)
    requirements = root.parent / "requirements-cuda.txt"
    if requirements.is_file():
        count += 1
        for no, raw in enumerate(requirements.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            model = names_blocked_model(line) if line else None
            if model:
                found.append(Violation(requirements.name, no, model, line[:60]))
    return found, count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m plate_pixels.licences", description="PLATE model licence registry"
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if a backend names a blocked model")
    parser.add_argument(
        "--root", type=Path, default=None, help="package folder to scan (default: this package)"
    )
    parser.add_argument("--list", action="store_true", help="print the registry")
    args = parser.parse_args(argv)
    if args.list or not args.check:
        for entry in REGISTRY:
            flag = "ok " if entry.commercial_ok else "OFF"
            print(f"{flag} {entry.name:<26} weights: {entry.weights_licence} | code: {entry.code_licence}")
        if not args.check:
            return 0
    violations, count = scan(args.root)
    if violations:
        for item in violations:
            print(f"FAIL {item}", file=sys.stderr)
        print(f"licence check FAILED: {len(violations)} blocked-model reference(s)", file=sys.stderr)
        return 1
    print(f"licence check OK: {count} files scanned, {len(blocked_models())} blocked models absent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
