"""`python -m plate_daemon.tools <command>`: the maintenance tools without a running daemon.

    backup <project> <dest> [--cmd CMD]        copy the project to <dest>; run CMD (else PLATE_BACKUP_CMD) after
    drill <backup> --tmp DIR [--no-restore] [--keep] [--report FILE]
                                               the quarterly restore drill
    purge <project> [--apply] [--older-than-days N] [--no-keep-ledger] [--no-keep-masters]
                    [--no-keep-gate-reports]   dry run unless --apply; writes purge_<ts>.json when applied
    reindex <project> [--home DIR] [--verify]  rebuild the SQLite index and check it against the folder
    alerts <project> [--write]                 scan the ledger for budget alerts (--write appends alerts.jsonl)
    vendor-deletions <project>                 runs whose vendor-side files are not yet recorded as deleted
    vendor-deleted <project> --run ID --by NAME [--job ID] [--provider P] [--method manual|api]
                                               record one vendor-side deletion in the ledger

Add `--json` after the command for the full report. Exit codes: 0 ok, 2 a check failed (drill, reindex consistency, backup
command), 3 refused (project locked, broken ledger, bad folder), 4 usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config import UnsafeDatabaseLocation
from ..ledger import Ledger
from ..lock import ProjectLocked
from ..projects import ProjectError

EXIT_OK, EXIT_CHECK, EXIT_REFUSED, EXIT_USAGE = 0, 2, 3, 4


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="python -m plate_daemon.tools", description=__doc__.split("\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print the full report as JSON")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backup", parents=[common])
    b.add_argument("project", type=Path)
    b.add_argument("dest", type=Path)
    b.add_argument("--cmd", help="command to run after the copy; {src} and {dest} are replaced")
    b.add_argument("--timeout-s", type=float, default=None)

    d = sub.add_parser("drill", parents=[common])
    d.add_argument("backup", type=Path)
    d.add_argument("--tmp", type=Path, required=True, help="scratch folder for the restored copy")
    d.add_argument("--no-restore", action="store_true", help="hash in place; do not copy")
    d.add_argument("--keep", action="store_true", help="keep the restored copy")
    d.add_argument("--report", type=Path, help="also write the report JSON here")

    g = sub.add_parser("purge", parents=[common])
    g.add_argument("project", type=Path)
    g.add_argument("--apply", action="store_true", help="delete (default is a dry run)")
    g.add_argument("--older-than-days", type=float, default=30.0)
    g.add_argument("--no-keep-ledger", action="store_true")
    g.add_argument("--no-keep-masters", action="store_true")
    g.add_argument("--no-keep-gate-reports", action="store_true")

    r = sub.add_parser("reindex", parents=[common])
    r.add_argument("project", type=Path)
    r.add_argument("--home", type=Path)
    r.add_argument("--verify", action="store_true", help="re-hash every blob")

    a = sub.add_parser("alerts", parents=[common])
    a.add_argument("project", type=Path)
    a.add_argument("--write", action="store_true", help="append new alerts to alerts.jsonl")

    v = sub.add_parser("vendor-deletions", parents=[common])
    v.add_argument("project", type=Path)

    m = sub.add_parser("vendor-deleted", parents=[common])
    m.add_argument("project", type=Path)
    m.add_argument("--run", required=True)
    m.add_argument("--job")
    m.add_argument("--by", required=True)
    m.add_argument("--provider")
    m.add_argument("--method", choices=("manual", "api"), default="manual")
    m.add_argument("--reason")
    return p


def _emit(args: argparse.Namespace, payload: dict[str, Any], line: str) -> None:
    print(json.dumps(payload, indent=1, ensure_ascii=True) if args.json else line)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _dispatch(args)
    except ProjectLocked as e:
        print(f"refused: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except (ProjectError, UnsafeDatabaseLocation, FileNotFoundError, NotADirectoryError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return EXIT_REFUSED


def _dispatch(args: argparse.Namespace) -> int:
    cmd = args.command
    if cmd == "backup":
        from .backup import BackupCommandError, BackupError, run

        try:
            rep = run(args.project, args.dest, args.cmd, command_timeout_s=args.timeout_s)
        except BackupCommandError as e:
            _emit(args, e.report.to_dict(), f"backup copied but the command failed: {e}")
            return EXIT_CHECK
        except BackupError as e:
            print(f"refused: {e}", file=sys.stderr)
            return EXIT_REFUSED
        _emit(
            args,
            rep.to_dict(),
            f"backup ok: {rep.files} files, {rep.bytes} bytes ({rep.copied} copied, {rep.reused} reused) "
            f"-> {rep.dest}",
        )
        return EXIT_OK
    if cmd == "drill":
        from .backup import restore_drill

        rep = restore_drill(args.backup, args.tmp, restore=not args.no_restore, keep=args.keep)
        if args.report:
            rep.write(args.report)
        _emit(args, rep.to_dict(), rep.summary_line())
        return EXIT_OK if rep.ok else EXIT_CHECK
    if cmd == "purge":
        from .purge import PurgeError, end_of_project

        keep = {
            "ledger": not args.no_keep_ledger,
            "masters": not args.no_keep_masters,
            "gate_reports": not args.no_keep_gate_reports,
        }
        try:
            rep = end_of_project(
                args.project, keep, older_than_days=args.older_than_days, dry_run=not args.apply
            )
        except PurgeError as e:
            print(f"refused: {e}", file=sys.stderr)
            return EXIT_REFUSED
        verb = "would delete" if rep.dry_run else "deleted"
        n = len(rep.candidates) if rep.dry_run else len(rep.deleted)
        size = rep.bytes_candidates if rep.dry_run else rep.bytes_freed
        owed = len(rep.pending_vendor_deletions)
        _emit(
            args,
            rep.to_dict(),
            f"purge: {verb} {n} of {rep.objects_total} blobs ({size} bytes); "
            f"{owed} vendor-side deletion(s) still owed"
            + (f"; report {rep.report_path}" if rep.report_path else ""),
        )
        return EXIT_OK if rep.ok else EXIT_CHECK
    if cmd == "reindex":
        from .reindex import reindex

        rep = reindex(args.project, args.home, verify=args.verify)
        _emit(args, rep.to_dict(), rep.summary_line())
        return EXIT_OK if rep.ok else EXIT_CHECK
    if cmd == "alerts":
        from . import alerts

        res = alerts.run(args.project) if args.write else alerts.scan(Ledger(args.project))
        _emit(
            args,
            {
                "summary": asdict(res.summary),
                "alerts": [a.to_dict() for a in res.alerts],
                "written": res.written,
            },
            res.summary.line + (f"; {res.written} alert(s) written" if args.write else ""),
        )
        return EXIT_OK
    if cmd == "vendor-deletions":
        from .purge import vendor_deletions

        pending = [p.to_dict() for p in vendor_deletions(Ledger(args.project))]
        _emit(
            args,
            {"pending": pending},
            f"{len(pending)} vendor-side deletion(s) owed"
            + "".join(
                f"\n  {p['provider'] or '?'} job {p['provider_job_id']} (run {p['run_id']})" for p in pending
            ),
        )
        return EXIT_OK
    if cmd == "vendor-deleted":
        from .purge import record_vendor_deletion

        rec = record_vendor_deletion(
            Ledger(args.project),
            run_id=args.run,
            provider_job_id=args.job,
            by=args.by,
            provider=args.provider,
            method=args.method,
            reason=args.reason,
        )
        _emit(args, rec, f"recorded deleted_vendor_side for run {args.run} (ledger seq {rec['seq']})")
        return EXIT_OK
    raise AssertionError(f"unhandled command {cmd!r}")


if __name__ == "__main__":
    raise SystemExit(main())
