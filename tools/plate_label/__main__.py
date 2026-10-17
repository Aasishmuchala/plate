"""`python -m plate_label ...` entry point: hands argv to plate_label.cli.main."""

from __future__ import annotations

import sys

from plate_label.cli import main

if __name__ == "__main__":
    sys.exit(main())
