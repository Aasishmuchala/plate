# Runbook: the nightly jobs

Every night PLATE refreshes prices, checks that pinned models still exist, replays every adapter's recorded fixtures, reconciles yesterday's vendor bills against the ledger, and backs the projects up. Once a week it also re-runs the canaries. All of it is the daemon's `Nightly` scheduler (`daemon/plate_daemon/scheduler.py`). This page says what each job does, where it writes, how to read the result, and what to do when one raises an alert.

None of it spends money on its own. The default run reads local files only (vendor catalogue JSON, billing CSVs, recorded fixtures). A live vendor call happens only with `PLATE_LIVE=1`, a budget (`live_budget_usd`), and cases someone registered on purpose.

## When it runs

Once per local date, from `02:00` local time. The scheduler is a `threading.Timer` on a daemon thread that never sleeps longer than 15 minutes, so a laptop that slept through 02:00 or a clock change is noticed at the next wake-up. If the daemon starts after 02:00 and today has not run, it runs 30 seconds after start (`catch_up`, on by default).

A date is **done** when its record file says `"done": true`. A done date is never run again by the timer. A crash in the middle is not a lost night: the record is rewritten after every job, so the next tick runs only the jobs that have no result yet. An unfinished date from an earlier day is not resumed after midnight; run it by hand (below).

## The jobs, in order

| job | what it does | alerts it can raise |
|---|---|---|
| `prices_refresh` | Compares the vendor catalogue files with the prices in force and rewrites the price overlay (`manifests/_prices.yaml`, or `PLATE_PRICES_OVERLAY`) only when something moved. A move of more than 5%, up or down, flags every QUOTED run on that engine (and tier) for a requote. | `price_change`, `requote`, `unit_mismatch` |
| `retire_check` | A model id that a manifest or a project pins and that no catalogue lists any more, or a pinned version that is no longer served. | `retired_pin` |
| `contract_tests` | Runs the recorded-fixture contract suites of every adapter (a child pytest over `engines/tests`: Atlas quirks, transport, image and video adapters). With `PLATE_LIVE=1` and a budget it also runs the cheapest live cases and the output-depends-on-input check. Registered schema pairs go through `schema_diff`. | `contract_test_failed`, `schema_change` |
| `reconcile` | Yesterday's UTC day of billing CSVs against every project's ledger. Matched bills are written into the owning project's ledger once (`billed` records). Over 1% on any engine, or any unledgered spend, fails the day. | `reconcile_failed` |
| `canaries` | Weekly. Twelve owned plates and four moves through every engine, compared with the previous report; a pass rate that fell by more than 15 points, or a changed model string, suspends the engine. | `canary_drift`, `engine_suspended` |
| `backup` | `plate_daemon.tools.backup.run` for each project into `<backup_dir>/<project_id>/<date>`. | `backup_failed` |

A job whose input is not set up is **skipped**, with the reason in the record: no catalogue files, no billing exports, no canary set registered, no `backup_dir`, no backup tool installed. Skipped is not an error and raises no alert. If a job raises an exception, its status is `error`, a `job_error` alert is written, and the rest of the night still runs. An errored job is not retried the same date.

## Where the output is

All under the app home's `logs` folder (`%LOCALAPPDATA%\Plate\logs` on Windows, `~/Library/Application Support/Plate/logs` on a Mac):

| file | what |
|---|---|
| `nightly/nightly_<YYYY-MM-DD>.json` | The record of that date: each job's `status` (`ok`, `alerted`, `skipped`, `error`), start and finish time, a message, a `summary` with the numbers, and the alert lines the job wrote. `done` is the marker. |
| `nightly/canaries_<YYYY-MM-DD>.json` | The canary report the next weekly run is compared with. Keep it. |
| `alerts.jsonl` | The shared alert stream, one JSON line per alert. The router's health board reads engine suspensions from here. |

`alerts.jsonl` carries two families of kinds. The engine kinds (`price_change`, `requote`, `unit_mismatch`, `retired_pin`, `schema_change`, `canary_drift`, `engine_suspended`, `engine_resumed`) are written by `plate_engines.health.AlertLog`. The nightly kinds (`job_error`, `contract_test_failed`, `reconcile_failed`, `backup_failed`) are written by the scheduler in the same line shape. `AlertLog.read()` skips lines whose kind it does not know, so to see everything use `plate_daemon.scheduler.read_alerts(path)`.

The quickest read of a night is the record file: open `nightly_<date>.json` and look at each job's `status` first. `alerted` means open its `alerts` list. The Python seam `Nightly.status()` returns the same in one dictionary for the UI status bar.

## Configuration

An optional `"nightly"` object in `config.json`. Every key is optional; an unknown key is refused when the scheduler is created, so a typo cannot silently turn a job off.

```json
{
  "nightly": {
    "enabled": true,
    "time": "02:00",
    "catch_up": true,
    "window_h": 4,
    "poll_s": 900,
    "startup_delay_s": 30,
    "catalogue_dir": "catalogues",
    "billing_dir": "billing",
    "backup_dir": null,
    "prices_overlay": null,
    "live_budget_usd": 0,
    "canary_every_days": 7,
    "record_billed": true,
    "disabled_jobs": []
  }
}
```

Relative folders are relative to the app home. `time` is local wall-clock time. With `catch_up` off, a date is skipped once the clock is more than `window_h` hours past `time`. `PLATE_NIGHTLY_TIME=03:30` overrides the time and `PLATE_NIGHTLY=0` turns the scheduler off.

What each folder holds:

- `catalogue_dir`: one JSON file per vendor, in the format documented in `engines/plate_engines/prices.py` (`vendor`, `as_of`, `models: [{id, unit, usd, tiers?, available?}]`). A person or a later live fetch job drops them here. Both `prices_refresh` and `retire_check` read it.
- `billing_dir`: the vendors' billing exports as `openai.csv`, `google.csv` and `atlas.csv` with the columns `at,provider_job_id,model,usd` (see `plate_daemon/reconcile.py`). Drop each day's export in before 02:00; the night reconciles the UTC day before.
- `backup_dir`: where `backup` writes. Leave it null to run no backup from the scheduler.

## Running it by hand

Save this as a script (a one-liner breaks on PowerShell quoting) and run it with the repo's Python:

```python
import json
from plate_daemon.config import Settings
from plate_daemon.scheduler import Nightly

nightly = Nightly(Settings.load())
record = nightly.run_now(["reconcile"], force=True)   # one job; omit the list for all of them
print(json.dumps({k: v["status"] for k, v in record["jobs"].items()}, indent=1))
```

`force=True` reruns a job that already has a result today. Without `force`, only jobs with no result are run. `run_now` ignores the 02:00 window and blocks while another run is in progress.

## When an alert lands

**`requote`.** A price moved more than 5%, so quotes made at the old price are stale. The alert names each run. Quotes already expire after 15 minutes, so in practice the runs to look at are the ones sitting QUOTED in a project; ask for a fresh quote before approving. The new price is in the overlay file and in the `prices_refresh` summary.

**`price_change` without a `requote`.** Informational: a move of 5% or less. Nothing to do. `unit_mismatch` means the vendor now quotes in a different unit than the manifest's price model, so that entry was skipped rather than guessed; fix the manifest.

**`retired_pin`.** A pinned model is gone from the vendor's catalogue. Nothing is swapped silently. Decide the replacement, re-pin, and re-quote. See `vendor-down.md` if work is in flight.

**`contract_test_failed`.** An adapter no longer matches its recorded fixture, or (live) a vendor returned the same output for two different inputs, which means it ignored the input. The message names the test or the engine. Run `.venv/bin/pytest engines/tests -q` to reproduce. A live failure on a recorded-green adapter is a vendor change: see `schema-change.md`.

**`schema_change`.** A required field was removed or an enum changed. Follow `schema-change.md`.

**`reconcile_failed`.** The ledger and the vendor bills disagree by more than 1% on an engine, or a billing row matches no ledger run (unledgered spend). The alert carries the failures and the unledgered total; the record's `summary.report` lists the unmatched rows with their vendor job ids. Find the call that was made outside PLATE (a key used elsewhere, a call from a script) or the run that never reached the ledger. A second message of the same kind with severity `warn` means the bills were matched but could not be written into a project's ledger (the project lock was held); rerun `reconcile` with `force=True` once the project is closed.

**`canary_drift` and `engine_suspended`.** The engine's pass rate fell more than 15 points or its model string changed, and the router now skips it. Look at `canaries_<date>.json` against the previous one. When you have decided the new behaviour is acceptable (or the vendor fixed it), resume it: `AlertLog(alerts_path).resume("<engine-id>", "reason")`. A person resumes an engine; the scheduler never does.

**`backup_failed`.** The destination was unreachable or the backup tool raised. The message names the project. Fix the destination and run `backup` by hand with `force=True`. Do not rely on a night with a `backup_failed` for a restore.

**`job_error`.** A job raised. The message is the exception. The record's job entry has the same text. Fix the cause and rerun that job with `force=True`.

## What this does not do

It does not approve or run anything, and it never resumes a suspended engine. It does not fetch vendor catalogues or billing exports by itself: the folders above are filled by a person or by a fetcher passed to `Nightly(catalogue_fetcher=...)`, which runs only under `PLATE_LIVE=1`. Canaries run only when a canary set is registered (`Nightly(canary_set=...)`); until the box day wires real adapters and gates there is nothing to run and the job is skipped.
