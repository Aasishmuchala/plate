# Gates (`gates/plate_gates`)

Numeric gates that can block delivery. Every gate is a pure function over images; the CLI, the daemon and the
tests call the same functions. Import the package as `plate_gates` (`gates/` is on the pytest `pythonpath`; on a
shell use `PYTHONPATH=gates`, which is what `make selftest` does).

## Module map (gate core, task T1)

| Module | Public names | Replaces |
|---|---|---|
| `color.py` | `srgb_to_lab`, `lab_to_srgb`, `hex_to_rgb8`, `ciede2000` | colour section of `vizmaker-dossier/tools/fidelity_bench.py`; Lab inverse from `car-entry/tools_labmatch.py` |
| `structure.py` | `ssim_gray`, `canny`, `long_edges`, `tol_metrics`, `best_shift`, `LONG` | structure section of `fidelity_bench.py` |
| `compare.py` | `load`, `compare`, `idmask`, `erode_mask`, `maskdiff`, `views`, `read_image`, `write_png` | driver section of `fidelity_bench.py` (`load`, `compare`, `idmask`, `erode_mask`, `maskdiff`, `views`) |
| `labmatch.py` | `lab_match` | `car-entry/tools_labmatch.py` (Reinhard mean/std transfer in Lab) |
| `thresholds.py` | `THRESHOLDS`, `DEFAULT_THRESHOLDS_ID` | BUILD_PLAN decision 14 numbers, now data |
| `report.py` | `metric_code_hash`, `gate_report` | new (decision 27: versioned gate reports) |
| `selftest.py` | `run_selftest` | `selftest()` in `fidelity_bench.py` |
| `cli.py`, `__main__.py` | `python -m plate_gates {selftest,pair,idmask,maskdiff,views}` | the `fidelity_bench.py` argparse driver |
| `fidelity_bench.py` | shim | keeps `python -m plate_gates.fidelity_bench selftest` (Makefile, CI) working |

The motion, texture, alignment, crop, composite and sheet gates live in the sibling modules owned by the
motion/tools task (`video`, `motion`, `texture`, `align`, `crops`, `composite`, `sheets`, `tools_cli`).

## Numerics

Ported function for function; kernels, sigmas and constants are unchanged (SSIM 11x11 sigma 1.5, Canny sigma 1.2
with median-adaptive thresholds, long edge = 30 px connected, CIEDE2000 per Sharma/Wu/Dalal 2005, D65 white
(0.95047, 1, 1.08883)). Deliberate differences, none of which change a metric value:

- `long_edges` keeps components through a label lookup table instead of one full-image compare per component.
- Image I/O uses `imdecode` / `imencode` so non-ASCII Windows paths work; failures raise `FileNotFoundError` /
  `ValueError` instead of `SystemExit`.
- `lab_match` treats a zero-variance channel as gain 1 (the script divided by zero).
- `views()` resolves relative image paths against `base_dir` (default: the manifest's folder).

## Gate reports

`report.gate_report(metrics, thresholds_id, engine_version)` wraps any metrics dict as
`{gate_version, metric_code_hash, thresholds_id, engine_version, created_utc, metrics}`.
`metric_code_hash()` is the BLAKE3 hex of `color.py + structure.py + compare.py + labmatch.py` (LF-normalised), so
editing a metric changes the hash on every later report. Changing a threshold means adding a new id in
`thresholds.py`; shipped reports keep pointing at the set they were scored against. CLI: add `--report` to `pair`,
`maskdiff` or `views`.

## Goldens

`gates/tests/golden/stills/` holds three 640 px derivatives (about 1.2 MB) of real project frames, and
`expected_stills.json` freezes their metrics (identity, 3 px roll, centred-box maskdiff, plaza-vs-vantage cross).
They always run in CI. To regenerate (needs the historical folders; `PLATE_TEST_ROOT` defaults to
`~/Desktop/test`):

```
PYTHONPATH=gates .venv/bin/python gates/tests/golden/make_goldens_stills.py
```

Commit the PNGs and the JSON together. Tolerances in `test_golden_stills.py`: floats `abs=1e-3` or `rel=1e-2`
(whichever is looser), ints 0.5%. `best_shift_long.dx/dy` on natural images are not frozen (the argmax is a
near-tie); `recall_r2` is. The texture goldens under `golden/texture/` belong to the texture gate's task.

`test_fullres_optional.py` compares the full-size Vantage frame against a GPT still and skips when
`PLATE_TEST_ROOT` is absent.
