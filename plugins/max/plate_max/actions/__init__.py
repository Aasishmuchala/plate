"""plate_max.actions: what the dock's buttons do, each one a run() over the bridge (all pymxs goes
through rt.runtime()) that reports per camera and never raises across cameras.

  send_packet    render each camera into a Shot Packet (EXR + TIFF16 + legend + manifest) and post it
  batch_cameras  send_packet per recipe with progress, plus a batch_<ts>.json summary
  receive        load a returned image into the V-Ray VFB history or the viewport background

Contract with the dock (plate_max/ui/controller.py): run(subjects, out_root, daemon_base, options)
-> list of results with camera, ok, packet_id, errors; options is a dict (or attribute-readable dict)
with recipes {camera: recipe}, progress(done, total, message) and backup. Nothing is imported at
package import time, so the dock can probe for each module on its own.
"""

from __future__ import annotations

__all__ = ["batch_cameras", "receive", "send_packet"]
