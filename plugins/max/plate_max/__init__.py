"""plate_max: the PLATE sender inside 3ds Max 2026 (Python 3.11, standard library only).

It reads the camera and scene through pymxs (behind the lazy runtime in rt.py), derives the ID
legend and the AI-edit policy, writes a Shot Packet folder (spec/PACKET.md) and tells the daemon
where it is over loopback HTTP. Seeded from the MaxDirector and MaxGaffer bridges (lazy runtime
getter, stdlib config in %LOCALAPPDATA%, startup-script install). Nothing here needs Max to import.
"""

from __future__ import annotations

__version__ = "0.1.0"
PLUGIN_VERSION = __version__

# Value of the X-Plate-Packet-Version header and of manifest.json "packet_version". Its
# major.minor must appear in the daemon's /api/v0/health "packet_versions".
PACKET_VERSION = "0.1.1"
CONVENTION_ID = "plate-cam-v0"

SENDER = {"name": "plate_max", "version": PLUGIN_VERSION}
