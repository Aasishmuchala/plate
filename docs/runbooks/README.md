# Runbooks

Short, ordered procedures for the things that go wrong on a delivery day. Each one starts with how to tell it is this problem, then what to do in order, then what to record. The rule behind all of them: **no runbook bypasses the quote, the custody check, the gates or the ledger.** If a step below would, stop and ask.

| runbook | use it when |
|---|---|
| [daemon-wont-start.md](daemon-wont-start.md) | the daemon exits at start, the page says token missing, the plugin gets 401 or 403 |
| [vendor-down.md](vendor-down.md) | a vendor is down, slow or refusing, and a deadline is close |
| [key-revoked.md](key-revoked.md) | an API key was revoked, leaked, expired or never set |
| [runway.md](runway.md) | a Runway video run was refused, throttled, failed or left in an unknown state; the first live smoke and the two hosts to record; the way to C2 |
| [vram-oom.md](vram-oom.md) | a local model (Qwen-Image-Edit, Wan 2.2, Real-ESRGAN) is refused or runs out of GPU memory |
| [schema-change.md](schema-change.md) | a nightly check says a vendor changed its request or response schema |
| [max-update-breaks-dock.md](max-update-breaks-dock.md) | a 3ds Max, V-Ray or Windows update broke the PLATE dock or Send |
| [manual-pipeline-fallback.md](manual-pipeline-fallback.md) | PLATE itself is down and the work must still go out |
| [backup-restore.md](backup-restore.md) | backing up, the quarterly restore drill, end-of-project purge, vendor-side deletions |
| [nightly.md](nightly.md) | what the nightly jobs do and how to read their output |
| [weather-and-season.md](weather-and-season.md) | a client wants rain, mist, a season or an occasion: which recipe, which classes, what needs a different V-Ray packet |
| [two-point-perspective.md](two-point-perspective.md) | a V-Ray camera has vertical tilt correction or a vertical shift: level-and-crop, the taller packet, the box measurement |
| [what-goes-back-to-max.md](what-goes-back-to-max.md) | a change lives in a locked pixel (decorations, fixtures, wet walls, tiles, cars, branches, paving): the Max work and the AI finishing after it |

`backup-restore.md` and `nightly.md` are written by the tooling tasks that own those jobs (backup, purge and reindex; the scheduler); they are linked here so the set is complete in one place.

**Where to look first, always.** Daemon logs: `%LOCALAPPDATA%\Plate\logs\daemon.out.log` and `daemon.err.log` on Windows, the terminal on a Mac. A run's own story: its event log and the project's `runs.jsonl`. What left the box: the project's `outbound.jsonl`. Alerts: `alerts.jsonl` in the app-data `logs` folder (the nightly jobs write `logs/nightly/nightly_<date>.json` beside it).
