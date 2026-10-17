"""plate_gates: PLATE's numeric gates (colour, structure, motion, texture, alignment, compositing).

Consolidated from the one-off project scripts under ~/Desktop/test (fidelity_bench, labmatch, the
motion/texture/alignment gates). Every gate is a pure function over images or clips; the CLI and
the daemon call the same functions. This package root stays import-light on purpose (no numpy or
cv2 import here) so submodules can be imported independently.
"""

__version__ = "0.1.0"
