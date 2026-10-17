# Engines: manifests, router, Atlas contract

Code lives in `engines/plate_engines/`, data in `engines/plate_engines/manifests/<id>.yaml` (14 files, plus `_*.yaml` overlays such as the price overlay), recorded request and response fixtures in `engines/plate_engines/contract/<vendor>/`, tests in `engines/tests/`. Everything is pure Python and runs the same on Windows and macOS. No test opens a socket: `engines/tests/conftest.py` makes `socket.socket`, `socket.create_connection` and `socket.getaddrinfo` raise `RuntimeError("network disabled in engines tests")`.

| module | what it does |
|---|---|
| `tasks.py` | task vocabulary (`TASKS`) and the doctrine's `FORBIDDEN_TASKS` |
| `models.py` | pydantic 2 models: `EngineManifest`, `Route`, `PriceSpec`, `IOField`, `Param`, `Licence` |
| `loader.py` | `load_manifests(dir)` and `ManifestError` |
| `router.py` | pure routing: `custody_filter`, `doctrine_filter`, `rank`, `route`, `estimate_price` |
| `atlas.py` | `AtlasClient` over an injected `Transport`, plus `FakeTransport` for tests |
| `adapters/` | one adapter per vendor route (Gemini, OpenAI Images, Atlas image and video, BFL, Vertex Veo, BytePlus and WaveSpeed Seedance fallbacks, local Qwen and Wan stubs, upscalers) over a `Transport`; every outbound request goes through the daemon's egress guard, and the tests use recorded fixtures only |
| `router.py` (`choose`) | the full policy used by the daemon: custody, then doctrine, then pins, then health, then engine cards (Wilson lower bound once n >= 20), price, latency, region. `next_same_model` returns only routes of the same model and sets `requote` |
| `health.py` | health entries (ok, degraded, down, suspended) and the `alerts.jsonl` writer (`schema_change`, `price_change`, `requote`, `unit_mismatch`, `retired_pin`, `canary_drift`, `engine_suspended`, `engine_resumed`) |
| `prices.py`, `schema_diff.py`, `retire.py` | nightly price refresh into an overlay (a move over 5% forces a requote), vendor schema diff, retired-pin check |
| `cards.py`, `canaries.py` | per engine, task and shot class pass rates with Wilson bounds; the weekly canary run and the drift that suspends an engine |

## Manifest field reference

One YAML file per engine, named `<id>.yaml`. Unknown keys are rejected. Version strings are `unpinned` placeholders and `pinned: false` until the P0 smoke test.

| field | meaning |
|---|---|
| `id` | `^[a-z0-9][a-z0-9_.-]*$`, equals the file name stem |
| `name`, `version`, `pinned` | display name, pinned version string, whether the version is pinned |
| `kind` | `image_edit`, `image_gen`, `video_i2v`, `video_r2v`, `upscale` |
| `inputs`, `outputs` | `IOField(name, type, required)`; type in `image, images, video, text, mask, json` |
| `params` | `Param(name, type, default, range, enum)`; `range` only for int/float, `enum` only for enum; default must satisfy both |
| `price` | `PriceSpec(model, usd, notes, status)`; model in `flat, per_megapixel, per_second, per_image`; `status` is `unconfirmed` (default) or `confirmed` |
| `determinism` | `seeded`, `best_effort`, `nondeterministic` (cloud engines say `nondeterministic` until the nightly contract tests measure them) |
| `licence` | `vendor_terms_url` (http(s) URL or `unknown`), `training_opt_out` (`null` = unknown or not applicable) |
| `allowed_tasks` | subset of `relight, people, vegetation, sky, rephotograph_close, crop_pair_motion, previs, upscale_ai_layers, recolour`; the four forbidden tasks (`text_to_image, plate_upscale, hero_wide_rephotograph, invent_camera_angle`) fail validation |
| `routes` | at least one `Route` |

`Route`: `provider`, `endpoint` (https URL, `local` or `unknown`), `region`, `custody_grade` (`C0 C1 C2 D`), `custody_status` (`confirmed` or `pending_written_terms`), `terms_hash` (sha256 hex or `unknown`), `upload_visibility` (`public private local unknown`), `same_model_fallback_of` (provider of the primary route of the same model, or null), `note`.

Doctrine checks that run at load time: a route that uploads to public URLs must be grade D; C0 and `local` go together; C1 and C2 require `private`; a C1 or C2 route cannot be `confirmed` without a `terms_hash`; a fallback must name another route of the same manifest; `recolour` is in the vocabulary but no generator lists it (colour is a numeric node).

### What is placeholder in the shipped manifests

Nothing about a vendor price, region or written terms is asserted. Cloud prices are `usd: 0.0` with `status: unconfirmed` (not a vendor price). Regions are `unknown` for every cloud route, `terms_hash` is `unknown` everywhere, `vendor_terms_url` is `unknown` everywhere, `training_opt_out` is `null`. Non-Atlas endpoints are the vendors' public base URLs as understood on 2026-10-17 and stay unverified until the adapter smoke tests (`byteplus`, `wavespeed` and `magnific` are `unknown`). Only the local engines carry `confirmed` facts: C0, local, `0.0` with no vendor fee (power and amortisation are not modelled). The custody matrix (Day 9) and the vendor emails replace these values.

## Custody semantics (BUILD_PLAN decision 9)

C0 local, C1 first-party with written no-training and zero-retention terms, C2 named reseller with terms held, D unknown or public. The router does not trust the declared grade: `effective_grade(route)` is the declared grade only when `custody_status == "confirmed"`, otherwise `D`. So Gemini, OpenAI and Vertex are "C1 candidates" (`pending_written_terms`) and count as D until the written terms are filed and the manifest is edited. A route with `upload_visibility: public` is D regardless of what it declares.

Atlas uploads return public OSS URLs, so every Atlas route is `D / public` and is refused for NDA projects (`max_grade` C1 or lower). There is no C1 video path today: NDA video means local Wan 2.2 previs or written client consent.

## Router order

`route(task, project_max_grade, manifests, health, prefer_region=None, engine_pin=None) -> RouteDecision`, else `Refusal(code, reason)` with code in `custody`, `doctrine`, `health`, `pin_missing`.

1. A pin that is not in the manifests is `pin_missing`. A pin restricts the pool to that engine and nothing else: a pinned engine that fails custody is a `custody` refusal, never a silent swap.
2. A forbidden task is a `doctrine` refusal before anything else.
3. Custody filter: routes with `effective_grade <= project_max_grade` in `C0 < C1 < C2 < D`. Empty means `custody`; the message names what blocked it (for example "atlas uploads return public URLs (grade D)").
4. Doctrine filter: manifests whose `allowed_tasks` contain the task. Empty means `doctrine`, except when some engine does allow the task and only custody removed it, which is reported as `custody`.
5. Health, price, latency, region (`rank`): drop providers that are down or have no health entry (no signal, no spend); sort by `price.usd * price_weight + latency_ms * latency_weight`; engines with an `unconfirmed` price sort after those with a confirmed one; a region match breaks ties; then engine id and provider for determinism. A same-model fallback route is held back while the primary route it falls back from is still available. Nothing left means `health`.

`RouteDecision` carries `engine_id, version, provider, region, custody_grade` (effective), `price_usd` (the unit price), plus `price_status`, `custody_status` and `fallback_of` so the quote shows when a number is still a placeholder. `estimate_price(manifest, megapixels, seconds, images)` is unit price times the billed quantity times the output count.

## Atlas quirks (ported from `~/Desktop/staffage/src/lib/atlas.ts`, each pinned in `engines/tests/test_atlas_contract.py`)

| quirk | atlas.ts |
|---|---|
| base URL `https://api.atlascloud.ai/api/v1`; result hosts `.aliyuncs.com`, `.atlascloud.ai` | 8-9 |
| missing or empty `ATLASCLOUD_KEY` fails with a fixed message | 11-15 |
| upload is multipart `file` to `/model/uploadMedia`, Bearer header only (no Content-Type: the boundary is the transport's) | 17-25 |
| upload URL is `data.download_url`, else top-level `download_url` (nested wins); a non-2xx status fails even when a URL is present | 26-29 |
| every failure message is `<call> failed: <status> <first 300 chars of the compact JSON body>`; a non-JSON body becomes `{}` | 26, 28, 38, 40 |
| generateImage is JSON to `/model/generateImage` with Bearer and `Content-Type: application/json` | 33-37 |
| prediction id is `data.id`, else `id`, stringified | 39-41 |
| poll sleeps (3 s) before the first GET of `/model/prediction/{id}` | 47-50 |
| poll never checks the HTTP status; an unparsable or error body just means "keep polling" | 51 |
| payload is `data`, or the top level when `data` is null or absent | 52 |
| `outputs[0]` counts only if it is a string starting with `http`, and it wins over a failure status | 53-54 |
| status containing `fail`, `error` or `cancel` (case-insensitive) is terminal: `generation <status>: <300 chars>` | 55-58 |
| deadline passed: `prediction <id> timed out` (default 120 s) | 45-46, 60 |
| download checks the host suffix before any request; `aliyuncs.com.evil.example` and the bare domain are rejected | 64-65 |
| download sends no Authorization header (the key never goes to the OSS host) | 66 |
| download of a non-2xx response fails with `download failed: <status>`; parents are created | 67-68 |

Differences from the TypeScript, all deliberate: the key is read once before polling (a missing key fails before any sleep); the prediction id is URL-quoted into the path; downloads are written to a temp file and moved with `os.replace` (retry 5 times, 50 ms doubling) so a failed write never leaves a partial result. The module ships a `Transport` protocol and a `FakeTransport`; the adapters build on them, and live traffic only ever leaves through the daemon's egress guard.
