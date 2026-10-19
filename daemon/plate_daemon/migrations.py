"""Numbered SQL migrations for a project index (`<home>/index/<project_id>.sqlite`).

Each migration is a list of single statements (not a script) so `db.migrate` can apply it inside one
explicit `BEGIN IMMEDIATE` transaction. `run_events` and `packet_events` are append-only: triggers abort
any UPDATE or DELETE. A CHECK keeps the "SUBMITTED and later carry a provider_job_id" invariant at the
storage layer too, so no code path can write a submitted run that recovery could not re-poll.

M002 (router plan T5) adds what the project-folder cache needs: `assets` (one row per CAS object),
`events` (the SSE bus log that `Last-Event-ID` replays), `project_meta` (the project's folder root and
custody, so a project on the NAS is found by id), `runs.node_id` / `runs.quote_json`, a unique
`idempotency_key`, `packets.view_hash`, and read-only aliases `runs.run_id` (= `id`) and
`packets.manifest_hash` (= `manifest_blake3`) under the names the plan uses. Applied migrations are never
edited: a change is a new numbered entry.

M003 (Wave 12, T134) adds `ledger_outbox`: records the project ledger could not take while its file was
unwritable or its lock was held, parked in order until `ledger.drain_outbox` appends them (Decision 73).

M004 (Wave 13, T152) adds `packet_router`: the render-it router's inputs for a packet (`detail_resolvability`'s
answer as JSON), computed once at ingest and read by every quote (Decision 84). `version` is the pipeline's
`ROUTER_SUMMARY_VERSION`; a row of another version is recomputed and replaced. It is a cache: a rebuild from the
folder leaves it empty and the next quote refills it.
"""

from __future__ import annotations

from .states import PROVIDER_STATES, RunState

_STATES = ", ".join(f"'{s.value}'" for s in RunState)
_PROVIDER_STATES = ", ".join(f"'{s.value}'" for s in sorted(PROVIDER_STATES, key=lambda s: s.value))
_PRIORITIES = "'client_deadline', 'interactive', 'agent_batch', 'calibration'"


def _append_only(table: str) -> list[str]:
    return [
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
    ]


M001 = [
    """CREATE TABLE packets (
        packet_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        shot_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        tier TEXT NOT NULL,
        packet_version TEXT NOT NULL,
        scene_hash TEXT,
        path TEXT NOT NULL,
        manifest_blake3 TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ingesting', 'ingested', 'failed')),
        sender_name TEXT,
        sender_version TEXT,
        received_utc TEXT NOT NULL,
        ingested_utc TEXT,
        error TEXT
    )""",
    """CREATE TABLE packet_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        packet_id TEXT NOT NULL REFERENCES packets(packet_id),
        seq INTEGER NOT NULL,
        type TEXT NOT NULL,
        data TEXT NOT NULL,
        at_utc TEXT NOT NULL,
        UNIQUE (packet_id, seq)
    )""",
    *_append_only("packet_events"),
    """CREATE TABLE blobs (
        packet_id TEXT NOT NULL REFERENCES packets(packet_id),
        relpath TEXT NOT NULL,
        role TEXT NOT NULL,
        size INTEGER NOT NULL,
        blake3 TEXT NOT NULL,
        in_cas INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (packet_id, relpath)
    )""",
    """CREATE TABLE budget_envelopes (
        id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        max_total_usd REAL NOT NULL CHECK (max_total_usd >= 0),
        max_per_run_usd REAL NOT NULL CHECK (max_per_run_usd >= 0),
        spent_usd REAL NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
        created_utc TEXT NOT NULL,
        expires_utc TEXT NOT NULL
    )""",
    f"""CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        packet_id TEXT NOT NULL REFERENCES packets(packet_id),
        state TEXT NOT NULL CHECK (state IN ({_STATES})),
        priority TEXT NOT NULL CHECK (priority IN ({_PRIORITIES})),
        inputs TEXT NOT NULL,
        params TEXT NOT NULL,
        route TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK (attempt >= 1),
        idempotency_key TEXT,
        payload_key TEXT,
        submit_started_utc TEXT,
        provider_job_id TEXT,
        submitted_utc TEXT,
        result_url TEXT,
        quote_id TEXT REFERENCES quotes(id),
        result_key TEXT,
        normalized TEXT,
        gate_report TEXT,
        reason_code TEXT,
        created_utc TEXT NOT NULL,
        updated_utc TEXT NOT NULL,
        CHECK (state NOT IN ({_PROVIDER_STATES}) OR (provider_job_id IS NOT NULL AND provider_job_id != ''))
    )""",
    "CREATE INDEX runs_state ON runs(state)",
    """CREATE TABLE quotes (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        region TEXT NOT NULL,
        custody_grade TEXT NOT NULL CHECK (custody_grade IN ('C0', 'C1', 'C2', 'D')),
        price_usd REAL NOT NULL CHECK (price_usd >= 0),
        eta_s REAL NOT NULL CHECK (eta_s >= 0),
        created_utc TEXT NOT NULL,
        expires_utc TEXT NOT NULL,
        envelope_id TEXT REFERENCES budget_envelopes(id),
        approved_by TEXT,
        approved_utc TEXT
    )""",
    """CREATE TABLE run_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(id),
        seq INTEGER NOT NULL,
        kind TEXT NOT NULL DEFAULT 'transition',
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT,
        at_utc TEXT NOT NULL,
        UNIQUE (run_id, seq)
    )""",
    *_append_only("run_events"),
]

M002 = [
    """CREATE TABLE assets (
        hash TEXT PRIMARY KEY CHECK (length(hash) = 64),
        ext TEXT NOT NULL,
        bytes INTEGER NOT NULL CHECK (bytes >= 0),
        phash TEXT,
        added_utc TEXT NOT NULL
    )""",
    """CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        json TEXT NOT NULL
    )""",
    "CREATE INDEX events_run ON events(run_id, seq)",
    """CREATE TABLE project_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    "ALTER TABLE runs ADD COLUMN node_id TEXT",
    "ALTER TABLE runs ADD COLUMN quote_json TEXT",
    "ALTER TABLE runs ADD COLUMN run_id TEXT GENERATED ALWAYS AS (id) VIRTUAL",
    "CREATE INDEX runs_node ON runs(node_id)",
    "CREATE UNIQUE INDEX runs_idempotency_key ON runs(idempotency_key) WHERE idempotency_key IS NOT NULL",
    "ALTER TABLE packets ADD COLUMN view_hash TEXT",
    "ALTER TABLE packets ADD COLUMN manifest_hash TEXT GENERATED ALWAYS AS (manifest_blake3) VIRTUAL",
]

M003 = [
    """CREATE TABLE ledger_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,
        kind TEXT NOT NULL,
        record TEXT NOT NULL,
        created_utc TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT
    )""",
]

M004 = [
    """CREATE TABLE packet_router (
        packet_id TEXT PRIMARY KEY REFERENCES packets(packet_id),
        version TEXT NOT NULL,
        json TEXT NOT NULL,
        computed_utc TEXT NOT NULL
    )""",
]

MIGRATIONS: list[tuple[int, list[str]]] = [(1, M001), (2, M002), (3, M003), (4, M004)]
