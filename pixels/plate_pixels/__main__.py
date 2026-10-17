"""``python -m plate_pixels`` entry point: starts the pixels service exactly like
``python -m plate_pixels.server`` (127.0.0.1:4611, stub mode by default). Seeded from the
cv_sidecar ``server.main`` launcher.
"""

from .server import main

if __name__ == "__main__":
    main()
