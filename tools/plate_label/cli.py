"""Command line for the labelling tool: serve the loopback HTML UI over a --pairs manifest or a folder,
print label stats, or write one record from the shell (--headless). `python -m plate_label serve DIR`
(the Makefile form) is the same as `python -m plate_label DIR`. New code for PLATE (no seed script);
every record goes through store.LabelRecord, whichever front end wrote it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from plate_label import __version__
from plate_label.codes import VERDICTS, reason_codes
from plate_label.pairs import PairsError, load_manifest, pair_from_entry, scan_folder
from plate_label.server import DEFAULT_PORT, serve
from plate_label.store import LabelRecord, append_label, default_labels_path, read_labels

# First-word shorthands: `serve DIR` is the default action, `stats` is --stats.
ALIASES = {"serve": [], "stats": ["--stats"]}


def split_codes(text: str) -> list[str]:
    """'a2, p4 f11' -> ['A2', 'P4', 'F11'] (upper-cased; validation happens when the record is built)."""
    return [t.upper() for t in re.split(r"[,\s]+", text.strip()) if t]


def default_rater() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def run_headless(args: argparse.Namespace) -> int:
    """Write exactly one record from the command line and return 0, or 2 with a message on stderr."""
    if not args.take_id or not args.candidate or not args.verdict:
        print("error: --headless needs --take-id, --candidate and --verdict", file=sys.stderr)
        return 2
    entry = {
        "take_id": args.take_id,
        "candidate": args.candidate,
        "plate": args.plate,
        "run_id": args.run_id,
        "gate_report": args.gate_report,
    }
    try:
        pair = pair_from_entry(entry, Path.cwd(), "--headless")
        rec = LabelRecord.for_pair(pair, args.verdict, split_codes(args.reasons), args.seconds, args.rater)
    except ValueError as e:  # PairsError is a ValueError
        print(f"error: {e}", file=sys.stderr)
        return 2
    append_label(args.out, rec)
    print(f"labelled {rec.take_id} {rec.candidate} -> {rec.verdict} {','.join(rec.reasons)}".rstrip())
    return 0


def print_stats(out: Path, print_fn: Callable[..., None] = print) -> int:
    """Counts per rater, per verdict and per reason code in the labels file."""
    labels = read_labels(out)
    raters = Counter(r.get("rater") for r in labels)
    verdicts = Counter(r.get("verdict") for r in labels)
    reasons = Counter(c for r in labels for c in r.get("reasons", []))
    secs = [r["seconds"] for r in labels if isinstance(r.get("seconds"), (int, float))]
    print_fn(f"{len(labels)} label(s) in {out}")
    print_fn("raters")
    for name, n in sorted(raters.items(), key=lambda kv: str(kv[0])):
        print_fn(f"  {name}  {n}")
    print_fn("verdicts")
    for v in VERDICTS:
        print_fn(f"  {v:<10}{verdicts.get(v, 0)}")
    print_fn("reason codes")
    for code, desc in reason_codes().items():
        print_fn(f"  {code:<4}{reasons.get(code, 0):>4}  {desc}")
    if secs:
        print_fn(f"median time per label: {sorted(secs)[len(secs) // 2]:.1f} s")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m plate_label",
        description="Three-key labelling of plate-vs-candidate pairs: 1 Ship, 2 Fix, 3 Render-it, then "
        "reason codes. Serves a page on 127.0.0.1 and appends one JSON line per verdict to "
        "labels.jsonl in the data folder (see --out). Give FOLDER or --pairs FILE. "
        "(`serve FOLDER` = FOLDER, `stats` = --stats.)",
    )
    p.add_argument(
        "folder",
        nargs="?",
        help="scan a folder: <take>/plate.png + <take>/candidate*.png, <take>.plate.png + "
        "<take>.candidate*.png; a folder with neither is labelled image by image (no plate)",
    )
    p.add_argument(
        "--pairs",
        type=Path,
        metavar="FILE",
        help="JSON list of {take_id, plate, candidate, gate_report, run_id?}; relative paths are "
        "relative to the file",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="labels file (default: $PLATE_LABELS, else <data home>/calib/labels.jsonl; the data home is "
        "$PLATE_HOME or the OS app-data folder, the file the daemon writes canvas verdicts to)",
    )
    p.add_argument("--rater", default=default_rater(), help="rater name (default: $USER)")
    p.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT}; 0 = any free port)"
    )
    p.add_argument("--no-open", action="store_true", help="do not open a browser tab")
    p.add_argument("--stats", action="store_true", help="print counts per rater, verdict and reason code")
    p.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)  # old spelling, now the default
    hl = p.add_argument_group("--headless: write one record without the UI")
    hl.add_argument("--headless", action="store_true", help="write one record from these flags and exit")
    hl.add_argument("--take-id", default=None)
    hl.add_argument("--candidate", default=None, help="candidate image path")
    hl.add_argument("--plate", default=None, help="plate image path (optional)")
    hl.add_argument("--verdict", choices=VERDICTS)
    hl.add_argument("--reasons", default="", help="comma-separated reason codes, e.g. P1,A2")
    hl.add_argument("--seconds", type=float, default=0.0, help="time spent on the take")
    hl.add_argument("--run-id", default=None)
    hl.add_argument("--gate-report", default=None, help="path of the gate report that scored the candidate")
    p.add_argument("--version", action="version", version=f"plate_label {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ALIASES:
        argv[0:1] = ALIASES[argv[0]]
    args = build_parser().parse_args(argv)
    if args.out is None:
        args.out = default_labels_path()

    if args.stats:
        return print_stats(args.out)
    if args.headless:
        return run_headless(args)
    if args.pairs and args.folder:
        print("error: give a FOLDER or --pairs FILE, not both", file=sys.stderr)
        return 2
    try:
        if args.pairs:
            pairs = load_manifest(args.pairs)
        elif args.folder:
            if not Path(args.folder).is_dir():
                print(f"error: not a folder: {args.folder}", file=sys.stderr)
                return 2
            pairs = scan_folder(Path(args.folder))
        else:
            print("error: give a FOLDER of images or --pairs FILE", file=sys.stderr)
            return 2
    except PairsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        return serve(pairs, args.out, args.rater, args.port, open_browser=not args.no_open)
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 0
