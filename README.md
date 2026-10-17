# PLATE

Sthyra's scene-owning AI finishing pipeline for 3ds Max 2026 + V-Ray 7 (+ Vantage 3.3). The 3ds Max plugin exports a **Shot Packet** (camera, Z, normals, Cryptomatte IDs, the VFB-corrected 16-bit master, clay motion sequences) instead of a screenshot; a Python daemon composites AI output back onto the untouched render only inside the masks the IDs allow; measured fidelity gates can block delivery; every run is quoted before any spend; BYOK, no credits. Working name, rename freely.

**The plan is [`~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md`](../test/vizmaker-dossier/BUILD_PLAN.md)** (30 settled decisions in section 3, architecture in section 4, phases in section 5). The doctrine and the model-routing rule are in [CLAUDE.md](CLAUDE.md). The packet contract is [spec/PACKET.md](spec/PACKET.md). Everything else is indexed in [docs/README.md](docs/README.md); what changed is in [CHANGELOG.md](CHANGELOG.md).

## Start here

| you are | open |
|---|---|
| an artist delivering a job | [docs/artist-manual.md](docs/artist-manual.md) |
| on the render box and something broke | [docs/runbooks/](docs/runbooks/README.md) |
| answering a client about AI and their images | [docs/client-explainer.md](docs/client-explainer.md) |
| deciding where pixels may go | [docs/custody.md](docs/custody.md) |
| setting up Windows | [docs/windows.md](docs/windows.md) |
| changing code | [CLAUDE.md](CLAUDE.md), then the doc for the package below |

## Layout

| path | what | read |
|---|---|---|
| `spec/` | Shot Packet v0.1.1: `PACKET.md`, `packet.schema.json`, example packets, `plate_spec` (validator, reference camera math, Cryptomatte hash) and the canvas-rule vectors shared by the UI and the daemon | [PACKET.md](spec/PACKET.md) |
| `daemon/` | the FastAPI daemon (127.0.0.1:4610): packet ingest, projects and the canvas graph, quote then approve then run, the job state machine, egress and custody, gate wiring, lineage, review bundles, motion, delivery, numeric nodes, the MCP server, the nightly scheduler and the `plate` command line | [docs/mcp.md](docs/mcp.md), [docs/custody.md](docs/custody.md) |
| `pixels/` | the GPU worker (127.0.0.1:4611): depth, calibration, segmentation, flow, OCR, Tier C packets, admission, behind a commercial-safe licence registry; stub mode on the Mac | [docs/pixels.md](docs/pixels.md) |
| `engines/` | engine manifests (YAML), vendor adapters over recorded fixtures, the routing policy, price refresh, schema diff, engine cards and canaries | [docs/engines.md](docs/engines.md) |
| `gates/` | the fidelity gates (`plate_gates`): registration, outside-mask, G-buffer edges, invention, per-material colour, texture, tone, video, the render-it router, calibration | [docs/gates.md](docs/gates.md) |
| `ui/` | Vite + React + TypeScript canvas: typed nodes, enforced rules, Make bar, candidate strip, WebGL2 viewer, node editors, panels | [docs/ui.md](docs/ui.md) |
| `plugins/max/` | `plate_max`: the Max-side sender (standard library only), the dock, the client packet kit, installers, fake-pymxs tests | [docs/max-plugin.md](docs/max-plugin.md) |
| `tools/plate_label/` | the three-key labelling tool writing `calib/labels.jsonl` | [docs/labeller.md](docs/labeller.md) |
| `fixtures/` | Blender-rendered fixture packets and numpy generators; no client data | [fixtures/README.md](fixtures/README.md) |
| `calib/` | reason codes, the labels file (gitignored) and the calibration report | [calib/README.md](calib/README.md) |
| `scripts/` | bootstrap, test, start-daemon, the daemon logon task, the Windows-hazard scanner | [docs/windows.md](docs/windows.md) |
| `docs/` | manuals, runbooks, gate notes, custody, colour | [docs/README.md](docs/README.md) |

## Run it

```bash
make setup     # uv sync (Python 3.12) + npm install
make test      # fidelity selftest, hazard scan, pytest, then npm test
make lint
make daemon    # prints  PLATE daemon http://127.0.0.1:4610/#token=...  ; open that URL
```

On Windows, from PowerShell: `scripts\bootstrap.ps1`, `scripts\test.ps1`, `scripts\start-daemon.ps1` (see [docs/windows.md](docs/windows.md)). Tests use fixtures and fakes only: no live vendor call, no model download.

## What the code enforces

These are the claims this repository stands behind; each is meant to be backed by a test (see CLAUDE.md for the full doctrine):

- AI pixels land only inside the IDs a packet marks editable; outside the mask the delivered pixels are the render, bit for bit.
- Every run is quoted (provider, model, price, custody grade) and approved before any spend, and a quote expires after 15 minutes.
- On an NDA project nothing goes to a route graded below C1, and a route whose written terms are pending counts as grade D.
- There is no text-to-image node, the plate is never upscaled, and an invented camera angle needs an explicit override.
- A take that fails a hard gate is kept and flagged and cannot be exported without a written reason; a shipped gate report is never rewritten.
- An agent over MCP cannot approve, deliver, override or export a master.
- Previs output (PREVIS) can never reach Export or Delivery (a watermarked previs may go into a Review), and Concept images can never reach Export, Review or Delivery.

## Status

The Max plugin has run only against a fake `pymxs`; no live vendor has been called; vendor prices are placeholders and cloud custody terms are not yet held in writing (every cloud route counts as grade D). `docs/VERIFICATION.md`, written by the final verification pass, records what passed and the box-day checklist.
