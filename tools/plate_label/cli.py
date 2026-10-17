"""Command line for the labelling tool: terminal loop, --headless single record, --serve for the
loopback HTML UI, --stats. `python -m plate_label serve DIR` (the Makefile form) is an alias of
`--serve DIR`. New code for PLATE (no seed script); all records go through store.record_label.
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
from plate_label.codes import REASON_CODES, VERDICT_KEYS, VERDICTS
from plate_label.server import DEFAULT_PORT, serve
from plate_label.store import (
    DEFAULT_OUT,
    file_blake3,
    labelled_hashes,
    list_images,
    read_labels,
    record_label,
)

ALIASES = {"serve": "--serve", "stats": "--stats"}

HELP_TEXT = """\
  s [codes]    ship          f G01,C01   fix (needs >= 1 reason)     r G03   render it (needs >= 1 reason)
  n            add a note to this image (asked next, kept until you give a verdict)
  q            quit (resume later: labelled images are skipped)
  ?            this help and the code table"""


def split_codes(text: str) -> list[str]:
    """'g01, C01 x01' -> ['G01', 'C01', 'X01'] (upper-cased; validation happens later)."""
    return [t.upper() for t in re.split(r"[,\s]+", text.strip()) if t]


def default_rater() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def _code_table() -> str:
    return "\n".join(f"  {code}  {desc}" for code, desc in REASON_CODES.items())


def run_interactive(
    folder: Path,
    out: Path,
    rater: str,
    take_id: str | None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
) -> int:
    """Terminal loop over the images in `folder` that have no label yet. Returns a process exit code."""
    images = list_images(folder)
    done = labelled_hashes(out)
    todo = []
    for i, img in enumerate(images, start=1):
        if file_blake3(img) not in done:
            todo.append((i, img))
    print_fn(f"{len(images)} image(s) in {folder}, {len(images) - len(todo)} already labelled -> {out}")
    if not todo:
        print_fn("nothing left to label")
        return 0
    print_fn(HELP_TEXT)
    for n, img in todo:
        rel = img.relative_to(folder).as_posix()
        note = ""
        print_fn(f"{n}/{len(images)} {rel}")
        while True:
            try:
                line = input_fn("> ").strip()
            except EOFError:
                return 0
            if not line:
                continue
            cmd, _, rest = line.partition(" ")
            cmd = cmd.lower()
            if cmd == "q":
                return 0
            if cmd in {"?", "h", "help"}:
                print_fn(HELP_TEXT)
                print_fn(_code_table())
                continue
            if cmd == "n":
                try:
                    note = input_fn("note: ").strip()
                except EOFError:
                    return 0
                continue
            verdict = VERDICT_KEYS.get(cmd.upper())
            if verdict is None:
                print_fn(f"unknown command {cmd!r} (? for help)", file=sys.stderr)
                continue
            try:
                record_label(out, folder, img, verdict, split_codes(rest), note, rater, take_id)
            except ValueError as e:
                print_fn(f"not saved: {e}", file=sys.stderr)
                continue
            break
    print_fn("done")
    return 0


def run_headless(args: argparse.Namespace) -> int:
    """Write exactly one record for --headless IMAGE and return 0, or 2 with a message on stderr."""
    image = Path(args.headless)
    if not image.is_file() and args.folder and (Path(args.folder) / image).is_file():
        image = Path(args.folder) / image
    if not image.is_file():
        print(f"error: image not found: {args.headless}", file=sys.stderr)
        return 2
    if args.verdict is None:
        print("error: --headless needs --verdict ship|fix|render_it", file=sys.stderr)
        return 2
    folder = Path(args.folder) if args.folder else image.parent
    try:
        rec = record_label(
            args.out,
            folder,
            image,
            args.verdict,
            split_codes(args.reasons),
            args.note,
            args.rater,
            args.take_id,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"labelled {rec.image} -> {rec.verdict} {','.join(rec.reasons)}".rstrip())
    return 0


def print_stats(out: Path, print_fn: Callable[..., None] = print) -> int:
    """Counts per verdict and per reason code in the labels file."""
    labels = read_labels(out)
    verdicts = Counter(r.get("verdict") for r in labels)
    reasons = Counter(c for r in labels for c in r.get("reasons", []))
    print_fn(f"{len(labels)} label(s) in {out}")
    print_fn("verdicts")
    for v in VERDICTS:
        print_fn(f"  {v:<10}{verdicts.get(v, 0)}")
    print_fn("reason codes")
    for code, desc in REASON_CODES.items():
        print_fn(f"  {code}  {reasons.get(code, 0):>4}  {desc}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m plate_label",
        description="Label images Ship / Fix / Render-it with reason codes. Writes calib/labels.jsonl "
        "(also: `serve FOLDER` = --serve, `stats` = --stats).",
    )
    p.add_argument("folder", nargs="?", help="folder of images to label")
    p.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="labels file (default: <repo>/calib/labels.jsonl)"
    )
    p.add_argument("--rater", default=default_rater(), help="rater name (default: $USER)")
    p.add_argument("--take-id", default=None, help="optional take id stored on every record")
    p.add_argument(
        "--headless", metavar="IMAGE", help="write one record for IMAGE and exit (needs --verdict)"
    )
    p.add_argument("--verdict", choices=VERDICTS, help="verdict for --headless")
    p.add_argument("--reasons", default="", help="comma-separated reason codes for --headless, e.g. G01,C01")
    p.add_argument("--note", default="", help="note for --headless")
    p.add_argument(
        "--serve", action="store_true", help=f"start the loopback HTML UI (127.0.0.1:{DEFAULT_PORT})"
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="port for --serve (0 = any free port)")
    p.add_argument("--no-open", action="store_true", help="with --serve: do not open a browser tab")
    p.add_argument("--stats", action="store_true", help="print counts per verdict and reason code from --out")
    p.add_argument("--version", action="version", version=f"plate_label {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ALIASES:
        argv[0] = ALIASES[argv[0]]
    args = build_parser().parse_args(argv)

    if args.stats:
        return print_stats(args.out)
    if args.headless:
        return run_headless(args)
    if not args.folder:
        print("error: FOLDER is required (a folder of images to label)", file=sys.stderr)
        return 2
    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"error: not a folder: {folder}", file=sys.stderr)
        return 2
    folder = folder.resolve()
    try:
        if args.serve:
            return serve(folder, args.out, args.rater, args.take_id, args.port, open_browser=not args.no_open)
        return run_interactive(folder, args.out, args.rater, args.take_id)
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 0
