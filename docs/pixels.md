# Pixels service

The pixels service is PLATE's localhost CV worker: depth, calibration, segmentation, optical flow, signage text-lock, AI-layer upscale and LPIPS, each behind a commercial-safe licence registry. It is seeded from `~/Desktop/maxdirector/cv_sidecar` (a stdlib `ThreadingHTTPServer` with backends that fall back to a STUB) and lives in `pixels/plate_pixels/`.

Run it with `make pixels` or `python -m plate_pixels.server` (also `python -m plate_pixels`). It binds `127.0.0.1:4611` only; `PLATE_PIXELS_PORT` changes the port, nothing changes the address.

## Stub mode (the default)

On both Windows and macOS the service starts in stub mode and imports no `torch`: availability is probed with `importlib.util.find_spec`, never by importing, and a test fails if any module under `plate_pixels` imports `torch`/`torchvision`. Every stub answer carries `"stub": true` and a neutral value (`null`, `0.0`, `[]`) so a pipeline can be wired end to end without weights. A stub is never presented as a measurement: consumers must check `stub` before trusting a number.

`PLATE_PIXELS_REAL=1` flips the contract. A backend whose packages or weights are missing raises `ModelUnavailable` at startup instead of stubbing (the process exits with the reason). Real inference is not wired yet; see "Adding real models".

Environment: `PLATE_PIXELS_PORT` (default 4611), `PLATE_PIXELS_REAL` (`1` = require real models), `PLATE_MODELS` (model cache; default `platformdirs.user_cache_dir("Plate", appauthor=False)/models`, i.e. `%LOCALAPPDATA%\Plate\Cache\models` on Windows).

## Routes

All routes are `POST` with a JSON object body and a JSON reply. Paths are local file paths (the service is loopback only). Bad input is 400 `{"error": ...}`, an unknown route is 404 `{"error": "unknown route <path>"}`, a body over 1 MiB is 413.

| route | request | stub reply (besides `"stub": true`) |
|---|---|---|
| `/health` | `{}` | `{"ok": true, "stub_mode": true, "torch": false, "models": {<id>: {"available", "licence", "commercial_ok"}}}` (no `stub` key; lists all 17 registry ids) |
| `/depth` | `{"png"}` | `"model": "depth_anything_v2_small", "shape": [h, w], "depth_png": null` |
| `/calibrate` | `{"png"}` | `"model": "geocalib", "fov_deg": 60.0, "roll_deg": 0.0, "pitch_deg": 0.0` |
| `/segment` | `{"png", "points": [[x, y], ...]}` | `"model": "sam2.1", "mask_png": null, "shape": [h, w]` |
| `/flow` | `{"a", "b"}` | `"model": "raft", "eval_width": 960, "median_px": 0.0` |
| `/textlock` | `{"png"}` | `"model": "paddleocr", "boxes": []` |
| `/upscale` | `{"png", "scale"}` | `"model": "real_esrgan", "note": "AI layers only; never the plate"` |
| `/lpips` | `{"a", "b"}` | `"model": "lpips", "distance": null` |

`/upscale` with `{"is_plate": true}` is refused with HTTP 400 `{"error": "plate upscaling is forbidden (BUILD_PLAN section 1)"}`: the plate is never upscaled, AI upscalers touch AI-made layers only. `shape` is `[height, width]` read from the image header; a missing or unreadable image is a 400.

## Licence registry

`plate_pixels/licences.py` holds `REGISTRY: dict[str, ModelLicence]` (`id, name, licence, commercial_ok, weights_source, note`). `assert_commercial_safe(model_id)` raises `LicenceError("<id>: <licence> is not commercial-safe — <note>")` for an unknown or non-commercial id, and every backend calls it in `__init__`, so a blocked model cannot be constructed. `commercial_models()` lists the ten allowed ids. `tests/test_no_blocked_identifiers.py` fails CI if a blocked model name appears in an import or string literal anywhere in the package except the registry.

Allowed (commercial-safe):

| id | licence | note |
|---|---|---|
| `depth_anything_v2_small` | Apache-2.0 | Small checkpoint only |
| `geocalib` | Apache-2.0 code, CC-BY-4.0 weights | keep the weights attribution |
| `sam2.1` | Apache-2.0 | |
| `raft` | BSD-3-Clause | from torchvision |
| `paddleocr` | Apache-2.0 | signage text-lock |
| `real_esrgan` | BSD-3-Clause | AI layers only; never the plate |
| `lpips` | BSD-2-Clause | |
| `open_clip` | MIT | confirm the chosen checkpoint's licence |
| `qwen_image_edit_2509` | Apache-2.0 | confirm the licence file in the weights repo before any NDA claim |
| `wan2.2_vace` | Apache-2.0 | previs only, scheduled GPU windows |

Blocked (`commercial_ok=False`), and why:

| id | licence | why it is off |
|---|---|---|
| `depth_anything_v2_base` | CC-BY-NC-4.0 | non-commercial weights; only Small is Apache-2.0 |
| `depth_anything_v2_large` | CC-BY-NC-4.0 | same |
| `unidepth` | CC-BY-NC-4.0 | non-commercial weights |
| `superpoint` | Magic Leap research licence | research-only, non-commercial |
| `flux1_dev` | FLUX.1 [dev] Non-Commercial License | outputs feed paid client work |
| `matanyone` | S-Lab License 1.0 | non-commercial; off until cleared in writing |
| `rvm` | GPL-3.0 | stays isolated inside STAFFAGE, never imported here |

## Adding real models (later, not now)

Real models arrive through a separate GPU extra that is installed on the render box only, never in tests, bootstrap or CI. `pixels/requirements-cuda.txt` declares it: the RTX 5090 is sm_120 and needs torch built for CUDA 12.8+ (`https://download.pytorch.org/whl/cu128`; the default PyPI wheel on Windows is CPU-only), followed by the allowed model packages by name. A plain `uv sync` removes them again, so use `uv sync --inexact` on the box.

To wire a model: add its entry to the registry first (a reviewed licence, `commercial_ok` decided in writing), keep its package imports inside the backend's real path (never at module import time), fill in the backend's `_run_real` replacement, and keep the stub path unchanged so the Mac and CI stay torch-free. Weights live under `PLATE_MODELS/<model id>/`; with `PLATE_PIXELS_REAL=1` the backend refuses to start when that folder or its packages are missing.
