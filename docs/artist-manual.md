# PLATE artist manual

For the person who sits at the render box and delivers stills and clips. It follows one job from install to a delivered file: install, launch, create a project, send a packet from Max, work the canvas (Plate, Mask, Edit, Comp, Gate, Accept, Export, Review), read the gates, deliver. Each section says what to click, what you should see, and what PLATE refuses and why. The engineering notes live in the other files under `docs/` (index: [README.md](README.md)); when something breaks, go to [runbooks/](runbooks/README.md).

**What PLATE is, in one paragraph.** PLATE keeps the render. The Max plugin exports a **Shot Packet** (camera, Z depth, normals, Cryptomatte IDs, the VFB-corrected 16-bit master) instead of a screenshot. AI is allowed to change pixels only inside the IDs the packet marks editable; outside that mask the delivered pixels are the render, bit for bit. Every output is scored by measured gates, and a failing take cannot be delivered without a written override. Every run shows its price and where your pixels go before you pay. That is the whole bargain; most refusals below are one of these rules speaking.

> **What has and has not been exercised (read once).** The Max plugin is tested against a fake `pymxs` and has never run inside Max. The PowerShell scripts were written on a Mac. Every vendor adapter runs on recorded fixtures: no live vendor call has been made, prices in the manifests are placeholders (`unconfirmed`), and the cloud custody terms are not held in writing yet, so every cloud route counts as grade D today (section 3). The GPU worker is in stub mode (no weights). The Wave 9 additions (references, Edit after a take, Comp stacks, the Colour, Atmosphere and Sky nodes, end stills, templates, cutdowns, Recomposite, engine cards and chips) have run only against the fake engine and recorded fixtures, which is also why a price in them says `unconfirmed` or `estimated`. The canvas, viewer and panels are tested in jsdom and a GPU smoke page, not yet driven by hand in a real browser against a real daemon. Wherever this manual says "you should see", treat a mismatch as a bug to report, not as your mistake. The box-day checklist is in `docs/VERIFICATION.md` once it exists.

## Contents

1. [Install](#1-install)
2. [Launch and the token](#2-launch-and-the-token)
3. [Create a project (custody)](#3-create-a-project-custody)
4. [Send a packet from Max](#4-send-a-packet-from-max)
5. [The canvas, step by step](#5-the-canvas-step-by-step)
6. [The Make bar and quotes](#6-the-make-bar-and-quotes)
7. [N candidates and engine comparison](#7-n-candidates-and-engine-comparison)
8. [The viewer and its keys](#8-the-viewer-and-its-keys)
9. [Reading the gate panel](#9-reading-the-gate-panel)
10. [Override policy](#10-override-policy)
11. [Verdict keys, the scrub and the timer](#11-verdict-keys-the-scrub-and-the-timer)
12. [Motion: CropPair, Move risk, previs](#12-motion-croppair-move-risk-previs)
13. [Numeric nodes](#13-numeric-nodes)
14. [References: pictures an engine may look at](#14-references-pictures-an-engine-may-look-at)
15. [Building on a take: Edit after Edit, Comp, Recomposite](#15-building-on-a-take-edit-after-edit-comp-recomposite)
16. [Recipes, tokens and the Project Sheet](#16-recipes-tokens-and-the-project-sheet)
17. [Templates, batches and Send + quote](#17-templates-batches-and-send--quote)
18. [Delivery formats and cutdowns](#18-delivery-formats-and-cutdowns)
19. [Review bundles for the client](#19-review-bundles-for-the-client)
20. [Agents](#20-agents)
21. [When something goes wrong](#21-when-something-goes-wrong)
22. [Appendix: folders, environment variables, ports](#22-appendix-folders-environment-variables-ports)

## 1. Install

Windows 10/11 x64 is the primary machine; the Mac is the development machine and runs the same code.

**Windows (the render box).** From the repo root in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

`bootstrap.ps1` installs `uv` and Node LTS with winget if they are missing (`-NoInstall` only checks), creates the Python 3.12 environment, runs `npm ci` and builds the UI (`ui/dist`, which the daemon serves at `/`). It never installs the GPU extra. `test.ps1` is the same suite as `make test`; run it once after install and expect it to finish green. `-ExecutionPolicy Bypass` applies to that one process only.

**Mac.** `scripts/bootstrap.sh` then `scripts/test.sh` (or `make setup` and `make test`). `bootstrap.sh` does the same steps (uv, Node, the environment, `npm ci`, the UI build) and ends by printing the config path and the launch URL.

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

Both start scripts build the UI first when `ui/dist/index.html` is missing (they print `building ui/dist`). A failed build is a warning, not a stop: the daemon still starts, so Max can keep sending packets while you fix the UI (runbook F). The daemon prints one line:

```
PLATE daemon http://127.0.0.1:4610/#token=<long token>
```

Open that exact URL in a browser on the box. The part after `#` is your install's token. A browser never sends a URL fragment to the server: the page moves the token into the tab's session storage, removes it from the address bar and sends it as an `Authorization` header from then on. A reload keeps it; a new tab, a new window or another browser needs the full URL again (`scripts/plate_config.py url` prints it again). The status bar at the top of the canvas shows `daemon: up` and `token: present`; `token: missing` means you opened the page without the `#token=` part.

Rules of the road: the daemon always accepts `127.0.0.1:<port>` and `localhost:<port>` as the page's address (Host and Origin); only the Vite dev server's origins (`127.0.0.1:5173`, `localhost:5173`) need `PLATE_DEV=1`. A browser keeps the token per address, so a tab opened on `localhost` needs the full URL once even if another tab on `127.0.0.1` already has it. The token lives in `config.json` (or in the file `PLATE_TOKEN_FILE` names; there is no separate `token` file in the app-data folder). For curl and the smoke steps, `.venv/bin/python scripts/plate_config.py token` prints it (`.venv\Scripts\python.exe` on Windows). The daemon listens on loopback only, and reaching it from the Mac over Tailscale needs `-Tailscale` plus one scoped firewall rule (see `docs/windows.md`); do not paste the token into chat, tickets or screenshots. If the daemon exits with "another daemon already runs", you started it twice; see [runbooks/daemon-wont-start.md](runbooks/daemon-wont-start.md).

## 3. Create a project (custody)

A project is a folder: `project.json` (the canvas graph and the custody setting), `assets/` (every stored image, named by its hash), `runs.jsonl` (the append-only ledger of every quote, approval, run and export), `gates/` (versioned gate reports), `review/`, `packets.json`, `references.json` (the pictures uploaded for Reference nodes) and, if you save any, `recipes/` and `templates/`. By default it lives under the app-data folder; a project may sit on the NAS because the folder is the truth and the daemon's SQLite index is only a cache kept on the local disk. Only one machine writes a project at a time (a lock file enforces it).

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
| PROJECT | the project this scene sends to: a combo of the daemon's projects, or type a new id (it is slugged). The choice is kept in `<scene>.plate.json` next to the `.max` file. A label shows the project's custody and turns amber, `custody unset (C0 only)`, after a send to a project nobody has set custody on yet. |
| CAMERAS | the scene's cameras (hidden ones are marked). Tick the cameras to send; the recipe below edits the focused one. **Refresh** rereads the scene. |
| RECIPE | per camera and stored next to the scene in `<scene>.plate.json`: **Engine** and **Preset** the shot is meant for (labelled `used by Send + quote`: nothing else reads them, a plain Send carries neither), **Safe frame** (the deliverable crop inside the render, x y w h), **Overscan** (render wider or taller than the camera so the engine gets its native aspect; the original frame stays centred), and **EDITABLE CLASSES**. |
| ACTIONS | **Send Packet**, **Batch Cameras** (every ticked camera, in order, one summary at the end), **Send + quote** (Send, then lay an Edit chain on the canvas and quote it; with an **auto-approve under cap** tick, off by default; section 17), **Send Motion**, **Light**, **Scout**, **Receive**. The **supersedes last** tick makes each new packet supersede that camera's last packet from this dock, so approved layers carry over where the IDs are unchanged (section 13). |
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
| `SHIFTED_CAMERA` | lens shift, tilt or lens distortion is on | V-Ray's vertical tilt correction (two-point perspective) or a vertical lens shift is a **warning**, not a stop: Send renders it as a level camera and a crop (below). Distortion, a horizontal shift or tilt, tilt and shift together, a pitch beyond 45 degrees and a rolled camera still stop the Send: zero them |
| `HIDDEN_CAMERA` | the camera, or its layer, is hidden | unhide it |
| `BAD_NAME_CHARS` | a node name contains `:`, `/` or `\` | rename it; node names become part of file names |
| `UNLABELLED_SHARE` | nodes on no labelled layer cover more of the frame than the limit (5% by default) | put those nodes on a labelled layer |
| `LOCKED_AND_EDITABLE` / `UNLABELLED_EDITABLE` | the policy contradicts itself | a bug if you see it; report it with the scene name |

Other stops you may meet: *colour mode: ...* (Max's colour manager cannot be read, or OCIO has no display or view: fix the colour setup, PLATE will not guess), *render elements missing: ...* (add them in Render Setup), *the EXR does not hold what the manifest would claim* (a render element was renamed or half-float Cryptomatte was used: Cryptomatte must be 32-bit float and lossless), *Cryptomatte manifest gives X id ..., its MurmurHash3 is ...* (set the VRayCryptomatte id type to node name).

**Two-point perspective cameras (level-and-crop).** A V-Ray camera with vertical tilt correction (auto, or a tilt value) or a vertical lens shift keeps its verticals vertical, which a Shot Packet 0.1 pinhole cannot describe, but which is a crop of one. Send does not refuse it: it renders a temporary level copy of the camera (your camera is never touched; the copy is deleted afterwards) on a taller frame, and records your original frame as the packet's safe frame. Every gate then runs on an ordinary packet. The dock lint shows `SHIFTED_CAMERA` as a warning, and the packet's overscan note says `level-and-crop from a tilt-corrected camera: pitch X deg, dy Y px`. Two things to know. The file is taller than your frame, and the part you deliver is the safe frame (section 18). And V-Ray's unit for a manual vertical shift has not been measured yet, so a shifted (as opposed to tilt-corrected) camera is read as a fraction of the frame height until someone measures it on the box. The runbook is [two-point-perspective](runbooks/two-point-perspective.md).

**What the daemon answers.** Sent: `202 ingesting`. Already sent: `200 known` (re-sending the same packet is safe). Stopped: `409` (this packet id exists with different content: rebuild the packet), `422` (the packet failed validation; the dock shows the error list), `426` (plugin and daemon disagree on the packet version; update on a quiet day), `401` (token rejected: re-run the installer or fix `config.json`), no answer (daemon not running). After a good Send the packet is registered with the project; link a Plate node to it with the Plate editor (section 5, step 1).

**Receive** puts a finished image back into Max for an in-Max A/B without touching your scene: into the V-Ray VFB history (compare with V-Ray's own tools) or as the active viewport's background image.

## 5. The canvas, step by step

**Layout.** The top bar shows `daemon: up` and `token: present`. The canvas fills the middle; `+ node` (top left) adds a node of any kind. The Make bar is docked at the bottom and acts on the selected node. The right-hand side holds the node list (and the last refusal), the editor for the selected node, the compare viewer, and the panels (tabs Projects, History, Gate, Custody, Timer, Spend and Settings; the verdict keys stay visible on every tab). One dark theme. The status bar carries three chips: the save state (`saved 14:02:11`, `unsaved`, `saving…`, or red `save failed: <message>`); the latest alert from the daemon until you dismiss it (amber for a warning, red for an error: a nightly check, a budget line, quotes waiting for approval); and the Max dock chip, which asks `GET /plugin/status` every 30 s and reads green `Max dock · polled 12 s ago`, amber when jobs are queued and no dock has polled for more than 30 s, grey when no dock has ever polled. The **Spend** tab shows, per engine and ISO week, what was quoted, what was billed, how many takes were accepted and the cost per accepted take (an **All projects** switch widens it to the whole studio); every figure is read from the ledger. **Settings** is read-only: the daemon version, the folders it watches, dev mode, router mode, the pixels worker, the nightly jobs and which vendor-key variables are set (names only, never values), with a **Help** link that opens this manual in a new tab.

**Node kinds.** There is no text-to-image node and no "image creator" anywhere; every generation starts from a packet's own camera.

| kind | what it is |
|---|---|
| Plate | one packet: its beauty and, for nodes that read elements, the packet itself (Z, normals, Cryptomatte, camera). Tier A is a render with a G-buffer; Tier C is a client plate with estimated geometry, badged "estimated geometry" |
| Mask | which IDs the next Edit may change (Cryptomatte pick, paint, SAM click, OCR text lock) |
| Edit | a generative edit through an engine (relight, vegetation, sky, close re-photograph) |
| Comp | stacks accepted takes over a base, each inside its own mask (section 15). Every Edit already composites its own take mask-locked, feathered inside the mask, with signage pasted back bit for bit; Comp is for combining takes |
| Colour | a numeric grade in linear light (exposure, white balance, tint, contrast, saturation, hue) inside a scope; no generator is ever asked to fix colour (section 13) |
| Atmosphere | depth fog computed from the packet's own Z (section 13) |
| Sky | a reference sky photograph placed inside the render's own sky coverage, horizon from the camera (section 13) |
| LightMix, Recolour, Set, Light, Scout | numeric and Max round-trip nodes (section 13) |
| Gate | the measured gate report for a candidate against its plate |
| Judge | the semantic judge (two-sample); it can never pass a frame that fails a numeric gate |
| CropPair, Motion | start/end crops of an approved still; a camera move (section 12) |
| People | people over STAFFAGE, occluded by the packet's Z |
| Compare | A/B of two takes |
| Export, Review | delivery: files for the client's pipeline; a static review bundle |
| Concept | the only unmeasured thing on the canvas: watermarked, "CONCEPT, UNMEASURED", can never reach Export, Review or Delivery |
| Reference | a picture an engine may look at (section 14); never composited, never a plate anchor |
| Frame | a labelled box to group nodes by eye; an annotation with no ports that every rule ignores |

The `+ node` palette offers every kind except Compare (a Compare metric run comes later; a saved graph that already holds one still opens). Colour is back in it: a numeric grade in linear light, never a request to a generator (section 13).

Every node shows a header with its kind, its tier (`measured`, or `concept_unmeasured` in amber with a watermark icon), the gate result (pass, fail, advisory), an approval tick and, for moves, a risk label. Handles are typed by what they carry (`image16`, `mask`, `packet`, `video`, `report`, `reference`); a wire only joins an output to an input that accepts its type.

**Working on the canvas.** Right-click a node for its menu: Open in viewer, Set as A, Set as B (section 8), Duplicate, Focus, Copy prompt, Copy output hash, Delete (on a selected node these act on the whole selection). A duplicate keeps the parameters and its incoming wires and starts with no take and no approval, 40 px down and to the right. Right-click the empty canvas for Add node here, Fit, Tidy and Clear graph (it asks again inside the menu, then saves at once). **Tidy** lays the graph out left to right by distance from the Plate (a column every 340 px, a row every 180 px; Frames stay where they are). Drag a wire out of an output and let go on empty canvas: a picker lists the kinds whose first input takes that output, and a pick adds the node there and wires it through the same checks as a hand-made wire; a kind the rules would refuse is listed greyed with the reason. Keys on the canvas: Backspace or Delete removes the selection, `z` focuses it, Ctrl or Cmd+D duplicates. Double-click a node to focus it and open it in the viewer; hover a node to see its `prompt: ... · run <hash>`. With no project open the canvas shows an empty-state card (Open a project, New project, Import client plate). The graph saves itself: a structural change 400 ms after you stop, a move 1.5 s after you let go of the node, always right before a quote; the browser tab is titled `PLATE · <project>`, with ` *` while there is something unsaved. A **Frame** is for you only: a 480 × 320 px box titled `frame` that sits behind the other nodes and moves on its own (moving it does not move the nodes over it). It takes no wires, and has no resize handle or title field yet: a different size or title is set on the node's `params` (`w`, `h`, `title`).

**The walkthrough: Plate, Mask, Edit, Comp, Gate, Accept, Export, Review.**

1. **Plate: link it to a packet.** Add a Plate node and select it. The **Plate editor** lists the project's packets, each with its shot id, tier badge, camera, the time it arrived and a thumbnail of the beauty; a packet that a later one supersedes is greyed. Type in the search box to filter (shot, camera or packet id), then click a packet to link it. The node now shows that thumbnail and its tier. A Plate with no packet says `no packet linked` in amber, and nothing downstream can be quoted from it. A packet appears in the list once its ingest is finished; the list refreshes itself when Max (or the client-plate import below) delivers one, and **Refresh** forces it.
2. **Mask.** Wire Plate to Mask. Click a pixel in the plate to pick the object under it (the editor shows the ID, its name and its class); pick as many as the edit needs, or paint a region or click with SAM; a painted or SAM mask is uploaded to the project. The mask the edit may touch is the picked IDs plus the painted and SAM regions, kept to the packet's editable IDs. A locked ID (architecture, glazing, signage, anything unlabelled) is never editable: if you pick one, the quote is refused with `ids [...] are locked by the packet policy`, and a painted mask has the locked IDs cut out of it automatically. With no Mask node the edit may touch every editable ID in the packet's policy (on a Tier C plate with no editable IDs, the whole frame). Signage inside the frame always gets an OCR text lock (the lettering is pasted back untouched); the **show OCR text-lock boxes** switch only draws those boxes, it cannot turn the lock off.
3. **Edit: pick a recipe or type a prompt.** Wire Mask (and the Plate image) into Edit and select it. The **Edit editor** has a **Recipe** select (the still-edit recipes, grouped, each with its title and engine; section 16), a **Prompt** box, a **Task** select (relight, people, vegetation, sky, rephotograph close) and a **Shot class** select (wide, medium, close). Picking a recipe also sets the Make bar's engine when you have not chosen one. Text in the Prompt box wins over the recipe; the line `prompt source: explicit` or `prompt source: recipe` says which one will be sent. A recipe's prompt is assembled from its sheet and, only when the project's custody allows the Claude route, composed by Claude; otherwise (every NDA project today) the template is simply filled in. An engine that needs a prompt and has none is refused at the quote (`prompt_required`) before anything is created or spent. Then pick an engine in the Make bar, Quote, read the price and the custody grade, Make (sections 6 and 7). To show the engine a picture, wire a Reference to the Edit's `refs` input (section 14); to start from an earlier take instead of the plate, wire that take to its `image` input (section 15); the recipe, its tokens and saving your own are in section 16. Behind the one click the run does: custody check, egress (an 8-bit sRGB PNG under an opaque name, metadata stripped; the mask goes along, in the same geometry as the picture, only to engines that declare a mask input and whose route can take one, and the composite enforces it either way, so an engine that is sent no mask still cannot change a pixel outside it), the engine, ingress (undo the size map and re-register the engine's pixels onto the plate on the ring outside the mask), composite, text lock, gates.
4. **Comp.** The composite is the plate everywhere outside the mask, by construction (outside-mask difference is exactly zero). Inside the mask the edit is blended with a 4 to 12 px feather that lives inside the mask, so there is no halo outside it. Every Edit does this for its own take; the separate **Comp** node, which stacks several accepted takes, is in section 15.
5. **Gate.** The gate report scores the composite against the plate (section 9). A pass turns the node's gate badge green; a fail keeps the take, flags it, and blocks export.
6. **Accept the take after the scrub.** Look at the candidate at 100% (section 8), do the full scrub (section 11), then press **Accept take** in the verdict panel (a Ship verdict, key `1`, offers the same button). The button stays disabled until the scrub is logged. The daemon records the acceptance and gives the node its approval tick; it works out each node's gate result and approval itself every time the graph is saved, so an approval the browser makes up does not survive.
7. **Export.** Wire the approved output to Export and select the node. The **Export editor** lists the upstream node's takes (finished ones first) and asks for a format (`png16`, `tiff16` or `exr`), an output folder (an absolute path on the machine that runs the daemon), an optional file name and, for a GATE_FAIL take, an override reason of at least 10 characters (section 10). **Export** writes the file with its lineage and `<name>.gate.json` beside it, shows both paths, and marks the take's gate report as shipped. Export does not wait for Accept (it is the hand-off), and it never overwrites a file and refuses previs (section 18). The same editor does batch export and social cutdowns (section 18).
8. **Review bundle.** For a client round, wire takes into a Review node. The **Review editor** lists its upstream takes (finished or overridden); tick the ones to show, set the watermark text (default `REVIEW`) and an optional title, and press **Build bundle**. It shows the zip's path and size (section 19).
9. **Import a client plate.** When the client sends a plate instead of a V-Ray render, open the **Projects** tab and expand **Import client plate**, then choose *Tier C plate* or *Client kit*. A Tier C plate needs the client's consent record (the field is required and the daemon's own refusal text is shown as written); a kit takes the folder the client's MAXScript kit wrote (Z, normals, Cryptomatte and the camera JSON; `plugins/max/maxscript/README.md`) together with the client's plate, and makes a measured Tier A packet from them. Submit shows `packet <id> ingesting`; when it finishes, the packet is in the Plate editor's list like any other (a Tier C plate is badged "estimated geometry"). The Projects tab's **Custody** row edits the project's NDA flag and allowed grades; loosening them needs a written reason.

**The rules the canvas enforces.** A wire that breaks one never connects: the message shows on the target node for four seconds and in the node list. The daemon re-checks every edge when the graph is saved (the browser check is only a courtesy), so a hand-edited graph cannot get around it.

| rule | message you will see | doctrine |
|---|---|---|
| no loops | `cycle` | the graph must stay a DAG |
| concept stays out of delivery | `concept tier cannot reach Export` (or Review) | geometry first |
| generation hangs off a plate | `generation must be anchored to a plate camera (source-view-only)` | no invented angles |
| depth at most 2 generations from the plate | `generation depth > 2 from the plate` | stop drift compounding |
| a reference is an input, not pixels | `a reference feeds an engine's reference input only; it is never composited` | references never alter a locked pixel |
| a Frame is only a label | `a Frame is an annotation; it takes no edges` | |
| a camera move waits for a good still | `motion is locked until the upstream still passes gates and is approved` (or `crop pair`; an unapproved still on a Motion's `end` input is refused the same way, naming the `end still`) | video last |
| high-risk moves need an override | `high risk move is blocked until it is overridden with a reason` | section 10 |
| an invented camera angle needs an override | `a camera override needs an explicit override and the label 'unseen geometry guessed'` | section 10 |
| no accept without a full scrub | `'<label>' cannot be accepted without a logged full scrub` | section 11 |

The manual follows the code; `docs/VERIFICATION.md` records the state at the last full verification.

## 6. The Make bar and quotes

**Nothing is spent before you have seen the price and where your pixels go.** Select an Edit, People or Motion node and the bar shows:

| control | meaning |
|---|---|
| Engine | only engines that make what this node makes and that the project's custody allows. On an NDA project today that is the local engines only. Each option reads `name · grade`, plus, once the engine has a card for this task and shot class, `≥ 70% (24) · $0.034`: the lower 95% bound of its gate pass rate, how many scored runs that rests on, and its mean cost. The advanced drawer shows every manifest parameter. |
| Compare engines… | quote the node on 2 to 5 engines at once as one group (section 7) |
| Preset | an engine preset (resolution and size); "default" if the engine has none. There is no preset select for an Edit: its recipe lives in the Edit editor (section 16) |
| N | candidates, 1 to 6 (section 7) |
| Region crop | send the mask's bounding box plus 25% context at the engine's best size instead of the whole frame; the plate is never upscaled |
| Quote | asks the daemon to price the request |

**Engine cards and the chips.** PLATE keeps a card per engine, task and shot class, built from the project's own ledger and gate reports: how many runs passed their gates (shown as the pessimistic 95% lower bound, so five lucky runs never look like a record) and what they cost on average. A card with 20 scored runs steers: the router ranks engines by that bound, and the option is drawn bold. Below 20 it is shown for information only. Under the engine select a row of chips says what the manifest declares and how the route is doing:

| chip | meaning |
|---|---|
| `max 4096×4096` | the largest size the engine answers at; the plate is never upscaled to fit it |
| `end frame` | takes an end still (a start/end move, section 12) |
| `refs ≤ 13` | how many reference images it takes beside the plate (section 14), where it states a limit |
| `elements` | takes element references for a move |
| `per image`, `per second`, ... | how the vendor bills it |
| `unverified` (amber) | nothing in this manifest has been checked against the vendor yet; the first live smoke test does that |
| `degraded` (amber) | three provider failures in a row on one route within the last hour: the router skips that route, and the quote's reasons name it |
| `suspended` (red) | the nightly checks suspended the engine: its option is greyed and the daemon will not route to it |
| `≥ 70% (24) · $0.034` | the engine's card for this task and shot class (bold when it steers) |

An engine with no vendor model id yet is greyed out as well (`model id unknown until the P0 smoke test`). `plate cards` prints the same cards, and `plate estimate` turns a quote into the expected price per accepted output (the quote times the count, divided by the card's lower bound; with no card it says so and divides by 1).

A quote reads **provider · model · price in USD · custody grade · ETA**. The grade chip is green for C0, blue for C1, amber for C2, red for D. Before you quote, the bar shows the same line greyed out with "quote pending" and never a price. The price chip says `unconfirmed` while the number is a list price that has not been reconciled against a real bill (every cloud price is unconfirmed today). A price that depends on a setting names the tier it used (`quality high · size 2K`), and a figure PLATE worked out itself instead of reading it from a price list says `estimated` in amber: Nano Banana 2.1 at 2K and 4K is quoted at 2 and 4 times its 1K list price, and GPT Image 2.5 on low quality at half of medium, until a real bill replaces the guess. Read an estimated price as a floor. The ETA is a per-kind estimate until the engine cards have measured real runs.

A quote **expires after 15 minutes**; the bar counts down, and Make is enabled only while the quote is fresh. If the price moves by more than 5% or the engine's model version changes, you are asked to quote again. An expired quote moves the run to EXPIRED; just quote again.

**Who may approve what.** A person clicking Make approves. A request under USD 2.00 per run (strictly below) approves itself; at or over it, a dialog shows provider, model, price (the total for N candidates), custody and ETA, and you approve or cancel (Escape cancels, nothing is spent). There is also a daily ceiling of USD 50 across auto approvals, and an unattended ceiling of USD 20 once nobody has touched PLATE for 30 minutes. Agents always need a budget envelope that a human created beforehand; they cannot approve without one.

**Refusals, in plain words.** `custody` (HTTP 403): the route's grade is outside what the project allows; change the engine or the project setting, there is no override. `quote_expired`: quote again. `auto_cap`/`daily_cap`/`unattended_cap`: a human must approve. `envelope_required`/`envelope_exceeded`: an agent needs a bigger or a new envelope.

**When an engine answers smaller than the frame.** A vendor tier can answer at 1K for a 4K region. PLATE never upscales a plate, but the AI layer, which is composited inside the mask anyway, may be: when the answer is at least 1.25 times smaller than the region it has to fill, a Real-ESRGAN pass (local, C0) replaces the plain resample, and the run records what it did (`normalized.ingress_hooks.upscale_ai_layer`). Without the GPU extra or the weights (this Mac, CI) that pass is the same Lanczos resample, flagged `stub`. `params.upscale_layers` is `auto` (default), `off`, or `magnific`: Magnific Precision v2 is used only when the quote declared it (its per-image price is added to the quote, so you approve the real total), only on a project whose custody allows that route (never NDA) and only with `PLATE_LIVE=1`; if any of that has changed by the time the take arrives, the free pass runs instead and the record says why.

**The ledger.** Every quote, approval, submission, result, gate, override and export is appended to the project's `runs.jsonl`, hash-chained so tampering shows. Money is settled against the quoted price when the provider had the job and refunded when it never did. A daily job reconciles the ledger against vendor billing (target: within 1%). The Spend tab (section 5) and `plate spend` read the same ledger.

## 7. N candidates and engine comparison

Set N to 2 to 6 and Quote once: the siblings share one quote, one expiry and one approval, and the dialog total is their sum. Candidates appear as a strip under the node, each row with a live state chip (QUEUED, RUNNING, GATING, DONE, GATE_FAIL ...). When every sibling has finished the strip ranks them by gate score. Click a row to compare it with the plate in the viewer (A is the plate, B is the candidate); with the strip focused, keys `1` to `6` pick that row as the node's output. A GATE_FAIL row shows its first failing check and an **Override (reason required)** button. Candidates come back registered to the plate, so they share its size. Each row is labelled `<node> · <engine> · attempt n` and has **Set as A** and **Set as B** (section 8).

**Compare engines.** N copies from one engine tell you about luck; the same job on several engines tells you about the engines. **Compare engines…** (beside the engine select) turns the engine select into a list: tick 2 to 5 engines, Quote, and the bar shows one group line (every engine with its price and custody grade, the total, and the least private grade of the group). The takes share one approval: Make approves the whole group against the total, once. When the last engine has finished, the strip ranks the takes across engines by gate score, and the labels tell you which engine made each. If any one engine is refused (custody, a missing prompt, no price), none of the group is quoted: the quotes already made are cancelled and the message names the refused engine, so no half group is left in the history. Do a comparison when a look is new to you; for routine work let the cards decide.

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

Pressing `d`, `e`, `h` or `t` again returns to wipe or toggle. A mode with no inputs (for example `e` without edge masks) says so and changes nothing. Double-click toggles fit and 100% at the cursor. A wipe or toggle between images of different sizes stretches B onto A and shows a badge saying so. A `W × H` chip at the corner of each loaded side always shows its size in pixels.

**Enlarge, fullscreen and A/B.** The viewer's **Enlarge** button hides the canvas (its state is kept; **Restore** or Esc brings it back) and lets the viewer fill that area, with the selected node's candidate strip docked below it. **Fullscreen** does the same and takes the page fullscreen. Outside the viewer `e` toggles Enlarge and `f` goes fullscreen; inside the viewer `e` and `f` keep their meanings (edges, fit). A and B are no longer tied to plate and candidate: each candidate row has **Set as A** and **Set as B**, a node's right-click menu has the same, a click on a History row opens that take against its plate and Shift-click puts it on side A. So you can set two engines' takes side by side, a take against its base, or last week's take against today's. The caption reads `A <label> · B <label>`, each label `<node> · <engine> · attempt n`. Selecting a node when nothing is being compared shows its picture alone, captioned `<label> (no comparison)`. One rule does not bend: only plate-versus-candidate counts as a scrub, so with any other pair the verdict panel says `compare a candidate with its plate to scrub` and the scrub is not recorded. The Re-mask bar (section 15) sits under the viewer when the take you are looking at kept its raw answer.

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

On Tier C (client plate, no G-buffer) the edge-recall and invention checks do not exist; the panel shows depth agreement as advisory and the take is badged "estimated geometry". The **backend notes** line says what ran as a stand-in (for example the optical-flow backend: RAFT on the box, a simpler fallback on a laptop). **Version chips** show each stored report (n = 1, 2 ...); a lock marks the report that shipped. **Rescore** (`POST /api/v0/runs/<id>/gate/rescore`) adds a new version after a threshold or metric change and never rewrites an older one, and never touches a shipped report. **Open tiles** puts the viewer into tiles mode at the flagged points. The deeper reference is [gates.md](gates.md) and `docs/gates/`.

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

**CropPair** (the safe default for a push-in). Pick a centre on the approved still and two zoom levels between 1.0 and 3.0: a push-in (start below end), a **pull-back** (start above end; the editor labels it `Pull-back`, and it has the push-in's risk row) or a **locked** frame (both equal). Both crops are cut from the still at native pixels, resampled to the engine's legal size with the same scale factor, and sent as the start and end frames of a Kling start/end clip. The clip is delivered as generated; the crop boxes are recorded so the move can be reconformed in Resolve. (No digital zoom is baked in, and no plate is upscaled.)

**An end still.** A Motion node has an `end` input beside its start `image`. Wire a second approved still to `end` and the engine gets the two real pictures as its first and last frame: nothing is cut from one still, and nothing is invented between them but the motion. Both stills must have passed their gates and been approved by a person (the same rule as any move, and the refusal names the `end still`), and both must come from the same camera: the same packet, or two packets with the same camera name and scene hash (otherwise `end_frame_camera`). The Motion editor's `Stills` row shows the start still and the end still (or `none: single-still move`) with the lock state of each. With nothing on `end` the node cuts its start and end frames from the one approved still, as before. The start/end engines are Kling O3 4K, Kling v3 Pro and Veo 3.1 (and, start-frame only, Seedance 2.5 image-to-video and Gen-4.5, which take no end still: an `end` wired to them is refused as `end_unsupported`); Veo is grade D until Vertex's written terms are filed, so an NDA project never sees it and an open project sees the D chip.

**Light-beat reveals.** The pattern for a day-to-dusk or dim-to-lit reveal: make both states as Edits off the same Plate (the day look as one Edit, the relit look as another, using a recipe from the lighting group, section 16; a Plate cannot be wired straight in, because a start or end still must be a take that passed its gates and was approved), scrub and Accept both takes, wire the first to `image` and the second to `end`, pick the recipe **Time of day (two stills)** and quote. The recipe is a locked tripod: the camera does not move and the light changes between two real frames. The video gates still check that the clip lands on both stills and that the camera stayed locked. A reveal never joins two different cameras: that would be an invented move. The recipe will not quote without an end still (`end_required`).

**Locked, slider and single-image moves.** A locked move keeps one zoom; its end crop equals its start crop and the camera-lock gate measures that nothing moved. Slider drift keeps the zoom but moves the end crop a few percent sideways. A locked tripod may send the start frame alone (`single_image`: no end frame is pinned), which is allowed only for a frame that holds. Reference nodes wired to `elements` go to engines that take element references (the `elements` chip); an engine without them refuses the quote (`elements_unsupported`). Every parameter the chosen engine declares (for example `cfg_scale`) is in the advanced drawer and reaches the job.

**Motion recipes.** The Motion editor's `recipe` select lists twelve shipped recipes with their move and risk, none above medium: Living render (locked), Slider drift, Crane rise, Light sweep, Weather pass, Night glow, Vegetation breeze, Material macro, Water study, Entourage life, Pull-back reveal and Time of day (two stills). The `prompt` box overrides the recipe's text, and the assembled text is shown below it. The high-risk shots of the library (orbit, aerial drift, threshold cross) are deliberately not shipped: render those. The format is in `daemon/plate_daemon/recipes/README.md`.

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

**Engines and custody for video.** **Runway is the preferred route** for Seedance 2.5 (image-to-video and reference-to-video), Kling O3 4K, Kling 3.0 Pro (PLATE's `kling-v3-pro`), Veo 3.1 and Gen-4.5 (start frame only); Atlas, Vertex, BytePlus and WaveSpeed stay as fallbacks for the same models. Runway is **grade D** on its self-serve terms (the Terms of Use give Runway a training licence on what you send it), so an NDA project never sees it; every cloud video route is effectively D today. Wan 2.2 VACE is local (C0) and makes previs only. **An NDA project has no cloud video path.** NDA video means local previs, a V-Ray or Vantage render, or the client's written consent naming the vendor ([consent-template.md](consent-template.md)).

- *What you see.* The quote line reads `runway · <model> · USD x · D`, and the chip row under the engine select shows each route of a video engine with its grade and per-second price (for example `runway · D · USD 0.43 per second`), the preferred route first.
- *Changing the preference.* It is a project setting, written through the daemon: `PUT /projects/{id}/settings {"preferred_video_provider": "none"}` keeps each manifest's own route order (`"atlas"` puts Atlas first), and `{"video_route_pins": {"kling-o3-4k": "atlas"}}` pins one engine to one route while the others keep the preference. A `route_id` in a quote request wins over both. There is no Settings-panel control for this yet.
- *A frame Runway cannot serve.* Runway centre-crops a frame whose aspect it has no ratio for, which would move a crop pair's landing, so such a frame (a 2.39:1 crop on Kling O3 4K, say) goes to Atlas and the quote's reasons say so.
- *Durations snap up on every route.* A Veo shot of 5 s becomes 6 s (Runway takes 4, 6 or 8), and a Kling shot under 3 s becomes 3 s; the take outlasts the shot, and the quote prices the length it will run.
- *What Runway cannot express.* A negative prompt is folded into the prompt as a last `Avoid:` line, a seed is not available on Kling (the quote refuses it), and Seedance **480P is a Runway draft** (the cheap preview tier, billed at the 480p rate). Seedance asks at least USD 0.80 per clip. If a setting does not exist on the Runway route (`camera_fixed`, `return_last_frame`) the quote says so; pin the Atlas route for it.
- *When a Runway run goes wrong,* see [runbooks/runway.md](runbooks/runway.md); the engine details are in [engines.md](engines.md).

**Video gates.** First and last frame must land on the approved stills (first at most 12/255, last at most 16/255 after an aspect-correct crop; both still being calibrated), the camera must stay locked (optical-flow median at most 1 px at 960 wide; hard on the box's RAFT backend, advisory on the laptop fallback, and the report states the backend), reprojection drift against the packet's G-buffer edges must keep at least 0.80 recall, and the clip must cover the shot. A **time map** relates packet frame numbers to engine frame numbers when the frame rates differ; the video player in the Motion editor shows it with the landing, lock and drift chips. The player has no download button, no picture-in-picture and no right-click menu (a clip leaves PLATE through **Deliver…**, which is guarded, section 18), and a **Compare** toggle that plays two clips on one clock, frame-aligned through their time maps: the clay motion reference against the take when the packet has one, else the node's two latest takes; `,` and `.` step one frame (Shift: ten), Space plays and pauses. See [gates/video.md](gates/video.md).

**Previs (Seedance 2.5 reference-to-video).** For a walkthrough, PLATE builds the move from the clay sequence: it splits the camera path into rooms by which architecture is on screen, picks the approved still that sees each room best as that room's reference (at most 30 references, the hero still first), writes timing hints from the path ("0-3s: lobby; 3-7s: corridor ..."), and runs a 480p draft first and the 1080p complete pass after you like the draft. **Every previs frame carries a tiled PREVIS watermark and the run is flagged `previs`; a previs output can never reach Export or Delivery: the canvas and the exporter both refuse it.** It may go into a Review bundle, where it stays watermarked. Previs is for agreeing the move with the client, not for delivering.

**People.** The People node runs over STAFFAGE, which is itself an upload route (grade D), so it is refused on NDA projects unless STAFFAGE is configured as local-only. The person is placed with a ground point, given a depth in metres, and composited under the packet's geometry using Z, so a person behind a column is occluded by it. With `params.video` on and an approved locked or crop-pair clip on the node's `clip` input, the take is composited over that clip frame by frame instead: the person follows the clip's crop (the person scales with a push-in), is occluded by the packet's Z in every frame, and the video gates check that the person did not move the camera lock by more than half a pixel. It is priced per second of the clip, it is refused on NDA projects like any STAFFAGE take, and an unapproved clip is refused as `VIDEO_LOCKED`.

## 13. Numeric nodes

These run in the daemon with no generator, no price (zero) and custody C0, still quoted and approved for uniformity.

- **Set** keeps a set of cameras consistent: pick a hero view; per material it takes the target colour from the hero and applies a numeric per-ID correction to the other views; the cross-view gate checks the 90th-percentile ΔE00 at world-position correspondences (target at most 3).
- **LightMix** blends the light-select passes by non-negative least squares to reach a mood without re-rendering; gains are seeded by the solver and adjustable, with presets. The editor has the `dusk` button. `params.preset` also takes `moonlight` (no sun, a faint sky, interiors at 0.6, facade lights at 0.3), `luxury_exterior` (no sun, interiors off, facade lights at 2.5, landscape lights at 1.5) and `artificial_light` (no sun, interiors at 3.0, facade and landscape lights at 1.0), matched to light-select names such as `interior_*` and `facade_*`; a preset is a starting guess for the sliders, not a solution. A light's gain is one number, or `[r, g, b]` in `params.gains` to weight the colour channels separately (a warmer interior, say); the preview says which each light used. It needs a Tier A packet with light selects.
- **Recolour** recolours a material by recomposing its render elements to a RAL or hex target, with an expected ΔE shown before you run it. A degraded path (whole-beauty scaling) is expected to fail its gate on dark semi-gloss panels; that is correct behaviour. **Several materials in one pass:** `params.materials` is a list of 1 to 8 `{material, target}` pairs with distinct ids; each is recoloured through its own coverage, the preview gives an expected ΔE per entry, the gate scores every material (the worst one decides) and everything outside all of them stays identical. The editor's **RAL…** button opens a searchable grid of all 213 RAL Classic codes (search by code or name; arrow keys and Enter pick), and **Project swatches** keeps your own named colours (hex or Lab, with a note on how it was measured) in this browser, per project. The grid's colours are sRGB approximations transcribed from a public table; RAL's own Lab values are licensed and are not in the file, so a measured swatch always beats a table value.
- **Comp** stacks accepted takes over a base, each inside its own mask (section 15).
- **Colour** grades in linear light inside a scope, with no generator: `ev` (-4 to 4 stops), `kelvin` (2000 to 12000, or empty to keep the temperature) with `tint` (-100 to 100), `contrast` (-50 to 50), `saturation` (-100 to 100) and `hue_deg` (-180 to 180). The scope (`scope`) is the Mask wired to `mask`, a list of ids (`ids`) or the whole frame (`frame`); the default is the mask when one is wired. There is no feather: outside the scope the output equals the source exactly (a hard check). Structure and the invention flags are hard checks too. The material ΔE checks are advisory, because a grade moves materials by design, and the lineage records the grade (`graded`). You choose the scope, so a grade may touch a locked material; the report names the locked ids the scope covers. 6500 K with tint 0 and everything else at zero is the identity.
- **Atmosphere** adds depth fog from the packet's own Z (Tier A only; a Tier C plate or a packet without Z is refused as `no_z`). `visibility_m` (default 800, 20 to 20000) is the distance at which contrast falls to 2%; `colour` is `[r, g, b]` in linear light (by default the mean colour of the lowest tenth of the sky pixels); `sky_fog` (0 to 1, default 0.6) is how much of the fog the sky itself takes; `height_falloff_m` (off by default) thins the fog above the camera when the packet has a world-position element. The whole frame may change, so the gate is: structure (G-buffer edge recall) hard, material ΔE and texture and tone advisory. A fog layer is a per-pixel function of the plate and its Z: it can wash edges out, which the structure check catches, but it cannot draw geometry.
- **Sky** puts a sky photograph inside the render's own sky coverage. Wire a Reference (role `sky`) to its `sky` input (or give `skyHash`) and the packet to `packet`. It needs a Tier A packet with Cryptomatte and a SKY-class ID in the legend (`no_cryptomatte` and `no_sky` otherwise). The photo is scaled to the plate's width times `scale` (default 1.0) and its row `horizon_frac` (default 0.5) is put on the horizon of the packet's camera, plus `dx_px`; `tone_match` (default on) moves the photo's mean colour to the plate's over the sky pixels. Wherever the render has no sky the output is the plate, bit for bit (a hard check). A sky photograph may be resampled to fit; a plate never is.
- **Light** asks MaxGaffer in Max to match a reference image for one camera and re-renders a new packet that supersedes the old one; the dock button is disabled when MaxGaffer is not installed (`MAXGAFFER_MISSING`). It can also start the Vantage live link.
- **Scout** asks Max to place temporary scout cameras, render low-resolution previews and, optionally, full packets; the temporary cameras are deleted afterwards unless you keep them.

Comp, Colour, Atmosphere and Sky have no parameter panel yet: select the node, Quote and Make as for any numeric node, and set the values on the node's `params` (in `project.json`, through `PUT /projects/<id>/graph`, or as `params` in the quote request).

When a client revises the scene, a new packet that `supersedes` the old one marks runs on the old packet superseded and re-binds nodes; approved layers whose touched IDs are unchanged (same name, material, class, and a mask overlap of at least 0.98) carry over, are re-composited on the new plate and re-gated; the rest are dropped with the reason (for example "material changed"). This now happens by itself when the new packet arrives (**auto carry-over**, on by default; the switch is in the Project Sheet disclosure, and `PLATE_AUTO_CARRY_OVER=0` turns it off for every project), and the daemon publishes `packet.carried` with the counts. A carried layer is a new run (engine `carry-over`, no spend) that you still have to Accept after a scrub. With the switch off, select the new packet's Plate: its editor offers **Carry over approved layers from <old packet>**, and the list shows `carried` or `dropped · reason` for each layer.

## 14. References: pictures an engine may look at

A **Reference** node holds one picture you want an engine to look at: a material sample, a planting style, a sky, a person, a colour swatch. It is an input to an engine and nothing else; it never becomes pixels of the output.

**Add one.** Add a Reference node and select it. The **Reference editor** takes the picture three ways: drop a PNG or JPEG on it, paste with Ctrl+V while it is open (a screenshot or a copied picture), or choose a file. It needs an open project, because the picture is stored in that project: PLATE re-encodes it as an 8-bit RGB PNG (alpha and all metadata are gone), shrinks a side over 4096 px (it never enlarges), and refuses a file over 32 MiB. The editor lists **This project's references**, so a picture you uploaded earlier can be reused on another node. Set a **role** (`style`, `object`, `sky`, `person` or `swatch`) and a note ("what to take from it"). The node's card shows the picture.

**Wire it.** A Reference has one output, and it can go to exactly three places: an Edit's `refs` input, a Motion's `elements` input (section 12) and a Sky's `sky` input (section 13). Any other target is refused with `a reference feeds an engine's reference input only; it is never composited`. A Reference does not anchor anything: an Edit whose only upstream is a Reference still has no plate and is refused (`generation must be anchored to a plate camera`).

**What a reference can change.** Only what the mask already lets the engine change. The engine gets the plate (or the take you started from, section 15) first and your references after it, in the order you wired them; the composite that follows is mask-locked, so a locked pixel (architecture, glazing, signage, ground, vehicles) cannot take anything from a reference, whatever the prompt says. Say in the prompt what to take from it. The `@ref1` to `@refN` shorthand that the build plan describes is **not wired in this build**: nothing replaces it in a prompt, so typing `@ref1` sends those characters to the engine as they are. Refer to a reference in words ("the second reference image"). The role and note stay with the node and the run's record; an image engine receives the picture, not the label.

**What refuses a quote.** An engine whose manifest takes no reference images refuses with `references_unsupported` (the built-in fake engine is one; every shipped image-edit engine takes them); more references than the engine takes (Nano Banana 2.1 and Pro take 13, GPT Image 2.5 takes 15) is `too_many_references`; a Reference with no picture, or whose picture is no longer in the project's store, is `reference_missing`. These are refused before a run exists, so nothing is created or spent. The `refs ≤ N` chip in the Make bar (section 6) shows the limit, where the engine states one, before you quote.

**Custody.** A reference leaves the box with the run, on the run's route and under the project's custody grade, exactly as the plate does: the vendor gets each reference as an 8-bit PNG under an opaque name with no metadata, the `submitted` ledger record lists their hashes, and the project's `outbound.jsonl` shows the upload. On an NDA project only the local engines are allowed, so nothing goes anywhere else. A reference is also somebody's material: a client photograph is client material for custody purposes, and a stock or web picture needs a licence that lets you send it to a vendor. A Recomposite (section 15) sends nothing, so it carries no references.

## 15. Building on a take: Edit after Edit, Comp, Recomposite

**Edit after a take.** An Edit's `image` input may come from a take instead of a Plate. Wire a finished node (an Edit, or a numeric node such as LightMix, Colour, Atmosphere, Sky or Comp) into the next Edit's `image` input and the engine starts from that picture, not from the plate: relight the plate, then add people to the relit take. The upstream node must have a picked output (click its candidate, or press its digit in the strip) that is still in the project's store; otherwise the quote is refused with `no_base` ("make and pick a take first"), because an Edit never silently falls back to the plate. The take must be the plate's size (`base_size`). Three things follow from the base:

- **The mask.** The second Edit's own mask says where it may change things. The gates score the result against the **plate**, inside the union of this mask, the earlier Edit's mask and whatever the base step changed (a LightMix changes the whole frame, by design); everywhere outside that union the delivered picture is the plate, bit for bit. A pixel the first Edit changed outside the second mask stays as the first Edit left it, because the composite lands on the base.
- **The lettering.** The text lock restores signage lettering from the plate, not from the base.
- **Depth 2.** Generation depth is counted from the plate: Plate, Edit, Edit is depth 2, and a third generating step is refused (`generation depth > 2 from the plate`). Numeric nodes (LightMix, Recolour, Set, Colour, Atmosphere, Sky, Comp) add no depth. Past two generations the drift compounds faster than the gates can catch it; if a look needs a third, render it in V-Ray.

**Comp stacks.** The Comp node lays accepted takes over a base, each inside its own mask, in order. Wire the base (a Plate, or any node with a picked take) to `base` and the nodes whose takes you want to `layers`. A layer is that node's picked take, else its latest finished one, where finished means DONE or a GATE_FAIL you have overridden. With `require_accept` on (the default) every layer must carry an Accept, or the quote is refused (`LAYER_NOT_ACCEPTED`, naming the node). The order is the edge order, or `params.order` (a list of node ids; layers it does not name follow). Each layer lands only where its own run stored its mask (the union of the chain's masks when it was an Edit on a take); a layer from a numeric node, which stores no mask, lands where it differs from the base. Where two layers overlap the later one wins, and the preview reports how many pixels are claimed twice. The result is gated against the plate inside the union of the layers' masks, so outside it the picture is the plate; Comp makes no pixel of its own, it only chooses which stored take shows where. Use it to combine takes from different engines (sky from one, planting from another) without a second generation. Comp has no parameter panel yet: set `order` and `require_accept` on the node's `params`.

**Recomposite: a free re-mask.** When a take is right in the middle and wrong at its edge, you do not need to pay for another one. A take made by the pipeline keeps what the engine sent back, the size map and the picture that was sent. The **Re-mask** bar under the viewer (it appears when the take you are comparing kept its raw answer) has **Feather** (4 to 12 px, inside the mask; blank keeps the take's own), **Grow / shrink** (-32 to 32 px; positive grows the mask) and **Re-mask**. It puts the stored answer through the Edit node's Mask **as it is now**, so edit the Mask node first (or pass a stored mask), then press the button. It costs nothing (0.00 USD, a local C0 route, no engine called, nothing leaves the box), makes a new run with the next attempt number, and the run is composited and gated like any take: you still scrub and Accept it. The region crop lands exactly where it did for the original, so a bigger or smaller mask re-composites the very pixels the engine produced. It refuses a take that did not keep its raw answer (`not_recompositable`), a mask that touches a locked ID (`mask_locked`) and an empty one (`mask_empty`). A Re-mask is not a sample of the engine, so it does not count in the engine cards.

## 16. Recipes, tokens and the Project Sheet

**Picking a recipe.** The **Edit editor's Recipe** select is grouped (lighting, weather, season, people, vegetation, beauty, and any group you save); each option reads `title · engine`, with `(studio)` or `(project)` after a recipe from those folders. Picking one shows which classes it edits (chips; **whole frame** when it lists none, in which case the gates still hold every locked ID) and sets the Make bar's engine if you had not chosen one. The Make bar has no preset select for an Edit: the recipe owns that.

**Seeing the text before you pay.** The **Assembled prompt** disclosure shows the recipe's prompt and negative prompt exactly as they will go (tokens filled from the Project Sheet and from the fields below), with the number still open in amber (`n unfilled`). **Start from this** copies the assembled text into the Prompt box, so you can edit it; from then on the line reads `prompt source: explicit` and your text wins over the recipe. **Expand** opens the Prompt in a large box (Copy, Close).

**Token fields.** Every `[TOKEN]` in a recipe gets a field, with the recipe's default shown as the placeholder (`[SEASON]` defaults to autumn, `[TARGET HOUR]` to late afternoon, `[OCCASION]` to Diwali). Typing in it overrides the default for this node only (it is stored as `params.tokens`). A token with no default that the Project Sheet leaves empty is marked **open**: fill it before you quote, because an open token travels as the literal bracketed text.

**The Project Sheet.** In the **Projects** tab, **Project Sheet** holds the 14 fields every recipe shares: name, location, format, light logic, the three palette colours, lens character, depth register, species, materials, climate, audio bed and scale note. A blank field uses the default shown. The recipes' longer tokens (`[NAME THE SPECIES]`, `[HERO MATERIALS]`, `[CLIMATE / CULTURE]` and the like) are filled from it, so filling the sheet once saves typing it on every Edit. A recipe's own token defaults beat the sheet. **Save sheet** stores it with the project. The same disclosure holds the **auto carry-over** switch (section 13).

**Saving your own recipe.** When a prompt works, keep it: **Save as recipe…** in the Edit editor asks for an id (lower_snake_case: a letter, then a to z, 0 to 9 and `_`), a title, a scope (**studio**: every project on this machine, kept in the app-data `recipes/` folder; **project**: this project's `recipes/` folder), an optional group, the engine (the one in the Make bar, which must be picked), a task, the editable classes (ticked from the connected Mask's classes when there is one) and the prompt and negative prompt. Saving under an id that already exists anywhere is refused until you tick **overwrite if the id exists**. A studio or project recipe shadows a shipped one with the same id; the shipped files are never written. The recipe then appears in the select, marked `(studio)` or `(project)`. `plate recipes list` and `plate recipes save` do the same from the command line (section 18). The recipe format is in `daemon/plate_daemon/recipes/README.md`.

**The shipped Edit recipes.**

| group | recipes (Lensbook id) | edits |
|---|---|---|
| lighting | Same-family relight (a01), Cross-family relight (a02), Twilight hero (a03), Night exterior (a10) | sky, vegetation, water, people |
| weather | Monsoon pass (a04) | sky, vegetation, water, people |
| season | Season swap (a05), Festive dress | vegetation, sky, water; Festive dress also people and furniture |
| people | Plaza staffage (a06), Interior occupants (a07) | people |
| vegetation | Planting refresh (a09) | vegetation |
| beauty | Photographic imperfections, close shot (a08) | the whole frame, close shots only (the router checks the shot class) |
| no group | Night relight, change-list edit (sky, vegetation, people); Sunburst re-photograph, close shot (whole frame, close shots only) | |

Cross-family relight is honest about its odds: budget three to five attempts, and if the light must be exact, light it in V-Ray (runbook [weather-and-season](runbooks/weather-and-season.md)). The twelve shipped Motion recipes are in section 12.

## 17. Templates, batches and Send + quote

A **template** is a chain of nodes laid down for a packet in one step. Three ship with PLATE (a project can add its own in `<project>/templates/`, which shadow a shipped id):

| template | chain per packet |
|---|---|
| `edit_chain` | Plate, Mask (sky, vegetation, people), Edit, Judge, Export |
| `night_chain` | Plate, LightMix (dusk), Mask (sky, vegetation, people), Edit (the recipe `night_relight_changelist`, its image taken from the LightMix), Export |
| `set_chain` | Plate, Mask, Edit for every packet, then one Set node over all the new Edits |

**Applying one.** In the **Projects** tab, **Apply template…** asks for: the template; the packets (ticked by default: the packets that have no Plate on the canvas yet; a packet that already has a Plate keeps it, so a Plate is made once per packet); an engine and a recipe (blank means "set it on each Edit"); **quote every new Edit**; and **auto-approve under cap**. Each packet gets its own column to the right of what is on the canvas, and the graph is stored through the same canvas rules as a hand-drawn one. The result line reads `n chains · k quoted · total USD`; an Edit the pipeline refuses (no engine, a locked mask, a custody route) is listed with its reason and does not stop the others. Nothing is spent by applying: a quote is a quote. With auto-approve ticked, a quote under the project's per-run cap (USD 2.00) is approved for you and runs; anything at or over the cap stays QUOTED, the panel says `k waiting for your approval`, and the status bar shows one alert (`k quotes waiting for approval: USD`). Open each Edit and approve it from the Make bar as usual.

**Send + quote from Max.** The dock's **Send + quote** button (beside Send Packet, with an **auto-approve under cap** tick that is off by default) runs the same route for you: Send, then, for each camera the daemon ingested, `edit_chain` with the dock's Engine and Preset, quoted. The log row for each camera reads `quoted <usd> USD · waiting for approval` or `approved · running`. A host never creates nodes on its own initiative; the artist asked by pressing the button. The Engine and Preset selectors in the dock are labelled `used by Send + quote` because that is the only place they are read.

**From the command line.** `plate run <recipe> --project <dir> --packets a,b,c` lays one chain per packet, quotes and runs each under the same `--approve`/`--envelope` rules, prints one line per packet and exits 2 if any of them fails its gate.

**The Tier A night chain (LightMix, then the night relight).** For a night version of a Tier A packet, let the render do the building and the generator do only the world around it:

1. Apply `night_chain` to the packet. You get Plate, LightMix, Mask, Edit and Export. If you ticked **quote every new Edit**, expect the result to list that Edit as refused with `no_base`, because the LightMix has no take yet; quote it yourself after step 2.
2. Select the **LightMix**. Its light-select passes are listed with a gain each; the `dusk` button seeds them (the other presets, `moonlight`, `luxury_exterior` and `artificial_light`, are set with `params.preset`; section 13). Adjust the gains, Quote (0.00 USD, in-process) and Make, then pick the take so the node has an output. The building's own fixtures, windows and shadows now come from V-Ray's real light passes.
3. Select the **Edit**. Its `image` already comes from the LightMix take, which is why step 2 must finish first (`no_base` otherwise). The recipe `night_relight_changelist` repaints the sky, vegetation and people to match; choose the engine in the Make bar, Quote, Make.
4. Scrub and Accept as usual. The gates score the Edit against the plate inside the union of its mask and what the LightMix changed (section 15), so the architecture is the V-Ray light-mix, not an AI re-light.

If the packet has no light selects, LightMix refuses the quote (`no_light_selects`) because it has nothing to mix, and it needs a Tier A packet; light the scene in V-Ray instead, or use the Edit recipes `night_exterior` or `night_relight_changelist` straight from the plate, which is the cross-family case with its three-to-five-attempt odds.

## 18. Delivery formats and cutdowns

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

Stills go out from the **Export** node (section 5), the `plate export` command below, or `POST /api/v0/runs/<id>/export`; all three use the same export code. The video formats run through `POST /api/v0/runs/<id>/deliver` (body: `format` as `prores4444`, `hevc`, `exr_sequence`, `av1_retag` or `fcpxml`, `out_dir`, optional `name`, `override_reason` and, for `fcpxml`, `timeline`) on a finished clip take, or from Python as the `plate_daemon.delivery` library with a `Provenance` built from the run (`Provenance.from_run(run, project_dir=...)`), which carries the lineage and the gate report. In the canvas, a clip take in the **Export** editor offers `prores4444`, `hevc`, `exr_sequence` and `fcpxml` (the lossless `av1_retag` repair stays on the command line, `plate deliver`), and the video player's **Deliver…** button opens the same choice. Every encode refuses a previs output, and a GATE_FAIL run without an override reason, **before any output exists**; it then verifies the colour tags it wrote by probing the file, and writes the `.gate.json` sidecar.

Command line, from the repo root. The `plate` command works on the same per-machine index and project folders as the daemon, in its own process, so a run made here shows up in the canvas and the ledger like any other:

```
plate run <recipe id or .yaml> --project <dir> --packet <id|dir> [--engine ID] [--n N] [--approve auto|human] [--envelope USD] [--nda]
plate run <recipe> --project <dir> --packets a,b,c            (one chain per packet)
plate gate <plate> <candidate> [--packet <dir>] [--out report.json]
plate export <run_id> --to <dir> [--format tiff16|png16|exr] [--override-reason "..."] [--name STEM]
plate export --runs r1,r2 --to <dir>        plate export --project <p> --accepted --to <dir>
plate review <project> [--runs ...|--all-exportable] [--watermark TEXT] [--title TEXT]
plate import <file> --project <p> --packet <id> [--source higgsfield|runway|manual]
plate deliver <run_id> --format prores4444|hevc|exr_sequence|av1_retag|fcpxml --to <dir>
plate cards [--project <p>] [--task T] [--shot-class C]
plate estimate --engine X --task T --shot-class C (--count N | --seconds S)
plate spend <project> [--by engine|day|week] [--since ISO]      plate spend --all-projects --by week
plate recipes list [--project <p>]        plate recipes save <yaml> --scope studio|project [--overwrite]
plate doctor        plate timer start|stop|today
plate reindex <project>      plate gc <project> [--dry-run]      plate pack <project> --out <zip> [--full]
plate smoke [--live --budget-usd 5 --engines a,b]
```

`plate` is `scripts/plate` (Mac) or `scripts\plate.cmd` (Windows), the `plate` console script after `uv sync`, or `.venv/bin/python -m plate_daemon.cli`. `run` prints the quote and waits for `y` (`--approve human`, the default); `--approve auto` goes through the same per-run, daily and unattended caps as the daemon and exits 3 if one says no; `--envelope USD` is a hard ceiling either way. A vendor engine needs `PLATE_LIVE=1`; `PLATE_FAKE_ENGINE=1` swaps in the in-process fake engine for a dry run. `gate` compares a candidate with its plate at the same size (it never resizes either) and, with `--packet`, adds the G-buffer, material and invention gates for a Tier A packet. `export` refuses a GATE_FAIL take without `--override-reason` (ten characters or more), embeds the lineage, writes `<name>.gate.json` beside the file and never overwrites. Exit codes: 0 ok, 1 an error, 2 a gate failure, 3 a custody, budget or approval refusal (nothing was sent), 4 usage. `smoke` without `--live` replays the recorded vendor fixtures through every adapter's cheapest call and touches no network; `--live` needs `PLATE_LIVE=1` and an explicit budget, prints the custody grade and quote of every call before sending it, and stops before the running total would pass the budget.

`plate doctor` checks the install and always exits 0: git against `origin/main` (`no remote` is tolerated), the Max startup script against the installed copy (`not installed` is tolerated), `ui/dist/index.html`, ffmpeg and ffprobe, the pixels worker and the token. `plate recipes list` shows the shipped, studio and (with `--project`) project recipes, and `plate recipes save` validates a recipe file and saves it. `plate import` files a take made outside PLATE (Higgsfield, Runway, a file by hand) as a gated, ledgered run, `plate deliver` encodes a clip take, `plate review` builds a review bundle and `plate timer` drives the human-minute timer of the running daemon.

**Batch export.** Tick takes in the Export editor's **Batch export** list (or **Select all accepted**) and press its button: every take goes through the same export as the single route, one at a time, and a take that cannot leave never stops the others. The answer lists what was written (path and sidecar) and what was refused, with the reason: a GATE_FAIL take needs an override you recorded earlier (there is no typed override in a batch), previs is refused, an existing file is never overwritten, and a take that is not DONE is not exportable. Files are named `{node}_{run8}` unless you give a pattern (`{node}`, `{run}`, `{run8}`, `{packet}`, `{engine}`, `{n}`); characters Windows would not accept become `-`. `plate export --runs` and `--accepted` do the same.

**Cutdowns (social crops).** The Export editor's **Cutdown** disclosure cuts a 9:16, 4:5, 1:1 or 16:9 crop of an approved master: the largest box of that aspect inside the packet's safe frame, centred where the **centre x** and **centre y** sliders say, for a still or a clip. A still becomes an 8-bit sRGB PNG or JPEG, a clip an H.264 or HEVC file with BT.709 tags. A cutdown is **downsample-only**: the optional **long side** can shrink the crop, never enlarge it (asking for more is refused, "cutdowns never upscale"), and PLATE never generates a social close-up, because that would be an invented angle. It is guarded exactly like a delivery (previs refused; a GATE_FAIL take needs a recorded override), carries its lineage inside the file, and writes `<name>.cutdown.json` beside it (the source run, the box, the preset, the codec or format and the gate report it came from). The output is marked as shipped.

**AI-provenance marker.** Every embedded lineage record says whether the file was touched by a generator (`ai_touched`). For one that was, the file also carries the IPTC `digitalSourceType` `compositeWithTrainedAlgorithmicMedia` in a machine-readable form (a PNG or JPEG XMP packet, the TIFF's XMP packet, an EXR `iptc:DigitalSourceType` attribute, an MP4 `digital_source_type` tag). A file made only by numeric nodes, the plugin, a manual import or a Recomposite carries no marker. Anyone who checks the file can read that it is AI-assisted; [consent-template.md](consent-template.md) has the line for telling the client, and for describing a social post as AI-assisted finishing.

**Where the safe frame matters.** Export hands over the whole rendered frame, overscan included. The safe frame (`camera.safe_frame` in the packet's `manifest.json`) is the rectangle that is the deliverable; today only cutdowns and review bundles crop to it by themselves, so crop a full-size export to it in retouch or Resolve.

## 19. Review bundles for the client

A review bundle is a zip the client opens from a folder with no install, no server and no network: a single `index.html` with a before/after wipe, a toggle, a 100% zoom, a gate badge per take (pass, fail or advisory with the first failing measurement) and a comment form that builds a `mailto:` link and a downloadable `comments.json`. Pictures are 8-bit sRGB **proxies**, never wider than 2048 px, tiled with a `REVIEW` watermark at 12% opacity, with the take number as the only file name (`r1_a.png`, `r1_b.png`): no project, run or engine name in any file name. The candidate's lineage record stays inside its proxy so a dropped copy can be traced home; pass `keep_lineage=False` to `export_review` for a bundle that must not carry ids. There is no 16-bit master in a bundle.

Make one from the **Review** node (**Build bundle**, section 5), with `POST /api/v0/projects/<id>/review` (body: `run_ids`, optional `watermark`, `title`, `max_width` and `reply_to`; the zip is written into the project's `review/` folder and the answer gives its path, size and take count), from Python as `plate_daemon.review_bundle.export_review(project, run_ids, out_dir)` (it returns the zip path, and a second file next to it, not in the zip, maps takes back to runs for your own records), or through an agent's `export_review` tool. A GATE_FAIL take without an override is refused, as it is for Export. Hosting the unzipped folder on Cloudflare R2 behind signed, expiring URLs is documented in `daemon/plate_daemon/review_template/HOSTING.md`; PLATE itself never uploads. On an NDA project an R2 bucket is a custody C2 route: send the zip by hand.

## 20. Agents

A Claude agent can drive PLATE through an MCP server (`python -m plate_daemon.mcp_server`) with eight tools: list projects, get a graph, add a node (Export and Review cannot be created by an agent), estimate a run, run a node (a budget envelope is mandatory), get a gate report, compare two runs, and export a review bundle. **There is no approve, no deliver, no master export and no override tool**, by design. A human creates the envelope first (`python -m plate_daemon.mcp_server --new-envelope --project <id> --usd 10 --hours 8`). Everything read from a plate (names, EXIF, lettering) is handed to the agent as data, never as instructions. An agent session is an egress route: it must run on an API key under commercial terms, never a consumer login, and on NDA projects the tools return numbers, ids and paths only, never pixels. See `docs/mcp.md`.

## 21. When something goes wrong

| symptom | go to |
|---|---|
| the daemon will not start, or the page says token missing | [runbooks/daemon-wont-start.md](runbooks/daemon-wont-start.md) |
| a vendor is down or slow on delivery day | [runbooks/vendor-down.md](runbooks/vendor-down.md) |
| a key was revoked, leaked or expired | [runbooks/key-revoked.md](runbooks/key-revoked.md) |
| a Runway video run was refused, throttled, failed or left in an unknown state | [runbooks/runway.md](runbooks/runway.md) |
| a local model fails with out of memory | [runbooks/vram-oom.md](runbooks/vram-oom.md) |
| a vendor changed its API and a nightly check alerts | [runbooks/schema-change.md](runbooks/schema-change.md) |
| a Max update broke the dock | [runbooks/max-update-breaks-dock.md](runbooks/max-update-breaks-dock.md) |
| PLATE is down and the deadline is not | [runbooks/manual-pipeline-fallback.md](runbooks/manual-pipeline-fallback.md) |
| backup, restore, purge | [runbooks/backup-restore.md](runbooks/backup-restore.md) |
| the nightly jobs | [runbooks/nightly.md](runbooks/nightly.md) |
| a client wants rain, snow or another season | [runbooks/weather-and-season.md](runbooks/weather-and-season.md) |
| a V-Ray camera has vertical tilt correction or a vertical shift | [runbooks/two-point-perspective.md](runbooks/two-point-perspective.md) |
| the shot needs decorations, fixtures, wet walls, new tiles, cars, branches or paving the AI may not change | [runbooks/what-goes-back-to-max.md](runbooks/what-goes-back-to-max.md) |

**Alerts and degraded routes.** The status bar shows the daemon's latest alert (section 5). The full list, newest first, is `GET /api/v0/alerts` (`?since=<ISO time>&limit=100`): each line has a kind, a level (`error` for an engine suspended, a canary that drifted or a retired pin; `warning` for a price change, a re-quote, a schema change or a unit mismatch; budget lines keep their own severity), a message, the time, and the engine or project it names. After three provider failures in a row on one route within the hour, PLATE marks that route `degraded`: the router skips it, the Make bar shows the amber chip and the quote's reasons name it, and one successful fetch clears it ([runbooks/vendor-down.md](runbooks/vendor-down.md)). `GET /api/v0/health` also reports when a Max dock last polled and how many alerts the last 24 hours held.

## 22. Appendix: folders, environment variables, ports

**Folders.** App data: `%LOCALAPPDATA%\Plate` on Windows, `~/Library/Application Support/Plate` on a Mac (`PLATE_HOME` moves it): `config.json` (token and daemon URL; the Max plugin adds `repo_path` and `packets_dir`), `index/<project_id>.sqlite` (a rebuildable cache, always on the local disk), `logs/`, `cache/`, `packets/`, `projects/`. A project folder holds `project.json`, `assets/<hash prefix>/<hash>.<ext>`, `runs.jsonl`, `gates/<run_id>.<n>.json`, `review/`, `packets.json`, `references.json` (the pictures uploaded for Reference nodes), optional `recipes/` and `templates/` (yours, for this project; `<app data>/recipes/` holds the studio's recipes), and `outbound.jsonl` (one line before every request that leaves the box, one after). The app-data `logs/` folder holds the daemon logs, `alerts.jsonl` and the nightly records.

**Ports.** Daemon and built UI `127.0.0.1:4610`; pixels worker `4611`; labelling tool `4612`; Vite dev server `5173` (only under `PLATE_DEV=1`). Everything binds loopback.

**Environment variables** (all optional):

| variable | effect |
|---|---|
| `PLATE_HOME`, `PLATE_CONFIG`, `PLATE_TOKEN_FILE` | move the app-data folder, the config file; name a file holding only the token (wins over `config.json`) |
| `PLATE_PORT`, `PLATE_BIND`, `PLATE_TAILSCALE_IP` | daemon port and bind list (loopback by default; never all interfaces) |
| `PLATE_DEV` | allow the Vite dev origins (`127.0.0.1:5173`, `localhost:5173`) and register the dev-only run routes (`POST /runs`, `/runs/<id>/quote`, `/queue`, `/step`); `127.0.0.1:<port>` and `localhost:<port>` are always allowed without it |
| `PLATE_FAKE_ENGINE` | run the in-process fake engine (no network, no spend). `1` (also `true`, `yes`, `on`) is the **echo** fake: it hands back the image it was sent, registered exactly, so a run gates as a pass; `0`, `false` or empty is off; a JSON object scripts its flags (`echo`, `shift_px`, `behaviour`, ...). The Make bar then lists an engine called `Fake (echo)` |
| `PLATE_PIXELS_INPROC` | call the pixels backends inside the daemon (stubs unless `PLATE_PIXELS_REAL=1`) instead of the worker on 4611 |
| `PLATE_NIGHTLY`, `PLATE_NIGHTLY_TIME` | `0` turns the nightly jobs off; `HH:MM` moves their start ([runbooks/nightly.md](runbooks/nightly.md)) |
| `PLATE_CAS_COPY` | not needed. Plate pixels are served from the packet folder by reference; setting it to `1` also copies every packet file into the project's store, which doubles the disk for no gain |
| `PLATE_AUTO_CARRY_OVER` | `0` stops a packet that supersedes another from carrying the approved layers over by itself, for every project (the per-project switch is in the Project Sheet disclosure) |
| `PLATE_LIVE` | required, with a budget, for any real vendor call (and for a Magnific upscale); never set in tests or CI |
| `PLATE_FFMPEG`, `PLATE_FFPROBE`, `PLATE_OIIOTOOL` | locate the tools when they are not on PATH |
| `PLATE_PIXELS_PORT`, `PLATE_PIXELS_REAL`, `PLATE_MODELS` | the pixels worker: port, require real models, model cache |
| `PLATE_GPU_WINDOWS`, `PLATE_FORCE_LOCAL`, `PLATE_ALLOW_UNMANAGED_GPU` | when heavy local models may run (see [runbooks/vram-oom.md](runbooks/vram-oom.md)) |
| `PLATE_ROUTER_MODE` | render-it router: `advisory` or `blocking` |
| `ATLASCLOUD_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `BFL_API_KEY`, `ARK_API_KEY`, `WAVESPEED_API_KEY`, `MAGNIFIC_API_KEY` | vendor keys, read from the environment only, never written to a project or a log |
| `RUNWAYML_API_SECRET` | the Runway Dev API key (project-scoped, shown once in the Runway dev portal): the preferred video route; read from the environment only, never written to a project or a log, and used only with `PLATE_LIVE=1` for a real call. Grade D: an NDA project never reaches it ([runbooks/runway.md](runbooks/runway.md)) |
