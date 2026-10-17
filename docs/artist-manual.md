# PLATE artist manual

For the person who sits at the render box and delivers stills and clips. It follows one job from install to a delivered file: install, launch, create a project, send a packet from Max, work the canvas (Plate, Mask, Edit, Comp, Gate, Export), read the gates, deliver. Each section says what to click, what you should see, and what PLATE refuses and why. The engineering notes live in the other files under `docs/` (index: [README.md](README.md)); when something breaks, go to [runbooks/](runbooks/README.md).

**What PLATE is, in one paragraph.** PLATE keeps the render. The Max plugin exports a **Shot Packet** (camera, Z depth, normals, Cryptomatte IDs, the VFB-corrected 16-bit master) instead of a screenshot. AI is allowed to change pixels only inside the IDs the packet marks editable; outside that mask the delivered pixels are the render, bit for bit. Every output is scored by measured gates, and a failing take cannot be delivered without a written override. Every run shows its price and where your pixels go before you pay. That is the whole bargain; most refusals below are one of these rules speaking.

> **What has and has not been exercised (read once).** The Max plugin is tested against a fake `pymxs` and has never run inside Max. The PowerShell scripts were written on a Mac. Every vendor adapter runs on recorded fixtures: no live vendor call has been made, prices in the manifests are placeholders (`unconfirmed`), and the cloud custody terms are not held in writing yet, so every cloud route counts as grade D today (section 3). The GPU worker is in stub mode (no weights). The canvas, viewer and panels are tested in jsdom and a GPU smoke page, not yet driven by hand in a real browser against a real daemon. Wherever this manual says "you should see", treat a mismatch as a bug to report, not as your mistake. The box-day checklist is in `docs/VERIFICATION.md` once it exists.

## Contents

1. [Install](#1-install)
2. [Launch and the token](#2-launch-and-the-token)
3. [Create a project (custody)](#3-create-a-project-custody)
4. [Send a packet from Max](#4-send-a-packet-from-max)
5. [The canvas, step by step](#5-the-canvas-step-by-step)
6. [The Make bar and quotes](#6-the-make-bar-and-quotes)
7. [N candidates](#7-n-candidates)
8. [The viewer and its keys](#8-the-viewer-and-its-keys)
9. [Reading the gate panel](#9-reading-the-gate-panel)
10. [Override policy](#10-override-policy)
11. [Verdict keys, the scrub and the timer](#11-verdict-keys-the-scrub-and-the-timer)
12. [Motion: CropPair, Move risk, previs](#12-motion-croppair-move-risk-previs)
13. [Numeric nodes](#13-numeric-nodes)
14. [Delivery formats](#14-delivery-formats)
15. [Review bundles for the client](#15-review-bundles-for-the-client)
16. [Agents](#16-agents)
17. [When something goes wrong](#17-when-something-goes-wrong)
18. [Appendix: folders, environment variables, ports](#18-appendix-folders-environment-variables-ports)

## 1. Install

Windows 10/11 x64 is the primary machine; the Mac is the development machine and runs the same code.

**Windows (the render box).** From the repo root in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

`bootstrap.ps1` installs `uv` and Node LTS with winget if they are missing (`-NoInstall` only checks), creates the Python 3.12 environment and runs `npm ci`. It never installs the GPU extra. `test.ps1` is the same suite as `make test`; run it once after install and expect it to finish green. `-ExecutionPolicy Bypass` applies to that one process only.

**Mac.** `scripts/bootstrap.sh` then `scripts/test.sh` (or `make setup` and `make test`). `bootstrap.sh` ends by printing the config path and the launch URL.

**Optional but useful.** `ffmpeg` and `ffprobe` on PATH (or `PLATE_FFMPEG` and `PLATE_FFPROBE`) so the video gates and delivery formats work; without them those checks skip and say so. `oiiotool` is not required.

**The Max plugin** installs separately, on the box only, never in a delivery week:

```powershell
powershell -ExecutionPolicy Bypass -File plugins\max\scripts\install.ps1
```

It copies one startup script into Max 2026's user startup folder and writes the daemon token and URL into `config.json`. Restart Max; the MAXScript Listener should show `[PLATE] plate_max 0.1.0 loaded` and `[PLATE] macro PLATE_Dock registered`. Check `Customize > Preferences > Scripting > Safe Scene Script Execution` once (details in `plugins/max/scripts/README.md`). Uninstall with `uninstall.ps1`.

## 2. Launch and the token

Start the daemon:

| machine | command |
|---|---|
| Windows | `powershell -ExecutionPolicy Bypass -File scripts\start-daemon.ps1` (logs in `%LOCALAPPDATA%\Plate\logs\`); `scripts\register-daemon-task.ps1` starts it at logon |
| Mac | `scripts/start-daemon.sh`, or `make daemon` |

It prints one line:

```
PLATE daemon http://127.0.0.1:4610/#token=<long token>
```

Open that exact URL in a browser on the box. The part after `#` is your install's token. A browser never sends a URL fragment to the server: the page moves the token into the tab's session storage, removes it from the address bar and sends it as an `Authorization` header from then on. A reload keeps it; a new tab, a new window or another browser needs the full URL again (`scripts/plate_config.py url` prints it again). The status bar at the top of the canvas shows `daemon: up` and `token: present`; `token: missing` means you opened the page without the `#token=` part.

Rules of the road: use the `127.0.0.1` address as printed (the `localhost` spelling is accepted only under `PLATE_DEV=1`); the daemon listens on loopback only, and reaching it from the Mac over Tailscale needs `-Tailscale` plus one scoped firewall rule (see `docs/windows.md`); do not paste the token into chat, tickets or screenshots. If the daemon exits with "another daemon already runs", you started it twice; see [runbooks/daemon-wont-start.md](runbooks/daemon-wont-start.md).

## 3. Create a project (custody)

A project is a folder: `project.json` (the canvas graph and the custody setting), `assets/` (every stored image, named by its hash), `runs.jsonl` (the append-only ledger of every quote, approval, run and export), `gates/` (versioned gate reports), `review/` and `packets.json`. By default it lives under the app-data folder; a project may sit on the NAS because the folder is the truth and the daemon's SQLite index is only a cache kept on the local disk. Only one machine writes a project at a time (a lock file enforces it).

In the **Projects** tab choose **New project**:

| field | what to enter |
|---|---|
| Project id | a lower-case slug: letters, digits, `_` or `-`, starting with a letter or digit, up to 64 characters. It is kept out of review-bundle file names and out of everything sent to a vendor. |
| NDA | on for anything under a confidentiality agreement. NDA locks the allowed grades to C0 and C1. |
| Allowed grades | the custody grades this project may use. With NDA on, C2 and D are greyed out. |
| Engine pins | optional: pin a task to an engine and version. A pin is never silently replaced. |

**Custody grades** (the full matrix is [custody.md](custody.md); the client-facing version is [client-explainer.md](client-explainer.md)):

| grade | meaning | NDA project |
|---|---|---|
| C0 | local only; nothing leaves the box | allowed |
| C1 | first-party vendor API with written no-training and zero-retention terms | allowed |
| C2 | a named reseller whose written terms we hold | not allowed |
| D | terms unknown or not held, or the upload becomes a public link | not allowed |

Two facts decide what you will see today. First, **a route whose written terms are still pending counts as D.** OpenAI, Gemini and Vertex are written down as C1 candidates, but no terms are filed, so they are D until someone files them and flips the manifest. Atlas is D on the evidence (its upload links are public). Second, **a project with no custody set yet is C0**: if a packet arrives before you create the project, PLATE makes the folder with custody unset and nothing may leave the box until you fill it in. So, today, an NDA project can run only on the local engines (Qwen-Image-Edit, Wan 2.2 previs, Real-ESRGAN for AI layers), and it has no Claude prompt composer and no semantic judge: the composer degrades to template filling, and the Judge node reports `unavailable: custody`. That is by design, not a fault. Check `/status` in any Claude Code session before it reads client material: it must be signed in with an API key, never a consumer login.

## 4. Send a packet from Max

Open the dock from the **PLATE** category (macro `PLATE_Dock`). Top to bottom it shows:

| section | what it does |
|---|---|
| Status line | `daemon <version> · <url> · compatible`, or `NOT compatible: <reason>`. **Check** repeats the handshake. Do not send while it says NOT compatible. |
| CAMERAS | the scene's cameras (hidden ones are marked). Tick the cameras to send; the recipe below edits the focused one. **Refresh** rereads the scene. |
| RECIPE | per camera and stored next to the scene in `<scene>.plate.json`: **Engine** and **Preset** the shot is meant for, **Safe frame** (the deliverable crop inside the render, x y w h), **Overscan** (render wider or taller than the camera so the engine gets its native aspect; the original frame stays centred), and **EDITABLE CLASSES**. |
| ACTIONS | **Send Packet**, **Batch Cameras** (every ticked camera, in order, one summary at the end), **Send Motion**, **Light**, **Scout**, **Receive**. |
| LINT | **Check selected** runs the checks below without rendering. |
| PROGRESS | one line per stage and a log. |

**Editable classes can only narrow what your layer names already allow.** A node is editable only when its layer starts with one of `VEG_`, `GRASS`, `SKY`, `PEOPLE`, `WATER` or `ENT_`, it carries no `architecture`, `glazing` or `signage` tag (user property `plate_tags`, comma separated), and it is not unlabelled. Everything else is locked: layers `ARCH_`, `GLZ_`/`GLAZING`, `SIGN_`, `HARD_`, `VEH_`, a node on layer `0`, a node on a layer that matches nothing. A material is editable only if every visible object using it is. This is the single most important habit: **name your layers.**

**What Send does, per camera, in this order.** Any failure stops that camera, says why, and moves to the next.

| stage | what happens |
|---|---|
| colour | reads Max's colour pipeline (OCIO or legacy gamma). An unknown mode stops here, before anything renders. |
| lint | scene checks (table below). |
| backup | with the backup option on, the scene is saved aside first. |
| elements | makes sure the default render elements exist: beauty, Z depth, normals, Cryptomatte object and material, wire colour, diffuse filter. |
| render | renders into a local staging folder at the recipe's overscan. Widening a camera never changes the pixel scale (checked to 1e-6). |
| exr | reads the EXR header back: size, lossless compression, the channels the manifest will claim, Cryptomatte manifests, and per-ID coverage (the legend's pixel share). |
| packet | moves `beauty.exr` and `display.tif` into the packet folder. |
| legend | builds the ID legend from the visible names, derives the policy, runs the unlabelled-share lint on the measured coverage. |
| hash | BLAKE3 of every file, asked of the daemon (the plugin has no BLAKE3). |
| manifest | writes the manifest last. |
| post | hands the folder to the daemon (below). |

**Lint messages, explained.**

| code | what it says | what to do |
|---|---|---|
| `NO_VRAY` | the current renderer is not V-Ray | set V-Ray as the production renderer |
| `SCALED_CAMERA` | the camera's transform is scaled, sheared or mirrored | reset the transform (the camera must be a clean rotation plus position) |
| `SHIFTED_CAMERA` | lens shift, vertical or horizontal tilt, or lens distortion is on | zero them; Shot Packet 0.1 supports an unshifted, untilted, undistorted pinhole only |
| `HIDDEN_CAMERA` | the camera, or its layer, is hidden | unhide it |
| `BAD_NAME_CHARS` | a node name contains `:`, `/` or `\` | rename it; node names become part of file names |
| `UNLABELLED_SHARE` | nodes on no labelled layer cover more of the frame than the limit (5% by default) | put those nodes on a labelled layer |
| `LOCKED_AND_EDITABLE` / `UNLABELLED_EDITABLE` | the policy contradicts itself | a bug if you see it; report it with the scene name |

Other stops you may meet: *colour mode: ...* (Max's colour manager cannot be read, or OCIO has no display or view: fix the colour setup, PLATE will not guess), *render elements missing: ...* (add them in Render Setup), *the EXR does not hold what the manifest would claim* (a render element was renamed or half-float Cryptomatte was used: Cryptomatte must be 32-bit float and lossless), *Cryptomatte manifest gives X id ..., its MurmurHash3 is ...* (set the VRayCryptomatte id type to node name).

**What the daemon answers.** Sent: `202 ingesting`. Already sent: `200 known` (re-sending the same packet is safe). Stopped: `409` (this packet id exists with different content: rebuild the packet), `422` (the packet failed validation; the dock shows the error list), `426` (plugin and daemon disagree on the packet version; update on a quiet day), `401` (token rejected: re-run the installer or fix `config.json`), no answer (daemon not running). After a good Send the packet is registered with the project, and a Plate node on the canvas stands for it (see the limits note in section 5).

**Receive** puts a finished image back into Max for an in-Max A/B without touching your scene: into the V-Ray VFB history (compare with V-Ray's own tools) or as the active viewport's background image.

## 5. The canvas, step by step

**Layout.** The top bar shows `daemon: up` and `token: present`. The canvas fills the middle; `+ node` (top left) adds a node of any kind. The Make bar is docked at the bottom and acts on the selected node. The right-hand side holds the node list (and the last refusal), the editor for the selected node, the compare viewer, and the panels (tabs Projects, History, Gate, Custody, Timer; the verdict keys stay visible on every tab). One dark theme.

**Node kinds.** There is no text-to-image node and no "image creator" anywhere; every generation starts from a packet's own camera.

| kind | what it is |
|---|---|
| Plate | one packet: its beauty and, for nodes that read elements, the packet itself (Z, normals, Cryptomatte, camera). Tier A is a render with a G-buffer; Tier C is a client plate with estimated geometry, badged "estimated geometry" |
| Mask | which IDs the next Edit may change (Cryptomatte pick, paint, SAM click, OCR text lock) |
| Edit | a generative edit through an engine (relight, vegetation, sky, close re-photograph) |
| Comp | the mask-locked composite: AI pixels only inside the mask, feathered inside it, signage pasted back bit for bit |
| Colour | numeric exposure, white balance and tone; no generator is ever asked to fix colour |
| LightMix, Recolour, Set, Light, Scout | numeric and Max round-trip nodes (section 13) |
| Gate | the measured gate report for a candidate against its plate |
| Judge | the semantic judge (two-sample); it can never pass a frame that fails a numeric gate |
| CropPair, Motion | start/end crops of an approved still; a camera move (section 12) |
| People | people over STAFFAGE, occluded by the packet's Z |
| Compare | A/B of two takes |
| Export, Review | delivery: files for the client's pipeline; a static review bundle |
| Concept | the only unmeasured thing on the canvas: watermarked, "CONCEPT, UNMEASURED", can never reach Export, Review or Delivery |

Every node shows a header with its kind, its tier (`measured`, or `concept_unmeasured` in amber with a watermark icon), the gate result (pass, fail, advisory), an approval tick and, for moves, a risk label. Handles are typed by what they carry (`image16`, `mask`, `packet`, `video`, `report`); a wire only joins an output to an input that accepts its type.

**The walkthrough: Plate, Mask, Edit, Comp, Gate, Export.**

1. **Plate.** A Plate node stands for one packet you sent from Max; once it is bound to the packet (limits note below) its thumbnail is the packet's beauty.
2. **Mask.** Wire Plate to Mask. Click a pixel in the plate to pick the object under it (the editor shows the ID, its name and its class); pick as many as the edit needs. A locked ID (architecture, glazing, signage, anything unlabelled) is never editable: if you pick one, the quote is refused with `ids [...] are locked by the packet policy`, and a painted mask has the locked IDs cut out of it automatically. With no Mask node the edit may touch every editable ID in the packet's policy (on a Tier C plate with no editable IDs, the whole frame). Signage inside the frame gets an OCR text lock (the lettering is pasted back untouched).
3. **Edit.** Wire Mask (and the Plate image) into Edit. Pick an engine in the Make bar, Quote, read the price and the custody grade, Make (sections 6 and 7). Behind the one click the run does: custody check, egress (an 8-bit sRGB PNG under an opaque name, metadata stripped, plus the mask), the engine, ingress (undo the size map and re-register the engine's pixels onto the plate on the ring outside the mask), composite, text lock, gates.
4. **Comp.** The composite is the plate everywhere outside the mask, by construction (outside-mask difference is exactly zero). Inside the mask the edit is blended with a 4 to 12 px feather that lives inside the mask, so there is no halo outside it.
5. **Gate.** The gate report scores the composite against the plate (section 9). A pass turns the node's gate badge green; a fail keeps the take, flags it, and blocks export.
6. **Export.** Wire the approved output to Export (section 14). A node cannot be marked approved without a logged full scrub (section 11).

**The rules the canvas enforces.** A wire that breaks one never connects: the message shows on the target node for four seconds and in the node list. The daemon re-checks every edge when the graph is saved (the browser check is only a courtesy), so a hand-edited graph cannot get around it.

| rule | message you will see | doctrine |
|---|---|---|
| no loops | `cycle` | the graph must stay a DAG |
| concept stays out of delivery | `concept tier cannot reach Export` (or Review) | geometry first |
| generation hangs off a plate | `generation must be anchored to a plate camera (source-view-only)` | no invented angles |
| depth at most 2 generations from the plate | `generation depth > 2 from the plate` | stop drift compounding |
| a camera move waits for a good still | `motion is locked until the upstream still passes gates and is approved` (or `crop pair`) | video last |
| high-risk moves need an override | `high risk move is blocked until it is overridden with a reason` | section 10 |
| an invented camera angle needs an override | `a camera override needs an explicit override and the label 'unseen geometry guessed'` | section 10 |
| no accept without a full scrub | `'<label>' cannot be accepted without a logged full scrub` | section 11 |

**Current limits of the canvas (as built).** Several canvas features call daemon routes that do not exist yet, and a few buttons are not there yet. They are small and listed in [ui.md](ui.md); until they land you will meet them as a refusal message or a missing control, not as a crash:

- *Binding a Plate node to a packet.* A Plate node added from `+ node` says "no packet linked": there is no packet picker, so a node is bound by writing its `packetId` into the project graph (the router plan's smoke test does this over the API).
- *Export.* Stills are exported by `plate export` or the export route (section 14); the Export node carries the rules but has no button.
- *Cryptomatte pick, SAM click, OCR text lock* and saving a painted mask call daemon routes that are not there yet; until they land, write the Mask node's `ids` into the project graph, or leave the Mask node out and let the packet's editable IDs apply.
- *History* lists runs through a project run-list route that is not there yet, the *Make bar's engine list* falls back to a built-in copy of the manifests, and *Rescore* has no route.

The manual follows the code; `docs/VERIFICATION.md` records the state at the last full verification.

## 6. The Make bar and quotes

**Nothing is spent before you have seen the price and where your pixels go.** Select an Edit, People or Motion node and the bar shows:

| control | meaning |
|---|---|
| Engine | only engines that make what this node makes and that the project's custody allows. On an NDA project today that is the local engines only. The advanced drawer shows every manifest parameter. |
| Preset | an engine preset (resolution and size); "default" if the engine has none |
| N | candidates, 1 to 6 (section 7) |
| Region crop | send the mask's bounding box plus 25% context at the engine's best size instead of the whole frame; the plate is never upscaled |
| Quote | asks the daemon to price the request |

A quote reads **provider · model · price in USD · custody grade · ETA**. The grade chip is green for C0, blue for C1, amber for C2, red for D. Before you quote, the bar shows the same line greyed out with "quote pending" and never a price. The price chip says `unconfirmed` while the number is a list price that has not been reconciled against a real bill (every cloud price is unconfirmed today). The ETA is a per-kind estimate until the engine cards have measured real runs.

A quote **expires after 15 minutes**; the bar counts down, and Make is enabled only while the quote is fresh. If the price moves by more than 5% or the engine's model version changes, you are asked to quote again. An expired quote moves the run to EXPIRED; just quote again.

**Who may approve what.** A person clicking Make approves. A request under USD 2.00 per run (strictly below) approves itself; at or over it, a dialog shows provider, model, price (the total for N candidates), custody and ETA, and you approve or cancel (Escape cancels, nothing is spent). There is also a daily ceiling of USD 50 across auto approvals, and an unattended ceiling of USD 20 once nobody has touched PLATE for 30 minutes. Agents always need a budget envelope that a human created beforehand; they cannot approve without one.

**Refusals, in plain words.** `custody` (HTTP 403): the route's grade is outside what the project allows; change the engine or the project setting, there is no override. `quote_expired`: quote again. `auto_cap`/`daily_cap`/`unattended_cap`: a human must approve. `envelope_required`/`envelope_exceeded`: an agent needs a bigger or a new envelope.

**The ledger.** Every quote, approval, submission, result, gate, override and export is appended to the project's `runs.jsonl`, hash-chained so tampering shows. Money is settled against the quoted price when the provider had the job and refunded when it never did. A daily job reconciles the ledger against vendor billing (target: within 1%).

## 7. N candidates

Set N to 2 to 6 and Quote once: the siblings share one quote, one expiry and one approval, and the dialog total is their sum. Candidates appear as a strip under the node, each row with a live state chip (QUEUED, RUNNING, GATING, DONE, GATE_FAIL ...). When every sibling has finished the strip ranks them by gate score. Click a row to compare it with the plate in the viewer (A is the plate, B is the candidate); with the strip focused, keys `1` to `6` pick that row as the node's output. A GATE_FAIL row shows its first failing check and an **Override (reason required)** button. Candidates come back registered to the plate, so they share its size.

## 8. The viewer and its keys

The compare viewer is WebGL2 and keeps 16-bit pixels from the daemon to the shader (a small amber **8-bit preview** badge means the machine has no float render targets). Click the viewer to focus it, then:

| key | action |
|---|---|
| `` ` `` | toggle A/B |
| `[` `]` | move the wipe by 5% (`{` `}` by 1%); you can also drag the wipe line |
| `1` | true 100% (one device pixel per image pixel, nearest-neighbour) |
| `f` | fit |
| `+` `-` | zoom by powers of two; the wheel zooms about the cursor and snaps to exactly 100% near it |
| arrows | pan 64 device pixels (Shift: 256); dragging also pans |
| `d` | difference (A minus B, times 4, in linear light) |
| `e` | edge overlay: red = edges only in the plate's G-buffer, cyan = only in the candidate, white = both |
| `h` | colour-difference heatmap (CIEDE2000: dark blue 0, blue 1, cyan 2, yellow 3.5, orange 5, magenta 10 and above) |
| `t` | tiles: one 100% crop pair at every point the gate flagged |
| `?` | the key list |

Pressing `d`, `e`, `h` or `t` again returns to wipe or toggle. A mode with no inputs (for example `e` without edge masks) says so and changes nothing. Double-click toggles fit and 100% at the cursor. A wipe or toggle between images of different sizes stretches B onto A and shows a badge saying so.

## 9. Reading the gate panel

The **Gate** tab shows the latest report of the selected take. The headline is **pass**, **advisory** or **fail**. Failures are listed first, then advisories, then passes; every row is metric, measured value, threshold, status.

- **hard** checks block delivery. A hard check that could not be measured counts as a fail: an unmeasured gate never passes.
- **advisory** checks are reported and never block (triage numbers, or a threshold still being calibrated).
- **calibration_needed** means the number is a first guess until the labelling data fixes it; it behaves as advisory.

| check | what it catches | notes |
|---|---|---|
| `registration.residual_px`, `registration.shift_px` | the output moved relative to the plate | hard: at most 1 px residual, 8 px shift |
| `outside_mask.de00`, `feather_band.de00_p99` | pixels changed where they must not | hard: outside the mask exactly 0; feather band p99 at most 1.0 ΔE00 (the feather-band number is the first candidate for recalibration, because a large in-mask edit can trip it) |
| `gbuffer_edge_recall_r2` | the output lost or moved real architecture edges | Tier A only: at least 0.90 (preview 0.85), edges weighted by depth-of-field blur |
| `invention_flags` | new long edges on planar architecture | Tier A only: must be 0 |
| `material.de00_median`, `material.de00_p90` | the worst locked material shifted colour | at most 2 and 5 ΔE00 |
| `ral.de00` | a RAL-recoloured panel against its swatch | at most 1 ΔE00 |
| `texture.lapvar`, `texture.fine_ratio` | AI over-texture or mush | valid only at 3840 px wide; on smaller frames it is advisory and says so |
| `tone.luma`, `tone.contrast`, `tone.sat` | the look drifted | advisory |
| `image_edge_agreement` | image-only edge agreement | advisory triage only |

On Tier C (client plate, no G-buffer) the edge-recall and invention checks do not exist; the panel shows depth agreement as advisory and the take is badged "estimated geometry". The **backend notes** line says what ran as a stand-in (for example the optical-flow backend: RAFT on the box, a simpler fallback on a laptop). **Version chips** show each stored report (n = 1, 2 ...); a lock marks the report that shipped. **Rescore** (its route is not in the daemon yet) adds a new version after a threshold or metric change and never rewrites an older one, and never touches a shipped report. **Open tiles** puts the viewer into tiles mode at the flagged points. The deeper reference is [gates.md](gates.md) and `docs/gates/`.

## 10. Override policy

Overrides exist so a deadline is never held hostage by a number, not so a number can be ignored. Each one needs a written reason and is ledgered with who and when.

| situation | what an override does | rules |
|---|---|---|
| GATE_FAIL take | lets the take be exported | reason of at least 10 non-blank characters; recorded in the run's event log and the ledger; the delivered file's `.gate.json` carries it |
| High or highest-risk move (orbit, aerial drift, threshold cross, or any move its dials have pushed that high) | unblocks Motion | reason of at least 10 characters; the node gets the label `unseen geometry guessed`, which travels downstream |
| An invented camera angle on an Edit | allows it | explicit override plus the same label, everywhere downstream |
| Render-it verdict `render_move`, once the router is in blocking mode | nothing | no override lifts it; render the move in V-Ray or Vantage |
| Custody (a route outside the project's grades) | nothing | change the engine, change the project, or get the client's written consent and record it |
| Concept to Export, a text-to-image request, upscaling the plate, a locked ID | nothing | these are not overridable |
| The semantic judge | nothing | it cannot pass a frame that fails a numeric gate |

## 11. Verdict keys, the scrub and the timer

**The full scrub.** A take can be accepted only after a logged full scrub: the **Full scrub** box in the verdict panel stays disabled until the viewer has shown you all four quadrants of the image at 100% or more (zoom to 100% with `1`, then pan through the four quadrants; the four small squares fill in as you go). Ticking the box logs the scrub with the daemon, which returns a scrub id; accepting a take (and the approval tick on a node) is refused without it.

**Verdict keys.** With a candidate selected: `1` Ship, `2` Fix, `3` Render it. Fix and Render it then ask for reason codes (click chips; Enter submits, Esc cancels). Ship goes straight through. The digit keys belong to the widget that has focus: inside the viewer `1` means "zoom to 100%" and inside the candidate strip `1` to `6` pick a row, so use **Shift+1/2/3** there. Verdicts are written through the daemon to `calib/labels.jsonl` (client-work data, gitignored); they are the ground truth the gates are calibrated against, so rate honestly and without looking at the gate first when you can.

Reason codes: `A1` geometry warp, `A2` texture crawl, `A3` scale drift, `A4` vegetation slop, `A5` re-cladding drift, `F2` slideshow, `F4` action ignored, `F6` camera improvises, `F7` continuity, `F9` extra people or objects, `F11` text or logos, `F12` light shift, and PLATE's own `P1` registration, `P2` invented edge, `P3` colour shift, `P4` texture band, `P5` tone band, `P6` outside-mask leak, `P7` people implausible.

**The timer.** The bet behind PLATE is measured in human minutes per finished deliverable, so there is a timer. In the **Timer** tab, name the deliverable, focus the panel and press Space to start; Space again stops. The daemon ledgers the elapsed minutes and the tab lists today's minutes per deliverable. Start it when you start working on a still or a clip, stop it when you stop.

## 12. Motion: CropPair, Move risk, previs

A camera move is the last thing you make, not the first: **Motion and CropPair stay locked until their upstream still has passed its gates and a human approved it** (with a logged scrub).

**CropPair** (the safe default for a push-in). Pick a centre on the approved still and two zoom levels, `1.0 <= start < end <= 3.0`. Both crops are cut from the still at native pixels, resampled to the engine's legal size with the same scale factor, and sent as the start and end frames of a Kling start/end clip. The clip is delivered as generated; the crop boxes are recorded so the move can be reconformed in Resolve. (No digital zoom is baked in, and no plate is upscaled.)

**Move risk.** Every move has a risk from the Lensbook ladder, shown as a label on the node:

| move | risk | what it invents |
|---|---|---|
| locked tripod | none | nothing; the default |
| push-in, slider drift | low | existing pixels enlarge; a thin slice of parallax |
| crane rise, light sweep | medium | upper-storey continuation and a new ground-plane angle; every shadow redrawn per frame |
| orbit up to 30 degrees, aerial drift | high | the elevation rotating into view; the roofscape and the whole context |
| threshold cross | highest | an entire unseen room: split the shot at the doorway, always |

The dials only ever raise a move's risk: a push-in or crane rise that reveals more than 3 m is at least medium and more than 10 m is high; an orbit past 30 degrees is highest (the model is drawing the unseen elevation); slider travel over ten centimetres is at least medium. High and highest are blocked until overridden with a reason of at least ten characters (section 10).

**The render-it router.** Before you spend, the router reads the packet's own depth, IDs and camera path and says whether AI should be making this at all. It answers two separate questions.

- *Can AI re-photograph this still?* The detail resolvability `D` is how many pixels the characteristic detail gets (a leaf 6 cm, a brick 6.5 cm, a sett 10 cm, fabric 2 cm) at its distance. `D` under 8 px means V-Ray renders it and AI is limited to people and patches (`vray_only`); 8 to 24 px means AI edits inside ID masks only (`ai_by_region`); 24 px or more allows a whole-frame re-photograph, for close and medium frames only (`whole_frame_ok`). Hero wide shots are never re-photographed.
- *Can AI make this move?* The disocclusion fraction is how much of the end frame was never visible from the start frame. Up to 2% is safe for AI interpolation (`ai_interpolate`); 2% to 10% wants rendered start and end frames plus a clay pass (`rendered_endpoints_plus_clay`); over 10%, an orbit over 30 degrees, or any threshold, room-change or occluder crossing on the path means **render the move in V-Ray or Vantage** (`render_move`) and AI does people only.

The verdict is **advisory** at first: the Motion editor shows it as a chip, the move is not stopped, and a move the router says to render is labelled `unseen geometry guessed`. It becomes **blocking** once it has been calibrated, and then a `render_move` verdict stops the move whatever override text you type. The editor shows which mode is active (`PLATE_ROUTER_MODE`, or the project setting, overrides the default). On a Tier C plate the router reports "unknown" because relative depth cannot give these numbers, and a person decides.

**Engines and custody for video.** Kling O3 4K and v3 Pro (start/end), Seedance 2.5 (image-to-video, reference-to-video) and Veo 3.1 are cloud routes, all effectively D today; Wan 2.2 VACE is local (C0) and makes previs only. **An NDA project has no cloud video path.** NDA video means local previs, a V-Ray or Vantage render, or the client's written consent naming the vendor ([consent-template.md](consent-template.md)).

**Video gates.** First and last frame must land on the approved stills (first at most 12/255, last at most 16/255 after an aspect-correct crop; both still being calibrated), the camera must stay locked (optical-flow median at most 1 px at 960 wide; hard on the box's RAFT backend, advisory on the laptop fallback, and the report states the backend), reprojection drift against the packet's G-buffer edges must keep at least 0.80 recall, and the clip must cover the shot. A **time map** relates packet frame numbers to engine frame numbers when the frame rates differ; the video player in the Motion editor shows it with the landing, lock and drift chips. See [gates/video.md](gates/video.md).

**Previs (Seedance 2.5 reference-to-video).** For a walkthrough, PLATE builds the move from the clay sequence: it splits the camera path into rooms by which architecture is on screen, picks the approved still that sees each room best as that room's reference (at most 30 references, the hero still first), writes timing hints from the path ("0-3s: lobby; 3-7s: corridor ..."), and runs a 480p draft first and the 1080p complete pass after you like the draft. **Every previs frame carries a tiled PREVIS watermark and the run is flagged `previs`; the canvas and the exporter refuse to send a previs output to Export, Review or Delivery.** Previs is for agreeing the move with the client, not for delivering.

**People.** The People node runs over STAFFAGE, which is itself an upload route (grade D), so it is refused on NDA projects unless STAFFAGE is configured as local-only. The person is placed with a ground point, given a depth in metres, and composited under the packet's geometry using Z, so a person behind a column is occluded by it.

## 13. Numeric nodes

These run in the daemon with no generator, no price (zero) and custody C0, still quoted and approved for uniformity.

- **Set** keeps a set of cameras consistent: pick a hero view; per material it takes the target colour from the hero and applies a numeric per-ID correction to the other views; the cross-view gate checks the 90th-percentile ΔE00 at world-position correspondences (target at most 3).
- **LightMix** blends the light-select passes by non-negative least squares to reach a mood without re-rendering; gains are seeded by the solver and adjustable, with presets.
- **Recolour** recolours a material by recomposing its render elements to a RAL or hex target, with an expected ΔE shown before you run it. A degraded path (whole-beauty scaling) is expected to fail its gate on dark semi-gloss panels; that is correct behaviour.
- **Light** asks MaxGaffer in Max to match a reference image for one camera and re-renders a new packet that supersedes the old one; the dock button is disabled when MaxGaffer is not installed (`MAXGAFFER_MISSING`). It can also start the Vantage live link.
- **Scout** asks Max to place temporary scout cameras, render low-resolution previews and, optionally, full packets; the temporary cameras are deleted afterwards unless you keep them.

When a client revises the scene, a new packet that `supersedes` the old one marks runs on the old packet superseded and re-binds nodes; approved layers whose touched IDs are unchanged (same name, material, class, and a mask overlap of at least 0.98) carry over, are re-composited on the new plate and re-gated; the rest are dropped with the reason (for example "material changed").

## 14. Delivery formats

Delivery reads the approved master, never a proxy. Every delivered file carries its **lineage** (project, node, run, packet, engine and version, gate report, time) inside the file (PNG text chunk, TIFF XMP, EXR header attribute, MP4/MOV comment) and a `<name>.gate.json` next to it. A file dropped back on the canvas finds its way home even if only the lineage survives; a stripped screenshot is matched by content and perceptual hash. Export never overwrites an existing file, refuses a GATE_FAIL take without an override (section 10), and refuses previs.

| format | for | how |
|---|---|---|
| TIFF 16-bit | stills for retouch | `plate export <run_id> --to <dir> --format tiff16` (the default) |
| PNG 16-bit | stills | `--format png16` |
| EXR | stills in linear light | `--format exr` |
| ProRes 4444 | video for the edit, with alpha when the layer has one; BT.709 limited range | `delivery.video.prores4444` |
| HEVC Main 10 | video for review and web; CRF 16, `hvc1`, BT.709 tags in the frames, the encoder and the bitstream; no alpha | `delivery.video.hevc` |
| EXR sequence | video for grading: scene-linear, half float, ZIP, Rec.709 `chromaticities` attribute | `delivery.video.exr_sequence` |
| FCPXML 1.10 | a timeline for Final Cut or Resolve: masters on the spine, alpha as a connected clip on lane 1, exact 23.976 rationals, escaped paths | `delivery.fcpxml.write` |
| AV1 re-tag | a Vantage 3.3 AV1 export that shows purple in a browser: a lossless stream copy that rewrites the colour tags only | `delivery.video.av1_retag` |

Stills go out through the `plate export` command below (or `POST /api/v0/runs/<id>/export`). The video formats are the `plate_daemon.delivery` library, called from Python with a `Provenance` built from the run (`Provenance.from_run(run, project_dir=...)`), which carries the lineage and the gate report; there is no button or command for them yet. Every encode refuses a previs output, and a GATE_FAIL run without an override reason, **before any output exists**; it then verifies the colour tags it wrote by probing the file, and writes the `.gate.json` sidecar.

Command line, from the repo root. The `plate` command works on the same per-machine index and project folders as the daemon, in its own process, so a run made here shows up in the canvas and the ledger like any other:

```
plate run <recipe id or .yaml> --project <dir> --packet <id|dir> [--engine ID] [--n N] [--approve auto|human] [--envelope USD] [--nda]
plate gate <plate> <candidate> [--packet <dir>] [--out report.json]
plate export <run_id> --to <dir> [--format tiff16|png16|exr] [--override-reason "..."] [--name STEM]
plate reindex <project>      plate gc <project> [--dry-run]      plate pack <project> --out <zip> [--full]
plate smoke [--live --budget-usd 5 --engines a,b]
```

`plate` is `scripts/plate` (Mac) or `scripts\plate.cmd` (Windows), the `plate` console script after `uv sync`, or `.venv/bin/python -m plate_daemon.cli`. `run` prints the quote and waits for `y` (`--approve human`, the default); `--approve auto` goes through the same per-run, daily and unattended caps as the daemon and exits 3 if one says no; `--envelope USD` is a hard ceiling either way. A vendor engine needs `PLATE_LIVE=1`; `PLATE_FAKE_ENGINE=1` swaps in the in-process fake engine for a dry run. `gate` compares a candidate with its plate at the same size (it never resizes either) and, with `--packet`, adds the G-buffer, material and invention gates for a Tier A packet. `export` refuses a GATE_FAIL take without `--override-reason` (ten characters or more), embeds the lineage, writes `<name>.gate.json` beside the file and never overwrites. Exit codes: 0 ok, 1 an error, 2 a gate failure, 3 a custody, budget or approval refusal (nothing was sent), 4 usage. `smoke` without `--live` replays the recorded vendor fixtures through every adapter's cheapest call and touches no network; `--live` needs `PLATE_LIVE=1` and an explicit budget, prints the custody grade and quote of every call before sending it, and stops before the running total would pass the budget.

## 15. Review bundles for the client

A review bundle is a zip the client opens from a folder with no install, no server and no network: a single `index.html` with a before/after wipe, a toggle, a 100% zoom, a gate badge per take (pass, fail or advisory with the first failing measurement) and a comment form that builds a `mailto:` link and a downloadable `comments.json`. Pictures are 8-bit sRGB **proxies**, never wider than 2048 px, tiled with a `REVIEW` watermark at 12% opacity, with the take number as the only file name (`r1_a.png`, `r1_b.png`): no project, run or engine name in any file name. The candidate's lineage record stays inside its proxy so a dropped copy can be traced home; pass `keep_lineage=False` to `export_review` for a bundle that must not carry ids. There is no 16-bit master in a bundle.

Make one from Python, `plate_daemon.review_bundle.export_review(project, run_ids, out_dir)` (it returns the zip path, and a second file next to it, not in the zip, maps takes back to runs for your own records), or through an agent's `export_review` tool. The Review node carries the canvas rules but has no button yet. Hosting the unzipped folder on Cloudflare R2 behind signed, expiring URLs is documented in `daemon/plate_daemon/review_template/HOSTING.md`; PLATE itself never uploads. On an NDA project an R2 bucket is a custody C2 route: send the zip by hand.

## 16. Agents

A Claude agent can drive PLATE through an MCP server (`python -m plate_daemon.mcp_server`) with eight tools: list projects, get a graph, add a node (Export and Review cannot be created by an agent), estimate a run, run a node (a budget envelope is mandatory), get a gate report, compare two runs, and export a review bundle. **There is no approve, no deliver, no master export and no override tool**, by design. A human creates the envelope first (`python -m plate_daemon.mcp_server --new-envelope --project <id> --usd 10 --hours 8`). Everything read from a plate (names, EXIF, lettering) is handed to the agent as data, never as instructions. An agent session is an egress route: it must run on an API key under commercial terms, never a consumer login, and on NDA projects the tools return numbers, ids and paths only, never pixels. See `docs/mcp.md`.

## 17. When something goes wrong

| symptom | go to |
|---|---|
| the daemon will not start, or the page says token missing | [runbooks/daemon-wont-start.md](runbooks/daemon-wont-start.md) |
| a vendor is down or slow on delivery day | [runbooks/vendor-down.md](runbooks/vendor-down.md) |
| a key was revoked, leaked or expired | [runbooks/key-revoked.md](runbooks/key-revoked.md) |
| a local model fails with out of memory | [runbooks/vram-oom.md](runbooks/vram-oom.md) |
| a vendor changed its API and a nightly check alerts | [runbooks/schema-change.md](runbooks/schema-change.md) |
| a Max update broke the dock | [runbooks/max-update-breaks-dock.md](runbooks/max-update-breaks-dock.md) |
| PLATE is down and the deadline is not | [runbooks/manual-pipeline-fallback.md](runbooks/manual-pipeline-fallback.md) |
| backup, restore, purge | [runbooks/backup-restore.md](runbooks/backup-restore.md) |
| the nightly jobs | [runbooks/nightly.md](runbooks/nightly.md) |

## 18. Appendix: folders, environment variables, ports

**Folders.** App data: `%LOCALAPPDATA%\Plate` on Windows, `~/Library/Application Support/Plate` on a Mac (`PLATE_HOME` moves it): `config.json` (token and daemon URL; the Max plugin adds `repo_path` and `packets_dir`), `index/<project_id>.sqlite` (a rebuildable cache, always on the local disk), `logs/`, `cache/`, `packets/`, `projects/`. A project folder holds `project.json`, `assets/<hash prefix>/<hash>.<ext>`, `runs.jsonl`, `gates/<run_id>.<n>.json`, `review/`, `packets.json`, and `outbound.jsonl` (one line before every request that leaves the box, one after). The app-data `logs/` folder holds the daemon logs, `alerts.jsonl` and the nightly records.

**Ports.** Daemon and built UI `127.0.0.1:4610`; pixels worker `4611`; labelling tool `4612`; Vite dev server `5173` (only under `PLATE_DEV=1`). Everything binds loopback.

**Environment variables** (all optional):

| variable | effect |
|---|---|
| `PLATE_HOME`, `PLATE_CONFIG`, `PLATE_TOKEN_FILE` | move the app-data folder, the config file; name a file holding only the token (wins over `config.json`) |
| `PLATE_PORT`, `PLATE_BIND`, `PLATE_TAILSCALE_IP` | daemon port and bind list (loopback by default; never all interfaces) |
| `PLATE_DEV` | allow the Vite dev origins (`127.0.0.1:5173`, `localhost:5173`) |
| `PLATE_FAKE_ENGINE` | run the in-process fake engine (no network, no spend) |
| `PLATE_LIVE` | required, with a budget, for any real vendor call; never set in tests or CI |
| `PLATE_FFMPEG`, `PLATE_FFPROBE`, `PLATE_OIIOTOOL` | locate the tools when they are not on PATH |
| `PLATE_PIXELS_PORT`, `PLATE_PIXELS_REAL`, `PLATE_MODELS` | the pixels worker: port, require real models, model cache |
| `PLATE_GPU_WINDOWS`, `PLATE_FORCE_LOCAL`, `PLATE_ALLOW_UNMANAGED_GPU` | when heavy local models may run (see [runbooks/vram-oom.md](runbooks/vram-oom.md)) |
| `PLATE_ROUTER_MODE` | render-it router: `advisory` or `blocking` |
| `ATLASCLOUD_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `BFL_API_KEY`, `ARK_API_KEY`, `WAVESPEED_API_KEY`, `MAGNIFIC_API_KEY` | vendor keys, read from the environment only, never written to a project or a log |
