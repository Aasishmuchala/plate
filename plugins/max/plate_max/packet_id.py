"""Packet ids: pkt_ + a 26-character Crockford base32 ULID (spec/PACKET.md section 2).

Time-sortable (48-bit millisecond timestamp, then 80 random bits) and opaque: no client, project
or file name goes into it, so it can appear in vendor-side ids and logs. Stdlib only.
"""

from __future__ import annotations

import os
import re
import time

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PACKET_ID_RE = re.compile(r"^pkt_[0-9A-HJKMNP-TV-Z]{26}$")


def new_packet_id(now_ms: int | None = None, entropy: bytes | None = None) -> str:
    """now_ms and entropy (10 bytes) are injectable for tests; defaults are the clock and os.urandom."""
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if not 0 <= ts < 1 << 48:
        raise ValueError("timestamp out of range for a ULID")
    raw = os.urandom(10) if entropy is None else entropy
    if len(raw) != 10:
        raise ValueError("entropy must be 10 bytes")
    n = (ts << 80) | int.from_bytes(raw, "big")
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD[n & 31])
        n >>= 5
    return "pkt_" + "".join(reversed(chars))
