# Runbook: backup, restore drill, purge, reindex and budget alerts

The maintenance tools live in `daemon/plate_daemon/tools/`. They work on a project **folder** (the truth: `project.json`, `assets/`, `runs.jsonl`, `gates/`, `review/`, `packets.json`), never on the SQLite index, which is a per-machine cache that `reindex` rebuilds. The nightly `backup` job (see [nightly.md](nightly.md)) calls the same `backup.run`. This page is what to do by hand: back up, prove the backup restores, restore after a loss, close a project out, and read the budget alerts.

BUILD_PLAN decision 23 is the policy: nightly encrypted offsite copy, ledger as a hash chain, vendor-side deletions tracked per job, reference-counted purge at the end of a project, a restore drill once a quarter.

## Running the tools

From the repo root, with the project folder as the argument (never the index). macOS:

```
PYTHONPATH=spec:daemon .venv/bin/python -m plate_daemon.tools --help
```

Windows PowerShell:

```powershell
$env:PYTHONPATH = "spec;daemon"; $env:PYTHONUTF8 = "1"
.venv\Scripts\python -m plate_daemon.tools --help
```

Commands: `backup`, `drill`, `purge`, `reindex`, `alerts`, `vendor-deletions`, `vendor-deleted`. Add `--json` after the command for the full report. Exit codes: 0 ok, 2 a check failed (a drill problem, an inconsistent index, the offsite command failing), 3 refused (project locked, broken ledger, not a project folder), 4 usage. The examples below write `plate-tools` for the whole `python -m plate_daemon.tools` line.

## What a backup holds, and what it leaves out

Copied: everything in the project folder. Left out on purpose: `_work/` (scratch, at any depth), the SQLite index (`*.sqlite`, `-wal`, `-shm`), `assets/.tmp/` (half-written blobs), lock files, temp files of an atomic write in flight, and symlinks. The index is not in the project folder to begin with (WAL is unsafe on SMB), so a backup never carries it.

**`lease.json` is not project content.** It is the running daemon's lease on the folder (host, pid, heartbeat; [daemon-wont-start.md](daemon-wont-start.md), section I). The backup tool copies it like any other file today, since its skip list holds the lock files (`*.lock`, `*.lock.json`, `*.lock.plate`) and the SQLite and temp files, not `lease.json`, so a backup can carry a lease that names a daemon that is no longer there: **never restore it** (the steps below leave it out), and delete it from any copy of a project folder.

A backup is a folder: the same layout as the project plus `backup_manifest.json`, which lists every file with its size and BLAKE3, and the ledger head (record count and hash). It is read-only on the project: it does not take `project.lock`, so it is safe to run while the daemon has the project open. A ledger line caught mid-append is a torn tail that every reader skips.

```
plate-tools backup D:\Projects\clubhouse E:\backups\clubhouse
```

Run it again into the same folder and it is incremental: a CAS blob already there at the right size is reused (its file name is its hash, so it cannot have changed), everything else is copied again, and files that an earlier manifest listed and the project has since purged are removed. Nothing else in the backup folder is ever deleted. The nightly job gives each night a new dated folder, so for a very large project point it at one stable folder per project if you want the incremental behaviour.

## Offsite copy (documented here, not built in)

The tool does not link an uploader or an encryption library. After the local copy and the manifest are written it runs one command you supply, with no shell: `--cmd` on the line, else the `PLATE_BACKUP_CMD` environment variable. Each argument has `{src}` (the project folder) and `{dest}` (the backup folder just written) replaced. A string is split like a shell would (quotes keep a path with spaces whole); an exit code other than 0 raises `backup_failed`, and the local copy is still complete and verified.

One-time setup with rclone and an encrypted Backblaze B2 remote (or any equivalent):

1. `rclone config`: create the remote `b2` (account id, application key, a private bucket such as `sthyra-plate-backups`).
2. `rclone config` again: create a **crypt** remote named `b2crypt` whose remote is `b2:sthyra-plate-backups`, filename encryption on, and set both the password and the salt.
3. **Store the crypt password and salt somewhere that is not the render box** (the company password manager, plus a printed copy in the safe). Without them the offsite copy is unreadable. rclone cannot recover them.
4. On the bucket, turn on versioning or object lock with a retention window, so a compromised box cannot delete the only offsite copy.
5. Test by hand: `rclone lsd b2crypt:` and a small `rclone copy`.

Then the command is, on Windows PowerShell (double quotes stay inside the single-quoted string):

```powershell
$env:PLATE_BACKUP_CMD = 'rclone sync "{dest}" "b2crypt:clubhouse" --checksum --transfers 4'
plate-tools backup D:\Projects\clubhouse E:\backups\clubhouse
```

and on a Mac `export PLATE_BACKUP_CMD='rclone sync "{dest}" b2crypt:clubhouse --checksum'`. `rclone sync` makes the remote match the folder, which is why the backup removes purged files locally first. Put `PLATE_BACKUP_CMD` where the daemon's scheduled task reads its environment if the nightly job should ship offsite.

## The quarterly restore drill

Do this in the first week of January, April, July and October, and after any change to the backup command. A backup nobody has restored is a hope.

1. Pull the **offsite** copy to a scratch disk, not the local staging folder: `rclone copy b2crypt:clubhouse E:\drill\clubhouse --checksum`. That also proves the crypt password and salt still work.
2. Run the drill on that folder, with a scratch folder for the restored project (it needs room for a full copy; `--no-restore` hashes in place when there is not):

```
plate-tools drill E:\drill\clubhouse --tmp E:\drill\scratch --report E:\backups\_drills\2026-10-clubhouse.json
```

3. Read the last line. `restore drill PASSED: N/N files, B bytes verified` is a pass. `FAILED` names each file and the problem (`missing from the backup`, `size differs`, `hash differs`, a ledger chain that does not verify, a ledger that ends at a different head than the manifest).
4. A pass means every file matched the manifest, the restored folder opens as a project, and its ledger hash chain walks from the first record to the head the manifest recorded. The scratch copy is deleted; add `--keep` to look at it.
5. Keep the report JSON. Note the date and the result in the drill log.

**If it fails:** do not delete anything. Re-run the backup from the project (`backup`), pull again and re-drill. If the same file fails twice, the corruption is in the project or the transport: compare the file's hash in the project with the manifest, and check the disk and the rclone log. A `warnings` entry that says a blob's "bytes do not hash to the object's name" means the blob was already damaged in the project before the backup; restore it from an earlier backup.

## Restoring after a loss

1. Pull the offsite copy to a new folder and run the drill on it (above). Do not restore from a copy that fails.
2. Copy the folder's contents, minus `backup_manifest.json` and `lease.json`, to where the project lives (a short path: the CAS stays under Windows MAX_PATH). If a `lease.json` came along, delete it. A restored lease names a daemon that is not running there and carries the heartbeat of the day it was backed up, so the first daemon to write takes it over at once with a warning; but a plain copy of a folder whose daemon is still running (or stopped less than 90 s ago) carries a live heartbeat, and the copy is refused `project_leased` until that heartbeat lapses.
3. Rebuild the index on the machine that will run the project: `plate-tools reindex <project>`. The line should say `consistent`. See the next section for what it reports.
4. Open the project. Everything the folder holds is back: graph, assets, ledger, gate reports, review bundles. What only lived in the old index (quotes, budget envelopes, run and packet event logs, the SSE log) is gone, and `reindex` lists it under `lost`.

## Reindex

The SQLite index is rebuilt from the folder: `project.json`, `packets.json`, `assets/` and `runs.jsonl`. Use it after a restore, a move to another machine, or when the index is damaged (a damaged file is set aside as `<name>.sqlite.corrupt-<time>` and the rebuild starts clean).

```
plate-tools reindex D:\Projects\clubhouse          (add --verify to re-hash every blob; slow on a big project)
```

It takes the project lock first. The lock is held only while something writes (a graph save, a ledger line), not while the daemon has the project open, so it is usually free: if a write is in flight you get `refused: ... locked by another PLATE process (host pid N since T)`, and a few seconds later the same command works. Because the lock is per write, it does not stop the daemon from starting a run right after the tool lets go: stop the daemon before a reindex that must see a still project. The lease does not stop the tool either (`reindex` and `purge` install no lease identity, so a project another daemon holds is still rebuilt), which is one more reason to stop that daemon first. A rebuild leaves the index's `packet_router` table empty; the next quote of each packet refills it ([../engines.md](../engines.md)). The report compares the ids the folder holds with the ids in the new index (assets, packets, runs). `INCONSISTENT` means something the folder holds is not in the index, usually a run whose packet is not in `packets.json` or a blob that fails `--verify`; the problems are listed by id.

## End-of-project purge

Closes a project out: deletes content-addressed blobs nothing needs, in the order below. Never skip the dry run.

1. Export the deliverables and check the gate reports are shipped.
2. Back up and run the restore drill on that backup. Purging is the one step that cannot be undone from the project folder.
3. Dry run (the default; it changes nothing and writes nothing):

```
plate-tools purge D:\Projects\clubhouse --older-than-days 30
```

It prints how many blobs it would delete and how many vendor-side deletions are still owed. `--json` lists every candidate with its hash, extension, size and age.

4. Decide what to keep. By default a blob is kept when the graph, `packets.json`, a gate report or **any** ledger record names it, so only true orphans go. For the end of a project add `--no-keep-ledger`: then the candidates nobody chose (re-rolled takes) also go, while the graph's picks, accepted and exported runs (with their inputs) and the gate reports stay. `--no-keep-masters` and `--no-keep-gate-reports` drop those protections too; use them only when the deliverables are archived elsewhere.
5. Apply it: `plate-tools purge D:\Projects\clubhouse --older-than-days 30 --no-keep-ledger --apply`. It takes the project lock for the whole apply, so the daemon's writes wait (a save is refused `project_locked`, and the canvas retries) until it finishes; the lock cannot stop a run that is already in flight from adding a blob the purge has not seen, so stop the daemon first anyway. It writes `purge_<time>.json` in the project folder: every deleted hash, its size and age, what was kept and why, and the vendor-side deletions still owed. `runs.jsonl` is never touched. A ledger whose hash chain does not verify refuses the purge, because references in damaged records cannot be seen.
6. Back up again (the backup removes the purged files from the offsite copy at the next `sync`).

A blob must be older than `--older-than-days` (by file time, which a backup and restore keep), so a take generated this morning is never a candidate.

## Vendor-side deletions

Every cloud run leaves files at the vendor (Atlas upload URLs are public and its retention terms are not held: see `docs/custody.md`). Each deletion is tracked per job in the ledger, as a `deleted_vendor_side` record.

```
plate-tools vendor-deletions D:\Projects\clubhouse                      lists the jobs still owed
plate-tools vendor-deleted D:\Projects\clubhouse --run run_01H... --job pred_a1b2 --by aasish --provider atlas --method manual
```

The first lists each run and provider job id that reached a vendor and has no deletion record; runs on a local route (custody C0) never left the box and are not listed. Delete the files in the vendor's console or API, then run the second command for each. A record naming only `--run` clears all of that run's jobs; naming only `--job` clears that job. Do this before the purge, and keep the list empty at project close.

## Budget alerts

`plate-tools alerts <project> --write` scans the ledger and appends to `<project>/alerts.jsonl`; the scan is also a library call (`plate_daemon.tools.alerts.run` and `status`) for the scheduler or the status-bar route to use; until one of them calls it, run it by hand or from a scheduled task. It writes each alert once (a key such as `daily_cap_exceeded:2026-10-16`), so running it every night never repeats one.

| alert | level | meaning |
|---|---|---|
| `daily_cap_near` | warn | a UTC day's approved spend reached 80% of the daily cap |
| `daily_cap_exceeded` | critical | it passed the cap (a human approval can; the auto-approve check only gates auto and agent runs) |
| `unattended_spend` | warn | auto or agent approvals were made with no human present |
| `unattended_cap_exceeded` | critical | auto and agent spend since the last human passed the unattended cap |

The status-bar line looks like `Budget: USD 12.40 of 50.00 today; USD 3.00 of 20.00 unattended (no human present); 1 active alert(s)`. The caps are the `budgets.Policy` defaults (USD 50 a day, USD 20 unattended after 30 minutes without a human). The scan counts a run's approved price as spent unless the run was refunded (cancelled or failed before it reached the vendor). The ledger only sees approvals, accepts, overrides and logged human minutes, so a person who only browses looks absent: the scan can be stricter than the live check, never looser. The same goes for money declared on an import: the live caps leave it out (it was spent elsewhere), but the scan reads the ledger's approvals and cannot tell an import from a generation, so a large declaration can still raise a `daily_cap_exceeded` line.

On a critical alert: find the runs (`runs.jsonl`, kind `approved`, `by` of `auto` or `agent:...`), cancel anything still queued, and have a human look at the envelope or the cap before any more agent work runs.

## When something fails

| you see | cause | do |
|---|---|---|
| `refused: ... locked by another PLATE process` | the daemon or another tool was writing at that instant: `project.lock` is taken per write, not held while a project is open (a second daemon is kept out by the lease, not by this lock: `project_leased` in [daemon-wont-start.md](daemon-wont-start.md), section I) | wait a few seconds and run it again; for `purge --apply` and a `reindex` that must see a still project, stop the daemon first. `backup` and dry-run `purge` do not need the lock |
| `backup copied but the command failed` | the offsite command exited non-zero or was not found (exit 127) | the local copy is fine; fix the command or the remote, run `backup` again; read `stderr_tail` in the report |
| `the backup folder and the project folder must not contain each other` | the destination is inside the project (or the reverse) | choose a destination on another disk |
| `restore drill FAILED` | see the drill section | do not delete the backup; re-run the backup and re-drill |
| `the ledger's hash chain breaks at seq N` (purge) | someone edited or damaged `runs.jsonl` | restore the ledger from a backup; the chain is evidence, do not repair it by hand |
| `reindex ... INCONSISTENT` | the folder holds ids the index could not take | read the `problems` list; most often a run whose packet is missing from `packets.json` |
