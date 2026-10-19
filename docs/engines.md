# Engines: manifests, router, Atlas contract

Code lives in `engines/plate_engines/`, data in `engines/plate_engines/manifests/<id>.yaml` (15 files, plus `_*.yaml` overlays such as the price overlay), recorded request and response fixtures in `engines/plate_engines/contract/<vendor>/`, tests in `engines/tests/`. Everything is pure Python and runs the same on Windows and macOS. No test opens a socket: `engines/tests/conftest.py` makes `socket.socket`, `socket.create_connection` and `socket.getaddrinfo` raise `RuntimeError("network disabled in engines tests")`.

| module | what it does |
|---|---|
| `tasks.py` | task vocabulary (`TASKS`) and the doctrine's `FORBIDDEN_TASKS` |
| `models.py` | pydantic 2 models: `EngineManifest`, `Route`, `PriceSpec`, `IOField`, `Param`, `Licence` |
| `loader.py` | `load_manifests(dir)` and `ManifestError` |
| `router.py` | pure routing: `custody_filter`, `doctrine_filter`, `rank`, `route`, `estimate_price` |
| `atlas.py` | `AtlasClient` over an injected `Transport`, plus `FakeTransport` for tests |
| `adapters/` | one adapter per vendor route (Gemini, OpenAI Images, Atlas image and video, Runway Dev API video, BFL, Vertex Veo, BytePlus and WaveSpeed Seedance fallbacks, local Qwen and Wan stubs, upscalers) over a `Transport`; every outbound request goes through the daemon's egress guard, and the tests use recorded fixtures only |
| `prefer.py` | pure: which route of a video engine goes first (`prefer_route`) and which whole-second durations are legal on every route (`legal_durations`, `clamp_to_legal`); see [Runway (Dev API) route](#runway-dev-api-route) |
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
| `price` | `PriceSpec(model, usd, notes, status, minimum_usd)`; model in `flat, per_megapixel, per_second, per_image`; `status` is `unconfirmed` (default) or `confirmed`; `minimum_usd` (default 0) is a per-generation floor, so a quote is never below it (Seedance 2.5 on Runway: USD 0.80) |
| `determinism` | `seeded`, `best_effort`, `nondeterministic` (cloud engines say `nondeterministic` until the nightly contract tests measure them) |
| `licence` | `vendor_terms_url` (http(s) URL or `unknown`), `training_opt_out` (`null` = unknown or not applicable) |
| `allowed_tasks` | subset of `relight, people, vegetation, sky, rephotograph_close, crop_pair_motion, previs, upscale_ai_layers, recolour`; the four forbidden tasks (`text_to_image, plate_upscale, hero_wide_rephotograph, invent_camera_angle`) fail validation |
| `routes` | at least one `Route` |

`Route`: `provider`, `endpoint` (https URL, `local` or `unknown`), `region`, `custody_grade` (`C0 C1 C2 D`), `custody_status` (`confirmed` or `pending_written_terms`), `terms_hash` (sha256 hex or `unknown`), `upload_visibility` (`public private local unknown`), `same_model_fallback_of` (provider of the primary route of the same model, or null), `note`, and two optional Wave 11 fields: `price` (a route's own `PriceSpec`; when present it wins over the manifest's `price` in quotes, `estimate_price(route=...)` and the router, and when absent the manifest's price applies) and `durations` (the whole seconds this route accepts when they are not a plain range, strictly increasing, each inside the manifest's int `duration` param range; Veo on Runway: `[4, 6, 8]`).

Doctrine checks that run at load time: a route that uploads to public URLs must be grade D; C0 and `local` go together; C1 and C2 require `private`; a C1 or C2 route cannot be `confirmed` without a `terms_hash`; a fallback must name another route of the same manifest; `recolour` is in the vocabulary but no generator lists it (colour is a numeric node).

### What is placeholder in the shipped manifests

Nothing about a vendor price, region or written terms is asserted. Cloud prices are `usd: 0.0` with `status: unconfirmed` (not a vendor price). Regions are `unknown` for every cloud route, `terms_hash` is `unknown` everywhere, `vendor_terms_url` is `unknown` everywhere, `training_opt_out` is `null`. Non-Atlas endpoints are the vendors' public base URLs as understood on 2026-10-17 and stay unverified until the adapter smoke tests (`byteplus`, `wavespeed` and `magnific` are `unknown`). Only the local engines carry `confirmed` facts: C0, local, `0.0` with no vendor fee (power and amortisation are not modelled). The custody matrix (Day 9) and the vendor emails replace these values. The `runway` routes (see [Runway (Dev API) route](#runway-dev-api-route)) are the one exception: they carry a region, a `confirmed` custody status of D and a catalogue price of their own, each with its source and date.

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

## The packet summary behind the render-it verdict (decision 84)

The render-it router ([gates/render_it.md](gates/render_it.md)) is a different router from the one above: before a quote it reads the packet's own depth and IDs (`detail_resolvability`) and says whether AI should be making the shot at all. That answer needs the packet's beauty EXR decoded, which at 4K used to be paid again on the first quote after every restart. It is now computed **once, at ingest**, and stored in the project index:

- **The table.** `packet_router (packet_id, version, json, computed_utc)`, one row per Tier A packet with a beauty EXR. The `json` is `detail_resolvability`'s answer; `version` is the code's `ROUTER_SUMMARY_VERSION`.
- **Written at ingest.** The last step of a packet's ingest (`Pipeline.warm_router`) computes the row. If that fails it is a warning, not an ingest failure, and the quote computes it lazily.
- **Read at the quote.** The quote reads the row and decodes the EXR only when the row is absent or its `version` is not the current one, and then stores the fresh answer. A cold 4K quote is therefore under half a second.
- **Refilled after a reindex.** The table lives in the index, which is a cache, so `plate reindex` (any rebuild from the project folder) leaves it empty, and the **first quote of each packet after a reindex** decodes the EXR once and refills its row. Changing the formula means bumping `ROUTER_SUMMARY_VERSION`; each packet's row is then recomputed by its next quote.

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

## Runway (Dev API) route

Wave 11 (decisions 59 to 70). Runway is the **preferred video provider**: Seedance 2.5 (image-to-video and reference-to-video), Kling O3 4K, Kling 3.0 Pro (PLATE's `kling-v3-pro`) and Veo 3.1 each gained a `runway` route, and `gen-4.5` is a new engine with that route only. The Atlas, Vertex, BytePlus and WaveSpeed routes stay as fallbacks. Images are untouched: they keep their direct routes. Code is `engines/plate_engines/adapters/runway_video.py` (`RunwayVideo`) over the tables in `adapters/runway_schema.py`, the ordering rule is `prefer.py`, the recordings are in `contract/runway/` (every file marked `synthetic`: authored from the vendor's documentation, not captured), and the tests are `engines/tests/test_adapters_runway.py`, `test_runway_schema.py` and `test_prefer.py`.

**Endpoint map (decision 59).** PLATE speaks Runway's developer REST API with the user's own key (`RUNWAYML_API_SECRET`, read from the environment at call time, never logged, never in a message). It does not use Runway's MCP (a Claude-session tool, not reachable from a daemon, and its video tool takes only a start image) or Runway's SDKs (they lag the API and retry creates automatically). Every request carries `Authorization: Bearer <key>`, `X-Runway-Version: 2024-11-06` and `Content-Type: application/json`, and leaves only through the daemon's `Egress` transport (host allowlist, PNG strip, leak scan, outbound log).

| call | used for |
|---|---|
| `POST https://api.dev.runwayml.com/v1/image_to_video` | a start frame, or a start and end pair, as `promptImage [{uri, position}]` |
| `POST /v1/text_to_video` | Seedance 2.5 reference mode (`seedance-2.5-r2v`): the hero still and the room stills as `references`, the clay sequence as `referenceVideos[0]`. The endpoint name is Runway's; the request is reference-to-video by content, and the adapter refuses an empty reference list exactly as the Atlas adapter does |
| `POST /v1/uploads`, then one multipart `POST` to the returned `uploadUrl` | a frame too big for a data URI, and every video |
| `GET /v1/tasks/{id}` | poll |
| `DELETE /v1/tasks/{id}` | cancel (204; a 404 is ignored, as the vendor says) |

**Model map and closed schemas (decision 60).** One adapter serves all six engines. Each Runway model has a closed field set in `runway_schema.MODELS` (the exact keys a body may carry, the legal ratio strings by tier, the legal durations, the prompt caps, keyframe and reference rules, credits per second). `build_body` can only emit keys in that set, `check_body` runs on every body, and the tests prove it for every model: an unknown field is never sent to a closed schema, and a prompt is never silently truncated (too long raises `InputError`).

| PLATE engine | Runway model | endpoint | ratio tiers | durations (s) | USD per second on the route |
|---|---|---|---|---|---|
| `seedance-2.5-i2v` | `seedance2_5` | `image_to_video` | 480P, 720P, 1080P | 4 to 30 | 0.20, 0.30, 0.68 by tier; at least 0.80 per generation |
| `seedance-2.5-r2v` | `seedance2_5` | `text_to_video` | 480P, 720P, 1080P | 4 to 30 | the same |
| `kling-o3-4k` | `klingO3_4k` | `image_to_video` | 4K (3840:2160, 2160:3840, 2880:2880) | 3 to 15 | 0.43 |
| `kling-v3-pro` | `kling3.0_pro` | `image_to_video` | 1080P (1920:1080, 1080:1920, 1440:1440) | 3 to 15 | 0.12 (audio is always off on this route) |
| `veo-3.1` | `veo3.1` | `image_to_video` | 720P, 1080P (16:9 and 9:16) | 4, 6, 8 | 0.20 without audio, 0.40 with |
| `gen-4.5` | `gen4.5` | `image_to_video` | six ratios (1280:720, 720:1280, 1104:832, 960:960, 832:1104, 1584:672) | 2 to 10 | 0.12 |

Prices are Runway's published catalogue at USD 0.01 per credit, read and checked on 2026-10-18, and the first bill reconciles them like every other price. The ratio always follows the source frame to within 2.5%: Runway centre-crops a frame whose aspect is not legal, and a crop would move a crop pair's landing, so such a frame is refused with the legal ratios named (the Atlas route follows the source aspect).

**Data URI or upload (decision 64).** A PNG goes inline as a `data:image/png;base64,` URI when the encoded string is at most 5,242,880 characters (about 3.9 MB of PNG). Above that, and for every video (the clay reference), the adapter makes one `POST /v1/uploads {filename, type: "ephemeral"}` with an opaque file name (the first 26 hex characters of the bytes' BLAKE3, never the client's name) and one multipart `POST` of `fields` plus `file` to the returned `uploadUrl`, with no Authorization header on that hop, then uses the `runway://` URI (valid 24 hours). A failed upload is not retried (the vendor says start over): the submit fails before any task exists.

**Hosts and the `runway_hosts_unverified` flag (decision 64).** Each runway route allows `api.dev.runwayml.com`, `.cloudfront.net` and `.amazonaws.com`. The only output host the vendor documents is a CloudFront distribution (`dnznrvs05pmza.cloudfront.net`, signed `_jwt` URLs that expire in 24 to 48 hours) and the presigned upload host is not documented at all (Runway runs on AWS), so both suffixes are a guess and every runway manifest carries the flag `runway_hosts_unverified`. An upload or output host outside the list fails closed with its name in the message (`HostNotAllowed` or `EgressBlocked`), never a silent retry. The live smoke records the real hosts in `outbound.jsonl`; then the list is narrowed and the flag dropped ([runbooks/runway.md](runbooks/runway.md)).

**The 5 second poll rule (decision 65).** Runway asks for no more than one look per task every 5 seconds. `poll` keeps the last answer and returns it without a request when asked again inside 5 seconds (the adapter's own cadence is 6 s). `PENDING` and `THROTTLED` read as `queued` (`THROTTLED` means the account is at its concurrency limit and the task waits at the vendor: not an error), `RUNNING` as `running`, a 429 or 5xx on the GET as `running` with a `transient` message, `SUCCEEDED` as `done` with every `output` URL, `CANCELLED` as `failed` ("cancelled at the vendor") and a 404 as `failed` ("task not found (deleted or cancelled)").

**No idempotency key, no retry (decision 65).** Runway has no idempotency key on a create and no list-tasks endpoint. So `RunwayVideo` makes exactly one create `POST` per submit and never retries it on a timeout, a 429, a 502, 503 or 504 (each is a `VideoSubmitError` naming the status; a timeout propagates), and it declares `idempotent_submit = False` and `supports_lookup = False`. The worker's existing rule then applies: a submit whose answer was lost ends the run `submit_state_unknown` for a human, and the task id, when the answer did arrive, is on the run before the first poll. Nothing re-approves blind. Since Wave 12 (decision 76) the worker classifies the fault instead of guessing: a submit that raised after the request may have gone out (an httpx timeout, network or protocol error, or a 5xx) is ambiguous, and with no lookup and no idempotent re-stage the run ends FAILED `submit_state_unknown`; the ledger's `failed` record then carries `billing_unknown: true` and is never `refunded`, because the money may have been spent. An error while PLATE was building the request (`staging_error`) is not a provider failure and never counts against the route's health.

**Failure prefixes (decision 66).** A failed task is classified by its `failureCode` prefix (a create answered with 400 and a `reason` of the same family is classified the same way). `JobState` is unchanged; the message prefix is what the runbook and the ledger read.

| `failureCode` starts with | state | message starts with | meaning |
|---|---|---|---|
| `SAFETY.`, `INPUT_PREPROCESSING.SAFETY.` | `refused` | `refused: runway:<code>:` | moderation; `SAFETY.INPUT.*` adds "credits are not refunded" |
| `INTERNAL.BAD_OUTPUT.` | `failed` | `bad_output:` | quality rejection; fix the prompt or the inputs, then re-quote |
| `ASSET.`, `INPUT_PREPROCESSING.VALIDATION`, `INPUT_VALIDATION.`, `DEPRECATED_MODEL`, `QUOTA`, `FORBIDDEN` | `failed` | `input:` | the asset, ratio or account is the problem |
| `THIRD_PARTY.`, `INTERNAL`, `INPUT_PREPROCESSING.INTERNAL`, or no code | `failed` | `transient:` | wait, then re-quote |

A create answered with 400 and a `SAFETY` reason becomes a stateless refused job id (the Atlas pattern); any other 400 is a `VideoSubmitError` naming `issues[].path` and the `reason`. A 401 names the key variable, a 429 says "daily generation cap or rate limit; not retried", and a 5xx says to check the dev portal Request History before re-quoting.

**What a Runway model cannot express is refused or folded, never dropped (decision 69).**

- `negative_prompt` on a model with no `negativePrompt` field (Kling, Seedance, Gen-4.5) is folded into `promptText` as a last line `Avoid: <text>`, and the submitted body shows it. Veo has the field and gets it.
- `seed` on Kling raises `ParamError` (there is no seed field); `cfg_scale` on `kling-o3-4k` other than the manifest default 0.5 raises `ParamError`. For a Motion, CropPair or previs run this is found at the quote, not at the submit, and the quote then picks another route (below).
- `elements` on `kling-v3-pro` raises `InputError`; on `kling-o3-4k` they become `references: [{uri}]` (names are not a Runway field), at most 7 images including the two keyframes.
- `camera_fixed: true` or `return_last_frame: true` on Seedance raises `ParamError` ("not expressible on the runway route; pin the atlas route").
- `resolution` on a Kling route must equal the model's tier (`4K` for `klingO3_4k`, `1080P` for `kling3.0_pro`) or be absent.
- `audio` is sent `false` unless the manifest's `generate_audio` is true (Veo, Seedance).
- An end frame given to a model that takes a start frame only raises `InputError` (Gen-4.5). Seedance image-to-video has no end input in PLATE's manifest, so the daemon refuses an end still on it earlier, as `end_unsupported`.

**The quote checks what the submit would (Wave 12).** A Motion, CropPair or previs quote builds the adapter of the route it picked, exactly as the submit does, and runs `validate_params` on the plan's parameters and the adapter's `check_expressible` (the same inexpressible-parameter refusals the submit makes) before any run row exists. Two consequences:

- **A negative prompt on a model with no field for it is folded at the quote.** The plan leaves `negative_prompt` out when it is empty. When it is set and the Runway model has no `negativePrompt` field (Kling, Seedance, Gen-4.5), `validate_params` accepts it and the body builder folds it into `promptText` as a final `Avoid: <text>` line; no `negative_prompt` key is ever sent to a closed schema, and the folded text is what the artist sees in the submitted body. Veo has the field and gets it.
- **A parameter the preferred route cannot express makes the quote pick the next route.** If the preferred provider's route refuses a parameter (a `seed` on a Kling model, which Runway has no field for), the quote tries the next route in the preference order that validates, for example Atlas for the same Kling model, and says so in `quote.router.reasons`: `prefer: runway skipped: <ParamError text>`. The price, grade and route shown are the ones that will run. If no route can express the parameter the quote is 422 `{"error": "params", "detail": <the adapter's text>}` and nothing is created. Nothing is dropped silently and nothing is sent that the vendor would ignore.

**`draft` (decision 68).** Seedance 2.5 `draft` is PLATE's 480P tier on Runway: `resolution: 480P` sends `draft: true` with the 480p ratio for the frame's aspect (Runway bills the preview at the 480p rate); 720P and 1080P send a tiered ratio and no `draft`. The 7-day enhance of a succeeded draft (`text_to_video {model, draftTaskId}` and nothing else) is built into the adapter as `inputs.draft_task_id` and is not wired into previs (open question 33).

**Route-level price fields (decision 61).** The manifest-level price stays as it was (unconfirmed, dated 2026-10-17) and the Atlas route stays **first** in every `routes:` list, so `prices.refresh` and every older fixture are unchanged. The runway route carries its own `Route.price` (status `confirmed`, `as_of 2026-10-18`, with `tiers` for Seedance by resolution and for Veo by `generate_audio`) and, for Seedance, `PriceSpec.minimum_usd: 0.80`. `quotes.build_quote`, the router's unit price and `router.estimate_price(route=...)` read the route's price first and the manifest's second; the floor applies per output. The Runway adapter's own quote comes from the credit tables in `runway_schema` (`usd_per_second`, `minimum_usd`), which carry the same figures. The Make bar and `GET /engines` show each route's price and whether it is the preferred one.

**Custody (decision 62).** The runway route declares grade **D**, status `confirmed`, `upload_visibility: private` (inline data URIs or 24 hour ephemeral uploads; outputs on signed expiring URLs; nothing publicly listed). D is the evidence, not a guess: the self-serve Terms of Use (15 Sep 2026) section 4.4 lets Runway train on Inputs and Outputs under a perpetual licence. A signed Runway Dev Enterprise agreement (no training, zero retention) would make it C2, and the promotion is a manifest edit on every `runway` route (grade `C2`, status `confirmed`, `terms_hash` = sha256 of the agreement) with nothing else changing. Until then `custody.project_allowed` keeps an NDA project away from it entirely; see [custody.md](custody.md) for the row, the sub-processors (Google Cloud, Byteplus and Kling AI in Singapore on Seedance, Kling and Veo calls) and the email to send.

**The preference rule (decision 63).** Preference is a rule, not list order. `prefer.prefer_route(manifest, candidates, provider=..., frame=..., tier=...)` moves the preferred provider's routes (default `runway`) to the front for a video engine when the frame fits one of that route's legal ratios; otherwise the routes keep manifest order and a reason says why, for example `prefer: runway skipped: no legal ratio for 1920x804 on klingO3_4k`. The reasons ride on the quote. The overrides, in precedence order:

1. the `route_id` in a quote body;
2. the project setting `video_route_pins` (`{engine_id: route_id}`);
3. the project setting `preferred_video_provider` (default `runway`; `none` means manifest order; a provider named by no active route is a 422 listing `known_providers`);
4. for `router.choose` only, `Pin.route_id` on an `engine_pins` entry (a route no manifest serves is a `RetiredPin`), and `Prefs.prefer_provider` as the first sort key.

The first three apply to Motion, CropPair and previs quotes (`kling_job.pick_route`, `previs._pick_route`). Settings are written with `PUT /projects/{id}/settings`.

**Durations are the intersection (decision 63).** `prefer.legal_durations(manifest)` is the set of whole seconds **every** active route of the engine accepts (a route's own `durations`, else the manifest's `duration` range). `kling_job.clamp_duration` ceils the shot length onto that set, so a plan is legal on whichever route the quote picks and the quote never under-prices. Veo is the example: Vertex takes 4 to 8, Runway only 4, 6 and 8, so a 5 s shot becomes 6 s on Vertex and Runway alike; Kling takes 3 to 15 on Runway, so a 1 to 2 s shot becomes 3 s on every route. The take outlasts the shot, never the other way round.

**Start-frame-only Motion engines (decision 70).** `kling_job.motion_engines(manifests)` replaces the hard-coded list: any manifest of kind `video_startend` or `video_i2v` with an `image` or `start_image` input is a Motion engine, and one with no `image_tail` or `end_image` input is single-image only (an `end` still wired to it is a 422 `end_unsupported`). `START_END_ENGINES` is now the tuple of the ones with a tail input (Kling O3 4K, Kling v3 Pro, Veo 3.1). Seedance 2.5 image-to-video and Gen-4.5 are Motion engines without one.

**Not routed: `video_to_video` (Aleph 2, Kling O3 v2v, Seedance v2v) and `video_upscale` (decision 67).** Aleph re-renders every pixel of an input clip: there is no mask lock and no plate outside the mask, so every frame is invention over the whole frame, which breaks doctrine rules 2 and 3 and the mask contract. A video upscaler was refused in Wave 9 for the same reason. If a concept-tier use ever appears it comes back as its own decision with a PREVIS-only watermark path; nothing here builds it.

Later option: Runway's `text_to_image` family (GPT Image 2.5, Nano Banana 2.1, Gen-4 Image) is on the same key; images keep their direct routes in this wave.
