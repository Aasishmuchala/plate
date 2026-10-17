"""PLATE pixels service: the localhost CV worker (depth, calibration, segmentation, flow, text-lock,
upscale, LPIPS) behind a commercial-safe licence registry.

Seeded from ``~/Desktop/maxdirector/cv_sidecar`` (stdlib HTTP server + backends with a STUB fallback). Stub
mode is the default and imports no ``torch``; the real models arrive later through the ``cuda`` extra on
the render box only (see ``pixels/requirements-cuda.txt`` and ``docs/pixels.md``).
"""

__version__ = "0.1.0"
