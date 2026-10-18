# Runbook: a Runway video run is refused, throttled, failed or stuck

Use when a Motion, CropPair or previs run on a Runway route ends REFUSED or FAILED, sits in SUBMITTED or RUNNING far past its ETA, or shows a message that starts with `runway`, `refused: runway:`, `bad_output:`, `input:` or `transient:`. Runway is the preferred video route for Seedance 2.5, Kling O3 4K, Kling 3.0 Pro, Veo 3.1 and Gen-4.5 ([engines.md](../engines.md#runway-dev-api-route)); it is grade D, so an NDA project never reaches it and this runbook does not apply there.

**The two rules that shape everything here.** Runway has **no idempotency key** and **no list-tasks endpoint**: PLATE sends exactly one create per submit and never retries it, so a create whose answer was lost may or may not have made a billed task, and only you can find out (section 2.4). And PLATE never swaps engine or model silently: the only automatic alternative is the same model on another route (Atlas for Kling and Seedance, Vertex for Veo), and it comes back as a **new quote** with its own price and grade for you to approve ([vendor-down.md](vendor-down.md)).

## 1. How to tell

Open the failing run's event log (History tab, or `GET /api/v0/runs/<id>`); the adapter's message is there, with the vendor's text and no key in it.

| the run says | what it is | go to |
|---|---|---|
| `no Runway key: set RUNWAYML_API_SECRET`, `runway 401: the key was refused` | the key is missing, wrong, disabled or the account is suspended | 2.1 |
| `runway 429: daily generation cap or rate limit; not retried` | the rolling 24 hour cap, on the create | 2.2 |
| `runway upload init failed: 429` | the upload rate limit | 2.2 |
| state `queued`, message `throttled: at the account's concurrency limit, queued at the vendor` | THROTTLED: waiting at the vendor | 2.3 |
| FAILED, reason `submit_state_unknown` | the create's answer was lost: a timeout, a dropped connection or a 5xx on the create | 2.4 |
| REFUSED, `refused: runway:SAFETY....` or `INPUT_PREPROCESSING.SAFETY....` | Runway's moderation | 2.5 |
| FAILED, `bad_output: runway:INTERNAL.BAD_OUTPUT....` | a quality rejection | 2.6 |
| FAILED, `transient: ...`, `runway 502/503/504: not retried ...` | the vendor, or a third-party model behind it | 2.7 |
| FAILED, `input: ...`, `runway 400 ... at <path>`, `no legal ratio for ...` | the asset, the ratio or the account | 2.8 |
| `HostNotAllowed`, `EgressBlocked`, or `runway upload failed: 403` | a host outside the route's `allowed_hosts` | 2.9 |
| the task succeeded at the vendor but the run cannot fetch the clip | the signed output URL expired | 2.10 |
| FAILED `result_url_expired`, `result_empty`, `result_not_image`, `result_too_large`, `fetch_error` or `resume_limit` | the download came back unusable, the URL stayed stale after one fresh poll, or the daemon stopped twice on the run | 2.10 |

## 2. What to do, by message

### 2.1 401 or no key

1. Follow [key-revoked.md](key-revoked.md). The variable is `RUNWAYML_API_SECRET`. A Runway key is **project-scoped** and shown **once** at creation; removing a person from the portal does not revoke a key they made. Rotate it in the portal.
2. Runway also answers 401 for a wrong host (`api.runwayml.com` is not the Dev API; the manifests carry `api.dev.runwayml.com`) and for a suspended account (too many moderated requests lead to suspension). If the key is right and the host is right, look at the portal.
3. A 401 **on a poll** says "the task may still run": it was created, it may bill. Look in the portal before anything else (2.4, step 2).

### 2.2 429 on create (the rolling 24 hour cap)

The create was refused; no task exists. The cap is per model, per project, over a rolling 24 hours (there is no fixed reset time), and it is set by the account's tier. PLATE does not retry.

| tier | video concurrency | video generations per rolling 24 h | monthly spend cap | reached after |
|---|---|---|---|---|
| 1 | 1 | 50 | USD 100 | default |
| 2 | 3 | 500 | USD 500 | USD 50 purchased |
| 3 | 5 | 1,000 | USD 2,000 | USD 100 purchased |
| 4 | 10 | 5,000 | USD 20,000 | USD 1,000 purchased |
| 5 | 20 | 25,000 | USD 100,000 | USD 5,000 purchased |

Tier upgrades apply immediately after the purchase. The limits count per model, per project (on tier 3 a project can run five Gen-4.5 tasks and five Veo tasks at once). Seedance 2.5 is assumed to count in the video bucket; the vendor does not say.

1. Wait for the window to roll, or buy credits to reach the next tier (the portal's usage page shows the tier and the usage).
2. Re-quote. A quote is not reusable across a refusal: new quote, new approval.
3. An upload that answers 429 (`runway upload init failed: 429`) is the separate upload rate limit: wait a few minutes and re-quote once. Do not loop.

### 2.3 THROTTLED

Not an error. Runway stored the task but has not started it because the account is at its concurrency limit; it starts them in about the order they were submitted. The run shows `queued` with the throttled message and then moves on by itself.

1. Wait. Do not cancel and resubmit: the new task goes to the back of the queue and, if both run, both bill.
2. PLATE already holds its own bound: every `runway` route has `max_inflight: 1` (tier 1 video concurrency is 1), so PLATE queues its own runs before Runway has to. You see THROTTLED when something else shares the key or project, or when the concurrency is lower than the tier says under system load.
3. Raise `max_inflight` on the `runway` routes (the six manifests in `engines/plate_engines/manifests/`) **only after the portal's usage page shows the higher tier** (3 on tier 2, 5 on tier 3), then restart the daemon. Raising it first only moves the queue from PLATE to Runway.

### 2.4 `submit_state_unknown`

PLATE sent a create and never got the task id back (a timeout, a dropped connection, a 5xx, the daemon stopped in that instant). Runway cannot be asked "did you get this?", and a create has no idempotency key, so the run ends FAILED `submit_state_unknown` for a human. A create that **timed out** is the common case: the request may have reached Runway and made a billed task, and PLATE cannot tell. The run's `failed` ledger record carries `billing_unknown: true` and is never marked `refunded`, so the ledger does not claim the money came back; if a task did bill, the daily reconciliation will show it as the gap, and step 5 is how you explain it. An error PLATE made while building the request is a different reason, `staging_error`: nothing was sent, nothing can be billed, and it does not count against the route's health.

1. **Do not re-approve blind.** A second create is a second billed task.
2. Open the Runway dev portal (dev.runway.com), **Manage, then Request History**, and look for the request by time and model.
3. If there is a task and you want its clip: let it finish in the portal, download it, and file it with `plate import <file> --project <dir|id> --packet <id|dir> --source runway` (it is imported, gated and ledgered as a grade D take; see [manual-pipeline-fallback.md](manual-pipeline-fallback.md)). If you do not want it, cancel it in the portal (a pending, throttled or running task is cancelled; a finished one is deleted).
4. If there is no task, re-quote the node and approve the new attempt.
5. Write down which of the two it was (section 6): the daily reconciliation compares the ledger with the bill, and a task PLATE never recorded is the gap it will show. The `billing_unknown` record stays as it was written; do not edit `runs.jsonl` (the hash chain is the evidence).

A 502, 503 or 504 on a create (`runway 503: not retried (no idempotency key; ...)`) needs the same step 2 first: Runway says it is shedding load, but the create may still have landed.

### 2.5 REFUSED: `SAFETY.*`

Runway's moderation rejected the input (`SAFETY.INPUT.*`, `INPUT_PREPROCESSING.SAFETY.*`) or the output (`SAFETY.OUTPUT.*`). The suffix (`TEXT`, `IMAGE`, `VIDEO`) is only a hint: it can say `TEXT` when no prompt was sent.

1. **Credits are not refunded for `SAFETY.INPUT.*`**; the message says so. Every retry with the same inputs costs again, and too many moderated requests lead to account suspension. Do not retry the same frame and prompt.
2. Change what Runway objected to: the frame (a recognisable person, for instance), the prompt, or the shot. Re-quote.
3. A refusal at create (HTTP 400 with a `SAFETY` reason) has no task behind it; the run is REFUSED and there is nothing to cancel.
4. If the same plate is refused again, take the Atlas route for that model (a new quote, also grade D) or render the move in V-Ray or Vantage ([vendor-down.md](vendor-down.md), section 3).

### 2.6 `bad_output:`

`INTERNAL.BAD_OUTPUT.*` is a rejection for quality or system reasons. The vendor's common causes are logos, watermarks or overlaid text in the inputs, a prompt that asks for text to be generated, and a prompt that asks Runway to write a prompt.

1. Fix the prompt or the input (a sign, a logo or a watermark in the frame is the usual cause on archviz plates), then re-quote. The message ends "(fix the prompt or inputs, then re-quote)".
2. Do not resubmit the same quote unchanged.

### 2.7 `transient:` and 5xx

`THIRD_PARTY.*` (a model provider behind Runway is down or shedding load), `INTERNAL`, `INPUT_PREPROCESSING.INTERNAL`, a failure with no code, or a 502, 503 or 504. The message ends "(wait, then re-quote)".

1. For a failed task, wait a few minutes and re-quote once. A failed task's credits are shown on the task (a fully refunded one reports 0); read the ledger afterwards.
2. For a 5xx **on the create**, do 2.4 step 2 first.
3. A string of these across several jobs is a vendor outage: [vendor-down.md](vendor-down.md), section 3, step 1 (Runway to Atlas for Kling and Seedance, Runway to Vertex for Veo).

### 2.8 `input:`

`ASSET.*`, `INPUT_PREPROCESSING.VALIDATION`, `INPUT_VALIDATION.*`, `DEPRECATED_MODEL`, `QUOTA`, `FORBIDDEN`, or a 400 whose message ends `at <field path>`.

1. **Asset or ratio.** The frame is off-ratio (PLATE refuses this before sending, naming the legal ratios: Runway would centre-crop it and move a crop pair's landing), is over 8000 px on a side, is not a PNG, JPEG or WebP, or the clay clip is not an MP4, MOV or WebM Runway accepts. Fix the asset; for an off-ratio frame the quote already goes to Atlas unless you pinned Runway. Re-quote.
2. **`DEPRECATED_MODEL`.** The vendor retired the model id: treat it as a schema change ([schema-change.md](schema-change.md)).
3. **`QUOTA` or `FORBIDDEN`.** The account, not the request: credits are out, or the key's project may not use the model. Check the portal.
4. A 400 that PLATE itself caused (a field the model does not take) should not be possible: every body is checked against the model's closed field list before it is sent. If you see one, it is a PLATE bug; keep the message and the run id.

### 2.9 `HostNotAllowed` or `EgressBlocked` on an upload or a download

Every `runway` route allows `api.dev.runwayml.com`, `.cloudfront.net` and `.amazonaws.com`. The documented output host is a CloudFront distribution and the upload host is not documented, so the last two are a guess and the manifests carry the flag `runway_hosts_unverified`. A host outside the list fails closed and the message names it. Nothing is retried.

1. Read the host in the message. Is it Runway's (a CloudFront or AWS name, or a Runway domain)? If it is not obviously theirs, stop and ask; do not widen the list for a host you cannot place.
2. Add that exact host name to `allowed_hosts` on the six `runway` routes (a wider suffix only if the vendor really uses many names under it), run `.venv/bin/pytest engines/tests -q`, restart the daemon, and re-quote the run. A failed upload is not retried (the vendor says start over), so the old run stays failed.
3. Record the host in section 3.
4. If the failure was on the **download** of a clip whose task succeeded, the task exists and has billed: the clip is still there. After the fix the run can be re-polled (2.10), or take the clip from the portal and import it.

### 2.10 The output URL expired

Output URLs are signed and expire within 24 to 48 hours. PLATE downloads the clip the moment the task succeeds, so this only happens when a run sat between SUCCEEDED and FETCHING for a day or two (the daemon was off, or a host was blocked, 2.9).

1. If the run is still open, restart the daemon: a run that holds a task id is re-polled, never resubmitted, and a fresh `GET /v1/tasks/<id>` returns freshly signed URLs for as long as the task is retrievable. A run left in FETCHING is re-polled **once** for a fresh URL on restart: if the task answers SUCCEEDED with a different output URL, the new URL is stored on the run and the download is tried one more time. If the URL is still refused, the run is FAILED `result_url_expired` (step 2 or 3). A download is also checked before it is kept: 0 bytes is `result_empty`, a body that is neither an image nor a clip is `result_not_image`, a picture declaring more than `PLATE_RESULT_MAX_PIXELS` (64 000 000 by default) is `result_too_large`, and a download that passes its byte cap (256 MiB for stills, 2 GiB for clips) or its 600 s deadline is `fetch_error`. A run the daemon was stopped on twice while it was in FETCHING, NORMALIZING or GATING is FAILED `resume_limit` on the third start instead of a third attempt; the message names the result blob that is kept.
2. If the run already failed, the portal's Request History still shows the task and its output while it is retained: download it there and import it (`--source runway`, 2.4 step 3).
3. If the task is gone (deleted, or past the vendor's retention, which is unpublished), re-quote. The credits were spent either way.

### 2.11 The draft enhance window

A Seedance 480P draft (`draft: true`) can be enhanced to 1080p by the vendor within **7 days** of the draft task, by sending the draft's task id alone. The adapter can do this (`inputs.draft_task_id`), but previs does not use it yet (open question 33): PLATE's 1080p pass after a 480p draft is a separate, full quote at the 1080P rate (USD 0.68 per second). Nothing is lost when the 7 days pass.

## 3. The live smoke, and the two hosts to record

Nothing in the repo has talked to the real Runway yet: the fixtures are `synthetic`, the two hosts are a guess, the prices are the vendor's catalogue and not yet a bill. The first real day does this once.

1. Put `RUNWAYML_API_SECRET` in the environment of the shell (never on the command line) and set `PLATE_LIVE=1`.
2. `plate smoke --live --budget-usd 5 --engines kling-v3-pro,seedance-2.5-i2v --out runway_live.json`. It sends the cheapest legal call of each Runway route: one 3 s Kling 3.0 Pro clip (USD 0.36) and one 4 s Seedance 480P draft (USD 0.80, the per-generation floor), prints the grade and the quote before each, and stops at the budget. It also runs every other active route of those two engines **whose key is set** (Atlas, BytePlus, WaveSpeed), so leave their keys out of that shell to test Runway alone. Read the `runway` rows in the printout and in `runway_live.json`.
3. **The smoke does not write `outbound.jsonl`**: it uses the adapter's own host allowlist, not the daemon's egress log. To read the real hosts, run one 3 s Kling 3.0 Pro clip from a throwaway **open** (non-NDA) project through the daemon with `PLATE_LIVE=1` in the daemon's environment, then read that project's `outbound.jsonl`. It has one line per request with the host (no URL query). Take the host of the upload hop (only when a frame over about 3.9 MB or a clay video is sent, as a 4K Kling frame usually is) and the host of the output download.
4. Write them here:

| hop | documented sample | observed on the box |
|---|---|---|
| presigned upload (`uploadUrl`) | none documented (the allowlist guesses `.amazonaws.com`) | _to fill in_ |
| output download (`output[]`) | `dnznrvs05pmza.cloudfront.net` (an example dated 2024) | _to fill in_ |

5. Narrow `allowed_hosts` on the six `runway` routes to those names, remove `runway_hosts_unverified` from each manifest's `flags`, run the engines tests and the fixture smoke, and note the date.

## 4. Custody facts, and the way to C2

- **Grade D, on the evidence.** Runway's self-serve Terms of Use (15 Sep 2026), section 4.4, say Inputs and Outputs may be used to train and improve Runway's models, under a non-exclusive, irrevocable, perpetual licence. No setting that limits it is documented. Zero retention and no-training are Enterprise-only.
- **Sub-processors.** A Seedance, Kling or Veo call can be processed by Byteplus Pte and Kling AI Pte (Singapore) and Google Cloud; Runway publishes the list but not which model uses which. That mapping is inferred; ask for it in writing. The row, the evidence and the email to send are in [custody.md](../custody.md) (sections 2 and 3.5).
- **What PLATE does about it.** The route declares `upload_visibility: private` (inline data URIs or 24 hour ephemeral uploads; outputs on signed URLs that expire; nothing publicly listed), so it is D for the training licence and not because the files are public. `custody.project_allowed` caps an NDA project at C0 and C1, so it never sees a Runway route.
- **The promotion to C2.** Only under a signed Runway Dev Enterprise agreement (no training, zero retention, a DPA). Then, on the six `runway` routes: `custody_grade: C2`, `custody_status: confirmed`, `terms_hash` = the SHA-256 of the signed agreement file (`shasum -a 256 <file>` on a Mac, `Get-FileHash <file>` in PowerShell), update the route `note`, edit the Runway row of [custody.md](../custody.md) and its change log, and run the custody tests. Nothing else changes. C2 admits Runway to a project that allows C2; an NDA project (C0 and C1 only) still never reaches it.

## 5. What not to do

- Do not resubmit a run that shows SUBMITTED or RUNNING. Runway still holds the task and the daemon is polling it, across restarts too.
- Do not re-approve a `submit_state_unknown` run without looking in Request History.
- Do not retry a `SAFETY.INPUT.*` refusal with the same inputs.
- Do not put the key in a script, a ticket or a chat; if it was, treat that place as compromised ([key-revoked.md](key-revoked.md)).
- Do not widen `allowed_hosts` to a host you cannot place, and never to a wildcard.
- Do not send an NDA project's frames here by hand (the web app and the Dev API are the same licence).

## 6. What to record

For every incident: the run id, the task id if there was one, the message, what the portal's Request History showed, and what you did. For a lost submit say whether a task existed. For a refusal say what you changed. For a new host say which hop and the date. If credits were spent on something unusable (a `SAFETY.INPUT.*` refusal, a task you cancelled), say so in the project notes: the next reconciliation will show it and this is the explanation.
