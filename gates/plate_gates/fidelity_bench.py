"""Compatibility shim: `python -m plate_gates.fidelity_bench selftest` (the Makefile and CI selftest entry).

fidelity_bench.py was the single-file ancestor of the gate core (~/Desktop/test/vizmaker-dossier/tools/);
every subcommand it had now lives in cli.py, and this module simply forwards to it.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
