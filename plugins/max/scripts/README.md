# PLATE Max plugin: install and uninstall

Windows only (3ds Max 2026). Run from the repo root or double-click `install.bat`.

```powershell
powershell -ExecutionPolicy Bypass -File plugins\max\scripts\install.ps1                 # token reused or generated
powershell -ExecutionPolicy Bypass -File plugins\max\scripts\install.ps1 -Token <hex> -DaemonUrl http://127.0.0.1:4610
powershell -ExecutionPolicy Bypass -File plugins\max\scripts\uninstall.ps1
```

`-ExecutionPolicy Bypass` applies to that one process and does not change the machine policy. The scripts are plain ASCII and run in Windows PowerShell 5.1 and pwsh 7.

## What the installer does

1. Copies `plugins\max\startup\plate_startup.py` into `%LOCALAPPDATA%\Autodesk\3dsMax\2026 - 64bit\ENU\scripts\startup`.
2. Creates or updates `%LOCALAPPDATA%\Plate\config.json` (the same file the daemon creates; `PLATE_CONFIG` and `PLATE_HOME` are honoured) with `token`, `daemon_url` and `repo_path`. A token or URL already in the file is kept unless you pass `-Token` / `-DaemonUrl`. The keys the installer added are listed in `plate_max_installed_keys`.

Nothing is installed into Max's Python and nothing is downloaded: `plate_max` is standard library only and is imported straight from your clone (`repo_path`).

## Before the first Send

- **Safe Scene Script Execution.** Max can block scripts that arrive inside scene files. The PLATE startup script is a user startup script, not a scene script, so it should load regardless, but check it once: restart Max and look in the MAXScript Listener for `[PLATE] plate_max 0.1.0 loaded`. If the line is missing, check the Safe Scene Script Execution setting in Max's preferences and your studio's security tools, then record the result in `docs/max-plugin.md`. This check has not been done on a Max box yet.
- **Daemon first.** Start the daemon once (`scripts\start-daemon.ps1`) so `config.json` exists, or let `install.ps1` generate the token and start the daemon afterwards; the daemon reuses whatever token is already in the file.
- **No auto-update in a delivery week (decision 25).** Do not install, update or `git pull` this plugin while a delivery is in progress. The plugin and the daemon must agree on the Shot Packet version, and the plugin refuses to Send a version the daemon does not list. Update on a quiet day, then run the handshake before the next Send.

## Uninstall

Run `uninstall.ps1`. It deletes `plate_startup.py` from the startup folder and removes only the `config.json` keys the installer added (a token the daemon created earlier stays, so the daemon keeps working). If nothing else is in the file it is deleted. The clone and `%LOCALAPPDATA%\Plate` are never touched. Restart Max afterwards.

If you installed into a non-default Max profile, pass the same `-StartupDir` to both scripts.
