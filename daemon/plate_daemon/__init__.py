"""plate_daemon: PLATE's local job daemon (SQLite WAL index, content-addressed store, run state machine).

New code for the PLATE monorepo (BUILD_PLAN section 4 "Daemon", task T3). It ingests Shot Packets by
reference over the loopback HTTP contract in spec/PACKET.md section 9, quotes and approves runs, drives
them through the job state machine against an engine adapter (only the scripted FakeEngine today) and
recovers from a crash without ever resubmitting blind. The package root stays import-light: FastAPI and
uvicorn load only when `plate_daemon.api` or `python -m plate_daemon` is used.
"""

__version__ = "0.1.0"
