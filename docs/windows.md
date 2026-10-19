# PLATE on Windows (the render box)

Windows 10/11 x64 is PLATE's primary runtime: the daemon, the GPU worker and the 3ds Max plugin run on the render box. Everything here also runs on macOS for development.

## First run

From the repo root in PowerShell (Windows PowerShell 5.1 or pwsh 7):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

`bootstrap.ps1` installs uv and Node LTS with winget if they are missing (`-NoInstall` only checks), creates the Python 3.12 venv with `uv sync` and runs `npm ci`. It never installs the pixels `cuda` extra. `test.ps1` runs the fidelity selftest, ruff, pytest and the UI tests, the same as `make test` on the Mac. `-ExecutionPolicy Bypass` applies to that one process only; it does not change the machine policy.

Optional: `winget install --id Gyan.FFmpeg -e` so the video-gate tests run instead of skipping (or point `PLATE_FFMPEG` and `PLATE_FFPROBE` at the binaries). Install OpenImageIO's `oiiotool` and set `PLATE_OIIOTOOL` when the P0 packet spikes need it.

## Where things live

| what | path |
|---|---|
| app-data (`PLATE_HOME` overrides) | `%LOCALAPPDATA%\Plate` |
| config (token, daemon URL; `PLATE_CONFIG` overrides) | `%LOCALAPPDATA%\Plate\config.json` |
| SQLite indexes, one per project | `%LOCALAPPDATA%\Plate\index\<project_id>.sqlite` (always local: WAL is unsafe on SMB) |
| daemon logs | `%LOCALAPPDATA%\Plate\logs\daemon.out.log`, `daemon.err.log` |
| Max startup script | `%LOCALAPPDATA%\Autodesk\3dsMax\2026 - 64bit\ENU\scripts\startup` |

Project folders and packets may sit on the NAS (a UNC path such as `\\nas\plate\...`). Each write takes the project lockfile (`project.lock`, per write), and the project lease (`lease.json`, Wave 13) keeps a second daemon's writes out while the first one's heartbeat is fresh: they are refused 409 `project_leased`, reads still work ([runbooks/daemon-wont-start.md](runbooks/daemon-wont-start.md), section I). The SQLite index never goes on the share.

**Long paths.** Content-addressed paths are kept short so everything fits under MAX_PATH (260). Enabling long paths is optional: `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1` (admin, then reboot). PLATE never assumes it.

## Running the daemon

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-daemon.ps1            # 127.0.0.1:4610, foreground
powershell -ExecutionPolicy Bypass -File scripts\register-daemon-task.ps1    # start at logon (per user, no admin)
powershell -ExecutionPolicy Bypass -File scripts\register-daemon-task.ps1 -Unregister
```

There is no Windows service in v1. The daemon binds `127.0.0.1` only. To open the canvas from the Mac over Tailscale, start it with `-Tailscale` (it then also binds the machine's Tailscale IPv4) and add one inbound firewall rule scoped to the Tailscale interface, from an elevated PowerShell:

```powershell
New-NetFirewallRule -DisplayName "PLATE daemon (Tailscale only)" -Direction Inbound -Protocol TCP -LocalPort 4610 -InterfaceAlias "Tailscale" -Action Allow -Profile Any
```

Add the rule before the first `-Tailscale` start. Without it, Windows Firewall prompts the first time python.exe listens on a non-loopback address, and whatever you answer creates a broad rule for python.exe: allowing opens every interface, while denying or cancelling creates a block rule, and block rules override the scoped allow rule. If the prompt was answered, open `wf.msc`, Inbound Rules, and delete the python.exe rules it created. Never bind `0.0.0.0`.

## 3ds Max plugin

`plugins\max\scripts\install.bat` (a wrapper around `install.ps1`) copies the PLATE startup script into the Max 2026 user startup folder and writes the daemon token to `%LOCALAPPDATA%\Plate\config.json`. `uninstall.ps1` removes exactly what the installer added. Do not install or update the plugin during a delivery week (decision 25).

## Known Windows traps the code already handles

- Text files are read and written as UTF-8 explicitly (the default code page is cp1252); `PYTHONUTF8=1` is set by the scripts as a second guard.
- Generated file names never contain `:` (timestamps are written `20261017T094107Z`), and names never differ only by case.
- New files can be held briefly by Windows Defender or an open handle, so moves into the content-addressed store retry `os.replace` with backoff.
- No symlinks (they need admin or Developer Mode); copies or hardlinks instead.
- In Windows PowerShell 5.1, redirecting a native command's stderr under `ErrorActionPreference = Stop` aborts on the first log line, so `start-daemon.ps1` redirects with `Start-Process`.
