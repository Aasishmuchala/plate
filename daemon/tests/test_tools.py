"""Maintenance tools (router plan T42): backup and restore drill, end-of-project purge and vendor-side
deletion tracking, index rebuild with a consistency report, budget alerts, and the module CLI."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from blake3 import blake3

from plate_daemon.db import tx
from plate_daemon.index import Index, apply_packet_entry
from plate_daemon.ledger import Ledger
from plate_daemon.lock import ProjectLocked
from plate_daemon.projects import Lock, Project
from plate_daemon.tools import alerts, backup, purge, reindex
from plate_daemon.tools.__main__ import main as tools_main
from plate_daemon.tools.common import UnsafePath, safe_join

T0 = datetime(2026, 10, 16, 9, 0, 0, tzinfo=UTC)
ROUTE = {"engine_id": "seedance-2.5-i2v", "provider": "atlas", "region": "sg"}


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------- a project to work on


def build_project(tmp_path: Path, name: str = "proj") -> SimpleNamespace:
    """A project with every kind of reference the purge has to tell apart, plus junk a backup skips."""
    root = tmp_path / name
    project = Project.create(root, "tula-a", {"nda": False})
    cas = project.cas()
    keys = {
        n: cas.put_bytes((n + "-bytes-").encode() * 8, "png")
        for n in (
            "plate",
            "packet_manifest",
            "accepted",
            "rerolled",
            "orphan",
            "orphan2",
            "graph_pick",
            "exported",
            "gate_ref",
            "graph_run_result",
        )
    }
    project.save_graph(
        {
            "nodes": [{"id": "n1", "type": "Generate", "data": {"pick": keys["graph_pick"], "run": "run_G"}}],
            "edges": [],
        }
    )
    project.register_packet(
        "pkt_a",
        {
            "shot_id": "s01",
            "kind": "still",
            "tier": "A",
            "packet_version": "0.1.1",
            "path": str(tmp_path / "packets" / "pkt_a"),
            "manifest_blake3": keys["packet_manifest"],
        },
    )
    led = Ledger(root)
    quoted = {"packet_id": "pkt_a", "route": ROUTE, "quote": {"provider": "atlas"}}

    def run(rid: str, job: str, result: str | None, minutes: float, **done: Any) -> None:
        led.append(
            kind="quoted",
            run_id=rid,
            engine_id=ROUTE["engine_id"],
            route_id="atlas",
            custody_grade="D",
            cost_usd_quoted=1.0,
            at=at(minutes),
            data=quoted,
        )
        led.append(kind="approved", run_id=rid, by="human", cost_usd_quoted=1.0, at=at(minutes + 1))
        led.append(kind="submitted", run_id=rid, provider_job_id=job, at=at(minutes + 2))
        led.append(
            kind="done",
            run_id=rid,
            provider_job_id=job,
            at=at(minutes + 3),
            data={"result_key": result, "inputs": {"plate": keys["plate"]}, **done},
        )

    run("run_A", "job-A", keys["accepted"], 0, decision="accepted")
    run("run_B", "job-B", keys["rerolled"], 10, decision="rerolled")
    run("run_E", "job-E", keys["exported"], 20)
    led.append(kind="export", run_id="run_E", at=at(25), data={"dest_name": "final.png"})
    run("run_G", "job-G", keys["graph_run_result"], 30)

    gates = root / "gates"
    gates.mkdir()
    (gates / "run_A.1.json").write_text(
        json.dumps({"candidate": keys["gate_ref"], "verdict": "PASS"}), encoding="utf-8", newline="\n"
    )

    # Things a backup must skip.
    (root / "_work").mkdir()
    (root / "_work" / "scratch.bin").write_bytes(b"scratch")
    (root / "sub" / "_work").mkdir(parents=True)
    (root / "sub" / "_work" / "deep.bin").write_bytes(b"scratch")
    (root / "index.sqlite").write_bytes(b"SQLite format 3\x00 not really")
    (root / "index.sqlite-wal").write_bytes(b"wal")
    (root / "assets" / ".tmp").mkdir(exist_ok=True)
    (root / "assets" / ".tmp" / "stale-put").write_bytes(b"half a blob")
    (root / ".project.json.abc123def456.tmp").write_bytes(b"atomic write in flight")
    (root / "project.lock").write_text("{}", encoding="utf-8")
    (root / "review").mkdir()
    (root / "review" / "bundle.html").write_text("<html>review</html>", encoding="utf-8")
    return SimpleNamespace(root=root, project=project, keys=keys, name_of={v: k for k, v in keys.items()})


@pytest.fixture
def proj(tmp_path: Path) -> SimpleNamespace:
    return build_project(tmp_path)


def names(p: SimpleNamespace, entries: list[dict[str, Any]]) -> set[str]:
    return {p.name_of[e["hash"]] for e in entries}


def blob_path(p: SimpleNamespace, name: str) -> Path:
    found = p.project.cas().find(p.keys[name])
    assert found is not None, name
    return found


def hash_bytes(path: Path) -> str:
    return blake3(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------- backup: plan and run


def test_plan_lists_the_project_and_skips_scratch_index_staging_and_locks(proj):
    rels = {rel for _src, rel in backup.plan(proj.root)}
    assert {"project.json", "packets.json", "runs.jsonl", "gates/run_A.1.json", "review/bundle.html"} <= rels
    assert any(r.startswith("assets/") and r.endswith(".png") for r in rels)
    for skipped in (
        "_work/scratch.bin",
        "sub/_work/deep.bin",
        "index.sqlite",
        "index.sqlite-wal",
        "assets/.tmp/stale-put",
        ".project.json.abc123def456.tmp",
        "project.lock",
    ):
        assert skipped not in rels
    assert all("\\" not in r and ":" not in r for r in rels)
    assert [r for _s, r in backup.plan(proj.root)] == sorted(rels)


def test_run_copies_everything_and_the_manifest_hashes_match_independently(proj, tmp_path):
    dest = tmp_path / "bk"
    rep = backup.run(proj.root, dest)
    assert rep.ok and rep.copied == rep.files and rep.reused == 0
    doc = json.loads((dest / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert doc["kind"] == "plate-backup" and doc["project_id"] == "tula-a"
    assert {e["path"] for e in doc["files"]} == {rel for _s, rel in backup.plan(proj.root)}
    for e in doc["files"]:
        copy = dest.joinpath(*e["path"].split("/"))
        assert copy.read_bytes() == proj.root.joinpath(*e["path"].split("/")).read_bytes()
        assert blake3(copy.read_bytes()).hexdigest() == e["blake3"] and copy.stat().st_size == e["bytes"]
    seq, head = Ledger(proj.root).head()
    assert doc["ledger"]["head_hash"] == head and doc["ledger"]["records"] == seq
    assert doc["ledger"]["chain_ok"] is True
    assert not (dest / "_work").exists() and not (dest / "index.sqlite").exists()
    # modification times survive, so the purge's age rule still works on a restored copy
    src = blob_path(proj, "accepted")
    assert abs((dest / "assets" / src.parent.name / src.name).stat().st_mtime - src.stat().st_mtime) < 2
    assert not list(dest.rglob("*.tmp"))


def test_second_run_is_incremental_and_prunes_what_the_project_dropped(proj, tmp_path):
    dest = tmp_path / "bk"
    first = backup.run(proj.root, dest)
    objects = len(list(proj.project.cas().iter_objects()))
    second = backup.run(proj.root, dest)
    assert second.reused == objects and second.copied == first.files - objects
    orphan = blob_path(proj, "orphan")
    rel = f"assets/{orphan.parent.name}/{orphan.name}"
    assert (dest / rel).is_file()
    orphan.unlink()
    third = backup.run(proj.root, dest)
    assert third.pruned == [rel] and not (dest / rel).exists()
    # only what an earlier manifest listed is ever removed
    stranger = dest / "keep-me.txt"
    stranger.write_text("not ours", encoding="utf-8")
    backup.run(proj.root, dest)
    assert stranger.read_text(encoding="utf-8") == "not ours"
    assert backup.restore_drill(dest, tmp_path / "scratch").extra == ["keep-me.txt"]


def test_incremental_run_recopies_a_blob_that_is_not_whole_in_the_backup(proj, tmp_path):
    dest = tmp_path / "bk"
    backup.run(proj.root, dest)
    victim = blob_path(proj, "accepted")
    copy = dest / "assets" / victim.parent.name / victim.name
    copy.write_bytes(copy.read_bytes()[:-5])  # a half-uploaded or damaged copy at the right name
    again = backup.run(proj.root, dest)
    assert again.copied == again.files - len(proj.keys) + 1
    assert copy.read_bytes() == victim.read_bytes()
    assert backup.restore_drill(dest, tmp_path / "scratch").ok


def test_dest_may_not_overlap_the_project_or_be_a_file(proj, tmp_path):
    with pytest.raises(backup.BackupError):
        backup.run(proj.root, proj.root / "backups")
    with pytest.raises(backup.BackupError):
        backup.run(proj.root, tmp_path)
    clash = tmp_path / "a-file"
    clash.write_text("x", encoding="utf-8")
    with pytest.raises(backup.BackupError):
        backup.run(proj.root, clash)
    with pytest.raises(FileNotFoundError):
        backup.run(tmp_path / "no-such-project", tmp_path / "bk")


# ---------------------------------------------------------------------------- backup: the offsite command

HOOK = """\
import json, sys
# usage: hook.py OUT EXIT_CODE [ARG ...]: records ARG ... in OUT, then exits with EXIT_CODE
with open(sys.argv[1], "w", encoding="utf-8", newline="\\n") as fh:
    json.dump(sys.argv[3:], fh)
sys.exit(int(sys.argv[2]))
"""


def _hook(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "hook.py"
    script.write_text(HOOK, encoding="utf-8", newline="\n")
    return script, tmp_path / "hook-args.json"


def test_command_gets_src_and_dest_as_single_arguments_without_a_shell(tmp_path):
    p = build_project(tmp_path, "my project")  # a space in the path: one argv element, not two
    script, out = _hook(tmp_path)
    dest = tmp_path / "back up"
    rep = backup.run(p.root, dest, [sys.executable, str(script), str(out), "0", "{src}", "{dest}"])
    assert rep.command == {"program": Path(sys.executable).name, "returncode": 0, "stderr_tail": ""}
    assert json.loads(out.read_text(encoding="utf-8")) == [str(p.root), str(dest)]


def test_command_string_from_the_environment_is_split_like_a_shell_would(tmp_path, monkeypatch):
    p = build_project(tmp_path)
    script, out = _hook(tmp_path)
    dest = tmp_path / "bk"
    monkeypatch.setenv(backup.BACKUP_CMD_ENV, f'"{sys.executable}" "{script}" "{out}" 0 "{{src}}" "{{dest}}"')
    rep = backup.run(p.root, dest)
    assert rep.command and rep.command["returncode"] == 0
    assert json.loads(out.read_text(encoding="utf-8")) == [str(p.root), str(dest)]
    # an explicit argument wins over the environment
    other = tmp_path / "other-args.json"
    backup.run(p.root, dest, [sys.executable, str(script), str(other), "0", "{dest}"])
    assert json.loads(other.read_text(encoding="utf-8")) == [str(dest)]


def test_failing_command_raises_with_the_complete_local_backup_attached(tmp_path):
    p = build_project(tmp_path)
    script, out = _hook(tmp_path)
    dest = tmp_path / "bk"
    with pytest.raises(backup.BackupCommandError) as ei:
        backup.run(p.root, dest, [sys.executable, str(script), str(out), "7", "{src}", "{dest}"])
    assert ei.value.report.command["returncode"] == 7
    assert not ei.value.report.ok
    assert backup.restore_drill(dest, tmp_path / "scratch").ok  # the staged copy is whole
    with pytest.raises(backup.BackupCommandError) as missing:
        backup.run(p.root, dest, [str(tmp_path / "no-such-program"), "{dest}"])
    assert missing.value.report.command["returncode"] == 127
    assert backup.run(p.root, dest, "").command is None  # an empty command means "none"
    with pytest.raises(backup.BackupError):
        backup.build_argv("", src=p.root, dest=dest)


def test_split_command_keeps_quoted_arguments_whole():
    argv = backup.build_argv('rclone sync "{dest}" "b2crypt:plate/my proj"', src=Path("S"), dest=Path("D D"))
    assert argv == ["rclone", "sync", "D D", "b2crypt:plate/my proj"]
    assert backup.build_argv(["x", "{src}-{dest}"], src=Path("a"), dest=Path("b")) == ["x", "a-b"]


# ---------------------------------------------------------------------------- restore drill


def test_drill_passes_on_a_clean_backup_and_cleans_up_after_itself(proj, tmp_path):
    dest, scratch = tmp_path / "bk", tmp_path / "scratch"
    rep = backup.run(proj.root, dest)
    drill = backup.restore_drill(dest, scratch)
    assert drill.ok and not drill.failures
    assert drill.files_expected == drill.files_ok == rep.files
    assert drill.bytes_ok == rep.bytes
    assert drill.project_ok and drill.ledger_ok
    assert drill.ledger_head["head_hash"] == Ledger(proj.root).head()[1]
    assert drill.restored_to is None and not list(scratch.glob("restore-drill-*"))
    assert drill.summary_line().startswith("restore drill PASSED")

    kept = backup.restore_drill(dest, scratch, keep=True)
    restored = Path(kept.restored_to)
    assert Project.load(restored).project_id == "tula-a"
    assert Ledger(restored).verify() == (True, None)
    out = kept.write(tmp_path / "drill-report.json")
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is True

    in_place = backup.restore_drill(dest, scratch, restore=False)
    assert in_place.ok and in_place.restored_to is None


def test_drill_names_a_tampered_file(proj, tmp_path):
    dest = tmp_path / "bk"
    backup.run(proj.root, dest)
    victim = blob_path(proj, "accepted")
    rel = f"assets/{victim.parent.name}/{victim.name}"
    copy = dest / rel
    size = copy.stat().st_size
    copy.write_bytes(b"\xff" * size)  # same size, different bytes: only the hash can catch it
    for restore in (True, False):
        drill = backup.restore_drill(dest, tmp_path / "scratch", restore=restore)
        assert not drill.ok
        assert [f["path"] for f in drill.failures] == [rel]
        assert drill.failures[0]["problem"] == "hash differs"
        assert drill.failures[0]["expected_blake3"] == proj.keys["accepted"]
        assert drill.files_ok == drill.files_expected - 1
        assert rel in drill.summary_line() and "FAILED" in drill.summary_line()


def test_drill_catches_missing_truncated_and_edited_ledger(proj, tmp_path):
    dest = tmp_path / "bk"
    backup.run(proj.root, dest)
    (dest / "review" / "bundle.html").unlink()
    gate = dest / "gates" / "run_A.1.json"
    gate.write_bytes(gate.read_bytes()[:-3])
    runs = dest / "runs.jsonl"
    lines = runs.read_bytes().split(b"\n")
    lines[1] = lines[1].replace(b"human", b"humaN")  # same length: only the hash and the chain notice
    runs.write_bytes(b"\n".join(lines))
    drill = backup.restore_drill(dest, tmp_path / "scratch")
    problems: dict[str, set[str]] = {}
    for f in drill.failures:
        problems.setdefault(f["path"], set()).add(f["problem"])
    assert problems["review/bundle.html"] == {"missing from the backup"}
    assert problems["gates/run_A.1.json"] == {"size differs"}
    assert {"hash differs", "the restored ledger's hash chain does not verify"} <= problems["runs.jsonl"]
    assert not drill.ok and drill.ledger_ok is False


def test_drill_reports_a_missing_manifest_and_refuses_paths_that_leave_the_folder(proj, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not backup.restore_drill(empty, tmp_path / "scratch").ok
    dest = tmp_path / "bk"
    backup.run(proj.root, dest)
    manifest = dest / backup.MANIFEST_NAME
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    doc["files"].append({"path": "../escape.txt", "bytes": 1, "blake3": "0" * 64})
    manifest.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    drill = backup.restore_drill(dest, tmp_path / "scratch")
    assert any("unsafe path" in f["problem"] for f in drill.failures)
    assert not (tmp_path / "escape.txt").exists()
    for bad in ("../x", "/abs", "a/../b", "a\\b", "C:/x", "a//b", ""):
        with pytest.raises(UnsafePath):
            safe_join(tmp_path, bad)
    assert safe_join(tmp_path, "a/b.txt") == tmp_path / "a" / "b.txt"


# ---------------------------------------------------------------------------- purge


def test_dry_run_lists_exactly_the_unreferenced_blobs_and_changes_nothing(proj):
    before = sorted(p.name for _k, _e, p in proj.project.cas().iter_objects())
    rep = purge.end_of_project(proj.root, older_than_days=0)
    assert rep.dry_run and rep.ok
    assert names(proj, rep.candidates) == {"orphan", "orphan2"}
    assert rep.deleted == [] and rep.bytes_freed == 0 and rep.report_path is None
    assert rep.bytes_candidates == sum(c["bytes"] for c in rep.candidates)
    assert sorted(p.name for _k, _e, p in proj.project.cas().iter_objects()) == before
    assert not list(proj.root.glob("purge_*.json"))
    assert rep.kept == {
        "graph": 3,  # the node's pick, run_G's result and, through run_G, its input plate
        "packets": 1,
        "masters": 2,  # accepted run_A, exported run_E
        "gate_reports": 1,
        "ledger": 1,  # run_B's rerolled candidate: only the ledger still names it
        "index": 0,
        "explicit": 0,
        "young": 0,
    }
    assert rep.objects_total == len(proj.keys)


def test_real_run_deletes_only_those_and_writes_the_report(proj):
    ledger_before = (proj.root / "runs.jsonl").read_bytes()
    rep = purge.end_of_project(proj.root, older_than_days=0, dry_run=False)
    assert names(proj, rep.deleted) == {"orphan", "orphan2"} and rep.ok
    assert rep.bytes_freed == rep.bytes_candidates > 0
    remaining = {k for k, _e, _p in proj.project.cas().iter_objects()}
    assert remaining == set(proj.keys.values()) - {proj.keys["orphan"], proj.keys["orphan2"]}
    assert (proj.root / "runs.jsonl").read_bytes() == ledger_before  # the audit trail is untouched
    assert Ledger(proj.root).verify() == (True, None)
    report_file = Path(rep.report_path)
    assert report_file.parent == proj.root and report_file.name.startswith("purge_")
    assert ":" not in report_file.name
    doc = json.loads(report_file.read_text(encoding="utf-8"))
    assert {d["hash"] for d in doc["deleted"]} == {proj.keys["orphan"], proj.keys["orphan2"]}
    assert doc["dry_run"] is False and doc["bytes_freed"] == rep.bytes_freed
    again = purge.end_of_project(proj.root, older_than_days=0, dry_run=False)
    assert again.deleted == []  # nothing left to do
    # a second report in the same second does not overwrite the first
    assert Path(again.report_path) != report_file
    assert json.loads(report_file.read_text(encoding="utf-8"))["deleted"] == doc["deleted"]


def test_default_keep_is_the_reference_count_plus_packets_and_gate_reports(proj):
    cas = proj.project.cas()
    zero = {k for k, n in cas.refcount(proj.project).items() if n == 0}  # graph + runs only
    rep = purge.end_of_project(proj.root, older_than_days=0)
    candidates = {c["hash"] for c in rep.candidates}
    assert candidates <= zero
    # what the plain count would delete but the purge also sees a reference for
    assert zero - candidates == {proj.keys["packet_manifest"], proj.keys["gate_ref"]}


def test_a_blob_has_to_be_old_enough(proj):
    fresh = purge.end_of_project(proj.root, older_than_days=30)
    assert fresh.candidates == [] and fresh.kept["young"] == 2
    later = datetime.now(UTC) + timedelta(days=40)
    old = purge.end_of_project(proj.root, older_than_days=30, now=later)
    assert names(proj, old.candidates) == {"orphan", "orphan2"}
    aged = blob_path(proj, "orphan")
    stamp = (datetime.now(UTC) - timedelta(days=45)).timestamp()
    os.utime(aged, (stamp, stamp))
    mixed = purge.end_of_project(proj.root, older_than_days=30)
    assert names(proj, mixed.candidates) == {"orphan"}
    assert mixed.candidates[0]["age_days"] >= 44


def test_keep_flags_decide_what_the_ledger_alone_protects(proj):
    # ledger kept: only orphans go (CAS.refcount's rule: graph + runs)
    assert names(proj, purge.end_of_project(proj.root, older_than_days=0).candidates) == {"orphan", "orphan2"}
    # ledger not kept: unchosen candidates go; masters, gate reports, graph picks and the plate stay
    keep = {"ledger": False}
    rep = purge.end_of_project(proj.root, keep, older_than_days=0)
    assert names(proj, rep.candidates) == {"orphan", "orphan2", "rerolled"}
    # masters not kept either: accepted and exported go; the run the graph names (run_G) stays
    rep = purge.end_of_project(proj.root, {"ledger": False, "masters": False}, older_than_days=0)
    assert names(proj, rep.candidates) == {"orphan", "orphan2", "rerolled", "accepted", "exported"}
    assert "graph_run_result" not in names(proj, rep.candidates)
    assert {"plate", "packet_manifest"}.isdisjoint(names(proj, rep.candidates))  # run_G's input; packets.json
    # gate reports not kept: the blob only a report names goes too
    rep = purge.end_of_project(
        proj.root, {"ledger": False, "masters": False, "gate_reports": False}, older_than_days=0
    )
    assert "gate_ref" in names(proj, rep.candidates)
    assert "graph_pick" not in names(proj, rep.candidates)  # the graph itself always protects
    with pytest.raises(purge.PurgeError):
        purge.end_of_project(proj.root, {"everything": False})
    with pytest.raises(purge.PurgeError):
        purge.end_of_project(proj.root, older_than_days=-1)


def test_protect_and_index_references_are_honoured(proj, tmp_path):
    rep = purge.end_of_project(proj.root, older_than_days=0, protect=[proj.keys["orphan"]])
    assert names(proj, rep.candidates) == {"orphan2"}

    idx = Index(tmp_path / "home")
    with idx.project("tula-a", create=True) as conn:  # opening an index never creates one unless asked (T144)
        with tx(conn):
            apply_packet_entry(
                conn,
                "tula-a",
                "pkt_a",
                {
                    "shot_id": "s01",
                    "kind": "still",
                    "tier": "A",
                    "packet_version": "0.1.1",
                    "path": "p",
                    "manifest_blake3": proj.keys["packet_manifest"],
                },
            )
            conn.execute(
                "INSERT INTO blobs (packet_id, relpath, role, size, blake3, in_cas) VALUES (?, ?, ?, ?, ?, 1)",
                ("pkt_a", "beauty.exr", "beauty", 1, proj.keys["orphan2"]),
            )
        assert purge.index_refs(conn) == {proj.keys["orphan2"]}
        rep = purge.end_of_project(proj.root, older_than_days=0, index=conn)
    assert names(proj, rep.candidates) == {"orphan"} and rep.kept["index"] == 1


def test_purge_needs_the_project_lock_but_a_dry_run_does_not(proj):
    with Lock(proj.root):
        assert purge.end_of_project(proj.root, older_than_days=0).ok  # dry run: read-only
        with pytest.raises(ProjectLocked):
            purge.end_of_project(proj.root, older_than_days=0, dry_run=False)
    assert blob_path(proj, "orphan").exists()
    with Lock(proj.root) as held:  # the daemon passes the lock it already holds
        rep = purge.end_of_project(proj.root, older_than_days=0, dry_run=False, lock=held)
        assert names(proj, rep.deleted) == {"orphan", "orphan2"} and held.held
    rep = purge.gc(proj.root, dry_run=False)
    assert rep.deleted == []


def test_a_broken_ledger_refuses_the_purge_unless_told_otherwise(proj):
    runs = proj.root / "runs.jsonl"
    lines = runs.read_bytes().split(b"\n")
    lines[3] = lines[3].replace(b"job-A", b"job-Z")  # edit a middle record: the chain breaks there
    runs.write_bytes(b"\n".join(lines))
    with pytest.raises(purge.PurgeError, match="hash chain breaks at seq 4"):
        purge.end_of_project(proj.root, older_than_days=0, dry_run=False)
    assert blob_path(proj, "orphan").exists()
    rep = purge.end_of_project(proj.root, older_than_days=0, allow_broken_ledger=True)
    assert "orphan" in names(proj, rep.candidates)


def test_stale_staging_files_are_cleaned_once_old_enough(proj):
    stale = proj.root / "assets" / ".tmp" / "stale-put"
    recent = proj.root / "assets" / ".tmp" / "live-put"
    recent.write_bytes(b"being written right now")
    old = (datetime.now(UTC) - timedelta(days=3)).timestamp()
    os.utime(stale, (old, old))
    rep = purge.end_of_project(proj.root, older_than_days=0, dry_run=False)
    assert rep.tmp_removed == ["stale-put"] and recent.exists() and not stale.exists()


# ---------------------------------------------------------------------------- vendor-side deletions


def test_vendor_deletions_lists_cloud_jobs_until_each_is_recorded(proj):
    led = Ledger(proj.root)
    owed = purge.vendor_deletions(led)
    assert [(p.run_id, p.provider_job_id, p.provider) for p in owed] == [
        ("run_A", "job-A", "atlas"),
        ("run_B", "job-B", "atlas"),
        ("run_E", "job-E", "atlas"),
        ("run_G", "job-G", "atlas"),
    ]
    assert owed[0].custody_grade == "D" and owed[0].engine_id == "seedance-2.5-i2v"
    assert owed[0].submitted_utc.startswith("2026-10-16T09:02")
    purge.record_vendor_deletion(led, run_id="run_A", provider_job_id="job-A", by="aasish", provider="atlas")
    purge.record_vendor_deletion(led, run_id="run_B", by="aasish", method="api", data={"ref": "del-77"})
    purge.record_vendor_deletion(led, run_id="elsewhere", provider_job_id="job-G", by="aasish")
    still = purge.vendor_deletions(led)
    assert [p.run_id for p in still] == ["run_E"]  # A by job, B by run, G by job id alone
    assert led.verify() == (True, None)  # the records are ordinary chained records
    rec = [r for r in led.records() if r["kind"] == "deleted_vendor_side"][1]
    assert rec["data"] == {"method": "api", "ref": "del-77"}
    assert purge.vendor_deletions(led.records()) == still  # a plain record list works too


def test_local_routes_never_left_the_box(tmp_path):
    root = tmp_path / "p"
    Project.create(root, "local-a", {"nda": True})
    led = Ledger(root)
    led.append(
        kind="quoted", run_id="r_local", custody_grade="C0", at=at(0), data={"route": {"provider": "local"}}
    )
    led.append(kind="submitted", run_id="r_local", provider_job_id="local-1", at=at(1))
    led.append(
        kind="submitted",
        run_id="r_fake",
        provider_job_id="fake-1",
        at=at(2),
        data={"route": {"provider": "fake"}},
    )
    led.append(kind="submitted", run_id="r_cloud", provider_job_id="cloud-1", at=at(3), route_id="google")
    assert [p.run_id for p in purge.vendor_deletions(led)] == ["r_cloud"]
    assert purge.vendor_deletions(led)[0].provider == "google"  # falls back to the route id
    assert {p.run_id for p in purge.vendor_deletions(led, include_local=True)} == {
        "r_local",
        "r_fake",
        "r_cloud",
    }


def test_the_purge_report_carries_the_deletions_still_owed(proj):
    rep = purge.end_of_project(proj.root, older_than_days=0)
    assert {p["provider_job_id"] for p in rep.pending_vendor_deletions} == {
        "job-A",
        "job-B",
        "job-E",
        "job-G",
    }


# ---------------------------------------------------------------------------- reindex


def test_reindex_row_counts_match_the_folder(proj, tmp_path):
    home = tmp_path / "home"
    rep = reindex.reindex(proj.root, home)
    assert rep.ok, rep.to_dict()
    assert rep.expected == {"assets": len(proj.keys), "packets": 1, "runs": 4}
    assert rep.after["assets"] == rep.expected["assets"]
    assert rep.after["packets"] == 1 and rep.after["runs"] == 4
    assert rep.missing == {} and rep.extra == {} and rep.problems == []
    assert rep.ledger["chain_ok"] and rep.before is None and rep.lost == {}
    assert "consistent" in rep.summary_line()
    with Index(home).project("tula-a") as conn:
        states = dict(conn.execute("SELECT id, state FROM runs").fetchall())
    assert set(states) == {"run_A", "run_B", "run_E", "run_G"} and set(states.values()) == {"DONE"}


def test_reindex_reports_what_the_old_index_held_that_the_folder_does_not(proj, tmp_path):
    home = tmp_path / "home"
    reindex.reindex(proj.root, home)
    with Index(home).project("tula-a") as conn:
        conn.execute("INSERT INTO events (run_id, at, kind, json) VALUES ('run_A', 'x', 'k', '{}')")
        conn.execute("INSERT INTO budget_envelopes VALUES ('env1', 'agent', 5, 1, 0, 'a', 'b')")
    again = reindex.reindex(proj.root, home)
    assert again.ok and again.before["events"] == 1
    assert again.lost == {"events": 1, "budget_envelopes": 1}


def test_reindex_sets_a_damaged_index_aside_and_rebuilds(proj, tmp_path):
    home = tmp_path / "home"
    idx = Index(home)
    db = idx.db_path("tula-a")
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a sqlite database" * 100)
    rep = reindex.reindex(proj.root, home, index=idx)
    assert rep.ok and rep.quarantined and Path(rep.quarantined).read_bytes().startswith(b"this is not")
    assert ".corrupt-" in Path(rep.quarantined).name and ":" not in Path(rep.quarantined).name
    assert rep.after["runs"] == 4


def test_reindex_flags_a_run_whose_packet_is_not_registered(proj, tmp_path):
    Ledger(proj.root).append(
        kind="quoted", run_id="run_X", at=at(60), data={"packet_id": "pkt_gone", "route": ROUTE}
    )
    rep = reindex.reindex(proj.root, tmp_path / "home")
    assert not rep.ok
    assert rep.missing == {"runs": ["run_X"]}
    assert any("run_X" in p and "pkt_gone" in p for p in rep.problems)
    assert rep.after["runs"] == 4 and "INCONSISTENT" in rep.summary_line()


def test_reindex_verify_names_a_corrupt_blob(proj, tmp_path):
    blob = blob_path(proj, "accepted")
    blob.write_bytes(b"bit rot")
    clean = reindex.reindex(proj.root, tmp_path / "home")
    assert clean.ok  # without verify the cache trusts the file names
    rep = reindex.reindex(proj.root, tmp_path / "home", verify=True)
    assert not rep.ok and rep.verified_assets
    assert rep.missing["assets"] == [proj.keys["accepted"]]
    assert any("do not match the hash" in p for p in rep.problems)


def test_reindex_refuses_while_another_handle_holds_the_project(proj, tmp_path):
    with Lock(proj.root):
        with pytest.raises(ProjectLocked):
            reindex.reindex(proj.root, tmp_path / "home")
    with Lock(proj.root) as held:
        assert reindex.reindex(proj.root, tmp_path / "home", lock=held).ok


# ---------------------------------------------------------------------------- budget alerts


def approval(rid: str, minutes: float, usd: float, by: str = "auto") -> dict[str, Any]:
    return {
        "kind": "approved",
        "run_id": rid,
        "by": by,
        "cost_usd_quoted": usd,
        "at": at(minutes).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


def rec(kind: str, minutes: float, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "at": at(minutes).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), **extra}


def kinds(result: alerts.Scan) -> list[str]:
    return [a.kind for a in result.alerts]


def test_daily_cap_near_then_exceeded_once_per_day():
    records = [approval("r1", 0, 30, "human"), approval("r2", 1, 12, "human")]
    res = alerts.scan(records, now=at(5))
    assert kinds(res) == ["daily_cap_near"]  # 42 of 50 is past 80%
    near = res.alerts[0]
    assert near.severity == "warn" and near.key == "daily_cap_near:2026-10-16"
    assert near.data["spent_usd"] == 42.0 and near.data["cap_usd"] == 50.0
    records.append(approval("r3", 2, 9, "human"))
    res = alerts.scan(records, now=at(5))
    assert kinds(res) == ["daily_cap_near", "daily_cap_exceeded"]
    assert res.alerts[1].severity == "critical" and res.alerts[1].data["occurred_utc"].startswith(
        "2026-10-16T09:02"
    )
    records.append(approval("r4", 3, 5, "human"))
    assert kinds(alerts.scan(records, now=at(5))).count("daily_cap_exceeded") == 1
    assert res.summary.level == "critical" and res.summary.today_usd == 51.0


def test_a_refunded_run_does_not_count_and_a_new_day_starts_clean():
    records = [
        approval("r1", 0, 45, "human"),
        rec("cancelled", 1, run_id="r1"),  # never submitted: the reservation is refunded
        approval("r2", 2, 10, "human"),
        approval("r3", 3, 8, "human"),
        rec("submitted", 4, run_id="r3"),
        rec("failed", 5, run_id="r3", data={"refunded": True}),
        approval("r4", 60 * 24, 49, "human"),
    ]
    res = alerts.scan(records, now=at(60 * 24 + 5))
    assert [(a.kind, a.data["day"]) for a in res.alerts] == [("daily_cap_near", "2026-10-17")]
    assert res.summary.today_usd == 49.0
    settled = [
        approval("r5", 0, 45, "human"),
        rec("submitted", 1, run_id="r5"),
        rec("failed", 2, run_id="r5"),
    ]
    assert alerts.scan(settled, now=at(5)).summary.today_usd == 45.0  # the provider had it: not a refund


def test_unattended_spend_without_a_human_and_the_cap_it_passes():
    records = [approval("r1", 0, 8), approval("r2", 5, 9, "agent:env1")]
    res = alerts.scan(records, now=at(10))
    assert kinds(res) == ["unattended_spend"]
    a = res.alerts[0]
    assert (
        a.data["approvals"] == 2 and a.data["since_human_usd"] == 17.0 and a.key == "unattended_spend:never"
    )
    records.append(approval("r3", 8, 4, "agent:env1"))
    res = alerts.scan(records, now=at(10))
    assert kinds(res) == ["unattended_spend", "unattended_cap_exceeded"]
    assert res.alerts[1].data["occurred_utc"].startswith("2026-10-16T09:08")
    assert res.summary.level == "critical" and res.summary.unattended_now
    assert "USD 21.00 of 20.00 unattended (no human present)" in res.summary.line


def test_a_human_resets_the_window_and_attended_auto_spend_is_not_unattended():
    records = [
        rec("human_minutes", 0, run_id="r0", data={"minutes": 5}),
        approval("r1", 10, 15),  # 10 minutes after a human: attended
        approval("r2", 20, 3),
    ]
    assert kinds(alerts.scan(records, now=at(25))) == []
    records += [approval("r3", 100, 6), rec("override", 101, reason="shipping it"), approval("r4", 102, 4)]
    res = alerts.scan(records, now=at(110))
    # r3 came 100 minutes after the first human: unattended (spend since then 24); the override resets
    assert kinds(res) == ["unattended_spend", "unattended_cap_exceeded"]
    assert res.alerts[0].data["unattended_usd"] == 6.0 and res.alerts[0].data["since_human_usd"] == 24.0
    assert res.summary.unattended_usd == 4.0 and not res.summary.unattended_now


def test_human_means_a_named_person_not_the_system():
    for by, human in (
        ("human", True),
        ("aasish", True),
        ("auto", False),
        ("agent:e1", False),
        ("reconcile", False),
    ):
        assert alerts._is_human({"kind": "approved", "by": by}) is human, by
    assert alerts._is_human({"kind": "human_minutes"}) and alerts._is_human({"kind": "override"})
    assert not alerts._is_human({"kind": "quoted", "by": "aasish"})


def bare_project(tmp_path: Path) -> Path:
    root = tmp_path / "bare"
    Project.create(root, "bare-a", {"nda": False})
    return root


def test_run_writes_each_alert_once_in_the_engine_alert_shape(tmp_path):
    root = bare_project(tmp_path)
    led = Ledger(root)
    for i in range(3):
        led.append(kind="approved", run_id=f"r{i}", by="auto", cost_usd_quoted=9.0, at=at(i))
    led.append(kind="approved", run_id="r9", by="aasish", cost_usd_quoted=30.0, at=at(5))
    first = alerts.run(root, now=at(10))
    assert first.written == len(first.alerts) == 3
    assert kinds(first) == ["daily_cap_exceeded", "unattended_spend", "unattended_cap_exceeded"]
    path = root / alerts.ALERTS_JSONL
    raw = path.read_bytes()
    assert raw.isascii() and b"\r" not in raw and raw.endswith(b"\n")
    lines = [json.loads(x) for x in raw.decode("ascii").splitlines()]
    for x in lines:
        assert set(x) == {"v", "at", "kind", "severity", "engine_id", "route_id", "message", "data"}
        assert x["v"] == 1 and x["at"] == "2026-10-16T09:10:00Z" and x["engine_id"] is None
        assert x["data"]["project_id"] == "bare-a" and x["data"]["key"]
    assert [x["severity"] for x in lines] == ["critical", "warn", "critical"]
    again = alerts.run(root, now=at(30))
    assert again.written == 0 and path.read_bytes() == raw  # idempotent: the scan can run every night
    assert [a["data"]["key"] for a in alerts.read_alerts(path)] == [x["data"]["key"] for x in lines]
    with open(path, "ab") as fh:
        fh.write(b'{"v": 1, "torn')  # a torn last write is skipped by readers
    assert len(alerts.read_alerts(path)) == len(lines)
    elsewhere = alerts.run(root, now=at(31), alerts_path=tmp_path / "out" / "a.jsonl")
    assert elsewhere.written == 3 and (tmp_path / "out" / "a.jsonl").is_file()


def test_summary_line_for_the_status_bar(tmp_path):
    root = bare_project(tmp_path)
    led = Ledger(root)
    assert alerts.summary_line(root, now=at(1)) == (
        "Budget: USD 0.00 of 50.00 today; USD 0.00 of 20.00 unattended (no human present)"
    )
    led.append(kind="approved", run_id="r1", by="aasish", cost_usd_quoted=41.0, at=at(0))
    st = alerts.status(root, now=at(2))
    assert st.level == "warn" and st.today_usd == 41.0 and not st.unattended_now and st.active_alerts == 1
    assert st.line == "Budget: USD 41.00 of 50.00 today; USD 0.00 of 20.00 unattended; 1 active alert(s)"
    assert not (root / alerts.ALERTS_JSONL).exists()  # status never writes


# ---------------------------------------------------------------------------- the module CLI


def test_cli_backup_drill_and_exit_codes(proj, tmp_path, capsys):
    dest = tmp_path / "bk"
    assert tools_main(["backup", str(proj.root), str(dest)]) == 0
    assert "backup ok" in capsys.readouterr().out
    report = tmp_path / "drill.json"
    assert tools_main(["drill", str(dest), "--tmp", str(tmp_path / "scratch"), "--report", str(report)]) == 0
    assert "PASSED" in capsys.readouterr().out and json.loads(report.read_text(encoding="utf-8"))["ok"]
    victim = blob_path(proj, "accepted")
    (dest / "assets" / victim.parent.name / victim.name).write_bytes(b"x" * victim.stat().st_size)
    assert tools_main(["drill", str(dest), "--tmp", str(tmp_path / "scratch"), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["failures"][0]["problem"] == "hash differs"
    assert tools_main(["backup", str(tmp_path / "nowhere"), str(tmp_path / "bk2")]) == 3
    with pytest.raises(SystemExit) as ei:
        tools_main(["backup"])
    assert ei.value.code == 4


def test_cli_purge_reindex_alerts_and_vendor_commands(proj, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "home"))
    assert tools_main(["purge", str(proj.root), "--older-than-days", "0"]) == 0
    out = capsys.readouterr().out
    assert "would delete 2 of 10 blobs" in out and "4 vendor-side deletion(s) still owed" in out
    with Lock(proj.root):
        assert tools_main(["purge", str(proj.root), "--older-than-days", "0", "--apply"]) == 3
    capsys.readouterr()
    assert tools_main(["purge", str(proj.root), "--older-than-days", "0", "--apply", "--no-keep-ledger"]) == 0
    assert "deleted 3 of 10" in capsys.readouterr().out
    assert tools_main(["reindex", str(proj.root)]) == 0
    assert "consistent" in capsys.readouterr().out
    assert tools_main(["alerts", str(proj.root), "--write"]) == 0
    assert "Budget:" in capsys.readouterr().out
    assert tools_main(["vendor-deletions", str(proj.root)]) == 0
    assert "4 vendor-side deletion(s) owed" in capsys.readouterr().out
    assert (
        tools_main(["vendor-deleted", str(proj.root), "--run", "run_A", "--job", "job-A", "--by", "aasish"])
        == 0
    )
    capsys.readouterr()
    assert tools_main(["vendor-deletions", str(proj.root), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["pending"]) == 3


def test_the_index_is_never_inside_a_backup(proj, tmp_path):
    home = tmp_path / "home"
    reindex.reindex(proj.root, home)
    assert any(home.rglob("*.sqlite"))
    rep = backup.run(proj.root, tmp_path / "bk")
    assert rep.files and not list((tmp_path / "bk").rglob("*.sqlite*"))
    conn = sqlite3.connect(str(home / "index" / "tula-a.sqlite"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 4
    finally:
        conn.close()
