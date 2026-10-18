"""previs.check_unlocked returns the still on the Motion node's image port, not whichever edge comes first.

Since T81 a Plate packet edge or a Reference elements edge may also feed a Motion node; before this fix a
packet edge listed first made the Plate come back as "the approved still".
"""

from __future__ import annotations

from plate_daemon.motion import previs


def _graph(packet_first: bool) -> dict:
    nodes = [
        {"id": "p", "kind": "Plate", "packetId": "pkt_a", "tier": "A"},
        {"id": "e", "kind": "Edit", "gateStatus": "pass", "approved": True},
        {"id": "m", "kind": "Motion"},
    ]
    still = {"id": "e->m", "source": "e", "target": "m", "sourceHandle": "image", "targetHandle": "image"}
    packet = {"id": "p->m", "source": "p", "target": "m", "sourceHandle": "packet", "targetHandle": "packet"}
    edges = [{"id": "p->e", "source": "p", "target": "e", "sourceHandle": "image", "targetHandle": "image"}]
    edges += [packet, still] if packet_first else [still, packet]
    return {"nodes": nodes, "edges": edges}


def test_the_image_port_still_wins_over_an_earlier_packet_edge():
    assert previs.check_unlocked(_graph(packet_first=True), "m") == "e"
    assert previs.check_unlocked(_graph(packet_first=False), "m") == "e"
