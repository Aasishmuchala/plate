# Runbook: a vendor changed its schema

Vendors change request fields, enums and defaults without notice. PLATE's adapters are written against recorded request and response fixtures (`engines/plate_engines/contract/<vendor>/`), and the nightly jobs compare what the vendor serves today against what the adapter was built from. This runbook is for the alert that says they differ.

## The alert

`alerts.jsonl` gets a line of kind `schema_change` with `reasons` such as `removed required field 'image'` or `changed enum on 'resolution'`. The diff has three lists: `added` (new fields, harmless unless required), `removed`, and `changed` (an attribute such as a type, a range or an enum, with the values added and removed). An alert is raised only for the two changes that break an adapter silently: **a field that was required is gone, or an enum changed**. Everything else is reported in the lists without an alert.

Related kinds you may see beside it: `price_change` and `requote` (quotes made at the old price need a fresh quote; a move of more than 5% forces it), `unit_mismatch` (the vendor's price catalogue quotes a different unit than the manifest's price model, so that entry was skipped rather than guessed), `retired_pin` (a model id your project pins has disappeared from the vendor's catalogue), `canary_drift` and `engine_suspended`.

## Steps

1. **Read the diff, not just the alert.** Which engine, which fields. Is it the request (what we send) or the response (what we read)?
2. **Decide the class.**
   - *Additive* (a new optional field): nothing breaks. Record it, add it to the manifest's parameters if it is useful, move on.
   - *Enum changed* (a resolution or duration value was added or removed): the adapter may send a value the vendor now rejects, or miss a new one. Check the manifest's `params` and the adapter's size mapping.
   - *Required field removed or renamed*: the adapter will fail. Treat the engine as down for new quotes.
3. **Protect the work in flight.** The router skips an engine whose last alert is `engine_suspended` with no `engine_resumed` after it, so new quotes do not pick it. Existing approved runs finish or fail on their own and are not resubmitted. Tell whoever is mid-set which engine to avoid. If a vendor offers version pinning, pin the old version in the project's engine pins (a pin that names a version the vendor no longer serves raises `retired_pin`; nothing is swapped silently).
4. **Fix the adapter and the fixtures together.** Edit the manifest (`engines/plate_engines/manifests/<id>.yaml`) and the adapter, then regenerate the recordings with `PLATE_RECORD_CONTRACT=1` for the intended change and review the diff of the recordings: they are the contract. Run `.venv/bin/pytest engines/tests -q`. Fixtures marked `"note": "synthetic"` were authored from the vendor's documentation, not captured, so a synthetic recording is the first thing to replace with a real capture when you have one.
5. **Prove it with one cheap real call**, then resume: `PLATE_LIVE=1 plate smoke --live --budget-usd 1 --engines <id>`. It prints the custody grade of each call before sending. If the unit price moved, update `status` and the price in the manifest only after the bill confirms it.
6. **Resume the engine** (the `engine_resumed` alert), re-quote anything held, and note the change in `CHANGELOG.md` under Changed.

## What not to do

Do not edit a recording by hand to make a test pass; the recordings are the evidence of what the vendor said. Do not "fix" an Atlas quirk (`atlas_resolution()` rejects `4K` for Kling O3 4K while the manifest allows `4K`) until the live smoke test says which side is right.
