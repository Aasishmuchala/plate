"""Maintenance tooling the daemon runs: backup, purge, reindex, budget alerts and vendor-side deletion
tracking (router plan T42; BUILD_PLAN decision 23).

    backup.py   plan / run / restore_drill: project folder -> verified, hash-manifested copy; the quarterly drill
    purge.py    end_of_project / gc (reference-counted CAS purge), vendor_deletions / record_vendor_deletion
    reindex.py  rebuild the per-machine SQLite index from the folder with a consistency report
    alerts.py   scan the ledger for the daily and unattended spend caps -> alerts.jsonl and a status-bar line
    common.py   shared helpers (staged hashing copy, retrying delete, safe relative paths)

`python -m plate_daemon.tools --help` runs them without a daemon. Submodules are imported on use, not here.
"""
