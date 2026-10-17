"""Idempotency keys for provider submits, and ULID ids (BUILD_PLAN section 4: "no resubmission without polling").

The key is BLAKE3 hex of canonical JSON (`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`,
`allow_nan=False`, UTF-8) of `{"input_hashes", "params", "route_id", "attempt"}`, plus `"sibling_index"` for a
best-of-N sibling (siblings share inputs, params, route and attempt, so without it n siblings would collapse
into one provider job). Inputs enter by hash only (`input_hashes`): a CAS key stays as it is, any other value
is replaced by the BLAKE3 of its canonical JSON, so the key never carries prompt text. The same run content
gives the same key regardless of dict ordering; a human re-run bumps `attempt` and therefore the key, so a
provider that dedupes on the key can tell a deliberate retry from a network replay.

`new_id(prefix)` is `<prefix>_<ULID>`: 26 Crockford base32 characters (48-bit millisecond time, 80 random
bits), so ids sort by creation time and are safe in Windows file names.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from blake3 import blake3

from .timeutil import utcnow

CAS_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )


def ulid(now: datetime | None = None) -> str:
    ms = int((now or utcnow()).timestamp() * 1000) & ((1 << 48) - 1)
    n = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def new_id(prefix: str, now: datetime | None = None) -> str:
    return f"{prefix}_{ulid(now)}"


def value_hash(value: Any) -> str:
    """A CAS key is its own hash; anything else hashes as canonical JSON."""
    if isinstance(value, str) and CAS_KEY_RE.match(value):
        return value
    return blake3(canonical_json(value)).hexdigest()


def input_hashes(inputs: Mapping[str, Any]) -> dict[str, str]:
    return {str(k): value_hash(v) for k, v in inputs.items()}


def route_id(route: Mapping[str, Any]) -> str:
    """`<engine_id>/<route_id or provider>/<region>`: one provider endpoint for one model."""
    via = route.get("route_id") or route.get("provider") or ""
    return f"{route.get('engine_id') or ''}/{via}/{route.get('region') or ''}"


def idempotency_key(
    input_hashes: Mapping[str, str],
    params: Mapping[str, Any],
    route_id: str,
    attempt: int,
    *,
    sibling_index: int | None = None,
) -> str:
    doc: dict[str, Any] = {
        "input_hashes": dict(input_hashes),
        "params": dict(params),
        "route_id": route_id,
        "attempt": attempt,
    }
    if sibling_index is not None:
        doc["sibling_index"] = sibling_index
    return blake3(canonical_json(doc)).hexdigest()
