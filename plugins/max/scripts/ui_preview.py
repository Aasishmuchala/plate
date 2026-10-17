"""Offscreen preview of the PLATE dock: renders it to plugins/max/dock_preview.png.

Design changes are judged on this image, not on adjectives. It runs anywhere the dev venv has PySide6
(no 3ds Max, no daemon: the dock is driven by a FakeController):

    .venv/bin/python plugins/max/scripts/ui_preview.py [output.png]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PLUGIN = Path(__file__).resolve().parent.parent
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

DEFAULT_OUT = PLUGIN / "dock_preview.png"
SIZE = (600, 1260)


def build_demo_dock():
    """A dock in a lived-in state: compatible daemon, three cameras, one recipe edited, a passing and
    a failing lint, a send half done. Returns (dock, controller)."""
    from PySide6 import QtWidgets

    from plate_max.ui import controller as ctl
    from plate_max.ui import dock as dockmod

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    fake = ctl.FakeController(
        cameras=["CAM_hero_terrace", "CAM_lobby", "CAM_podium_dusk"],
        capabilities=(ctl.CAP_SEND_PACKET, ctl.CAP_BATCH, ctl.CAP_RECEIVE),
    )
    fake.lint_reports["CAM_lobby"] = {
        "passed": False,
        "unlabelled_share": 0.081,
        "problems": [
            {
                "code": "UNLABELLED_SHARE",
                "node": None,
                "detail": "unlabelled IDs cover 8.1% of the frame, over the 5.0% limit: "
                "put those nodes on a labelled layer (VEG_, ARCH_, ...) before sending",
            },
            {"code": "HIDDEN_CAMERA", "node": "CAM_lobby", "detail": "camera CAM_lobby is hidden: unhide it"},
        ],
    }
    fake.set_recipe(
        "CAM_hero_terrace",
        {
            "engine_id": "nano-banana-2.1",
            "preset": "night-relight",
            "safe_frame": {"x": 0, "y": 270, "w": 3840, "h": 1620},
            "overscan_aspect": "16:9",
            "editable_classes": ["vegetation", "sky", "water"],
        },
    )
    fake.calls.clear()
    dock = dockmod.PlateDock(fake)
    dock.resize(*SIZE)
    dock.cam_list.setCurrentRow(0)  # first: it replaces the selection
    for row in (0, 1):
        dock.cam_list.item(row).setSelected(True)
    dock.run_lint()
    for line in (
        "Send Packet: CAM_hero_terrace, CAM_lobby",
        "sent CAM_hero_terrace",
        "hashing 6 files through the daemon",
    ):
        dock._log(line)
    dock._on_progress(2, 5, "")
    return dock, fake


def render(out: Path | str | None = None) -> Path:
    """Render the demo dock to a PNG (default plugins/max/dock_preview.png) and return the path."""
    from PySide6 import QtWidgets

    from plate_max.ui import dock as dockmod

    target = Path(out) if out else DEFAULT_OUT
    dock, _fake = build_demo_dock()
    try:
        dock.show()
        QtWidgets.QApplication.processEvents()
        target.parent.mkdir(parents=True, exist_ok=True)
        if not dock.grab().save(str(target), "PNG"):
            raise OSError(f"could not write {target}")
    finally:
        dock.close()
        dock.deleteLater()
        dockmod.close_dock()
    return target


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = render(args[0] if args else None)
    print(f"dock preview -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
