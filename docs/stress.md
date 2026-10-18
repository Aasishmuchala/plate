# The stress target

`make stress` (Mac) or `just stress` (the render box) runs the attackers' regression scripts: the nine stress
attackers of Wave 12 (router/PLAN.md, T126-T149), each finding's repro turned into a check of the fixed
behaviour. It is opt-in: `make ci`, `make test`, the CI workflow and the test scripts never run it. On GitHub it
is the `stress` workflow, started by hand from the Actions tab (`workflow_dispatch`, both runners, 90 minutes).

    make stress                                          every script, one after another
    .venv/bin/python scripts/stress/run_all.py --only money             one script
    .venv/bin/python scripts/stress/run_all.py --only daily-cap         the scripts that define that check
    .venv/bin/python scripts/stress/money.py --only daily-cap --keep    one check, keeping its daemon home
    .venv/bin/python scripts/stress/money.py --list                     the checks of a script

Every script starts its own daemon (`python -m plate_daemon`, unpatched) on a free loopback port, with a fresh
home under `<repo>/.stress/<timestamp>/<name>/` (the folder ignores itself in git), `PLATE_FAKE_ENGINE` (`1`, the
echo fake, or the flags a check needs), `PLATE_PIXELS_INPROC=1` and `PLATE_NIGHTLY=0`; every other `PLATE_*`
variable of the caller's shell is dropped, `PLATE_LIVE` is never set and no vendor is ever reached (an adapter
that gets as far as the network meets the recorded-fixture transport). Each check prints one line, `ok`, `skip`
or `FINDING <check>: <detail>`, then the script prints `<script>: N checks, N ok, N skipped, N findings (N s)`
and exits 1 if any finding stands. A clean run deletes its homes (`--keep` keeps them); a run with a finding keeps
them and says where. `run_all.py` ends with a table and `stress: 9 scripts, N checks, N ok, N skipped, N findings`.

The scripts use the standard library and httpx and reach the daemon over HTTP only. The exceptions are named:
`max_plugin` imports `plate_max` and the stubs under plugins/max/tests, as its attacker did; `mcp_cli_imports`
imports `plate_daemon.mcp_server` and runs `plate_daemon.cli` and `plate_daemon.mcp_server --new-envelope` as
subprocesses. Two setup steps need a library the daemon itself uses and run as short helper processes:
`_lib.hold_project_lock` (portalocker, to hold a project's `project.lock` the way `plate reindex` does) and the
ledger fill of `scale` (blake3, to chain 50,000 records exactly as `ledger.py` does). Besides HTTP, a check may
read a home's files and its SQLite index; the only index writes are the quote-clock moves of `money`.
`scripts/tests/test_stress_scripts_static.py` holds every script to this (and runs under `make ci`).

To show that a check bites, point it at the code it was written against: `git archive 1f272f2 spec daemon pixels
engines gates plugins/max tools docs fixtures/packets | tar -x -C <scratch>` and run with
`PLATE_STRESS_SRC=<scratch>`; the scripts then put that tree's packages ahead of this checkout's, for the daemon and
for the two in-process scripts. On 1f272f2 every check finds its defect except `engine_faults`'s `submit-timeout`
(see there).

Measured on this Mac (Apple silicon, 2026-10-19, fake engine, nothing else running): the whole target takes about five
minutes (308 s); per script below. On 1f272f2 it takes about 13 minutes, most of it the defects themselves (a storm
that starves /health, stalled cancels, a serial resume).

## api_fuzz

`scripts/stress/api_fuzz.py`, about 20 s. The API-fuzz attacker's `repro_all.py`, turned around. One shared daemon
carries the checks, each on its own project; `unattended-clock` gets a daemon no request with `X-Plate-Client: ui`
ever touches.

- `unattended-clock` (finding unattended-clock): agent approvals stop at the 20 USD unattended cap, and a header-less
  `POST /timer` (start, stop, or with `X-Plate-Client: mcp`), scrub, verdict or accept does not lift it; a UI graph
  PUT does.
- `export-race` (export-race): 8 concurrent exports of two runs to one name have exactly one first write, the rest
  409 `not_exportable` or a repeat of the winner; one file and its sidecar; one ledger record. Cutdown and the batch
  route share the claim.
- `custody-unset` (custody-unset): a client-sent `unset` is dropped; a re-POST never loosens an NDA project (409).
- `envelope-inf` (envelope-inf): Infinity, 1e400, NaN, 1e308 and absurd TTLs are 422 and commit no row; the bounds
  themselves (1,000,000 USD, 366 days) are 201.
- `attempt-poison` (attempt-poison): attempt 2^63-1, 2^63 and 10^30 are 422; the node still quotes.
- `oserror-500` (oserror-500, 11 cases): out_dir under a file or in /etc, /dev/null/x, a 300-character component,
  NUL bytes in deliver, import and recipe paths: typed JSON, never a bare 500, no export record.
- `newline-slugs` (newline-slugs): `nl1\n`, `nlx\n` and a Tier C `s1\n` are 422 and make nothing.
- `unbounded-body` (unbounded-body): a PUT declaring 50 MiB is 413 before the body is sent; a 5 MiB graph is 422
  `graph_too_large`; project.json unchanged.
- `decode-bomb` (decode-bomb): a 435 KiB PNG declaring 12000 x 12000 is 422 `too_many_pixels` as a reference and as a
  mask, and the daemon's RSS does not move (ps; not measured on Windows).
- `pattern-width` (pattern-width): `{n:>900000000}` and the other spec, conversion and lookup patterns are 422
  `bad_name_pattern` at once.
- `ghost-index` (ghost-index): an envelope for a project that does not exist is 404 and opens no index.
- `quote-validation` (quote-validation): an unknown priority, `params.n=Infinity` and candidate counts 0.5 and 9 are
  422 before any run row; a count of 2 quotes.
- `docs-long-name` (docs-long-name): 250, 255 and 300 character doc names and a long folder are the JSON 404.
- `graph-unhashable-id` (graph-unhashable-id): a node id that is an object or an array is 422 `UNKNOWN_NODE`.
- `method-404` (method-404): HEAD /health is 200; TRACE, OPTIONS, DELETE on unknown routes and GET /api are the
  JSON 404.
- `recipe-yaml-leak` (recipe-yaml-leak): a recipe path that is not a recipe names the path, never the file's text.
- `symlink-manifest` (symlink-manifest): a manifest.json linked out of its packet folder is 422. Skipped on Windows
  (making a link needs a privilege there; the link is made with `ln -s`).

## doctrine

`scripts/stress/doctrine.py`, about 10 s. The doctrine attacker's repro_base.py, repro_override.py and
repro_mask_late.py, with flat standard-library PNGs where the attacker blurred the plate with cv2.

- `base-any-cas-blob` (base-any-cas-blob, gate-fail-laundering-via-base, depth-bypass-via-relay-node): (A) a
  reference upload relayed through a Colour node is 422 `base_not_a_take`; (B) a GATE_FAIL import picked as a base is
  422 `base_gate_fail` until it is overridden, and the child then fails its own gate and does not export without its
  own override; (C) a depth-2 take relayed through a Colour node is 422 `DEPTH_EXCEEDED` naming the chain.
- `override-not-ledgered` (override-not-ledgered-fail-closed): with runs.jsonl read-only (D) a typed-override export
  is 503 `ledger_unavailable` with no file, (E) POST /override is 503 and a batch export still refuses the take;
  (F) on a writable ledger the override record is written before the file.
- `cli-sidecar-reason` (cli-sidecar-reason-unledgered): `plate export --override-reason B` after an override A
  ledgers B before the export record, and the sidecar carries B.
- `mask-errors-at-quote` (mask-errors-after-approval): unknown ids, a non-string id and a 160 x 90 mask are 422 at
  the quote (`bad_mask`, `mask_size`) with no run row.

The read-only ledger uses the file's mode (the read-only attribute on Windows); a root account writes it anyway and
the check is skipped.

## money

`scripts/stress/money.py`, about 65 s. The money attacker's bin/ scripts on the shipped caps (auto 2.00, daily 50.00,
unattended 20.00 USD have no setting, so the checks spend nano-banana-pro quote prices instead of the attacker's
patched policy).

- `lost-submitted-record` (lost-submitted-record): the project lock held 7 s by a helper right after an approval
  loses no record: the run's records arrive in order and /spend counts 0.04 USD and one generation.
- `unattended-clock-reset` (unattended-clock-reset-by-scripts): auto approvals stop at the unattended cap; a
  header-less timer, override (with `by: human` in the body) or scrub does not lift it; a UI request does.
- `daily-cap` (daily-cap-per-index-not-per-user): across three projects the refusal is `daily_cap` "across 3
  projects" and the day's total stays at or under 50 USD.
- `ledger-order-race` (ledger-order-race-rebuild-wrong-state): 36 approve/cancel races (0 to 300 ms apart) leave one
  record per transition, in transition order, and a rebuilt index (boot after the index is deleted) agrees with the
  live one.
- `approve-500-on-cancel-race` (approve-500-on-cancel-race): the same races answer 200 or a typed 409, never 500.
- `run-stuck-after-db-locked` (run-stuck-running-after-db-locked): a second connection holding the index's write
  lock for 12 s delays a RUNNING run; it still reaches DONE.
- `concurrent-quote-same-attempt` (concurrent-quote-same-attempt): ten concurrent quotes of one node are attempts 1
  to 10 and all ten human-approved runs finish.
- `expired-ledgered` (expired-and-failclosed-cancel-not-ledgered): an expired quote is 409 `quote_expired`, EXPIRED,
  ledgered `expired`, and EXPIRED again after a rebuild.
- `quote-ttl-clock-back` (quote-ttl-clock-back): a quote made an hour "in the future" (the clock went back) is
  refused the same way, saying the clock moved.

The two expiry checks move the quote's clock by editing its rows in the project's SQLite index, as the crash
attacker's ttl_attack.py did.

## crash

`scripts/stress/crash.py`, about 30 s. The crash attacker's crash_matrix, runway_attack, corrupt_attack,
corrupt2b, ro_attack, disk_attack and export_sig.

- `seedance-negative-prompt` (seedance-i2v-runway-negative-prompt): a Seedance 2.5 Motion quoted on the preferred
  Runway route is approved and reaches the vendor transport, here the recorded-fixture transport (no recording
  matches, so the run fails there, never on `unknown parameter(s) ['negative_prompt']`). The daemon gets a dummy
  well-formed `RUNWAYML_API_SECRET`; nothing leaves the machine.
- `fetching-repoll` (fetching-no-repoll-on-boot): a run killed with SIGKILL at the echo fake's FETCHING hang marker
  is resumed after the restart and reaches DONE on its one provider job.
- `kling-seed-runway` (kling-seed-runway-submit-fail): kling-v3-pro with a seed quotes on Atlas with Runway's skip
  reason; pinning the Runway route is 422 `params` with no run.
- `corrupt-project-json` (corrupt-project-json-500): a truncated, invalid, empty or schema-99 project.json lists the
  project as broken and answers 4xx JSON on graph, runs, quote, PUT, POST /projects and approve; /health stays ok.
- `index-loss-rebuild` (index-loss-no-rebuild-sse-seq-reset): a deleted index is rebuilt on boot (runs listed, GET
  run, quote), and a pre-rebuild `Last-Event-ID` replays the new epoch from its first event.
- `ro-project-volume` (ro-volume-export-untracked): with the project's gates folder at mode 0o500 (no RAM disk
  needed) an export is 503 before anything reaches the client folder; the retry ships and the report is marked
  shipped. Skipped on Windows (folder modes) and for root.
- `low-disk` (low-disk-spend-then-fail): under the free-space floor the run fails `disk_full` before the vendor is
  called (no submit, no `submitted` record). The floor is the daemon's own `PLATE_MIN_FREE_MB`, set above the disk's
  free space, so no RAM disk or tmpfs is needed and nothing is skipped; the quote route's ENOSPC mapping needs a
  really full disk and is covered by daemon/tests/test_stress_wave12_api.py.
- `sigkill-export-part` (sigkill-export-part-left): exports killed with SIGKILL at 30 to 250 ms leave a hidden temp
  (planted when no kill lands mid-write); once older than the in-flight window the next export of the name sweeps it.

## engine_faults

`scripts/stress/engine_faults.py`, about 80 s. The attacker drove a fault-injecting subclass of the fake engine
through a launcher; these checks keep the daemon unpatched and inject the faults from outside, with the fake's own
flags, the daemon's settings and the file system.

- `decode-bomb-crashloop` (decode-bomb-crashloop): a result whose header declares more pixels than
  `PLATE_RESULT_MAX_PIXELS` fails `result_too_large` naming the size, and the daemon boots and stays up across two
  restarts. The cap is lowered to 100,000 so the fake's own 640 x 360 result plays the attacker's 16000 x 16000 PNG
  (the fake would have to draw 768 MB to make the real one); the default-cap refusal is unit-tested.
- `fetch-stall-cancel` (fetch-stall-cancel-api-freeze): with the vendor call stalled (`hang_at` RUNNING), 45 cancel
  clicks answer 202 at once and /health stays under 1 s; in FETCHING a cancel is 409 at once.
- `inflight-threads` (inflight-runs-pin-api-threads): 45 approvals of runs that never finish (`timeout`) each answer
  within 3 s; /health under 1 s, /engines under 2 s.
- `resume-starvation` (resume-serial-starvation): after a SIGKILL with two runs RUNNING, the restarted daemon polls
  both (read from the fake's journal), and both finish once the vendor does.
- `submit-timeout` (submit-timeout-recorded-as-refunded): the provider took the job and the daemon died before it saw
  the id (`crash_after_submit_before_persist`; the run executor logs the scripted crash and carries on, so the
  harness kills the process): the restarted daemon re-stages on the same key, one job at the provider, no refund
  recorded. The attacker's variant, an httpx timeout from a non-idempotent provider, cannot be injected into an
  unpatched daemon (the fake is idempotent); its `submit_state_unknown` / `billing_unknown` classification is
  unit-tested. This check passes on 1f272f2 too, whose boot recovery already re-staged a lost submit.
- `poll-transient` (poll-transient-error-fails-run): the fake's journal swapped for a folder for a few polls (its
  writes raise OSError, a transport fault) gives `poll_fault` notes, and the run finishes once the writes work.
- `unbounded-download` (unbounded-result-download): a result that is not an image (`output: text`) puts nothing into
  the CAS, no opaque `.bin`. The streamed byte cap of a real download is in the adapters
  (engines/tests/test_adapter_fetch_stream.py).
- `undecodable-results` (undecodable-result-hides-cause): the same result fails `result_not_image` at FETCHING, naming
  the bytes.

## scale

`scripts/stress/scale.py`, about 70 s (the storm alone is a minute).

- `autosave-storm` (autosave-storm-starves-daemon): 150 overlapping PUTs of a 500-node, 600-edge graph, one every
  400 ms without waiting, are each 200, 409 `graph_busy` or 429 `too_many_saves` (never 500 or `project_locked`), and
  /health stays under 3 s.
- `deep-graph` (graph-validate-superquadratic): a chain of 200 Colour nodes saves in under 2 s.
- `ingest-unlisted-ids` (ingest-unlisted-ids-quadratic): a packet whose Cryptomatte holds 16,000 one-pixel ids the
  legend does not list ingests in under 10 s. The attacker measured 400 such ids at 3840 x 2160 (28 s, 60 ms per
  id); the standard library cannot write a 4K multi-layer EXR in reasonable time, so the check rewrites two ZIP
  blocks of the committed 640 x 360 room_a EXR, the same ids-times-pixels work (1f272f2 takes 24 s, this code 0.3 s).
  The cold first quote of that packet is recorded, not asserted (quote-cold-4k-over-2s needs a 4K packet).
- `ledger-reread` (ledger-reread-per-action): with 50,000 records appended to runs.jsonl (a blake3 helper, chained
  as the daemon chains them, while the daemon is stopped) an approval answers in under 100 ms, median of ten.

## ui

`scripts/stress/ui.py`, a few seconds. The daemon's half of the UI attacker's findings; the Playwright scripts
(t3_fail, t4_restart, t5_reload, t6_twotabs) need node and a browser and are not ported: their browser half is
ui/src/test/saveResilience.test.tsx.

- `lost-update` (graph-lost-update-two-tabs): two tabs that read one revision both save; the second is 409
  `graph_conflict`, the first tab's edit is what is stored, and a client that sends no revision still saves.
- `append-concurrency` (mcp-add-node-lost-update, the daemon side): 12 concurrent `POST /projects/{id}/nodes` each
  get their own node and revision, and all 12 are stored.
- `graph-saved-revision` (graph-lost-update-two-tabs and autosave-failed-never-retried, the daemon side): the
  `graph.saved` event on `GET /events` carries the revision the save answered, so another tab can tell its copy is
  stale and a retried save can be checked against it.

## max_plugin

`scripts/stress/max_plugin.py`, about 20 s. On the fake pymxs scene, the simulated V-Ray and the stub daemon of
plugins/max/tests (as the attacker's work/ scripts did); the two job checks run against the real daemon's queue.

- `dup-node-names` (dup-node-names-editable): 'Shared' on ARCH_walls and on VEG_tree_01 stops at lint
  (`DUPLICATE_NODE_NAME`) in both scene orders, nothing rendered.
- `dup-cameras` (duplicate-camera-name-sends-first-twice): two cameras named CAM_dup stop at lint.
- `nonlatin-names` (nonlatin-camera-name-wastes-render, nonlatin-scene-name-collapses-project): CJK, Arabic,
  emoji, blank, Devanagari and dot camera names are `BAD_SHOT_SLUG` at lint with no render or folder; non-Latin
  scene names leave the project unresolved.
- `nonfinite-pose` (nan-inf-pose-accepted): NaN and infinite positions and axes are `BAD_POSE` at lint.
- `degenerate-lens` (degenerate-lens-values-late-or-opaque): focal -1 / NaN, f-number 0, film -36 / 0, zoom 0,
  ISO 0, shutter 0 are `BAD_LENS` naming the property, before the render.
- `corrupt-sidecar` (corrupt-sidecar-silent-rebind): a trailing comma, a UTF-16 BOM, an empty file, conflict
  markers or a numeric project_id block the senders with the sidecar's message and leave it byte-identical.
- `job-guard` (job-guard-fails-open): B (no manifest in the dock's packets folder) and C (sent from an unsaved
  scene) run with a "scene check skipped" warning; D (another project's master.max) is refused.
- `claimed-job-recovery` (claimed-job-orphaned): a job claimed for an answer the dock never got is taken up again
  when the dock starts, run once and closed `done` on the real daemon.
- `trickle-handshake` (no-overall-http-deadline): a peer answering one byte a second cannot hold handshake(3 s)
  past 4.5 s (the check gives up on the call after 15 s).
- `token-second-line` (malformed-token-raises-and-leaks): a token file with a second `# rotated` line refuses with
  Unauthorized; the secret is in no message and no log line, and never leaves the machine.
- `hash-503-retry` (transient-daemon-error-loses-render): one 503 from /hash costs one retry (1 s), not the render.

## mcp_cli_imports

`scripts/stress/mcp_cli_imports.py`, about 20 s. The attacker's repro.py f1-f12, with the MCP tools in process over a
loopback client and the CLIs as subprocesses on the daemon's home.

- `export-review-guard` (mcp-export-review-skips-review-guard): MCP export_review of a GATE_FAIL take is
  `not_reviewable` and writes no bundle; after a human override it builds.
- `nda-scrub` (mcp-nda-scrub-is-a-denylist): on an NDA project get_graph hands back none of the `ACME` sentinels (a
  prompt, a negative prompt, an outDir, an override's reason and by, a label) and names what it withheld.
- `fifo-imports` (http-import-no-regular-file-check): 45 FIFO imports are 422 `not_a_file` within 10 s and /health
  keeps answering. Skipped on Windows (no FIFOs).
- `concurrent-run-node` (mcp-run-node-same-node-race): 6 concurrent run_node calls on one node are attempts 1 to 6
  and all six finish.
- `concurrent-add-node` (mcp-add-node-lost-update): 12 concurrent add_node calls leave 12 nodes.
- `outbound-log-failure` (mcp-outbound-log-failure-after-spend): with outbound.jsonl read-only, run_node's and
  add_node's `outbound_log_failed` name the run and the node the daemon made.
- `still-named-mp4` (import-still-named-mp4-is-a-clip): a PNG renamed .mp4 is 422 `unreadable_video`, no run.
- `exif-orientation` (tierc-ignores-exif-orientation): a JPEG cutdown of a take with an EXIF Orientation 6 tag put in
  front becomes an upright Tier C packet.
- `declared-cost-bound` (declared-import-cost-unbounded): a declared import cost of 1e308, 1000.01 or 1e6 USD is 422
  `bad_cost`, Infinity is 422, no run; 1000 USD is taken.
- `kit-camera-containment` (kit-camera-name-escapes-folder): `../escape`, its Windows-separator form and `a/b` are
  422 `bad_camera` with a camera JSON waiting outside the kit folder, and no packet is built.
- `cli-envelope-edges` (cli-envelope-edges): `--new-envelope` with `--hours inf`, `--hours 1e12` or `--usd inf` exits
  2 with one line; `plate pack --max-blob-mb=nan` and `plate gc --min-age-hours=inf` exit 4 (usage); `plate export
  --to <a file>` exits 4 naming the path and leaves the file alone; POST /envelopes with Infinity is 422.
