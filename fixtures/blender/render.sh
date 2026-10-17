#!/usr/bin/env bash
# Render the fixture room in Blender (headless) and repack the shots into fixtures/packets/.
#   bash fixtures/blender/render.sh [--force] [--skip-render]
# Per variant this is:  Blender -b -P fixtures/blender/scene.py -- <work_dir> --variant <v>
# then:                 .venv/bin/python fixtures/blender/build_packets.py <work_dir> fixtures/packets
# (render.py does both and skips a variant whose stamp still matches). Set BLENDER to use another install.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
exec .venv/bin/python fixtures/blender/render.py "$@"
