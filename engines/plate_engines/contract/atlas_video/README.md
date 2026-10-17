# Video adapter recordings (T18)

Four folders feed `engines/tests/test_adapters_video.py`: `atlas_video/` (Seedance 2.5 i2v and r2v, Kling O3 4K and
v3 Pro on Atlas), `vertex/` (Veo 3.1 predictLongRunning and fetchPredictOperation), `byteplus/` and `wavespeed/`
(Seedance same-model fallbacks). Every file carries `"note": "synthetic: ..."`: the requests and responses were
authored from the documented vendor shapes (router plan T18, VIZMAKER.md section 4/5), not captured from a live run,
so each stays UNVERIFIED until the P0 smoke test (`plate smoke --live`) compares it with a real response.

A request matches on (method, host, path, blake3 of the canonical body). That makes the recordings a pin on the exact
request an adapter sends: change a body field and the test fails with `UnrecordedRequest` and the recorded body hashes.

- Bodies that carry a still embed its base64. The stills are `png_bytes(...)` in the test module: a solid-colour PNG with
  a stored deflate block, byte-identical on every OS. Changing that helper invalidates the `vertex/`, `byteplus/` and
  `wavespeed/` recordings.
- `atlas_video/upload_start_a.json` matches by `body_hash` (multipart: field name, file name `start_a.png`, file bytes).
- Result clips are placeholder bytes (`PLATE-FIXTURE-CLIP-A/B`), not playable mp4s.
- `vertex/gcs_c_media.json` writes the object path decoded (`out/c.mp4`) because FixtureTransport matches the decoded
  request path.
- The Kling O3 4K recording has no `resolution` field: 4K is the slug (see `KlingStartEnd.build_body`).
- Result hosts for BytePlus (`*.bytepluses.com`) and WaveSpeed (`*.wavespeed.ai`) are guesses inside each route's
  `allowed_hosts`; the real CDN hosts (`*.volces.com`, `*.cloudfront.net`) would be refused until the manifests list them.
