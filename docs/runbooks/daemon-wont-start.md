# Runbook: the daemon will not start, or will not talk to you

## Tell which problem it is

| what you see | meaning | go to |
|---|---|---|
| `plate_daemon: another daemon already runs on <folder>` (exit 3) | a second daemon, or one that has not released the folder yet | A |
| `cannot listen on ['127.0.0.1'] port 4610: ...` (exit 2) | something else owns the port | B |
| `plate_daemon: ...config.json: not valid JSON` / `PLATE_TOKEN_FILE=... cannot read it` (exit 2) | the config or token file is damaged | C |
| `the SQLite index must live on a local disk` | `PLATE_HOME` points at a network share | D |
| the page loads but the status bar says `token: missing` | the page was opened without `#token=` | E |
| the page says `UI not built: cd ui && npm run build` | the daemon is fine, the UI bundle is not built | F |
| Max's dock says `Token rejected` (401) | the plugin and daemon disagree on the token | G |
| 403 on every request from a browser | Host or Origin not on the allowlist | H |
| a project says it is locked by another host | another machine has the project open | I |

## A. Another daemon is running

1. Look for a daemon you forgot: the terminal where you started it, or on Windows the scheduled task (`scripts\register-daemon-task.ps1` starts one at logon). Only one daemon may use an app-data folder.
2. If you just killed one, wait 10 to 15 seconds: Windows can take a moment to release the lock, and the daemon retries for ten seconds by itself.
3. If it persists with no daemon running, reboot the box rather than deleting lock files by hand.

## B. The port is taken

Another program (or a second PLATE with a different `PLATE_HOME`) is on 4610. Start on another port with `scripts\start-daemon.ps1 -Port 4620` (Windows), `scripts/start-daemon.sh --port 4620` (Mac) or `PLATE_PORT`. The Max plugin and the browser must then use the new URL: the daemon prints it, and `config.json` holds `daemon_url`.

## C. Damaged config or token file

`config.json` is plain JSON with `token` and `daemon_url` (and the plugin's `repo_path`, `packets_dir`). Fix the syntax, or move the file aside: the daemon writes a fresh one with a new token, after which the Max plugin needs the new token (re-run `plugins\max\scripts\install.ps1`, which keeps what it can). `PLATE_TOKEN_FILE` must name a readable file holding only the token, at least 16 characters, and it wins over `config.json`.

## D. Index on a share

The daemon keeps one SQLite index per project in the app-data folder and refuses to put it on SMB (the write-ahead log is unsafe there). Unset `PLATE_HOME` or point it at a local folder. Projects themselves may live on the NAS; only the index may not.

## E. Token missing in the browser

Open the full URL the daemon printed: `http://127.0.0.1:4610/#token=...`. `scripts/plate_config.py url` prints it again. A new tab or window needs it again; a reload does not.

## F. UI not built

`npm run build` from the repo root (or `cd ui && npm run build`), then reload. For development, `npm run dev` serves on 5173 and needs the daemon started with `PLATE_DEV=1`.

## G. Plugin gets 401

Both read `config.json` (or the file named by `PLATE_TOKEN_FILE`). Compare the token the daemon is using with the one the plugin reads: if you set `PLATE_TOKEN_FILE` for the daemon only, set it for Max too, or remove it. Re-running `install.ps1` rewrites the plugin's side.

## H. 403 from the browser

Use the `127.0.0.1` address exactly as printed. `localhost` is allowed only under `PLATE_DEV=1`. A Tailscale address works only if the daemon was started with `-Tailscale` (it then also binds the machine's Tailscale IPv4) and the scoped firewall rule exists (`docs/windows.md`).

## I. Project locked

A project has one writer at a time (`project.lock`). The error names the holding host, process id and time. Close the project on that machine, or if that machine has crashed, restart the daemon there once: it recovers cleanly. Do not delete `project.lock` from a share while the holder might still be running.

## After a restart

Runs that were in flight are recovered at start: the daemon asks the provider about the job id it already holds, adopts a finished result or fails the run, and **never resubmits blind**. Open the History tab; anything still RUNNING is genuinely at the vendor. Spend for a run is settled at the quoted price if the provider had the job and refunded if it never did.
