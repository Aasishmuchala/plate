"""The PLATE dock: Qt widgets (dock.py) over a plain-Python Controller (controller.py).

This package is the only place in plate_max that may import qtmax or PySide6, and nothing outside it
imports it at module level: bootstrap.launch() imports it inside the call, so a missing Qt can never
break the headless modules (digest, packet, transport, policy). Importing the package itself pulls in no Qt.
"""
