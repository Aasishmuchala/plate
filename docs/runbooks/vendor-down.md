# Runbook: a vendor is down, slow or refusing

Use when quotes fail, runs sit in SUBMITTED or RUNNING far past their ETA, runs end FAILED or REFUSED with the same vendor error, or an alert says a canary drifted or an engine was suspended.

**The rule that shapes everything here:** PLATE never swaps engine or model silently. The only automatic alternative is *the same model on another route* (for example Seedance 2.5 on BytePlus or WaveSpeed instead of Atlas), and it comes back as a **new quote** with its own price and its own custody grade for you to approve. A different engine is a different look; you choose it.

## 1. Is it the vendor?

1. Open the failing run's event log (History tab, or `GET /api/v0/runs/<id>`): the adapter's error text names the HTTP status and the first 300 characters of the body.
2. Rule out your side first: a `MissingKey` or a 401/403 is a key problem ([key-revoked.md](key-revoked.md)); a custody refusal (`custody`, HTTP 403) is policy, not an outage.
3. Check the vendor's status page and, if you have one, a second route to the same vendor.
4. `plate smoke` (no `--live`) replays recorded fixtures; if it passes, PLATE's adapters are healthy and only the live vendor is in question. It cannot prove the vendor is up.
5. Read `alerts.jsonl`: `canary_drift` or `engine_suspended` means the nightly checks already noticed; `price_change` or `requote` means quoted runs need a fresh quote.

## 2. Decide by the clock

| time to the deadline | do |
|---|---|
| hours | wait and re-quote once; do nothing clever |
| less than the engine's usual ETA plus two retries | go to step 3 now |
| the client is waiting on a still that needs a hero wide | do not re-photograph it: render it in V-Ray or Vantage |

Put what is left of the job on the deadline priority class (quote the node with `priority: "client_deadline"`): the worker runs those first.

## 3. Options, in this order

1. **Same model, another route** (Runway ↔ Atlas for Kling and Seedance, Runway ↔ Vertex for Veo). Re-quote the node; if the router offers a fallback route of the same model, the quote shows the new provider, price and custody grade. BytePlus and WaveSpeed are grade D with terms pending, so they are **never available on an NDA project**. Approve only if the grade is allowed. Runway is the preferred video route and also grade D (see [runway.md](runway.md)); to quote the Atlas route on purpose, set the project's `preferred_video_provider` to `atlas` or pin the engine with `video_route_pins` (artist manual, section 12).
2. **A different engine for the same task** (for an image edit: another entry in the Make bar's engine list). Quote, approve, and let the gates judge the result. For a set of cameras, redo the whole set with the same engine so the looks agree; the Set node only corrects numbers, not style.
3. **Local engine** (Qwen-Image-Edit for stills, Wan 2.2 for previs) if the GPU is free ([vram-oom.md](vram-oom.md)). It is C0 and works on NDA projects, and it is slower and softer.
4. **Render it** in V-Ray or Vantage. This is always allowed and is what the render-it router recommends for hero wides and risky moves anyway.
5. **The manual pipeline**, using the vendor's web app by hand, with every file imported, gated and ledgered: [manual-pipeline-fallback.md](manual-pipeline-fallback.md). Web apps are grade D, so this is for non-NDA work or with the client's written consent.

## 4. What not to do

- Do not resubmit a run that shows SUBMITTED or RUNNING. The provider still holds that job and the daemon is polling it (also across restarts). Cancel it only if you will not use the result: cancelling makes one cancel call, and the price is settled at the quote if the provider had the job.
- Do not pin a different engine into the project "temporarily" and forget it; pins are the project's setting and are never silently replaced.
- Do not paste client images into a vendor's web app on an NDA project.

## 5. Afterwards

Note the outage (vendor, from and to, which runs, what you did) in the project's notes or the ticket. If the vendor was down for hours, run the nightly contract tests when it is back (`plate smoke --live --budget-usd 5 --engines <id>` with `PLATE_LIVE=1`) before the next real job, and look at the next reconciliation: runs that failed at the vendor should have been refunded in the ledger.
