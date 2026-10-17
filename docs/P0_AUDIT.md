# P0 audit: the day-1 scaffold against the router plan and BUILD_PLAN

2026-10-17, router plan task T1 (builder: Opus 5.5). Inputs: `~/Desktop/test/vizmaker-dossier/router/PLAN.md` (Layout contract, Decisions, T1) and `~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md` (sections 3, 4, 9). The scaffold was written by the "PLATE P0 day 1" session; nothing was committed before this audit.

State after T1: `make lint` clean, `make test` = fidelity selftest PASS, 851 pytest passed (gates 95, spec 115, pixels 60, engines 136, plugins/max 131, labeller 31, repo-wide 283), 20 vitest passed, `npm run build` works.

## 1. BUILD_PLAN section 9, day 1: what exists

Status words: **present** (exists and is tested), **partial** (exists, but untested or short of the plan), **missing** (not in the repo).

| deliverable | status | satisfied by | note |
|---|---|---|---|
| Monorepo dirs | present | `spec/ daemon/ pixels/ engines/ gates/ plugins/max/ tools/ ui/ scripts/ docs/ calib/ fixtures/` | `fixtures/`, `plugins/max/maxscript/`, `engines/plate_engines/{adapters,contract}/` created by T1 as stubs |
| Daemon skeleton: SQLite WAL, CAS, state machine on a fake engine | partial | `daemon/plate_daemon/` (23 modules, section 4) | imports and starts (`/api/v0/health` 200); untrusted seed |
| Daemon hypothesis tests for every transition | missing | none (`daemon/tests/` is empty) | `states.py` docstring claims a hypothesis suite that does not exist; T12 |
| Daemon crash-resume and idempotency tests | missing | none | `worker.recover_on_boot` and `idempotency.py` exist untested; T12 |
| Pixels worker, stub mode | present | `pixels/plate_pixels/{server,backends,licences}.py`, `pixels/tests` | 127.0.0.1:4611; torch never imported; CUDA via `pixels/requirements-cuda.txt` |
| Engine manifests | present | `engines/plate_engines/manifests/*.yaml` (13) | moved from `engines/manifests/` (section 2); T7 wants at least 14 (no separate `seedance-2.5-i2v`) |
| Atlas quirks as contract tests | present | `engines/plate_engines/atlas.py`, `engines/tests/test_atlas_contract.py` | |
| UI placeholder canvas | present | `ui/src/{App,PlateCanvas,canvasRules,store,nodeTypes}.ts(x)` | 20 vitest; dev port now 5173 (section 3) |
| plugins/max seeded from maxbridge, stdlib floor, fake pymxs | partial | `plugins/max/plate_max/`, `plugins/max/tests/{fake_pymxs,test_stdlib_only}.py` | tests and ruff ran for the first time in T1 (131 pass); never run inside Max; untrusted seed |
| Gates: fidelity_bench selftest as CI job 1 | present | `gates/plate_gates/selftest.py` via `python -m plate_gates.fidelity_bench selftest` | `fidelity_bench.py` is a 14-line shim onto `cli.py`, not the original single file |
| Gates: consolidated scripts and goldens | present | `gates/plate_gates/*.py`, `gates/tests/golden/` (2.4 MB) | client-data tests skip when `~/Desktop/test` folders are absent |
| Spec v0, JSON Schema, three examples | present | `spec/PACKET.md`, `spec/packet.schema.json`, `spec/examples/*`, `spec/plate_spec/` | now 0.1.1 (section 3) |
| `docs/custody.md` | present | `docs/custody.md` | every cloud route D today, Anthropic included |
| `docs/consent-template.md` | present | `docs/consent-template.md` | |
| Calibration labelling stub | present | `tools/plate_label/`, `calib/README.md` | writes `calib/labels.jsonl` (gitignored); 127.0.0.1:4612 |
| `CLAUDE.md` with routing rules and doctrine | present | `CLAUDE.md` | |
| ruff, pytest, vitest | present | `pyproject.toml`, `Makefile`, `ui/package.json` | |
| `.gitattributes` | present (fixed in T1) | `.gitattributes` | the scaffold's version put several patterns on one line, which git reads as invalid attribute names, so `*.ps1` got no `eol=crlf`; rewritten, and `tests/test_portability.py` now rejects that form |
| `scripts/*.ps1` | partial | `scripts/{_common,bootstrap,test,start-daemon,register-daemon-task}.ps1`, `plugins/max/scripts/{install,uninstall}.ps1` | static checks pass (section 5); never run; `*.sh` twins, `hazard_scan.py`, `firewall-tailscale.ps1` and `justfile` are missing (T11) |
| CI matrix windows-latest + macos-latest | partial | `.github/workflows/ci.yml` | matrix present; the hazard scan, licence check and `npm run build` steps are missing (T11) |
| platformdirs | present | `daemon/plate_daemon/paths.py`, `pixels/plate_pixels/backends.py` | `appauthor=False`; plugin re-derives it in stdlib (`plate_max/config.py`) |
| Explicit UTF-8 | present | `tests/test_portability.py` | enforced repo-wide, now includes `fixtures/` |
| portalocker lockfile | present | `daemon/plate_daemon/lock.py` | |
| SQLite index in local app data | present | `daemon/plate_daemon/paths.py` (`<home>/index/<project_id>.sqlite`), `db.py` (WAL) | refuses network paths |
| Four vendor emails | missing (human) | drafts in `docs/custody.md` section 3 | sending is Aasish's |
| Day 1 on the box: Max colour mode in `docs/colour.md` | missing (on-box) | none | needs Max 2026 |
| Day 1 on the box: `bootstrap.ps1` and `test.ps1` green on Windows | missing (on-box) | none | first Windows run of anything |

## 2. Renames and moves

Package names already matched the Layout contract (`plate_spec`, `plate_daemon`, `plate_pixels`, `plate_engines`, `plate_gates`, `plate_max`, `plate_label`); none was renamed. Nothing was tracked by git yet, so `git mv` was not possible; these are plain moves and the first commit records the new paths.

| from | to | why | references fixed |
|---|---|---|---|
| `engines/manifests/` | `engines/plate_engines/manifests/` | Layout contract; T7 writes `engines/plate_engines/manifests/**` | `loader.DEFAULT_MANIFEST_DIR`, `docs/engines.md`, `docs/custody.md` |
| `gates/plate_gates/colour.py` | `gates/plate_gates/color.py` | T3 imports `plate_gates.color.ciede2000` in the same wave as T6 creates `color.py`; T6 cannot delete `colour.py` | 4 gate modules, 3 tests, `report._HASHED_MODULES` (the metric-code hash covers `color.py`), `docs/gates.md` |

## 3. Reconciliation

Where the scaffold and the router plan disagreed, this is what holds now.

1. **Custody of the Anthropic API: D.** Kept the scaffold's grade (`docs/custody.md`): D until written no-training and zero-retention terms arrive. The custody filter runs first, so NDA projects (C0 and C1 only) run without the Claude composer and judge. T33's NDA-derivative policy applies once the grade is C1.
2. **Token.** Kept in `config.json` in the app-data folder (`{"token", "daemon_url"}`; `PLATE_CONFIG` moves the file, `PLATE_HOME` the folder). Decision 5 also needs the daemon to print `http://127.0.0.1:4610/#token=<token>` on start; the seed does not yet (it prints the bind URL only). PACKET.md section 9 now also defines `PLATE_TOKEN_FILE` (a file holding only the token, wins over `config.json`), so T5's `token_path` and T8's override have a home. Neither is implemented in the seed.
3. **App-data folder: `platformdirs.user_data_dir("Plate", appauthor=False)`.** The plan writes `("Plate", "Sthyra")` but names the result `%LOCALAPPDATA%\Plate`, which only `appauthor=False` gives (with `"Sthyra"` Windows would get `%LOCALAPPDATA%\Sthyra\Plate`). The scaffold's form is kept.
4. **UI serving and ports.** Vite remains the dev server; the built UI is served by the daemon. Ports: daemon 4610 (API under `/api/v0/`, `ui/dist` at `/`, which T5 must add: the seed returns 404 on `/`), pixels 4611, labeller 4612, Vite dev 5173 (moved from the scaffold's 4620 to match Decision 5, T5 and T10; `--strictPort` so it cannot drift off the allowlisted origin). Under `PLATE_DEV=1` the origin allowlist must hold `http://127.0.0.1:5173` as well as `http://localhost:5173`, because the dev server binds 127.0.0.1.
5. **Packet version 0.1.0 to 0.1.1.** The scaffold's richer fields (`uncovered_share`, `id_type`, per-ID `digest`, view hash, refused shift/tilt/distortion, `objecttransform` pose) are kept. The plan's patch is folded in: `render.session: fixture`, optional `files.beauty_exr.parts: single | multi` (default single). Decision 10 also needs fixture packets to be tier A with `host.app: none`, which the scaffold's tier A rule (`host.app: 3dsmax`) forbade, so the schema now says: tier A is `3dsmax`, except `session: fixture`, which is `none` and tier A only. PACKET.md section 14 said any new field is a minor bump; it now allows optional, backward-compatible additions as patches until the first external sender. Changelog line added; examples, `plate_spec.PACKET_VERSION` and `plate_max.PACKET_VERSION` are 0.1.1; 9 new spec tests cover it.
6. **Dependencies.** Added as the plan lists. `pytest-asyncio` went to the dev group with `pyside6`, not to runtime. The `gpu` extra (`torch`, `pynvml`) is in `pyproject.toml` but never installed (`uv sync` installs no extras; `uv add` had installed torch and a plain `uv sync` removed it). The box's CUDA 12.8 torch still comes from `pixels/requirements-cuda.txt`.
7. **Test paths.** `pytest` `pythonpath` gains `.` so tests can `from fixtures.synthetic import make_pair`; `testpaths` gains `fixtures/tests` (T2).

## 4. Untrusted seeds

Both packages import cleanly and pass ruff. T1 made minimal fixes only: ruff format (whitespace), one import sort, `RunState(str, Enum)` to `StrEnum` (values unchanged), the two `newline="\n"` JSON writes in `plugins/max/tests/test_startup.py`, and the 0.1.1 version constants. Nothing below is verified beyond "imports".

### daemon/plate_daemon (23 modules, no tests)

| module | contains | imports | plan owner |
|---|---|---|---|
| `__init__` | version 0.1.0 | yes | |
| `__main__` | settings, app-data lock, `recover_on_boot`, pre-bound sockets, uvicorn; refuses 0.0.0.0 | yes, starts | T5 |
| `adapter` | engine adapter protocol and errors | yes | T7 (`adapters/base.py`) |
| `api` | FastAPI routes under `/api/v0`, bearer token check; no Host/Origin allowlist, no static UI | yes | T5 (`app.py`, `auth.py`), T25 |
| `cas` | BLAKE3 content store under `<home>/cas` | yes | T5 `store.py`; Decision 6 wants per-project `assets/<h[:2]>/<h>.<ext>` |
| `db` | SQLite connection, WAL, transactions | yes | T5 |
| `engine_fake` | scripted fake provider with crash points | yes | T12 (`jobs/fake_engine.py`) |
| `export` | export from the CAS, GATE_FAIL refusal | yes | T34, T46 |
| `fsutil` | atomic writes with `os.replace` retry | yes | shared |
| `idempotency` | provider-submit idempotency keys | yes | T12 |
| `ingest` | packet ingest by reference | yes | T25 (`packets.py`) |
| `lock` | portalocker single-writer lock | yes | T5 (`projects.Lock`) |
| `machine` | `transition()` against the table, one SQLite transaction | yes | T12 (`jobs/machine.py`) |
| `migrations` | numbered SQL migrations | yes | T5 |
| `models` | pydantic runs, quotes, envelopes, events | yes | T12 |
| `paths` | app-data home, index path, CAS root, network-path refusal | yes | T5 (`config.py`) |
| `quotes` | quote, approval, budget envelope (fake 0.04 USD price) | yes | T12 |
| `settings` | env plus `config.json`, token creation | yes | T5 (`config.py`) |
| `specbridge` | puts `spec/` on `sys.path` outside pytest | yes | redundant with the editable install |
| `states` | `RunState` and the transition table as data | yes | T12 (`jobs/states.py`) |
| `store` | per-project SQLite index opener (`Index`) | yes | name collides with T5's `store.py` (the CAS) |
| `timeutil` | UTC helpers | yes | shared |
| `worker` | one transition per step, crash recovery that re-polls | yes | T12 (`jobs/runner.py`) |

### plugins/max (10 modules, a startup script, 131 fake-pymxs tests)

| module | contains | imports |
|---|---|---|
| `plate_max/__init__` | `PLUGIN_VERSION`, `PACKET_VERSION` 0.1.1, `SENDER` | yes |
| `config` | app-data folder and `config.json`, stdlib | yes |
| `digest` | camera (from `objecttransform`), scene and node digests through pymxs | yes |
| `fsutil` | atomic writes with retry | yes |
| `legend` | Cryptomatte murmur3 ids, `id_legend.json` builder | yes |
| `packet` | manifest builder and packet writer; refuses shift, tilt and distortion | yes |
| `packet_id` | `pkt_` + ULID | yes |
| `policy` | class rules, editable/locked split, lint | yes |
| `rt` | the only door to pymxs (fake in tests) | yes |
| `transport` | loopback HTTP to the daemon, version handshake | yes |
| `startup/plate_startup.py` | Max startup script: reads `repo_path` from `config.json`, imports `plate_max` | script (run by its test) |

The manifest it writes validates against `plate_spec` (`test_manifest_from_the_fake_scene_validates`, `test_written_folder_passes_validate_packet_dir`), so it emits the current shape, not an old one.

## 5. PowerShell: static only

There is no PowerShell on this Mac. `tests/test_ps1_static.py` lexes all 7 `*.ps1` (strings, here-strings, `$()` subexpressions and comments skipped) and checks: ASCII only; CRLF on disk (converted in T1, pinned by `.gitattributes`, and `install.bat` too); (), {} and [] balanced; `param(...)` first when present; no `&&`, `||`, `??`, `?.`, ternary or bash-isms. All pass, and planted hazards fail. **Runtime is unverified.** Read-through risks for the box: `start-daemon.ps1` sends stdout to `logs/daemon.out.log`, so the `#token=` URL will not reach the console; `_common.ps1`'s `Invoke-Step` checks `$LASTEXITCODE`, so a block with no native command would throw.

## 6. For later tasks (plan text that no longer matches the repo)

- **T5:** the seed's `store.py` is the index and `cas.py` the CAS (the opposite of T5's names); `settings.py`/`paths.py` play `config.py`. Host/Origin allowlist, the `#token` print, `PLATE_TOKEN_FILE` and the `ui/dist` mount are all missing. T5's files do not let it delete the seed modules.
- **T6:** `fidelity_bench.py` is a shim. The functions T6 says to import from it live in `color.py`, `structure.py` and `compare.py`. `crops.py`, `align.py`, `motion.py`, `cli.py`, `tools_cli.py` and their tests are outside T6's files, so the orchestrator should add them if T6 is to fold them in.
- **T7:** manifest ids are hyphenated (`nano-banana-2.1`) and the loader requires `<id>.yaml`, while T7 names them `nano_banana_2_1`.
- **T8:** the seed's `config.py` and `transport.py` do the work of T8's `paths.py` and `http.py`.
- **T12:** the seed state machine sits at the top level (`states.py`, `machine.py`, `worker.py`, `engine_fake.py`), not in `jobs/`.
- **T20:** the startup script lives at `plugins/max/startup/plate_startup.py` and reads `config.json`. T20 says `plate_max/startup/` and `plugin_config.json`; one `config.json` is the reconciled choice.
- **T2:** the fixture manifest shape in Decision 10 validates now (`spec/tests::test_fixture_packet_validates`); legacy gamma needs `colour.legacy_gamma` (a number).
