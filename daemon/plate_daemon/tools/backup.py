"""Project backup and the quarterly restore drill (BUILD_PLAN decision 23).

The project folder is the truth (Decision 6), so a backup is a copy of the folder, minus what can be
rebuilt or must not travel:

* `_work/` at any depth (scratch);
* the SQLite index (`*.sqlite`, `-wal`, `-shm`, `-journal`): it is a per-machine cache, `reindex` makes it again;
* `assets/.tmp/` (CAS staging), `*.lock` / `*.lock.json` (runtime locks) and in-flight `.<name>.<id>.tmp` files;
* symlinks (the CAS refuses them, the backup does not follow them).

`plan(project_dir)` lists what would be copied as `(src, rel)`, `rel` being `/`-separated on both OSes.

`run(project_dir, dest, encrypt_cmd=None)` copies every planned file into `dest` through a temp file and
`os.replace` (hashing the bytes as they are copied, source mtimes kept), then writes
`dest/backup_manifest.json`: `{schema, kind, project_id, created_utc, files: [{path, bytes, blake3}],
totals, ledger: {records, head_seq, head_hash, chain_ok}}`. Running it again into the same `dest` is
incremental: a CAS object (its file name is its hash, so it never changes) already in `dest` at the right
size is reused, and files the previous manifest listed that the project no longer has are removed (nothing
else in `dest` is ever deleted). The backup is read-only on the project: it does not take `project.lock`
(the daemon holds that while a project is open), and a ledger line caught mid-append is a torn tail that
every reader skips.

Encryption and the offsite copy are a user-supplied command, not embedded code. `encrypt_cmd` (else the
`PLATE_BACKUP_CMD` environment variable) is run once after the manifest is written, with `shell=False`: a
string is split with `shlex` (POSIX rules on POSIX, Windows rules with quotes stripped on Windows), a list
is used as given, and each argument has `{src}` (the project folder) and `{dest}` (the folder just written)
replaced. A typical command is `rclone sync {dest} b2crypt:plate/<project>`, where `b2crypt:` is an rclone
crypt remote; see `docs/runbooks/backup-restore.md`. A non-zero exit raises `BackupCommandError`
carrying the report: the local copy is complete and verified, only the offsite step failed.

`restore_drill(backup_dir, tmp)` is the quarterly drill: it reads the manifest, copies every file into a
scratch project under `tmp` while hashing it, compares each hash and size with the manifest, opens the
restored folder as a project and re-walks its ledger chain against the head the manifest recorded, then
returns a `DrillReport`. Any tampered, missing or truncated file fails the drill and is named.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..cas import OBJECT_RE, hash_file
from ..fsutil import read_json, write_json_atomic
from ..ledger import Ledger
from ..projects import Project
from ..timeutil import iso, utcnow
from .common import UnsafePath, copy_hashed, rel_posix, rmtree_retry, safe_join, unlink_retry

MANIFEST_NAME = "backup_manifest.json"
MANIFEST_KIND = "plate-backup"
SCHEMA = 1
BACKUP_CMD_ENV = "PLATE_BACKUP_CMD"
EXCLUDED_DIR_NAMES = frozenset({"_work"})
EXCLUDED_REL_DIRS = frozenset({"assets/.tmp"})
EXCLUDED_SUFFIXES = (
    ".sqlite",
    ".sqlite3",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite-journal",
    ".lock",
    ".lock.json",
    ".lock.plate",
)
STDERR_TAIL = 2000

ProgressFn = Callable[[str, int, int], None]


class BackupError(RuntimeError):
    pass


class BackupCommandError(BackupError):
    """The offsite / encrypt command failed; `report` is the (complete) local backup it was meant to ship."""

    def __init__(self, message: str, report: BackupReport):
        super().__init__(message)
        self.report = report


# ---------------------------------------------------------------------------- what to copy


def _excluded_file(name: str) -> bool:
    if name.endswith(EXCLUDED_SUFFIXES):
        return True
    return name.startswith(".") and name.endswith(".tmp")  # fsutil.temp_sibling: an atomic write in flight


def plan(project_dir: Path | str) -> list[tuple[Path, str]]:
    """Every file a backup of `project_dir` copies, as `(src, rel)` sorted by `rel`."""
    root = Path(project_dir)
    out: list[tuple[Path, str]] = []
    for here, dirnames, filenames in os.walk(root, followlinks=False):
        here_p = Path(here)
        keep: list[str] = []
        for d in sorted(dirnames):
            p = here_p / d
            if d in EXCLUDED_DIR_NAMES or p.is_symlink() or rel_posix(p, root) in EXCLUDED_REL_DIRS:
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            p = here_p / f
            if _excluded_file(f) or p.is_symlink() or not p.is_file():
                continue
            rel = rel_posix(p, root)
            if rel == MANIFEST_NAME:
                continue  # never part of its own project; a backup writes its own
            out.append((p, rel))
    out.sort(key=lambda t: t[1])
    return out


# ---------------------------------------------------------------------------- the external command


def split_command(text: str) -> list[str]:
    """A command string -> argument list. POSIX: `shlex` rules. Windows: backslashes stay literal (paths)
    and surrounding double or single quotes are stripped from each argument."""
    if os.name != "nt":
        return shlex.split(text)
    out = []
    for tok in shlex.split(text, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        out.append(tok)
    return out


def build_argv(cmd: str | Sequence[str], *, src: Path, dest: Path) -> list[str]:
    argv = split_command(cmd) if isinstance(cmd, str) else [str(a) for a in cmd]
    if not argv:
        raise BackupError("the backup command is empty")
    return [a.replace("{src}", str(src)).replace("{dest}", str(dest)) for a in argv]


# ---------------------------------------------------------------------------- reports


@dataclass
class BackupReport:
    dest: Path
    manifest_path: Path
    project_id: str
    created_utc: str
    files: int
    bytes: int
    copied: int
    reused: int
    pruned: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ledger: dict[str, Any] = field(default_factory=dict)
    command: dict[str, Any] | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.command is None or self.command.get("returncode") == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dest": str(self.dest),
            "manifest": str(self.manifest_path),
            "project_id": self.project_id,
            "created_utc": self.created_utc,
            "files": self.files,
            "bytes": self.bytes,
            "copied": self.copied,
            "reused": self.reused,
            "pruned": self.pruned,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "ledger": self.ledger,
            "command": self.command,
            "seconds": round(self.seconds, 3),
        }


@dataclass
class DrillReport:
    backup_dir: Path
    checked_utc: str
    files_expected: int = 0
    files_ok: int = 0
    bytes_ok: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    project_ok: bool | None = None
    ledger_ok: bool | None = None
    ledger_head: dict[str, Any] | None = None
    restored_to: str | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "backup_dir": str(self.backup_dir),
            "checked_utc": self.checked_utc,
            "files_expected": self.files_expected,
            "files_ok": self.files_ok,
            "bytes_ok": self.bytes_ok,
            "failures": self.failures,
            "extra": self.extra,
            "warnings": self.warnings,
            "project_ok": self.project_ok,
            "ledger_ok": self.ledger_ok,
            "ledger_head": self.ledger_head,
            "restored_to": self.restored_to,
            "seconds": round(self.seconds, 3),
        }

    def summary_line(self) -> str:
        if self.ok:
            return (
                f"restore drill PASSED: {self.files_ok}/{self.files_expected} files, "
                f"{self.bytes_ok} bytes verified"
            )
        bad = ", ".join(f"{f['path']} ({f['problem']})" for f in self.failures[:5])
        more = f" and {len(self.failures) - 5} more" if len(self.failures) > 5 else ""
        return f"restore drill FAILED: {len(self.failures)} problem(s): {bad}{more}"

    def write(self, path: Path | str) -> Path:
        path = Path(path)
        write_json_atomic(path, self.to_dict(), indent=1)
        return path


# ---------------------------------------------------------------------------- backup


def _read_manifest(backup_dir: Path) -> dict[str, Any] | None:
    path = backup_dir / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        doc = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("files"), list):
        return None
    return doc


def _entries(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in doc.get("files", []):
        if isinstance(e, dict) and isinstance(e.get("path"), str):
            out[e["path"]] = e
    return out


def _overlap(a: Path, b: Path) -> bool:
    a, b = a.resolve(), b.resolve()
    return a == b or a in b.parents or b in a.parents


def _ledger_summary(folder: Path) -> dict[str, Any]:
    v = Ledger(folder).check()
    return {
        "records": v.count,
        "head_seq": v.count,
        "head_hash": v.head_hash,
        "chain_ok": v.ok,
        "torn_tail": v.torn,
    }


def run(
    project_dir: Path | str,
    dest: Path | str,
    encrypt_cmd: str | Sequence[str] | None = None,
    *,
    command_timeout_s: float | None = None,
    progress: ProgressFn | None = None,
    now: datetime | None = None,
) -> BackupReport:
    """Back `project_dir` up into the folder `dest` (see the module docstring). `progress(rel, i, n)` is
    called before each file."""
    t0 = time.monotonic()
    root, dest = Path(project_dir), Path(dest)
    project = Project.load(root)
    if _overlap(root, dest):
        raise BackupError(f"{dest}: the backup folder and the project folder must not contain each other")
    if dest.exists() and not dest.is_dir():
        raise BackupError(f"{dest} exists and is not a folder")
    dest.mkdir(parents=True, exist_ok=True)

    previous = _read_manifest(dest)
    prev_entries = _entries(previous) if previous else {}
    items = plan(root)
    files: list[dict[str, Any]] = []
    skipped: list[str] = []
    warnings: list[str] = []
    copied = reused = 0
    for i, (src, rel) in enumerate(items):
        if progress is not None:
            progress(rel, i, len(items))
        target = dest.joinpath(*rel.split("/"))  # rel came from our own walk, not from a manifest
        cas_match = OBJECT_RE.match(src.name) if rel.startswith("assets/") else None
        try:
            size = src.stat().st_size
            old = prev_entries.get(rel)
            if (
                cas_match
                and old is not None
                and old.get("blake3") == cas_match.group(1)
                and old.get("bytes") == size
                and target.is_file()
                and target.stat().st_size == size
            ):
                digest, nbytes = cas_match.group(1), size  # immutable by name: nothing to re-read
                reused += 1
            else:
                digest, nbytes = copy_hashed(src, target)
                copied += 1
                if cas_match and cas_match.group(1) != digest:
                    warnings.append(
                        f"{rel}: the object's bytes do not hash to its name (damaged in the project)"
                    )
        except FileNotFoundError:
            skipped.append(f"{rel}: vanished while the backup ran")
            continue
        files.append({"path": rel, "bytes": nbytes, "blake3": digest})

    # Files an earlier backup put here that the project has since dropped (a purge): remove those only.
    pruned: list[str] = []
    keep = {e["path"] for e in files}
    for rel in sorted(set(prev_entries) - keep):
        try:
            gone = safe_join(dest, rel)
        except UnsafePath:
            continue
        if gone.is_file():
            unlink_retry(gone)
            pruned.append(rel)
            _remove_empty_parents(gone.parent, dest)

    created = iso(now or utcnow())
    ledger = _ledger_summary(dest)
    manifest = {
        "schema": SCHEMA,
        "kind": MANIFEST_KIND,
        "project_id": project.project_id,
        "created_utc": created,
        "files": files,
        "totals": {"files": len(files), "bytes": sum(f["bytes"] for f in files)},
        "ledger": ledger,
    }
    manifest_path = dest / MANIFEST_NAME
    write_json_atomic(manifest_path, manifest, indent=1)
    report = BackupReport(
        dest=dest,
        manifest_path=manifest_path,
        project_id=project.project_id,
        created_utc=created,
        files=len(files),
        bytes=manifest["totals"]["bytes"],
        copied=copied,
        reused=reused,
        pruned=pruned,
        skipped=skipped,
        warnings=warnings,
        ledger=ledger,
    )

    cmd = encrypt_cmd if encrypt_cmd is not None else os.environ.get(BACKUP_CMD_ENV)
    if cmd:
        report.command = _run_command(cmd, root, dest, command_timeout_s)
    report.seconds = time.monotonic() - t0
    if report.command is not None and report.command["returncode"] != 0:
        raise BackupCommandError(
            f"backup command {report.command['program']} exited {report.command['returncode']}: "
            f"{report.command.get('stderr_tail', '')}".rstrip(": "),
            report,
        )
    return report


def _remove_empty_parents(folder: Path, stop: Path) -> None:
    while folder != stop and stop in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def _run_command(cmd: str | Sequence[str], src: Path, dest: Path, timeout_s: float | None) -> dict[str, Any]:
    argv = build_argv(cmd, src=src, dest=dest)
    program = Path(argv[0]).name
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError:
        info = {"program": program, "returncode": 127, "stderr_tail": f"{argv[0]}: command not found"}
    except subprocess.TimeoutExpired:
        info = {"program": program, "returncode": 124, "stderr_tail": f"timed out after {timeout_s} s"}
    else:
        info = {
            "program": program,
            "returncode": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-STDERR_TAIL:].strip(),
        }
    return info


# ---------------------------------------------------------------------------- restore drill


def restore_drill(
    backup_dir: Path | str,
    tmp: Path | str,
    *,
    restore: bool = True,
    keep: bool = False,
    now: datetime | None = None,
) -> DrillReport:
    """Verify a backup the way a real restore would. Every file in the manifest is copied (`restore=True`)
    into `tmp/restore-drill-<random>/project` while hashed, or only read and hashed in place
    (`restore=False`, for a cheap check of a very large backup). A missing, truncated or altered file is a
    failure naming the file. After the copy the restored folder must open as a project and its ledger chain
    must end at the head the manifest recorded. The scratch copy is deleted unless `keep=True`."""
    t0 = time.monotonic()
    backup_dir, tmp = Path(backup_dir), Path(tmp)
    report = DrillReport(backup_dir=backup_dir, checked_utc=iso(now or utcnow()))

    def fail(path: str, problem: str, **extra: Any) -> None:
        report.failures.append({"path": path, "problem": problem, **extra})

    doc = _read_manifest(backup_dir)
    if doc is None:
        fail(MANIFEST_NAME, "missing or unreadable manifest")
        report.seconds = time.monotonic() - t0
        return report
    if doc.get("schema") != SCHEMA or doc.get("kind") != MANIFEST_KIND:
        fail(MANIFEST_NAME, f"not a schema {SCHEMA} {MANIFEST_KIND} manifest")
        report.seconds = time.monotonic() - t0
        return report

    entries = _entries(doc)
    report.files_expected = len(entries)
    work = Path(tempfile.mkdtemp(prefix="restore-drill-", dir=_ensure_dir(tmp))) if restore else tmp
    restored = work / "project"
    if restore:
        restored.mkdir()
        report.restored_to = str(restored)
    try:
        for rel, entry in sorted(entries.items()):
            try:
                source = safe_join(backup_dir, rel)
            except UnsafePath as e:
                fail(rel, f"unsafe path in the manifest ({e})")
                continue
            if not source.is_file():
                fail(rel, "missing from the backup", expected_blake3=entry.get("blake3"))
                continue
            try:
                if restore:
                    digest, size = copy_hashed(source, safe_join(restored, rel))
                else:
                    digest, size = hash_file(source)
            except OSError as e:
                fail(rel, f"unreadable ({e})")
                continue
            if size != entry.get("bytes"):
                fail(rel, "size differs", expected_bytes=entry.get("bytes"), actual_bytes=size)
            elif digest != entry.get("blake3"):
                fail(rel, "hash differs", expected_blake3=entry.get("blake3"), actual_blake3=digest)
            else:
                report.files_ok += 1
                report.bytes_ok += size
                m = OBJECT_RE.match(source.name) if rel.startswith("assets/") else None
                if m and m.group(1) != digest:
                    report.warnings.append(
                        f"{rel}: bytes do not hash to the object's name (damaged before backup)"
                    )

        known = set(entries) | {MANIFEST_NAME}
        for p in sorted(backup_dir.rglob("*")):
            if p.is_file() and not p.is_symlink():
                rel = rel_posix(p, backup_dir)
                if rel not in known:
                    report.extra.append(rel)

        if restore:
            _check_restored(restored, doc, report, fail)
        else:
            report.ledger_head = doc.get("ledger") if isinstance(doc.get("ledger"), dict) else None
    finally:
        if restore and not keep:
            rmtree_retry(work)
            report.restored_to = None
        report.seconds = time.monotonic() - t0
    return report


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _check_restored(
    restored: Path, doc: dict[str, Any], report: DrillReport, fail: Callable[..., None]
) -> None:
    try:
        Project.load(restored)
        report.project_ok = True
    except Exception as e:  # any reason the folder would not open is the finding
        report.project_ok = False
        fail("project.json", f"the restored folder does not open as a project ({e})")
    wanted = doc.get("ledger") if isinstance(doc.get("ledger"), dict) else None
    now_ledger = _ledger_summary(restored)
    report.ledger_head = now_ledger
    report.ledger_ok = bool(now_ledger["chain_ok"])
    if not now_ledger["chain_ok"]:
        fail("runs.jsonl", "the restored ledger's hash chain does not verify")
    if wanted is not None and (
        wanted.get("head_hash") != now_ledger["head_hash"] or wanted.get("records") != now_ledger["records"]
    ):
        report.ledger_ok = False
        fail(
            "runs.jsonl",
            "the restored ledger does not end at the head the manifest recorded",
            expected_head=wanted.get("head_hash"),
            actual_head=now_ledger["head_hash"],
        )
