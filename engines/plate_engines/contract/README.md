# plate_engines.contract: recorded request/response fixtures per provider for FixtureTransport; no live calls.

Layout: `contract/<engine>/<name>.json`, one request per file, read recursively as UTF-8. `FixtureTransport(dir)`
(adapters/transport.py) matches `(METHOD, host, path, blake3(canonical body)[:8])` and answers from the file. A request
with no recording raises `UnrecordedRequest`; it never reaches the network.

```json
{"request":  {"method": "POST", "url": "https://host/path", "body": {"any": "json"}},
 "response": {"status": 200, "json": {"id": "abc"}}}
```

- `request.url` may carry a query; a recording without one matches any query, one with a query matches only that query.
- `request.body` is hashed as compact sorted JSON (key order and spacing do not matter). Use `request.body_hash`
  (8 hex characters, from `transport.body_key`) for multipart or binary bodies; the multipart boundary is ignored.
- `response` is `{status, json | bytes_b64, headers?}`. `responses: [..]` replaces it for polling: served in order, the
  last one repeats.
- Write recordings with `transport.write_recording(...)` (UTF-8, LF, no ':' in names). Files marked `"note": "synthetic"`
  were authored from documented behaviour, not captured from a vendor.

Folders: `atlas/` (T7: upload, generateImage, generateVideo, prediction polling, a moderated prediction, a result
download) is the Atlas client's end-to-end fixture set. T17 and T18 add `gemini/`, `openai/`, `atlas_image/`, `bfl/`,
`atlas_video/`, `vertex/`, `byteplus/` and `wavespeed/` beside it.
