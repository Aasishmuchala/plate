# Runbook: a Max, V-Ray or Windows update broke the PLATE dock

The plugin (`plate_max`) runs inside Max's own Python 3.11 and uses only the standard library plus `pymxs` and, for the dock, Max's bundled PySide6. Many of the V-Ray, Vantage and Max property names it touches are candidate lists that were checked against a fake `pymxs` and then verified on the box (`plugins/max/ONBOX.md`); an update can rename any of them. **Decision 25: do not install or update the plugin, Max or V-Ray during a delivery week.** If an update happened anyway, this is the order of play.

## 1. Tell what broke

Open the MAXScript Listener right after Max starts.

| what you see | meaning |
|---|---|
| `[PLATE] plate_max 0.1.0 loaded (packet 0.1.1)` and `[PLATE] macro PLATE_Dock registered` | the plugin loads; the break is later (go to 3) |
| `[PLATE] plugin not loaded: <ErrorType>: <message>` | the startup script ran but importing `plate_max` failed (go to 2) |
| `[PLATE] repo_path missing from config.json; run plugins/max/scripts/install.ps1` | the profile or config changed (go to 4) |
| nothing printed | the startup script is not being run (go to 4) |
| the macro exists but the dock does not open | PySide6 or the dock host failed (go to 2) |
| the dock opens and Send stops at a stage | a property name changed (go to 3) |

Max never fails to start because of PLATE: the startup script prints the error and swallows it.

## 2. Import or dock failure

In the Listener (Python mode): `import sys; print(sys.version)` (should be 3.11.x), then `import plate_max` and `import PySide6; print(PySide6.__version__)`. A Python or PySide6 major change is the usual cause. If MaxDirector or MaxGaffer docks are also installed, a PySide6 or callback collision between the docks is possible; open PLATE alone to confirm (the shared dock host exists to prevent this; see `plugins/max/SHARED_BRIDGE.md`).

## 3. Send fails at a stage

The dock's progress line names the stage (`colour`, `lint`, `elements`, `render`, `exr`, `legend`, `hash`, `manifest`, `post`). The ones an update breaks:

- **colour** `no rt.ColorPipelineMgr` or an unreadable mode: Max's colour manager API changed; the mode lookup lives in `plate_max/bridge/colour.py`.
- **elements** `render elements missing`: V-Ray renamed an element class or property (`plate_max/bridge/elements.py`).
- **render**: the render-setup property names (`plate_max/bridge/render.py`).
- **exr** or **legend**: the EXR layout or the Cryptomatte id type changed.

Run the property dump in `plugins/max/ONBOX.md` step 1 on a copy of the scene, compare the output with the constant tuples named there, fix the tuple, delete nothing else, and rerun `scripts\test.ps1`. Edit one constant at a time. Do this on a quiet day.

## 4. Startup script not running

`plugins\max\scripts\install.ps1` copies `plate_startup.py` into the user startup folder for one Max profile (`%LOCALAPPDATA%\Autodesk\3dsMax\2026 - 64bit\ENU\scripts\startup`). A new Max version or a new profile has a different folder: pass `-StartupDir` (and `-MaxVersion`) to both `install.ps1` and `uninstall.ps1`. Re-check `Customize > Preferences > Scripting > Safe Scene Script Execution` and your studio's security tools.

## 5. The deadline is today and none of that is quick

You do not need the dock to deliver:

1. **Render in Max as usual** and save the VFB-corrected 16-bit TIFF, plus the Z, normals and Cryptomatte passes if V-Ray still writes them.
2. If V-Ray's passes are available, use the **client packet kit** (`plugins/max/maxscript/plate_client_kit.ms`, a MAXScript that writes Z, Cryptomatte and a camera JSON next to a plate) and ingest the folder as a packet; it needs no Python in Max.
3. Otherwise treat the render as a **Tier C plate** (estimated geometry; needs a client consent record) and work from the canvas, or take the work through [manual-pipeline-fallback.md](manual-pipeline-fallback.md).

## 6. After

Record the Max, V-Ray and Vantage versions, the constants you changed and the Send that proved it, in `plugins/max/ONBOX.md` or `docs/onbox-spikes.md`. Roll back the Autodesk update on the delivery machine if you can, and hold updates until the next quiet day. `uninstall.ps1` removes exactly what `install.ps1` added and never touches the clone, the backups or `%LOCALAPPDATA%\Plate`.
