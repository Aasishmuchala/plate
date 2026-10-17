"""Command-line wrappers for the motion, texture, alignment, crop and composite gates (prints JSON).

    python -m plate_gates.tools_cli {lock|pushin|texture|align|crops|composite|motion} ...

Thin argparse shells over video/motion/texture/align/crops/composite/sheets; they replace the standalone
scripts clip_gate.py, pushin_gate.py, align_check.py, cut_crops.py, crops.py and tools_composite_region.py
from the historical project folders. Every command prints one JSON document on stdout and exits 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from . import align, composite, crops, motion, sheets, texture
from .crops import load_rgb


def _box_xywh(text: str) -> tuple[int, int, int, int]:
    x, y, w, h = (int(v) for v in text.split(","))
    return x, y, w, h


def _named_box(text: str) -> tuple[str, tuple[int, int, int, int]]:
    name, _, box = text.partition("=")
    if not box:
        raise argparse.ArgumentTypeError(f"expected NAME=x,y,w,h, got {text!r}")
    return name, _box_xywh(box)


def _rgb(path: str) -> np.ndarray:
    return np.asarray(load_rgb(path))


def _emit(payload: dict) -> int:
    sys.stdout.write(json.dumps(payload, indent=1) + "\n")
    return 0


def _cmd_lock(a: argparse.Namespace) -> int:
    res = motion.camera_lock(Path(a.clip), a.regions, t0=a.t0, t1=a.t1, tol_px=a.tol)
    return _emit(res)


def _cmd_pushin(a: argparse.Namespace) -> int:
    res = sheets.pushin_gate(Path(a.clip), a.start, a.end, Path(a.sheet) if a.sheet else None)
    return _emit(res)


def _cmd_texture(a: argparse.Namespace) -> int:
    stats = texture.texture_stats(_rgb(a.image), width=None if a.width <= 0 else a.width)
    return _emit({"stats": stats, "band": texture.texture_band(stats)})


def _cmd_align(a: argparse.Namespace) -> int:
    agree, overlay = align.edge_agreement(
        _rgb(a.plate), _rgb(a.relit), width=None if a.width <= 0 else a.width
    )
    if a.overlay:
        Image.fromarray(overlay).save(a.overlay, quality=85)
    return _emit({"edge_agreement": agree, "overlay": a.overlay})


def _cmd_crops(a: argparse.Namespace) -> int:
    if a.mode == "pair":
        img = load_rgb(a.image)
        (s, sbox), (e, ebox) = crops.crop_pair(img, a.cx, a.cy, a.zoom_start, a.zoom_end)
        out = Path(a.out_prefix)
        s.save(out.with_name(out.name + "_start.png"))
        e.save(out.with_name(out.name + "_end.png"))
        return _emit({"start_box": sbox, "end_box": ebox, "size": [s.width, s.height]})
    images = [load_rgb(p) for p in a.files]
    labels = [Path(p).name for p in a.files]
    if a.mode == "tiles":
        boxes = [tuple(float(v) for v in b.split(",")) for b in a.boxes.split(";")]
        sheet = crops.crop_tiles(images, boxes, labels, tile_w=a.tile_w)
    else:
        named = dict(_named_box_x0y0x1y1(t) for t in a.boxes.split(";"))
        ref = tuple(int(v) for v in a.ref_size.lower().split("x"))
        sheet = crops.region_tiles(images, named, ref_size=ref, tile_w=a.tile_w, labels=labels)
    sheet.save(a.out, quality=88)
    return _emit({"out": a.out, "size": [sheet.width, sheet.height]})


def _named_box_x0y0x1y1(text: str) -> tuple[str, tuple[int, int, int, int]]:
    name, _, box = text.partition("=")
    if not box:
        raise ValueError(f"expected NAME=x0,y0,x1,y1, got {text!r}")
    x0, y0, x1, y1 = (int(v) for v in box.split(","))
    return name, (x0, y0, x1, y1)


def _cmd_composite(a: argparse.Namespace) -> int:
    res = composite.composite_region(_rgb(a.base), _rgb(a.patch), (a.x0, a.y0, a.x1, a.y1), a.feather)
    Image.fromarray(res).save(a.out)
    diff = float(np.abs(_rgb(a.base).astype(np.float32) - res.astype(np.float32)).mean())
    return _emit({"out": a.out, "size": [res.shape[1], res.shape[0]], "mean_abs_diff_vs_base_255": diff})


def _cmd_motion(a: argparse.Namespace) -> int:
    res = motion.region_motion(Path(a.clip), dict(a.region), a.times)
    return _emit({"times": a.times, "motion": res})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="plate_gates.tools_cli", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("lock", help="camera-lock check on static regions of a clip")
    s.add_argument("clip")
    s.add_argument("regions", nargs="+", type=_box_xywh, help="x,y,w,h in native clip pixels")
    s.add_argument("--t0", type=float, default=0.0)
    s.add_argument("--t1", type=float, default=None)
    s.add_argument("--tol", type=int, default=2)
    s.set_defaults(func=_cmd_lock)

    s = sub.add_parser("pushin", help="landing gate for a start+end-frame push-in clip")
    s.add_argument("clip")
    s.add_argument("start")
    s.add_argument("end")
    s.add_argument("--sheet", default=None)
    s.set_defaults(func=_cmd_pushin)

    s = sub.add_parser("texture", help="lapvar / fine-detail stats and band check for a still")
    s.add_argument("image")
    s.add_argument("--width", type=int, default=3840, help="evaluation width; 0 keeps native size")
    s.set_defaults(func=_cmd_texture)

    s = sub.add_parser("align", help="edge agreement of a relit still against the plate")
    s.add_argument("plate")
    s.add_argument("relit")
    s.add_argument("--overlay", default=None, help="write the red/cyan overlay here")
    s.add_argument("--width", type=int, default=1920, help="evaluation width; 0 keeps native size")
    s.set_defaults(func=_cmd_align)

    s = sub.add_parser("crops", help="push-in crop pair, fractional-box tiles or region tiles")
    cs = s.add_subparsers(dest="mode", required=True)
    c = cs.add_parser("pair")
    c.add_argument("image")
    c.add_argument("out_prefix", help="writes <prefix>_start.png and <prefix>_end.png")
    c.add_argument("cx", type=float)
    c.add_argument("cy", type=float)
    c.add_argument("zoom_start", type=float)
    c.add_argument("zoom_end", type=float)
    c = cs.add_parser("tiles")
    c.add_argument("out")
    c.add_argument("boxes", help='"x0,y0,x1,y1;..." as 0-1 fractions')
    c.add_argument("files", nargs="+")
    c.add_argument("--tile-w", type=int, default=900)
    c = cs.add_parser("regions")
    c.add_argument("out")
    c.add_argument("boxes", help='"name=x0,y0,x1,y1;..." in --ref-size coordinates')
    c.add_argument("files", nargs="+")
    c.add_argument("--ref-size", default="3840x2160")
    c.add_argument("--tile-w", type=int, default=760)
    s.set_defaults(func=_cmd_crops)

    s = sub.add_parser("composite", help="feathered region paste of PATCH into BASE")
    s.add_argument("base")
    s.add_argument("patch")
    s.add_argument("out")
    for k in ("x0", "y0", "x1", "y1"):
        s.add_argument(k, type=int)
    s.add_argument("--feather", type=int, default=60)
    s.set_defaults(func=_cmd_composite)

    s = sub.add_parser("motion", help="mean abs luma diff per named region over time")
    s.add_argument("clip")
    s.add_argument("--region", action="append", required=True, type=_named_box, help="NAME=x,y,w,h")
    s.add_argument("--times", type=float, nargs="+", required=True)
    s.set_defaults(func=_cmd_motion)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
