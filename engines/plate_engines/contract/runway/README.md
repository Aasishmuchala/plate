# Runway Dev API recordings (T118)

`engines/tests/test_adapters_runway.py` replays these through `FixtureTransport(CONTRACT_DIR / "runway")`; the CLI
fixture smoke (T121) replays them through `ReplayTransport`, which matches on method, host and path only. Every file
carries `"note": "synthetic: ..."`: the requests and responses were authored from the researched vendor shapes
(`router/research/runway/*.md`, skeptic passes included), not captured from a live call, so each stays UNVERIFIED
until `plate smoke --live` on a Runway route compares it with a real answer.

A request matches on (method, host, path, blake3 of the canonical body), so each create recording pins the exact body
`RunwayVideo` sends. Bodies that carry a still embed it as a `data:image/png;base64,` URI of a `png_bytes(...)` PNG
from the test module (solid colour, stored deflate block, byte-identical on every OS): changing that helper, a
prompt, or the clips below invalidates the matching recordings. The test module also holds the expected bodies.

Task ids: every happy create except Veo's and the draft enhance answers `0a6a3a0e-5a1e-4d7a-9f49-2b6f0a4c2d11`, so
`kling_o3_poll` and `kling_o3_result` serve the poll and the download for all of them (the smoke relies on this).
Every poll recording has its own task id, because a GET carries no body to tell two apart.

| file | request | answer |
| --- | --- | --- |
| `kling_o3_submit` | `POST /v1/image_to_video` klingO3_4k start+end, 3840:2160, 5 s, audio off, `Avoid:` line | 200, 215 credits |
| `kling_o3_poll` | `GET /v1/tasks/0a6a3a0e-...` | THROTTLED, PENDING, RUNNING 0.4, SUCCEEDED (cloudfront `_jwt` URL) |
| `kling_o3_result` | `GET https://dnznrvs05pmza.cloudfront.net/output.mp4?_jwt=test` | 48-byte `ftyp` stub |
| `kling_v3_submit` | kling3.0_pro start frame, 1920:1080, cfgScale 0.5 | 200 |
| `elements_o3_submit` | klingO3_4k start+end plus 3 elements as `references` | 200 |
| `upload_start_submit` | kling3.0_pro with `runway://ephemeral/big-start` (a 4 MB frame) | 200 |
| `veo_submit` / `veo_poll` | veo3.1 start+end, 1920:1080, duration 6 (from 5), negativePrompt, seed 1234 | 200; RUNNING, SUCCEEDED |
| `seedance_i2v_submit` | seedance2_5 keyframe, 1920:1080, 5 s, seed 7, no draft | 200 |
| `seedance_i2v_draft_submit` | seedance2_5 at 480P: `draft: true`, 854:480, 4 s | 200 |
| `seedance_r2v_submit` | `POST /v1/text_to_video` 2 references, the clay as `referenceVideos`, draft, 854:480, 8 s | 200 |
| `draft_enhance_submit` | `POST /v1/text_to_video` `{model, draftTaskId}` alone | 200 |
| `gen45_submit` | gen4.5 start frame, 1280:720, 5 s (no audio field) | 200 |
| `submit_safety_400` | a kling3.0_pro probe | 400 `reason: SAFETY.INPUT.IMAGE` |
| `submit_issues_400` | a kling3.0_pro probe | 400 validation, `issues[0].path ["promptImage"]` |
| `submit_429` / `submit_503` / `submit_401` | kling3.0_pro probes | 429 / 503 / 401 |
| `uploads_init` / `uploads_put` | `POST /v1/uploads` for the clay mp4; the multipart POST (`body_hash`) to `plate-test-uploads.s3.amazonaws.com` | 200 `runway://ephemeral/clay-test`; 204 |
| `uploads_init_png` / `uploads_put_png` | the same for the 4 MB start frame | 200 `runway://ephemeral/big-start`; 204 |
| `uploads_init_offlist` | a clip whose `uploadUrl` is `uploads.example.net` (the transport refuses the hop) | 200 |
| `uploads_init_403` / `uploads_put_403` | a clip whose presigned POST is refused | 200; 403 |
| `uploads_init_429` | a clip over the upload rate limit | 429 |
| `poll_failed_safety` / `_bad_output` / `_third_party` / `_asset` | `GET /v1/tasks/<own id>` | FAILED `SAFETY.INPUT.TEXT` / `INTERNAL.BAD_OUTPUT.CODE01` / `THIRD_PARTY.UNAVAILABLE` / `ASSET.INVALID` |
| `poll_cancelled` / `poll_404` / `poll_503` | `GET /v1/tasks/<own id>` | CANCELLED / 404 / 503 then SUCCEEDED |
| `cancel_204` / `cancel_404` | `DELETE /v1/tasks/<id>` | 204 / 404 |

Upload file names are opaque: the first 26 hex characters of the bytes' blake3 plus the extension. The upload and
output hosts (`*.amazonaws.com`, `dnznrvs05pmza.cloudfront.net`) are the documented sample and a guess inside the
route's `allowed_hosts`; the manifests flag them `runway_hosts_unverified` until the live smoke records the real ones.
