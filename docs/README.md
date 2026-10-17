# PLATE documentation

Start with the row that matches what you are doing. Everything here is plain Markdown and is kept next to the code it describes; a statement that a test enforces says so, and a statement nobody has verified yet says that too.

## If you deliver work

| doc | read it when |
|---|---|
| [artist-manual.md](artist-manual.md) | you sit at the box: install, launch, project, Send from Max, the canvas, quotes, gates, overrides, motion, delivery, review bundles |
| [runbooks/](runbooks/README.md) | something is down, revoked, out of memory or changed |
| [client-explainer.md](client-explainer.md) | a client asks what the AI does with their images (one page, no jargon) |
| [consent-template.md](consent-template.md) | a client must consent to a named third-party service (a template for counsel to adapt) |
| [windows.md](windows.md) | setting up or running on the Windows render box: paths, scripts, the daemon task, Tailscale |

## If you decide where pixels may go

| doc | what it holds |
|---|---|
| [custody.md](custody.md) | the custody grades, the vendor matrix with evidence and the generated live matrix, the day-1 emails, the per-project class table, the operational rules |
| [engines.md](engines.md) | engine manifests, the router, Atlas quirks pinned as contract tests |
| [mcp.md](mcp.md) | the agent channel: eight tools, no approve, no deliver, envelopes |

## If you work on the measurements

| doc | what it holds |
|---|---|
| [gates.md](gates.md) | the gate core: module map, numerics, versioned reports, goldens |
| [gates/gbuffer.md](gates/gbuffer.md) | G-buffer edges, depth-of-field weighting, invention flags |
| [gates/render_it.md](gates/render_it.md) | the pre-spend router: detail resolvability, disocclusion budget, orbit, crossings |
| [gates/video.md](gates/video.md) | time map, reprojection drift, camera lock, video gate assembly |
| [gates/crossview.md](gates/crossview.md) | cross-view correspondences and the Set-consistency gate |
| [gates/recolour.md](gates/recolour.md) | recolour by element recomposition |
| [colour.md](colour.md) | the colour rules and the on-box questions that are still open |
| [labeller.md](labeller.md), [../calib/README.md](../calib/README.md) | the three-key labelling tool, reason codes, the calibration report |

## If you work on the code

| doc | what it holds |
|---|---|
| [../CLAUDE.md](../CLAUDE.md) | the doctrine written as rules the code enforces, and the model-routing rule |
| [../spec/PACKET.md](../spec/PACKET.md) | the Shot Packet contract (and `packet.schema.json`, `plate_spec`) |
| [max-plugin.md](max-plugin.md), [../plugins/max/ONBOX.md](../plugins/max/ONBOX.md), [onbox-spikes.md](onbox-spikes.md) | the 3ds Max sender, and the checklist and answers that turn candidate names into facts on the box |
| [ui.md](ui.md) | the canvas, its rules, and the folder-level notes in `ui/src/viewer`, `ui/src/panels`, `ui/src/editors` |
| [pixels.md](pixels.md) | the GPU worker: routes, stub mode, GPU admission, the licence registry |
| [../fixtures/README.md](../fixtures/README.md) | the Blender-rendered fixture packets and the numpy generators every test reads |
| [../daemon/plate_daemon/recipes/README.md](../daemon/plate_daemon/recipes/README.md) | the runnable recipes and Lensbook doctrine as data |
| [P0_AUDIT.md](P0_AUDIT.md) | the day-1 scaffold audited against the plan, and the layout reconciliation |
| [../CHANGELOG.md](../CHANGELOG.md) | what changed, by release |
| `VERIFICATION.md` | written by the final verification pass: what passed, the UI smoke steps, and the box-day checklist |

## Conventions

Doctrine (CLAUDE.md) is code: no text-to-image path, no plate upscaling, no invented camera angle without an explicit override, no delivery of a failing gate without a written reason. If a doc and the code disagree, the code is the truth and the doc is a bug. No doc contains a credential, a client name or client imagery; client material under `~/Desktop/test` is referenced by absolute path from tests that skip when it is absent.
