"""Send + quote (decision 53): Send Packet, then POST /api/v0/projects/{pid}/templates/apply for every
packet the daemon ingested.

The controller half runs LiveController against a loopback http.server standing in for the daemon (the
apply route and the packet event log, the way test_ui_dock.py's stub serves health and projects); the
send action is a recorder (test_send_packet.py covers the real one). It proves the apply body (the Edit
chain template, the packet id, the recipe's engine and preset, quote on, auto_approve as ticked), the
wait for a packet still ingesting, and the row the dock shows: "quoted <usd> USD · waiting for
approval" or "approved · running". The dock half clicks the button offscreen over the FakeController.
"""

from __future__ import annotations

import json
import os
import threading
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # before anything imports Qt

from plate_max import PLUGIN_VERSION  # noqa: E402
from plate_max.packet_id import new_packet_id  # noqa: E402
from plate_max.ui import controller as ctl  # noqa: E402

TOKEN = "c0ffeec0ffeec0ffeec0ffeec0ffeec0"
QUOTED = "quoted 0.039 USD · waiting for approval"


def apply_answer(state: str = "QUOTED", usd: float = 0.039, **extra: Any) -> dict[str, Any]:
    """What POST templates/apply answers for one packet's chain (routes/templates.py's shape)."""
    run = {"id": "run_01", "state": state, "cost_usd_quoted": usd, "node_id": "pkt_x-edit"}
    return {
        "nodes_added": 5,
        "edits": ["pkt_x-edit"],
        "runs": [run],
        "total_usd": usd,
        "refused": [],
        "awaiting_approval": [],
        "added": [],
        "set_node": None,
        "graph": {"nodes": [], "edges": []},
        **extra,
    }


@pytest.fixture
def daemon(tmp_path):
    """The loopback daemon: POST .../templates/apply answers state["answer"] with state["status"] and
    records the path, body and bearer; GET .../events serves state["events"] as SSE. config.json (the
    file conftest points PLATE_CONFIG at) is told to use it."""
    state: dict[str, Any] = {
        "applied": [],
        "auth": [],
        "status": 200,
        "answer": apply_answer(),
        "events": [{"seq": 1, "type": "ingest.done", "data": {}}],
        "event_requests": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: Any) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/api/v0/health":
                return self._send(
                    200, {"ok": True, "daemon_version": PLUGIN_VERSION, "packet_versions": ["0.1"]}
                )
            if url.path.startswith("/api/v0/packets/") and url.path.endswith("/events"):
                state["event_requests"].append(self.path)
                frames = "".join(f"id: {e['seq']}\ndata: {json.dumps(e)}\n\n" for e in state["events"])
                raw = ("retry: 1000\n\n" + frames).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self._send(404, {"error": "not_found"})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path.startswith("/api/v0/projects/") and self.path.endswith("/templates/apply"):
                state["auth"].append(self.headers.get("Authorization"))
                state["applied"].append({"path": self.path, "body": body})
                return self._send(state["status"], state["answer"])
            self._send(404, {"error": "not_found"})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    config = Path(os.environ["PLATE_CONFIG"])
    config.parent.mkdir(parents=True, exist_ok=True)
    payload = {"daemon_url": url, "token": TOKEN, "packets_dir": str(tmp_path / "packets")}
    config.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    state["url"] = url
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def live_controller(monkeypatch, results: list[dict[str, Any]], project: str = "tula-wot"):
    """A LiveController whose send action answers results (and records the call)."""
    seen: dict[str, Any] = {}

    def send_run(cameras, out_root, daemon_base, options):
        seen["send"] = (list(cameras), options)
        return [dict(r) for r in results]

    monkeypatch.setattr(ctl.LiveController, "capabilities", lambda self: {ctl.CAP_SEND_PACKET})
    monkeypatch.setattr(ctl.LiveController, "_action", lambda self, cap: types.SimpleNamespace(run=send_run))
    live = ctl.LiveController()
    live.set_project(project)
    live.set_recipe("CAM_a", {"engine_id": "gpt-image-2.5", "preset": "night-relight"})
    return live, seen


def sent(packet_id: str, ingest: str = "ingested", camera: str = "CAM_a", **extra: Any) -> dict[str, Any]:
    return {
        "camera": camera,
        "ok": True,
        "packet_id": packet_id,
        "errors": [],
        "status_code": 202,
        "ingest": ingest,
        "events_url": f"/api/v0/packets/{packet_id}/events",
        **extra,
    }


def progress(done, total, message):
    pass


# --------------------------------------------------------------------------- the pure pieces


def test_the_apply_body_is_the_edit_chain_with_the_recipe():
    rec = {"engine_id": "gpt-image-2.5", "preset": "night-relight"}
    assert ctl.apply_body("pkt_1", rec) == {
        "template_id": "edit_chain",
        "packet_ids": ["pkt_1"],
        "engine_id": "gpt-image-2.5",
        "recipe": "night-relight",
        "quote": True,
        "auto_approve": False,
    }
    bare = ctl.apply_body("pkt_1", {"engine_id": "", "preset": " "}, auto_approve=True)
    assert bare == {"template_id": "edit_chain", "packet_ids": ["pkt_1"], "quote": True, "auto_approve": True}


@pytest.mark.parametrize(
    "answer, auto, line, err",
    [
        (apply_answer(), False, QUOTED, False),
        (apply_answer(usd=1.5), False, "quoted 1.50 USD · waiting for approval", False),
        (apply_answer(state="APPROVED"), True, "approved · running", False),
        (apply_answer(state="QUEUED"), True, "approved · running", False),
        (
            apply_answer(awaiting_approval=[{"run_id": "run_01", "error": "auto_cap", "usd": 0.039}]),
            True,
            "quoted 0.039 USD · waiting for approval (auto-approve refused: auto_cap)",
            False,
        ),
        (apply_answer(state="APPROVED"), False, QUOTED, False),  # nobody asked for an approval
        (
            {"runs": [], "total_usd": 0, "refused": [{"node": "e", "error": "custody", "detail": "D route"}]},
            False,
            "not quoted: custody (D route)",
            True,
        ),
    ],
)
def test_the_quote_row_says_what_the_daemon_did(answer, auto, line, err):
    summary = ctl.quote_summary(answer, auto)
    assert summary["line"] == line and summary["error"] is err


def test_usd_keeps_a_third_decimal_only_when_it_says_something():
    assert [ctl.usd_text(v) for v in (0.039, 0.04, 1.5, 0, None, 12.345)] == [
        "0.039",
        "0.04",
        "1.50",
        "0.00",
        "0.00",
        "12.345",
    ]


# --------------------------------------------------------------------------- LiveController


def test_send_and_quote_posts_the_apply_body_with_the_packet_id(monkeypatch, daemon):
    pid = new_packet_id()
    live, seen = live_controller(monkeypatch, [sent(pid)])
    [row] = live.send_and_quote(["CAM_a"], progress)
    assert seen["send"][0] == ["CAM_a"] and seen["send"][1]["project_id"] == "tula-wot"
    [applied] = daemon["applied"]
    assert applied["path"] == "/api/v0/projects/tula-wot/templates/apply"
    assert applied["body"] == {
        "template_id": "edit_chain",
        "packet_ids": [pid],
        "engine_id": "gpt-image-2.5",
        "recipe": "night-relight",
        "quote": True,
        "auto_approve": False,
    }
    assert daemon["auth"] == [f"Bearer {TOKEN}"]
    assert row["quote_line"] == QUOTED and row["quote_error"] is False
    assert row["quote"]["runs"] == ["run_01"] and row["quote"]["usd"] == pytest.approx(0.039)
    assert ctl.quote_message("Send + quote", row) == (f"Send + quote: CAM_a: {QUOTED}", False)
    assert daemon["event_requests"] == []  # already ingested: nothing to wait for


def test_auto_approve_reaches_the_daemon_and_the_row_says_running(monkeypatch, daemon):
    daemon["answer"] = apply_answer(state="APPROVED")
    live, _ = live_controller(monkeypatch, [sent(new_packet_id())])
    [row] = live.send_and_quote(["CAM_a"], progress, auto_approve=True)
    assert daemon["applied"][0]["body"]["auto_approve"] is True
    assert row["quote_line"] == "approved · running"


def test_a_packet_still_ingesting_is_followed_before_the_quote(monkeypatch, daemon):
    pid = new_packet_id()
    live, _ = live_controller(monkeypatch, [sent(pid, ingest="ingesting")])
    [row] = live.send_and_quote(["CAM_a"], progress)
    assert daemon["event_requests"] and f"/api/v0/packets/{pid}/events" in daemon["event_requests"][0]
    assert row["ingest"] == "ingested" and daemon["applied"][0]["body"]["packet_ids"] == [pid]
    assert row["quote_line"] == QUOTED


def test_an_ingest_that_does_not_finish_in_the_window_is_not_quoted(monkeypatch, daemon):
    daemon["events"] = [{"seq": 1, "type": "ingest.started", "data": {}}]
    live, _ = live_controller(monkeypatch, [sent(new_packet_id(), ingest="ingesting")])
    live.quote_ingest_wait_s = 0.0
    [row] = live.send_and_quote(["CAM_a"], progress)
    assert daemon["applied"] == [] and row["quote_error"] is True
    assert row["quote_line"].startswith("not quoted: the ingest is ingesting after 0 s")


def test_a_failed_send_or_ingest_posts_nothing(monkeypatch, daemon):
    failed = {"camera": "CAM_b", "ok": False, "packet_id": None, "errors": ["render: out of memory"]}
    bad_ingest = sent(new_packet_id(), ingest="failed: legend mismatch", camera="CAM_c")
    live, _ = live_controller(monkeypatch, [failed, bad_ingest])
    rows = live.send_and_quote(["CAM_b", "CAM_c"], progress)
    assert daemon["applied"] == []
    assert [r["quote_line"] for r in rows] == [
        "not quoted: the send failed",
        "not quoted: the ingest failed: legend mismatch",
    ]
    assert all(r["quote_error"] for r in rows)


def test_a_refusing_daemon_is_a_row_not_an_exception(monkeypatch, daemon):
    daemon["status"], daemon["answer"] = 409, {"error": "not_ingested", "detail": "packet still ingesting"}
    live, _ = live_controller(monkeypatch, [sent(new_packet_id())])
    [row] = live.send_and_quote(["CAM_a"], progress)
    assert row["quote_line"] == "not quoted: HTTP 409: not_ingested: packet still ingesting"
    assert row["quote_error"] is True and row["ok"] is True  # the packet itself was sent


def test_an_unreachable_daemon_after_the_send_is_a_row(monkeypatch, tmp_path):
    config = Path(os.environ["PLATE_CONFIG"])
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps({"daemon_url": "http://127.0.0.1:9", "token": TOKEN}), encoding="utf-8", newline="\n"
    )
    live, _ = live_controller(monkeypatch, [sent(new_packet_id())])
    [row] = live.send_and_quote(["CAM_a"], progress)
    assert row["quote_line"].startswith("not quoted: daemon:") and row["quote_error"] is True


def test_every_ingested_camera_gets_its_own_chain(monkeypatch, daemon):
    a, b = new_packet_id(), new_packet_id()
    live, _ = live_controller(monkeypatch, [sent(a), sent(b, camera="CAM_b")])
    rows = live.send_and_quote(["CAM_a", "CAM_b"], progress)
    bodies = [x["body"] for x in daemon["applied"]]
    assert [b_["packet_ids"] for b_ in bodies] == [[a], [b]]
    assert bodies[0]["engine_id"] == "gpt-image-2.5" and bodies[1]["engine_id"] == ctl.ENGINE_IDS[0]
    assert "recipe" not in bodies[1]  # CAM_b has the default recipe: no preset
    assert [r["quote_line"] for r in rows] == [QUOTED, QUOTED]


# --------------------------------------------------------------------------- the dock (Qt, offscreen)


@pytest.fixture(scope="module")
def qt():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return types.SimpleNamespace(app=app, QtCore=QtCore, QtWidgets=QtWidgets)


@pytest.fixture
def make_dock(qt):
    from plate_max.ui import dock as dockmod

    dockmod.close_dock()
    made = []

    def build(controller=None, **kwargs):
        fake = controller if controller is not None else ctl.FakeController(**kwargs)
        widget = dockmod.PlateDock(fake)
        made.append(widget)
        return widget, fake

    yield build
    for widget in made:
        widget.close()
        widget.deleteLater()
    dockmod.close_dock()


def select(dock, *names):
    for i in range(dock.cam_list.count()):
        item = dock.cam_list.item(i)
        if item.data(256) == names[0]:
            dock.cam_list.setCurrentRow(i)
    for i in range(dock.cam_list.count()):
        item = dock.cam_list.item(i)
        item.setSelected(item.data(256) in names)


def calls(fake, name):
    return [c for c in fake.calls if c[0] == name]


def row_of(widget):
    """The QHBoxLayout of the card that holds widget."""
    card = widget.parentWidget().layout()
    for i in range(card.count()):
        sub = card.itemAt(i).layout()
        if sub is not None and sub.indexOf(widget) >= 0:
            return sub
    return None


def test_the_button_sits_beside_send_and_auto_approve_starts_off(make_dock):
    dock, _ = make_dock()
    assert dock.btn_send_quote.text() == "Send + quote"
    row = row_of(dock.btn_send)
    assert row is not None and row is row_of(dock.btn_send_quote)
    assert row.indexOf(dock.btn_send_quote) == row.indexOf(dock.btn_send) + 1
    assert dock.chk_auto_approve.text() == "auto-approve under cap" and not dock.chk_auto_approve.isChecked()
    dock.cam_list.clearSelection()
    dock._refresh_buttons()
    assert not dock.btn_send_quote.isEnabled()  # nothing selected
    select(dock, "CAM_main")
    dock._refresh_buttons()
    assert dock.btn_send_quote.isEnabled() and dock.chk_auto_approve.isEnabled()
    assert "Edit chain" in dock.btn_send_quote.toolTip()


def test_send_and_quote_from_the_dock_logs_the_quoted_row(make_dock):
    dock, fake = make_dock()
    select(dock, "CAM_main")
    dock.btn_send_quote.click()
    assert calls(fake, "send_and_quote") == [("send_and_quote", ["CAM_main"], False)]
    log = dock.log.toPlainText()
    assert "Send + quote: CAM_main: quoted 0.04 USD · waiting for approval" in log
    assert calls(fake, "lint") and calls(fake, "daemon_status")  # the Send checks ran first


def test_ticking_auto_approve_asks_for_it_and_a_refusal_is_shown(make_dock):
    dock, fake = make_dock()
    select(dock, "CAM_main")
    dock.chk_auto_approve.setChecked(True)
    dock.btn_send_quote.click()
    assert calls(fake, "send_and_quote")[-1] == ("send_and_quote", ["CAM_main"], True)
    assert "Send + quote: CAM_main: approved · running" in dock.log.toPlainText()
    fake.quote_line = "not quoted: HTTP 409: not_ingested"
    dock.btn_send_quote.click()
    assert "not quoted: HTTP 409" in dock.log.toPlainText()


def test_a_lint_block_stops_send_and_quote_before_anything_is_sent(make_dock):
    dock, fake = make_dock()
    fake.lint_reports["CAM_main"] = {
        "passed": False,
        "unlabelled_share": None,
        "problems": [{"code": "SHIFTED_CAMERA", "node": "CAM_main", "detail": "distortion"}],
    }
    select(dock, "CAM_main")
    dock.btn_send_quote.click()
    assert calls(fake, "send_and_quote") == []


def test_lint_warnings_show_without_blocking(make_dock):
    dock, fake = make_dock()
    fake.lint_reports["CAM_main"] = {
        "passed": True,
        "unlabelled_share": 0.01,
        "problems": [],
        "warnings": [{"code": "SHIFTED_CAMERA", "node": "CAM_main", "detail": "sent level-and-crop"}],
    }
    select(dock, "CAM_main")
    assert dock.run_lint() is True
    assert (
        "SHIFTED_CAMERA" in dock.lint_view.toPlainText() and "level-and-crop" in dock.lint_view.toPlainText()
    )


def test_a_controller_without_send_and_quote_leaves_the_button_off(make_dock):
    class Older(ctl.FakeController):
        send_and_quote = None

    dock, _ = make_dock(Older())
    select(dock, "CAM_main")
    dock._refresh_buttons()
    assert dock.btn_send.isEnabled() and not dock.btn_send_quote.isEnabled()
    assert not dock.chk_auto_approve.isEnabled()
