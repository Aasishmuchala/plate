# PLATE Max plugin: install and uninstall

Windows only (3ds Max 2026). Run from the repo root, or double-click `install.bat`.

```powershell
powershell -ExecutionPolicy Bypass -File plugins\max\scripts\install.ps1 -RepoPath C:\src\plate   # token reused or generated
powershell -ExecutionPolicy Bypass -File plugins\max\scripts\install.ps1 -Token <hex> -DaemonUrl http://127.0.0.1:4610
powershell -ExecutionPolicy Bypass -File plugins\max\scripts\uninstall.ps1
```

`-ExecutionPolicy Bypass` applies to that one process and does not change the machine policy. The scripts are plain ASCII with CRLF line endings and run in Windows PowerShell 5.1 and pwsh 7. `-RepoPath` defaults to the clone the script sits in; `-MaxVersion` defaults to `2026` (it picks Max's Python and the default startup folder).

## What the installer does

1. Looks for Max's own Python at `C:\Program Files\Autodesk\3ds Max 2026\Python\python.exe` and reports its version (3.11 expected). It only looks: nothing is installed into it, not even `requests`, because `plate_max` is standard library only and is imported straight from your clone.
2. Copies `plugins\max\startup\plate_startup.py` into `%LOCALAPPDATA%\Autodesk\3dsMax\2026 - 64bit\ENU\scripts\startup`. A copy that differs from the new one is kept next to it as `plate_startup.py.bak`.
3. Creates or updates the **single** app-data `config.json` (`%LOCALAPPDATA%\Plate\config.json`; `PLATE_CONFIG` and `PLATE_HOME` are honoured; it is the same file the daemon uses) with `repo_path`, `packets_dir`, `daemon_url` and `token`. A token or URL already in the file is kept unless you pass `-Token` / `-DaemonUrl`. The keys the installer had to add are listed in `plate_max_installed_keys`, and the first time the old file is kept as `config.json.bak`. There is no second config file.
4. Prints the Safe Scene Script Execution note below.

At the next Max start the startup script puts `<repo_path>\plugins\max` on `sys.path`, imports `plate_max` and registers the `PLATE_Dock` macro (Customize User Interface, category PLATE). The macro opens the dock. Nothing is updated and nothing touches the network at startup.

## Before the first Send

- **Safe Scene Script Execution.** Max 2026 blocks scripts that arrive embedded in scene files, not user startup scripts, so the PLATE startup script should load regardless. Check it once anyway: in Max open `Customize > Preferences > Scripting > Safe Scene Script Execution` and confirm the startup folder the installer printed is not listed as blocked. Then restart Max and look in the MAXScript Listener for `[PLATE] plate_max 0.1.0 loaded` and `[PLATE] macro PLATE_Dock registered`. If either line is missing, check that setting and your studio's security tools, then record the result in `docs/max-plugin.md`. This check has not been done on a Max box yet.
- **Daemon first.** Start the daemon once (`scripts\start-daemon.ps1`) so `config.json` exists, or let `install.ps1` generate the token and start the daemon afterwards; the daemon reuses whatever token is already in the file.
- **No auto-update in a delivery week (decision 25).** Do not install, update or `git pull` this plugin while a delivery is in progress. The plugin and the daemon must agree on the Shot Packet version, and the dock refuses to Send a version the daemon does not list. Update on a quiet day, then check that the dock's daemon line says `compatible` before the next Send.

## Uninstall

Run `uninstall.ps1`. It deletes `plate_startup.py` from the startup folder and removes only the `config.json` keys listed in `plate_max_installed_keys` (a token the daemon created earlier stays, so the daemon keeps working). If nothing else is in the file it is deleted. The backups (`plate_startup.py.bak`, `config.json.bak`), the clone and `%LOCALAPPDATA%\Plate` are never touched. Restart Max afterwards.

If you installed into a non-default Max profile, pass the same `-StartupDir` to both scripts; `-ConfigPath` does the same for a config file in a non-default place.

## Dock preview (any OS with PySide6)

`ui_preview.py` renders the dock offscreen against a fake controller and writes `plugins/max/dock_preview.png`:

```
.venv/bin/python plugins/max/scripts/ui_preview.py
```
