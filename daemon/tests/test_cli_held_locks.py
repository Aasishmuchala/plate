"""The CLI holds an edge the daemon stored with `locked: true` (G2) instead of refusing the whole graph."""

from __future__ import annotations

from plate_daemon import canvas_rules
from plate_daemon.cli import drop_held_locks


def _graph(locked: bool) -> dict:
    edge = {"id": "e->m", "source": "e", "target": "m"}
    if locked:
        edge["locked"] = True
    return {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": "pkt_a", "tier": "A"},
            {"id": "e", "kind": "Edit", "gateStatus": "none", "approved": False},
            {"id": "m", "kind": "Motion"},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}, edge],
    }


def test_a_server_held_lock_is_not_a_refusal_but_an_unflagged_one_is():
    held = _graph(locked=True)
    assert [v["code"] for v in canvas_rules.validate_graph(held)] == ["VIDEO_LOCKED"]
    assert drop_held_locks(held, canvas_rules.validate_graph(held)) == []
    fresh = _graph(locked=False)
    assert [v["code"] for v in drop_held_locks(fresh, canvas_rules.validate_graph(fresh))] == ["VIDEO_LOCKED"]
