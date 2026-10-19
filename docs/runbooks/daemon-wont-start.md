# Runbook: the daemon will not start, or will not talk to you

## Tell which problem it is

| what you see | meaning | go to |
|---|---|---|
| `plate_daemon: another daemon already runs on <folder>` (exit 3) | a second daemon, or one that has not released the folder yet | A |
| `cannot listen on ['127.0.0.1'] port 4610: ...` (exit 2) | something else owns the port | B |
| `plate_daemon: ...config.json: not valid JSON` / `PLATE_TOKEN_FILE=... cannot read it` (exit 2) | the config or token file is damaged | C |
| `plate_daemon: PLATE_TOKEN_FILE=...: the token must be one line of printable ASCII without spaces, 16-256 characters` (exit 2); the same text naming `config.json: token` | the token file (or the token in `config.json`) holds a second line, a space, a non-ASCII character, or is too short or too long | C |
| `the SQLite index must live on a local disk` | `PLATE_HOME` points at a network share | D |
| the page loads but the status bar says `token: missing` | the page was opened without `#token=` | E |
| the page says `UI not built: cd ui && npm run build` | the daemon is fine, the UI bundle is not built | F |
| Max's dock says `Token rejected` (401) | the plugin and daemon disagree on the token | G |
| 403 on every request from a browser | Host or Origin not on the allowlist | H |
| a save says `project_locked` or `graph_busy`, or a tool says a project is locked by another PLATE process | a write was in flight in another process or save: the project lock is taken per write, not held while a project is open (the lease, below, is what keeps a second daemon out) | I |
| any write answers `409 project_leased` (`<folder> is leased by another PLATE daemon (<host> pid <n>, heartbeat <time>) ...`); the log says `project <id> is leased by <host> pid <n>: its runs stay with that daemon` | another daemon holds the project's lease (`lease.json`), and its heartbeat is less than 90 s old | I |

## A. Another daemon is running

1. Look for a daemon you forgot: the terminal where you started it, or on Windows the scheduled task (`scripts\register-daemon-task.ps1` starts one at logon). Only one daemon may use an app-data folder.
2. If you just killed one, wait 10 to 15 seconds: Windows can take a moment to release the lock, and the daemon retries for ten seconds by itself.
3. If it persists with no daemon running, reboot the box rather than deleting lock files by hand.

## B. The port is taken

Another program (or a second PLATE with a different `PLATE_HOME`) is on 4610. Start on another port with `scripts\start-daemon.ps1 -Port 4620` (Windows), `scripts/start-daemon.sh --port 4620` (Mac) or `PLATE_PORT`. The Max plugin and the browser must then use the new URL: the daemon prints it, and `config.json` holds `daemon_url`.

## C. Damaged config or token file

`config.json` is plain JSON with `token` and `daemon_url` (and the plugin's `repo_path`, `packets_dir`). Fix the syntax, or move the file aside: the daemon writes a fresh one with a new token, after which the Max plugin needs the new token (re-run `plugins\max\scripts\install.ps1`, which keeps what it can). `PLATE_TOKEN_FILE` must name a readable file holding only the token, and it wins over `config.json`. The token, from the file or from `config.json`, must be **one line of printable ASCII without spaces, 16 to 256 characters** (`one line of printable ASCII` in the message). A trailing newline is fine, a Windows `\r\n` too; a second line such as `# rotated 2026-10-01`, a space, a tab, a non-ASCII character or a 300-character paste is not, and the daemon stops at start with `<file>: the token must be one line of printable ASCII without spaces, 16-256 characters` instead of running on a bearer token no browser or plugin can send. The message never prints the token. Open the file in a plain editor, leave the token alone on its single line, save as UTF-8 (a BOM is ignored) and start again.

## D. Index on a share

The daemon keeps one SQLite index per project in the app-data folder and refuses to put it on SMB (the write-ahead log is unsafe there). Unset `PLATE_HOME` or point it at a local folder. Projects themselves may live on the NAS; only the index may not.

## E. Token missing in the browser

Open the full URL the daemon printed: `http://127.0.0.1:4610/#token=...`. `scripts/plate_config.py url` prints it again. A new tab or window needs it again; a reload does not.

## F. UI not built

`scripts/start-daemon.sh` and `scripts\start-daemon.ps1` build the UI themselves when `ui/dist/index.html` is missing (they print `building ui/dist`), and `bootstrap` builds it once. If that build failed, the daemon still started (it printed a warning) and only the page is missing. Run `npm run build -w ui` from the repo root to see the error: the usual cause is that `npm ci` has not run (run bootstrap again). Then reload. For development, `npm run dev` serves on 5173 and needs the daemon started with `PLATE_DEV=1`.

## G. Plugin gets 401

Both read `config.json` (or the file named by `PLATE_TOKEN_FILE`). Compare the token the daemon is using with the one the plugin reads: if you set `PLATE_TOKEN_FILE` for the daemon only, set it for Max too, or remove it. Re-running `install.ps1` rewrites the plugin's side.

## H. 403 from the browser

The daemon always accepts `127.0.0.1:<port>` and `localhost:<port>` (the port it printed) as the page's Host and Origin; anything else is a 403 `forbidden_host` or `forbidden_origin`, for example a numeric address of the box on the LAN, a machine name, or `localhost` on the wrong port. Only the Vite dev server's origins (`http://127.0.0.1:5173`, `http://localhost:5173`) need `PLATE_DEV=1`. A Tailscale address works only if the daemon was started with `-Tailscale` (it then also binds the machine's Tailscale IPv4) and the scoped firewall rule exists (`docs/windows.md`).

## I. Project locked or leased

The project lock (`project.lock`) is taken for each write (a graph save, a ledger line, a packet registered, a reindex or a purge) and let go as soon as that write is done. It is **not** held while a project is open, so it does not mean "another machine has the project open", and the lock alone does not stop a second daemon from opening the same folder (the lease, below, does that for writes).

What the messages mean now:

- `409 project_locked`, `409 graph_busy` or `429 too_many_saves` from the canvas: another save of this project is in progress (in this daemon, or in a tool or another daemon). Wait a second. The canvas retries by itself and shows `save failed: ...` until it lands.
- `locked by another PLATE process (host pid N since T)` from `plate-tools reindex` or `purge`, or from the daemon: that process was writing at that instant. Wait a few seconds and run it again. The holder's host, process id and time are in the message. The OS frees the lock when the holder dies, so a crashed process never leaves it stuck.

**One daemon per project folder: the lease.** The lock above cannot do that job, so the first daemon to write a project takes `<project>/lease.json` (`host`, `pid`, `daemon_id`, `started_utc`, `heartbeat_utc`) and refreshes `heartbeat_utc` every 20 s while it runs. While another daemon's lease is live, which means its heartbeat is less than 90 s old (`PLATE_LEASE_TTL_S` changes the 90), this daemon's writes to that project are refused:

- `409 project_leased` from a save, a node add, a quote, an import, a packet registration, anything that writes. The text is `<folder> is leased by another PLATE daemon (<host> pid <n>, heartbeat <time>); stop that daemon or wait 90 s for its lease to lapse`, and the body carries `host`, `pid`, `daemon_id` and `heartbeat_utc`. The canvas shows it as `save failed: ...` and keeps retrying, so it heals by itself once the lease is gone.
- **Reads still work**: the graph, the runs, the assets and the review bundles can be looked at from any daemon, and `GET /api/v0/projects` shows each project's `lease` (`host`, `pid`, `daemon_id`, `heartbeat_utc`, `live`, `mine`).
- At start the log says `project <id> is leased by <host> pid <n>: its runs stay with that daemon` (once per project per 90 s). This daemon leaves the other one's in-flight runs alone. Before the lease it would have adopted them and resumed them a second time.

What to do:

1. If you meant to move the project to another machine, stop the old daemon. A clean stop removes `lease.json`, and the new daemon's next write takes the lease.
2. If the old daemon crashed, wait: the lease lapses 90 s after its last heartbeat (at once when it ran on this machine and its process is gone). The first write after that takes it over and the log says `took over the stale lease on <path> from <host> pid <n> (last heartbeat <time>)`. Two machines on a NAS compare the heartbeat with their own clocks, so keep both clocks right.
3. Do not delete `lease.json` or `project.lock` while the other daemon might still be running. That puts two writers on the folder, which is exactly what the lease prevents.

`plate reindex` and `plate-tools purge` install no lease identity: they are neither refused by a lease nor do they take one, and behave as described above. A project folder that was copied while a daemon was running carries that daemon's `lease.json`; delete it from the copy ([backup-restore.md](backup-restore.md)).

## After a restart

Runs that were in flight are recovered at start: the daemon asks the provider about the job id it already holds, adopts a finished result or fails the run, and **never resubmits blind**. Open the History tab; anything still RUNNING is genuinely at the vendor. Spend for a run is settled at the quoted price if the provider had the job and refunded if it never did.
