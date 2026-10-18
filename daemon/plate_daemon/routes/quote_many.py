"""`POST /projects/{id}/nodes/{node}/quote_many`: one node quoted on several engines side by side (router plan T95).

Body: `{engine_ids: [2 to 5 distinct engine ids], ...}` and every `NodeQuoteBody` field but `engine_id`
(`preset`, `region_crop`, `params`, `prompt`, `recipe`, `task`, ...), the same for each engine. The node is
quoted once per engine, exactly as `POST /projects/{id}/nodes/{node}/quote` quotes it (`routes.projects.
quote_node`), then the runs are stamped as one best-of-N group: one new `sibling_group`, `sibling_index` 0 to
k - 1 in the order given, each run keeping its own engine's price. That is what `POST /runs/{id}/fanout` does
for copies of one quote, across engines instead; the rest is the existing group machinery. Approving the
primary (index 0) approves the whole group against the sum of the prices, and `Pipeline._rank_group` ranks the
takes across engines when the last one ends. Publishes `siblings.created`.

All or nothing: if any engine is refused (custody, a missing prompt, no price), the runs already quoted for the
earlier engines are cancelled and the refusal is answered with the engine it came from added as `engine_id`;
no half group is left for the artist to find in the history.

Answers `{sibling_group, runs: [run views], total_usd}`; `total_usd` is the sum of the quotes, the amount
approving the group commits. 404 unknown project or node; 422 fewer than 2 or more than 5 engines, a repeated
engine id, or `engine_id` (use `engine_ids`).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import Field, model_validator

from ..idempotency import new_id
from ..machine import annotate, get_run
from ..states import RunState
from . import API, ApiError, Ctx, ctx, require_token
from .projects import NodeQuoteBody, _project
from .projects import quote_node as quote_node_route
from .runs import run_view

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

log = logging.getLogger("plate_daemon")

MIN_ENGINES = 2
MAX_ENGINES = 5


class QuoteManyBody(NodeQuoteBody):
    """The `NodeQuoteBody` fields (so one added there is accepted here) with `engine_ids` for `engine_id`."""

    engine_ids: list[str] = Field(min_length=MIN_ENGINES, max_length=MAX_ENGINES)

    @model_validator(mode="after")
    def _engine_ids_only(self) -> QuoteManyBody:
        if self.engine_id is not None:
            raise ValueError(
                "send engine_ids, not engine_id: quote_many quotes the node on each engine named"
            )
        if any(not e.strip() for e in self.engine_ids):
            raise ValueError("engine_ids must be engine ids")
        return self


def _abandon(c: Ctx, project_id: str, run_ids: list[str]) -> None:
    """Cancel the QUOTED runs of a group that did not come to be; a failure here is logged, not raised, so
    the refusal that started it is what the caller reads."""
    with c.index.project(project_id) as conn:
        for rid in run_ids:
            try:
                c.pipeline.cancel(
                    conn, project_id, rid, actor="system", reason="quote_many: another engine was refused"
                )
            except Exception as e:
                log.warning("quote_many: run %s was not cancelled: %s", rid, e)


@router.post("/projects/{project_id}/nodes/{node_id}/quote_many")
def quote_many_route(request: Request, project_id: str, node_id: str, body: QuoteManyBody) -> dict[str, Any]:
    c = ctx(request)
    _project(request, project_id)
    if len(set(body.engine_ids)) != len(body.engine_ids):
        raise ApiError(422, {"errors": [f"engine_ids must be distinct, got {body.engine_ids}"]})
    common = body.model_dump(exclude={"engine_ids", "engine_id"})
    made: list[str] = []
    for engine_id in body.engine_ids:
        try:
            quoted = quote_node_route(
                request, project_id, node_id, NodeQuoteBody(**common, engine_id=engine_id)
            )
        except ApiError as e:
            _abandon(c, project_id, made)
            raise ApiError(e.status, {**e.body, "engine_id": engine_id}) from e
        made.append(quoted["run"]["id"])

    group = new_id("grp", c.now())
    with c.index.project(project_id) as conn:
        for i, run_id in enumerate(made):
            run = get_run(conn, run_id)
            terms = run.quote.terms_json()  # this engine's own price and expiry, plus the group
            terms.update({"sibling_group": group, "sibling_index": i, "siblings": len(made)})
            annotate(conn, run_id, expect=RunState.QUOTED, quote_json=terms)
        runs = [run_view(conn, run_id) for run_id in made]
    c.pipeline.publish(project_id, made[0], {"kind": "siblings.created", "group": group, "runs": made})
    return {
        "sibling_group": group,
        "runs": runs,
        "total_usd": round(sum(float(r.get("cost_usd_quoted") or 0.0) for r in runs), 6),
    }
