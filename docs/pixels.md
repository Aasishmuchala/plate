# Pixels service

The pixels service is PLATE's localhost CV worker: depth, calibration, segmentation, optical flow, signage text-lock, Tier C packet building, GPU admission, AI-layer upscale and LPIPS, each behind a commercial-safe licence registry. It is a FastAPI app (`pixels/plate_pixels/server.py`), seeded from MaxDirector's `cv_sidecar` but no longer a stdlib server.

Run it with `make pixels` or `python -m plate_pixels.server` (also `python -m plate_pixels`). It binds `127.0.0.1:4611` only; `PLATE_PIXELS_PORT` changes the port, nothing changes the address.

## Auth and guards

The worker uses the daemon's bearer token: `PLATE_TOKEN_FILE` (a file holding only the token, at least 16 characters) wins, else the `token` in `config.json` (`PLATE_CONFIG`, `PLATE_HOME`, or the platformdirs app-data folder). Every route except `/health` needs `Authorization: Bearer <token>`. The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>` (a DNS-rebinding guard), and a browser `Origin` header is refused: no web page talks to this worker. The canvas reaches it through the daemon, not directly.

## Stub mode (the default)

On both Windows and macOS the service starts in stub mode and imports no `torch`: availability is probed with `importlib.util.find_spec`, never by importing, and a test fails if any module under `plate_pixels` imports `torch` or `torchvision`. Every stub answer carries `"stub": true` and a neutral value so a pipeline can be wired end to end without weights. **A stub is never a measurement:** consumers must check `stub` before trusting a number. The optical-flow backend is real on every machine (OpenCV DIS, no weights) and the answer's `impl` says which one ran.

`PLATE_PIXELS_REAL=1` flips the contract: a model whose package or weights are missing is then an HTTP 500 on its own route instead of a quiet stub. Environment: `PLATE_PIXELS_PORT` (default 4611), `PLATE_PIXELS_REAL`, `PLATE_MODELS` (model cache; default `<user cache>/Plate/models`), and the GPU admission variables below.

## Routes

All JSON in, JSON out. File **paths** go in and file paths come out, never image bytes: an output defaults to a content-named file under `<app home>/cache/pixels`, and `out` overrides it. Every answer except `/health` carries `stub`; the model routes also carry `model` and `impl` (which implementation ran, for example RAFT or OpenCV DIS for flow).

| route | request | what comes back |
|---|---|---|
| `/health` (GET or POST, no token) | `{}` | `ok`, `version`, `stub_mode`, `real_required`, `torch`, per-backend description, and every registry model with `available`, `licence`, `commercial_ok` |
| `/depth` | `{image, out?}` | a float EXR with channel `Z` (relative depth, `depth_is_metric: false`), its shape, min and max, `depth_semantics` |
| `/intrinsics` | `{image}` | pinhole `fx, fy, cx, cy`, `fov_deg`, `roll_deg`, `pitch_deg` (GeoCalib) |
| `/segment` | `{image, points?, out?}` | a 16-bit label PNG and the segment list (SAM 2.1 with click prompts) |
| `/flow` | `{a, b, out?}` | a float EXR with `U` and `V`, the evaluation width (960), `median_px` |
| `/ocr` | `{image}` | text boxes `{bbox, text, conf}` for the signage text-lock |
| `/tierc` | `{plate_path, out_dir, project_id, shot_id, consent_record, labels?, nda?}` | builds a Tier C packet (estimated depth, intrinsics, segments); **428 without a client consent record** |
| `/admission` | `{required_mb, reserve_mb, model?}` | `ok`, the reason and a free/total memory snapshot |
| `/upscale` | `{image, scale, ai_layer, is_plate}` | refused with 400 unless `ai_layer` is true and `is_plate` is false: the plate is never upscaled |
| `/lpips` | `{a, b}` | a perceptual distance between two images |

Status codes: 400 bad input, 401 or 403 auth, 428 no consent record, 500 a real model is missing or a bug, 501 not wired yet, 503 the GPU cannot take the model.

## GPU admission

The render box shares one GPU with Max, V-Ray and Vantage, so a model is admitted before it loads (`nvml.py`): NVML must report enough free memory above a reserve (2 GB by default); with no NVML the answer is refused unless `PLATE_ALLOW_UNMANAGED_GPU=1`; the heavy generative models (Qwen-Image-Edit, Wan 2.2) run only inside `PLATE_GPU_WINDOWS` (such as `01:00-06:00,13:00-14:00`) or with `PLATE_FORCE_LOCAL=1`. See [runbooks/vram-oom.md](runbooks/vram-oom.md).

## Licence registry

`plate_pixels/licences.py` holds `REGISTRY`, one entry per model with its version, weights and code licences, `commercial_ok`, source and a note. `assert_commercial_safe(model_id)` raises `LicenceError` for an unknown or non-commercial model and every backend calls it in `__init__`, so a blocked model cannot be constructed. `python -m plate_pixels.licences --check` (a CI step) parses every backend module and `requirements-cuda.txt` and fails if any import or string names a blocked model.

Allowed (commercial-safe):

| id | licence (weights / code) | note |
|---|---|---|
| `depth-anything-v2-small` | Apache-2.0 / Apache-2.0 | Small checkpoint only |
| `geocalib` | CC-BY-4.0 / Apache-2.0 | keep the weights attribution; verify before NDA use |
| `sam2.1` | Apache-2.0 / Apache-2.0 | |
| `raft` | BSD-3-Clause / BSD-3-Clause | the box's flow backend |
| `opencv-dis` | none / Apache-2.0 | the Mac fallback for RAFT; real everywhere, no weights |
| `paddleocr` | Apache-2.0 / Apache-2.0 | signage text-lock |
| `real-esrgan` | BSD-3-Clause / BSD-3-Clause | AI layers only; never the plate |
| `lpips` | BSD-2-Clause / BSD-2-Clause | |
| `open-clip` | per checkpoint / MIT | confirm the chosen checkpoint's licence |
| `qwen-image-edit-2509` | Apache-2.0 / Apache-2.0 | scheduled GPU windows; confirm the licence file before any NDA claim |
| `wan2.2` | Apache-2.0 / Apache-2.0 | VACE previs only, scheduled windows |

Blocked (`commercial_ok=False`), and why:

| id | licence | why it is off |
|---|---|---|
| `depth-anything-v2-base`, `depth-anything-v2-large` | CC-BY-NC-4.0 weights | non-commercial; only Small is Apache-2.0 |
| `unidepth` | CC-BY-NC-4.0 | non-commercial weights |
| `superpoint` | Magic Leap research licence | research-only |
| `flux-dev` | FLUX.1 [dev] Non-Commercial License | outputs feed paid client work |
| `matanyone` | S-Lab License 1.0 | non-commercial; off until cleared in writing |
| `rvm` | GPL-3.0 | stays isolated inside STAFFAGE, never imported here |

## Adding real models (box only)

Real models arrive through a separate GPU extra installed on the render box only, never in tests, bootstrap or CI. `pixels/requirements-cuda.txt` declares it: the RTX 5090 is sm_120 and needs torch built for CUDA 12.8+ (`https://download.pytorch.org/whl/cu128`; the default PyPI wheel on Windows is CPU-only). A plain `uv sync` removes the extra again, so use `uv sync --inexact` on the box.

To wire a model: add its registry entry first (a reviewed licence, `commercial_ok` decided in writing), keep its package imports inside the backend's real path (never at module import time), fill in the backend's real replacement, and leave the stub path unchanged so the Mac and CI stay torch-free. Weights live under `PLATE_MODELS/<model>/`.
