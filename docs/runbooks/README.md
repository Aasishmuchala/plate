# Runbooks

Short, ordered procedures for the things that go wrong on a delivery day. Each one starts with how to tell it is this problem, then what to do in order, then what to record. The rule behind all of them: **no runbook bypasses the quote, the custody check, the gates or the ledger.** If a step below would, stop and ask.

| runbook | use it when |
|---|---|
| [daemon-wont-start.md](daemon-wont-start.md) | the daemon exits at start, the page says token missing, the plugin gets 401 or 403 |
| [vendor-down.md](vendor-down.md) | a vendor is down, slow or refusing, and a deadline is close |
| [key-revoked.md](key-revoked.md) | an API key was revoked, leaked, expired or never set |
| [vram-oom.md](vram-oom.md) | a local model (Qwen-Image-Edit, Wan 2.2, Real-ESRGAN) is refused or runs out of GPU memory |
| [schema-change.md](schema-change.md) | a nightly check says a vendor changed its request or response schema |
| [max-update-breaks-dock.md](max-update-breaks-dock.md) | a 3ds Max, V-Ray or Windows update broke the PLATE dock or Send |
| [manual-pipeline-fallback.md](manual-pipeline-fallback.md) | PLATE itself is down and the work must still go out |
| [backup-restore.md](backup-restore.md) | backing up, the quarterly restore drill, end-of-project purge, vendor-side deletions |
| [nightly.md](nightly.md) | what the nightly jobs do and how to read their output |

`backup-restore.md` and `nightly.md` are written by the tooling tasks that own those jobs (backup, purge and reindex; the scheduler); they are linked here so the set is complete in one place.

**Where to look first, always.** Daemon logs: `%LOCALAPPDATA%\Plate\logs\daemon.out.log` and `daemon.err.log` on Windows, the terminal on a Mac. A run's own story: its event log and the project's `runs.jsonl`. What left the box: the project's `outbound.jsonl`. Alerts: `alerts.jsonl` in the app-data `logs` folder (the nightly jobs write `logs/nightly/nightly_<date>.json` beside it).
