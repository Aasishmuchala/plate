"""The numeric and plugin node routes (router plan T48, mounted by T73): `routes.include_all` mounts the
`router` of every module in this package, and the node routes live with the nodes in `nodes/routes.py`, so
this module only re-exports that router.

    GET  /plugin/jobs, POST /plugin/jobs, GET /plugin/jobs/{id}, POST /plugin/jobs/{id}/result
                       the Max dock's job queue (Light, Scout)
    POST /projects/{id}/nodes/{node}/preview {what, params}
                       numbers for the node editors: set, lightmix, recolour, and router (the Motion editor's
                       router card, registered by `api.create_app`)
"""

from __future__ import annotations

from ..nodes.routes import router

__all__ = ["router"]
