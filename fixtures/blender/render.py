"""Driver behind render.sh: render every fixture variant in Blender, then repack into fixtures/packets.

    .venv/bin/python fixtures/blender/render.py [--work DIR] [--packets DIR] [--force] [--skip-render]

Idempotent: a variant is re-rendered only when its stamp (sha256 of scene.py + markers.json, written by
scene.py) no longer matches or a shot is missing; the repack always runs (seconds). The work folder is
fixtures/blender/_work (gitignored). Blender is found through $BLENDER, then the usual install folders on macOS
and Windows, then PATH. Pure pathlib + argument lists, so it runs the same on the Mac and on the Windows box.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
VARIANTS = {  # variant -> shots it renders (must match scene.SHOTS)
    "base": ["room_a", "room_b", "dolly_hero", "dolly_12f"],
    "ral3020": ["room_a_ral3020"],
    "v2": ["room_a_v2"],
}


def stamp() -> str:
    h = hashlib.sha256()
    h.update((HERE / "scene.py").read_bytes())
    h.update((HERE / "markers.json").read_bytes())
    return h.hexdigest()


def find_blender() -> Path:
    env = os.environ.get("BLENDER")
    if env and Path(env).is_file():
        return Path(env)
    candidates = [Path("/Applications/Blender.app/Contents/MacOS/Blender")]
    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if root:
            candidates += sorted(
                (Path(root) / "Blender Foundation").glob("Blender */blender.exe"), reverse=True
            )
    for c in candidates:
        if c.is_file():
            return c
    found = shutil.which("blender")
    if found:
        return Path(found)
    raise SystemExit("Blender not found: set BLENDER to the executable (fixtures use Blender 5.1)")


def variant_current(work: Path, variant: str) -> bool:
    vdir = work / variant
    st = vdir / "stamp.txt"
    if not st.is_file() or st.read_text(encoding="utf-8").strip() != stamp():
        return False
    return all((vdir / shot / "info.json").is_file() for shot in VARIANTS[variant])


def render_variant(blender: Path, work: Path, variant: str) -> None:
    cmd = [
        str(blender),
        "-b",
        "--factory-startup",
        "-P",
        str(HERE / "scene.py"),
        "--",
        str(work),
        "--variant",
        variant,
    ]
    print("[plate-fixture] blender", variant, flush=True)
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", type=Path, default=HERE / "_work")
    ap.add_argument("--packets", type=Path, default=REPO / "fixtures" / "packets")
    ap.add_argument("--force", action="store_true", help="re-render even when the stamp matches")
    ap.add_argument("--skip-render", action="store_true", help="repack the existing _work renders only")
    args = ap.parse_args()
    if not args.skip_render:
        blender = None
        for variant in VARIANTS:
            if not args.force and variant_current(args.work, variant):
                print(f"[plate-fixture] {variant}: up to date", flush=True)
                continue
            blender = blender or find_blender()
            render_variant(blender, args.work, variant)
    subprocess.run(
        [sys.executable, str(HERE / "build_packets.py"), str(args.work), str(args.packets)],
        check=True,
    )


if __name__ == "__main__":
    main()
