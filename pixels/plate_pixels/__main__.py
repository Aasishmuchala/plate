"""``python -m plate_pixels`` entry point: starts the pixels worker exactly like
``python -m plate_pixels.server`` (127.0.0.1:4611, stub mode by default, bearer token shared with the daemon).
"""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
