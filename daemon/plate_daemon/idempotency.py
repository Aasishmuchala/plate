"""Idempotency keys for provider submits (BUILD_PLAN section 4: "no resubmission without polling").

The key is BLAKE3 hex of canonical JSON (`sort_keys=True`, `separators=(",", ":")`,
`ensure_ascii=False`, UTF-8) of `{"inputs", "params", "route", "attempt"}`. The same run content gives the
same key regardless of dict ordering; a human re-run bumps `attempt` and therefore the key, so a provider
that dedupes on the key can tell a deliberate retry from a network replay.
"""

from __future__ import annotations

import json
from typing import Any

from blake3 import blake3


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def idempotency_key(inputs: dict, params: dict, route: dict, attempt: int) -> str:
    doc = {"inputs": inputs, "params": params, "route": route, "attempt": attempt}
    return blake3(canonical_json(doc)).hexdigest()
