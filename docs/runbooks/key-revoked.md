# Runbook: an API key was revoked, leaked, expired or never set

PLATE reads vendor keys from **environment variables only**. It does not write them to `config.json`, to a project or to a log; the outbound log omits URL queries on purpose (keys can ride in them).

| vendor / route | variable |
|---|---|
| Atlas (Seedream, Seedance, Kling, Nano Banana on Atlas) | `ATLASCLOUD_KEY` |
| Runway Dev API (Seedance 2.5, Kling O3 4K / 3.0 Pro, Veo 3.1, Gen-4.5) | `RUNWAYML_API_SECRET` (project-scoped; removing a portal member does not revoke their key: rotate in the portal; see [runway.md](runway.md)) |
| Google Gemini API (Nano Banana 2.1, Pro) | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) |
| Google Vertex AI (Veo 3.1) | `GOOGLE_CLOUD_PROJECT` and `VERTEX_ACCESS_TOKEN` (a short-lived token: an expired one looks like a revoked key) |
| OpenAI (GPT Image 2.5) | `OPENAI_API_KEY` |
| Black Forest Labs (FLUX 3) | `BFL_API_KEY` |
| BytePlus (Seedance fallback) | `ARK_API_KEY` |
| WaveSpeed (Seedance fallback) | `WAVESPEED_API_KEY` |
| Magnific (AI-layer upscaling) | `MAGNIFIC_API_KEY` (or `FREEPIK_API_KEY`) |
| Anthropic (prompt composer and semantic judge) | `ANTHROPIC_API_KEY` (an API key, never a consumer login; also needs `PLATE_LIVE=1`); custody is pending, so NDA projects do not use it |

## Symptoms

`<engine>: set <VARIABLE> in the environment` (not set), an HTTP 401 or 403 in a run's event log, or a vendor email about an unusual use.

## If a key may have leaked: act first

1. **Revoke it at the vendor now.** Do not wait to confirm.
2. Look at what went out: the project's `outbound.jsonl` lists every request (time, host, bytes, run, custody grade, opaque ids, body hash). Anything you do not recognise in the window since the key was exposed is the first thing to report to the vendor.
3. Check the vendor's usage page against the ledger (`runs.jsonl`, `billed` records). A gap the ledger cannot explain is spend you did not approve.
4. Create a new key, with the narrowest scope and the lowest spending cap the vendor allows.

## Setting the new key

1. Set the variable for the user that runs the daemon (Windows: System Properties, Environment Variables, user variables; or the scheduled task's environment). Do not put it in a script that is committed.
2. **Restart the daemon.** It reads the environment when the process starts; a running daemon keeps the old value. Runs in flight are recovered on start: the daemon re-polls the provider job ids it holds and never resubmits. A job created under the old key can be read only if the new key belongs to the same vendor account.
3. Check the key with the cheapest real call: `PLATE_LIVE=1`, then `plate smoke --live --budget-usd 1 --engines <id>`. It prints the custody grade of every call before sending it and stops at the budget.
4. Quote a small node on the Make bar and confirm the quote shows the right provider and an `unconfirmed` or reconciled price chip as before.

## Afterwards

Note the date and reason. If the key lived in a chat, a ticket or a screenshot, treat that place as compromised and clean it. Prices will not change because the key did, but the daily reconciliation compares the ledger to the vendor's bill: expect the first reconciliation after a key change to be the one worth reading.
