# PLATE

Sthyra's scene-owning AI finishing pipeline for 3ds Max 2026 + V-Ray 7 (+ Vantage 3.3). The 3ds Max plugin exports a **Shot Packet** (camera, Z, normals, Cryptomatte IDs, the VFB-corrected 16-bit master, clay motion sequences) instead of a screenshot; a Python daemon composites AI output back onto the untouched render only inside the masks the IDs allow; measured fidelity gates can block delivery; every run is quoted before any spend; BYOK, no credits. Working name, rename freely.

**The plan is [`~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md`](../test/vizmaker-dossier/BUILD_PLAN.md)** (29 settled decisions in section 3, architecture in section 4, phases in section 5, the first two weeks in section 9). The doctrine and the model-routing rule are in [CLAUDE.md](CLAUDE.md). The packet contract is [spec/PACKET.md](spec/PACKET.md).

## Layout

| path | what | status |
|---|---|---|
| `spec/` | Shot Packet v0.1.1: `PACKET.md`, `packet.schema.json`, three example packets, `plate_spec` (validator + reference camera math + Cryptomatte hash) | real, 115 tests |
| `daemon/` | FastAPI daemon (127.0.0.1:4610): SQLite WAL, content-addressed store, the job state machine on a fake engine | untrusted seed: imports and starts, no tests yet |
| `pixels/` | GPU worker seeded from MaxDirector's `cv_sidecar`; stub mode on the Mac; licence registry | stub mode, 60 tests |
| `engines/` | engine manifests (YAML, `plate_engines/manifests/`) for the decision-13 roster, router policy, Atlas quirks as contract tests | 13 manifests, 136 tests |
| `gates/` | the fidelity harness and the per-project gate scripts consolidated into `plate_gates`, with golden-image tests | selftest + goldens, 95 tests |
| `ui/` | Vite + React + TypeScript + @xyflow/react + zustand canvas placeholder | placeholder, 20 vitest |
| `plugins/max/` | `plate_max`: stdlib-only Max-side packet writer seeded from MaxDirector and MaxGaffer `maxbridge`, fake-pymxs harness | untrusted seed: 131 fake-pymxs tests, never run in Max |
| `tools/plate_label/` | three-key labelling tool (Ship / Fix / Render-it + reason codes) writing `calib/labels.jsonl` | stub, 31 tests |
| `fixtures/` | Blender-rendered fixture packets and numpy synthetics (router plan T2) | empty skeleton |
| `docs/` | custody matrix, client-consent template, [P0 audit](docs/P0_AUDIT.md) | custody and consent drafted; on-box items open |

## Run it

```bash
make setup     # uv sync (Python 3.12) + npm install
make test      # fidelity_bench selftest first, then pytest, then npm test
make lint
```
