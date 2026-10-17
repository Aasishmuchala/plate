"""plate_max.bridge: the side-effecting half of the sender, every call through rt.runtime().

  scene     get_prop/set_prop over candidate-name tuples (MaxGaffer), at_time(frame), camera lookup
  elements  the default V-Ray render-element set and the per-shot opt-ins, reported, never raised
  render    one packet frame: V-Ray multilayer EXR plus the VFB-corrected 16-bit TIFF
  colour    detect_colour_pipeline(): OCIO, legacy gamma or unknown (unknown blocks Send)
  hashing   BLAKE3 from the loopback daemon (decision 5b) for files, digests and sidecars
  vantage   .vrscene export with explicit frames and the live-link toggle (no CLI batch)
  safety    backup(), one_undo(), Journal (MaxDirector's safety trio)

Every V-Ray, Vantage and Max property or command name these modules use is a hypothesis until the
on-box checklist (plugins/max/ONBOX.md) has run; each one is marked TODO-verify where it is defined.
The package imports nothing at import time so plate_max.digest can use bridge.scene without a cycle.
"""

from __future__ import annotations

__all__ = ["colour", "elements", "hashing", "render", "safety", "scene", "vantage"]
