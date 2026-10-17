"""Node execution beyond the generator (T48): the numeric nodes Set, Recolour and LightMix, and the plugin nodes
Light and Scout. (The People node over STAFFAGE, T45, lives in `motion/`.)

None of these calls an image model. The numeric three run in this process on the packet's own passes, at custody
C0 and a quoted price of 0 USD; the plugin two hand the work to the Max dock through a job queue and wait for its
answer. All five walk the same run lifecycle as an Edit (QUOTED, APPROVED, QUEUED, ... GATING, DONE or
GATE_FAIL), so budgets, the ledger, SSE and gate reports treat them like any other node.

    set_node.py        hero targets, per-view numeric correction, the cross-view p90 <= 3 gate (T30)
    recolour_node.py   one material to a RAL / hex / Lab target by element recomposition (T31)
    lightmix_node.py   per-light gains by NNLS on light selects, recomposed to display (T32)
    light_node.py      `light.match` job for MaxGaffer; done when a superseding packet arrives
    scout_node.py      `scout` job: candidate cameras from Max
    plugin_queue.py    the `plugin_queue` table the dock polls (`GET /plugin/jobs`)
    runner.py          NodeRunner (quote, approve, make, advance, preview) and `install(pipeline)`
    routes.py          `/plugin/jobs...` and `/projects/{id}/nodes/{node}/preview` (an APIRouter)

Wiring into the daemon (the two files outside this package): `api.create_app` calls `nodes.install(pipeline)`
right after building the Pipeline (so quote, approve, resume and cancel handle node runs), and
`routes/numeric.py` is `from ..nodes.routes import router` (so `include_all` mounts the routes). Until then
`NodeRunner(pipeline)` drives the same runs directly.
"""

from .common import NUMERIC_ENGINE_ID, NUMERIC_ROUTE, PLUGIN_ENGINE_ID, PLUGIN_ROUTE
from .plugin_queue import ACTIONS, Job, PluginQueue
from .runner import KINDS, NUMERIC_KINDS, PLUGIN_KINDS, NodeRunner, install, is_node_run

__all__ = [
    "ACTIONS",
    "KINDS",
    "NUMERIC_ENGINE_ID",
    "NUMERIC_KINDS",
    "NUMERIC_ROUTE",
    "PLUGIN_ENGINE_ID",
    "PLUGIN_KINDS",
    "PLUGIN_ROUTE",
    "Job",
    "NodeRunner",
    "PluginQueue",
    "install",
    "is_node_run",
]
