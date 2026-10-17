# Runbook: the manual pipeline (PLATE is down, the deadline is not)

The fallback is the way the studio worked before PLATE: a person, a vendor's web app, an image editor. It stays documented so a delivery never depends on PLATE running. **What it must not become is a way around the rules**: the plate is still the render, AI pixels still land only inside the mask, the gates still score the result, and every file that leaves is recorded. The gate commands below run without the daemon; they need only the repo and its Python environment.

## 0. Before you start

1. **Custody.** A web app (Higgsfield, Runway, a vendor's own site) is grade D: its terms are not held in writing and an upload may become a public link. So this runbook is for **non-NDA projects**, or for an NDA project only with the client's written consent naming that vendor ([consent-template.md](../consent-template.md)). If neither holds, the manual route is a V-Ray or Vantage render, or the local engines. Write down the vendor, the account and the time of every upload.
2. **Is PLATE really down?** Check [daemon-wont-start.md](daemon-wont-start.md) and [vendor-down.md](vendor-down.md) first; a vendor outage is not a PLATE outage, and the Make bar already offers other routes.
3. **Find your inputs in the packet folder** (the folder Send wrote): `display.tif` (the VFB-corrected 16-bit master: what you signed off is what ships), `beauty.exr` (linear beauty plus the data passes), `id_legend.json` (which IDs are editable), `manifest.json`.

## 1. Prepare what you will upload

- Export `display.tif` once to a 16-bit PNG, `manual/plate16.png`, in the project folder: this one file is "the plate" for compositing and gating. From it make the upload copy: an 8-bit sRGB PNG with **no embedded metadata** under a neutral file name (not the project, client, camera or layer name). Never JPEG. If the edit is on a small area, crop to that area plus some context at native size; never upscale the plate to fit an engine.
- Write down the editable area as a mask: white where the packet's `editable_ids` allow a change, black everywhere else. Build it from the Cryptomatte selection in V-Ray, or in the canvas Mask editor. Anything in a locked class (architecture, glazing, signage, unlabelled) stays black.

## 2. Generate by hand

Use the web app as you would. Save every result you might keep into a `manual/` folder inside the project folder, named `YYYYMMDDTHHMMSSZ_<vendor>_<n>.png` (no colons in the time). Save the prompt next to it as `<same name>.txt`. Do not ask any engine to "fix the colour" and do not run it on a hero wide shot: those are renders.

## 3. Put it back on the plate, inside the mask only

Everything outside the mask must be the original plate pixels, and a 4 to 12 px feather must live *inside* the mask edge. In an editor, place the result over the plate and reveal it through the mask as a layer mask. To do it numerically with the repo's own compositor (the one the app uses, which also matches the tone of the result to the plate on a ring just outside the mask):

```python
# run with the repo's Python: PYTHONPATH=spec:daemon:pixels:engines:gates:plugins/max:tools .venv/bin/python
import cv2, numpy as np
from plate_gates.compare import read_image, write_png
from plate_gates.composite import composite_masked

RGB16 = cv2.IMREAD_COLOR | cv2.IMREAD_ANYDEPTH
plate = read_image("manual/plate16.png", RGB16)[..., ::-1]            # cv2 reads BGR; the compositor wants RGB
ai = read_image("manual/result.png", RGB16)[..., ::-1]         # the vendor's result, already the plate's size
mask = read_image("mask.png", cv2.IMREAD_GRAYSCALE) > 127      # white = editable
out = composite_masked(plate, ai, mask, feather_px=8)
write_png("manual/composite16.png", np.ascontiguousarray(out[..., ::-1]))
```

The result must already be the plate's size and registered to it; if the vendor changed the framing, line it up in the editor first (the app does this registration for you). Signage inside the frame must stay the original lettering: keep those areas black in the mask. Do not use `tools_cli composite`: it is the old rectangle paste whose blur bleeds past the box, kept only for old projects.

## 4. Gate it

With a packet (Tier A) the full gates run, including the G-buffer edge checks:

```
plate gate <manual/plate16.png> <composite16.png> --packet <packet folder> --out <composite16>.gate.json
```

`plate gate` needs the plate and the composite at exactly the same size (it never resizes either) and takes the editable mask from the packet's own policy. Because the mask comes from the packet, a composite that changed anything outside the packet's editable IDs fails `outside_mask.de00` whatever mask you drew; your own mask may only be narrower. It writes no ledger entry by itself; the report file is the evidence. Exit code 0 is a pass or advisory, 2 is a gate failure, 4 is a usage error. Without the CLI, `python -m plate_gates maskdiff manual/plate16.png manual/composite16.png mask.png --report` proves that nothing changed outside the mask, and `python -m plate_gates pair manual/plate16.png manual/composite16.png --mask mask.png --report --engine <vendor-model> --out <name>.json` scores the pair with the same metric code. **Compare like with like:** these commands read images at 8 bits, so a TIFF compared with a PNG reports phantom differences from two different 16-to-8-bit conversions. Use `manual/plate16.png` as "the plate" everywhere. `python -m plate_gates.tools_cli texture <image>` reports the texture band for a still (valid at 3840 px wide).

Read the report as you would in the Gate panel. A **hard** failure blocks delivery exactly as in the app: fix the composite, or deliver it only with a written override (at least ten characters of reason) kept with the file. An unmeasured hard gate is a fail.

## 5. Record it

Nothing leaves without a line in the project's record. Keep `manual/IMPORT_LOG.md`, one table row per file you bring back:

| file | vendor and account | route / model | uploaded at (UTC) | custody grade | consent reference | gate report | verdict | by |
|---|---|---|---|---|---|---|---|---|

When the daemon is back, give the files to PLATE so the audit trail catches up: add the composites under their packet, run them through the gates in the app (Compare, Gate), and record the verdict keys and the timer for the minutes spent. A file that carries PLATE lineage (it was exported by PLATE earlier) resolves by itself when dropped on the canvas; a file made by hand has none and is matched by its content and perceptual hash.

## 6. Deliver

`plate export <run_id> --to <dir> --format tiff16|png16|exr` writes lineage and the `.gate.json` next to the file when a run exists. For a hand-made composite, deliver the 16-bit file, its `.gate.json` and the log row together, and never rename a file so that it hides its source. The client-facing wording about AI processing is in [client-explainer.md](../client-explainer.md).

## Never

- Never upload the 16-bit master, `beauty.exr`, the manifest, the legend or anything with a client, project or layer name in it.
- Never skip the gate because it is late. The override exists; use it, in writing.
- Never mix a hand-made look with a PLATE look inside one set of cameras without telling the client.
- Never deliver a previs clip or a Concept image as a render.
