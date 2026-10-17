"""Command line for the gate core: `python -m plate_gates {selftest|pair|idmask|maskdiff|views}`.

Same subcommands and arguments as ~/Desktop/test/vizmaker-dossier/tools/fidelity_bench.py. Pass --report on
pair / maskdiff / views to wrap the JSON in the versioned gate report (report.GateReport via gate_report).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

from . import report as report_mod
from .color import hex_to_rgb8, srgb_to_lab
from .compare import compare, idmask, load, maskdiff, read_image, views, write_png
from .selftest import run_selftest
from .thresholds import DEFAULT_THRESHOLDS_ID


def _add_report_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--report", action="store_true", help="wrap the output in the versioned gate report")
    p.add_argument("--thresholds-id", default=DEFAULT_THRESHOLDS_ID)
    p.add_argument("--engine", default=None, help="engine id recorded in the report")
    p.add_argument("--engine-version", default=None)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="plate_gates", description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    q = sub.add_parser("idmask")
    q.add_argument("idpass")
    q.add_argument("r", type=int)
    q.add_argument("g", type=int)
    q.add_argument("b", type=int)
    q.add_argument("out")
    q.add_argument("--tol", type=int, default=0)
    q2 = sub.add_parser("maskdiff")
    q2.add_argument("ref")
    q2.add_argument("test")
    q2.add_argument("mask")
    _add_report_args(q2)
    q3 = sub.add_parser("views")
    q3.add_argument("manifest")
    q3.add_argument("--out")
    _add_report_args(q3)
    p = sub.add_parser("pair")
    p.add_argument("ref")
    p.add_argument("test")
    p.add_argument("--mask")
    p.add_argument("--ral-lab", nargs=3, type=float)
    p.add_argument("--ral-hex")
    p.add_argument("--out")
    p.add_argument("--width", type=int, default=1500)
    _add_report_args(p)
    return ap


def _emit(res: dict, args: argparse.Namespace) -> None:
    if args.report:
        res = report_mod.gate_report(res, args.thresholds_id, args.engine_version, args.engine)
    text = json.dumps(res, indent=1)
    out = getattr(args, "out", None)
    if out:
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
    print(text)


def main(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    a = ap.parse_args(argv)
    if a.cmd is None:
        ap.print_help()
        return 0
    if a.cmd == "selftest":
        return 0 if run_selftest(verbose=True) else 1
    try:
        if a.cmd == "idmask":
            m = idmask(a.idpass, (a.r, a.g, a.b), a.tol)
            write_png(a.out, m)
            print("mask px", int((m > 0).sum()))
        elif a.cmd == "maskdiff":
            res = maskdiff(read_image(a.ref), read_image(a.test), read_image(a.mask, cv2.IMREAD_GRAYSCALE))
            _emit(res, a)
        elif a.cmd == "views":
            _emit(views(Path(a.manifest)), a)
        elif a.cmd == "pair":
            ref = load(a.ref, a.width)
            test = read_image(a.test)
            mask = read_image(a.mask, cv2.IMREAD_GRAYSCALE) if a.mask else None
            lab = a.ral_lab or (list(srgb_to_lab(hex_to_rgb8(a.ral_hex))[0]) if a.ral_hex else None)
            _emit(compare(ref, test, mask, lab), a)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0
