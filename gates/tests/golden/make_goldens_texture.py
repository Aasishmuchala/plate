"""Regenerate the texture goldens and freeze their metrics (run from the repo root).

    .venv/bin/python gates/tests/golden/make_goldens_texture.py

Cuts the 100% plaque crop [1080:1512, 2360:3128] (768x432) from the Vantage plate and from the GPT still of the
car-entry project (historical folders under PLATE_TEST_ROOT, default ~/Desktop/test) into
gates/tests/golden/texture/ as PNG (compression 9) and writes expected_texture.json from
plate_gates.texture.texture_stats(crop, width=None). The committed goldens are what the tests use; this script
is only needed to rebuild them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATES_DIR = HERE.parents[1]
if str(GATES_DIR) not in sys.path:
    sys.path.insert(0, str(GATES_DIR))

import numpy as np  # noqa: E402

from plate_gates.crops import load_rgb  # noqa: E402
from plate_gates.texture import texture_stats  # noqa: E402

OUT = HERE / "texture"
CROP = (2360, 1080, 3128, 1512)  # x0, y0, x1, y1 -> arr[1080:1512, 2360:3128]
SOURCES = {
    "g_tex_vantage_v1_t493_plaque.png": Path("car-entry") / "refs" / "vantage_shot1" / "V1_t493.png",
    "g_tex_gpt_s1a_plaque.png": Path("car-entry") / "stills" / "S1_GPT_a.png",
}


def main() -> int:
    root = Path(os.environ.get("PLATE_TEST_ROOT", Path.home() / "Desktop" / "test"))
    OUT.mkdir(parents=True, exist_ok=True)
    expected = {}
    for name, rel in SOURCES.items():
        src = root / rel
        if not src.is_file():
            print(f"missing source: {src}", file=sys.stderr)
            return 1
        im = load_rgb(src)
        if im.size != (3840, 2160):
            print(f"{src} is {im.size}, expected 3840x2160", file=sys.stderr)
            return 1
        crop = im.crop(CROP)
        crop.save(OUT / name, optimize=True, compress_level=9)
        stats = texture_stats(np.asarray(crop), width=None)
        expected[name] = {
            "source": rel.as_posix(),
            "crop_xyxy": list(CROP),
            **{k: round(v, 6) for k, v in stats.items()},
        }
        print(name, expected[name])
    (OUT / "expected_texture.json").write_text(
        json.dumps(expected, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
