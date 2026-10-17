from __future__ import annotations

import http.client
import json
import threading

import pytest

from plate_label.pairs import load_manifest, scan_folder
from plate_label.server import LabelApp, make_server
from plate_label.store import read_labels


@pytest.fixture
def live(shots, tmp_path):
    out = tmp_path / "labels.jsonl"
    httpd = make_server(scan_folder(shots), out, "tester", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]

    def call(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        data = None if body is None else json.dumps(body)
        hdrs = (
            {"Content-Type": "application/json", **(headers or {})}
            if body is not None
            else dict(headers or {})
        )
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, resp.getheaders(), raw

    try:
        yield call, out, shots, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def meta(call, after=-1, unlabelled=False):
    return json.loads(call("GET", f"/api/next?after={after}" + ("&unlabelled=1" if unlabelled else ""))[2])


def test_binds_loopback_only(live):
    assert live[3].server_address[0] == "127.0.0.1"


def test_index_page_is_self_contained(live):
    call, *_ = live
    status, headers, raw = call("GET", "/")
    html = raw.decode("utf-8")
    assert status == 200
    for verdict in ("ship", "fix", "render_it"):
        assert f'data-verdict="{verdict}"' in html
    for code in ("A1", "F12", "P1", "P7"):
        assert f'data-code="{code}"' in html
    assert 'type="range"' in html  # the wipe slider
    assert "http://" not in html and "https://" not in html  # no CDN, no external fetch
    csp = dict(headers)["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "img-src 'self'" in csp
    assert (
        "style=" not in html.split("<body>", 1)[1].split("<script", 1)[0]
    )  # inline style attrs break the CSP


def test_page_never_shows_gate_scores(live):
    html = live[0]("GET", "/")[2].decode("utf-8").lower()
    assert "gate_report" not in html and "ensemble" not in html


def test_next_walks_the_pairs(live):
    call, *_ = live
    first = meta(call)
    assert first["index"] == 0 and first["total"] == 3 and first["take_id"] == "t1"
    assert first["candidate"] == "t1/candidate_a.png" and first["plate"] == "t1/plate.png"
    assert first["has_plate"] is True and first["labelled"] is False and first["remaining"] == 3
    assert meta(call, 0)["candidate"] == "t1/candidate_b.png"
    assert meta(call, 99)["index"] == 2  # clamps at the end
    assert meta(call, -5)["index"] == 0
    assert call("GET", "/api/next?after=x")[0] == 400


def test_round_trip_post_a_verdict_and_read_the_jsonl_line(live):
    call, out, *_ = live
    first = meta(call)
    status, _, raw = call(
        "POST",
        "/api/label",
        {
            "index": 0,
            "take_id": first["take_id"],
            "verdict": "fix",
            "reasons": ["P1", "A2"],
            "seconds": 12.34,
        },
    )
    assert status == 200 and json.loads(raw) == {"ok": True, "count": 1}
    (line,) = out.read_text(encoding="utf-8").splitlines()
    rec = json.loads(line)
    assert list(rec) == ["v", "at", "rater", "take_id", "plate", "candidate", "verdict", "reasons", "seconds"]
    assert rec["v"] == 1 and rec["rater"] == "tester" and rec["take_id"] == "t1"
    assert rec["plate"] == "t1/plate.png" and rec["candidate"] == "t1/candidate_a.png"
    assert rec["verdict"] == "fix" and rec["reasons"] == ["P1", "A2"] and rec["seconds"] == 12.3
    assert rec["at"].endswith("Z")


def test_next_unlabelled_skips_done_pairs_for_this_rater_only(live):
    call, out, *_ = live
    for i, verdict in [(0, "ship"), (2, "render_it")]:
        m = meta(call, i - 1)
        assert call("POST", "/api/label", {"index": i, "take_id": m["take_id"], "verdict": verdict})[0] == 200
    nxt = meta(call, -1, unlabelled=True)
    assert nxt["index"] == 1 and nxt["remaining"] == 1 and "all_done" not in nxt
    m = meta(call, 0)
    call("POST", "/api/label", {"index": 1, "take_id": m["take_id"], "verdict": "ship"})
    done = meta(call, -1, unlabelled=True)
    assert done["all_done"] is True and done["remaining"] == 0 and done["labelled"] is True
    # another rater starts from scratch against the same file
    other = LabelApp(scan_folder(live[2]), out, "someone-else")
    assert other.next(-1, True)["remaining"] == 3


def test_bad_labels_are_rejected_and_write_nothing(live):
    call, out, *_ = live
    ok = {"index": 0, "take_id": "t1"}
    for body, status, needle in [
        ({**ok, "verdict": "fix", "reasons": ["Z99"]}, 400, "Z99"),
        ({**ok, "verdict": "maybe"}, 400, "verdict"),
        ({**ok, "verdict": 3}, 400, "verdict"),
        ({**ok, "verdict": "fix", "reasons": "P1"}, 400, "list"),
        ({**ok, "verdict": "ship", "reasons": ["P1"]}, 400, "no reason"),
        ({**ok, "verdict": "ship", "seconds": -4}, 400, "seconds"),
        ({**ok, "verdict": "ship", "seconds": "fast"}, 400, "seconds"),
        ({"index": "0", "take_id": "t1", "verdict": "ship"}, 400, "index"),
        ({"index": 99, "take_id": "t1", "verdict": "ship"}, 404, "no such pair"),
        ({"index": 0, "take_id": "other", "verdict": "ship"}, 409, "out of date"),
        ([1, 2], 400, "object"),
    ]:
        code, _, raw = call("POST", "/api/label", body)
        assert code == status, body
        assert needle in json.loads(raw)["error"], body
    assert call("POST", "/api/label", None, {"Content-Length": "0"})[0] == 400  # empty body is not JSON
    assert not out.exists()


def test_undo_removes_only_what_this_session_wrote(live):
    call, out, shots, _ = live
    pre = LabelApp(scan_folder(shots), out, "tester")
    pre.label({"index": 0, "take_id": "t1", "verdict": "ship", "seconds": 1})  # an earlier session's line
    # the live server has not labelled anything itself, so there is nothing for it to undo
    code, _, raw = call("POST", "/api/undo", {})
    assert code == 409 and "nothing to undo" in json.loads(raw)["error"]
    assert len(read_labels(out)) == 1

    call(
        "POST", "/api/label", {"index": 1, "take_id": "t1", "verdict": "fix", "reasons": ["A3"], "seconds": 2}
    )
    call("POST", "/api/label", {"index": 2, "take_id": "t2", "verdict": "ship", "seconds": 3})
    assert len(read_labels(out)) == 3
    code, _, raw = call("POST", "/api/undo", {})
    assert code == 200 and json.loads(raw) == {"ok": True, "index": 2, "count": 2}
    assert [r["candidate"] for r in read_labels(out)] == ["t1/candidate_a.png", "t1/candidate_b.png"]
    code, _, raw = call("POST", "/api/undo", {})
    assert json.loads(raw)["index"] == 1 and len(read_labels(out)) == 1
    assert call("POST", "/api/undo", {})[0] == 409  # the earlier session's line is not ours to remove
    assert read_labels(out)[0]["verdict"] == "ship"
    # the undone pair is unlabelled again
    assert meta(call, 0)["labelled"] is False


def test_server_times_the_pair_when_the_client_sends_no_seconds(shots, tmp_path):
    ticks = iter([100.0, 107.5, 107.5, 107.5, 107.5])
    app = LabelApp(scan_folder(shots), tmp_path / "labels.jsonl", "tester", clock=lambda: next(ticks))
    app.next(-1)  # pair 0 shown at t=100
    app.label({"index": 0, "take_id": "t1", "verdict": "ship"})
    assert read_labels(tmp_path / "labels.jsonl")[0]["seconds"] == 7.5


def test_prefetch_peek_does_not_start_the_timer(shots, tmp_path):
    clock = {"t": 0.0}
    app = LabelApp(scan_folder(shots), tmp_path / "labels.jsonl", "tester", clock=lambda: clock["t"])
    app.next(-1)  # pair 0 shown at 0
    clock["t"] = 5.0
    app.next(0, peek=True)  # the browser prefetching pair 1
    clock["t"] = 9.0
    app.next(0)  # pair 1 really shown at 9
    clock["t"] = 10.0
    app.label({"index": 1, "take_id": "t1", "verdict": "ship"})
    assert read_labels(tmp_path / "labels.jsonl")[0]["seconds"] == 1.0


def test_images_are_served_by_pair_and_role_only(live):
    call, _, shots, _ = live
    status, headers, raw = call("GET", "/img/0/candidate")
    assert status == 200 and raw == (shots / "t1" / "candidate_a.png").read_bytes()
    assert dict(headers)["Content-Type"] == "image/png"
    assert call("GET", "/img/0/plate?v=abc")[2] == (shots / "t1" / "plate.png").read_bytes()
    assert call("GET", "/img/2/plate")[2] == (shots / "t2" / "plate.jpg").read_bytes()
    assert dict(call("GET", "/img/2/plate")[1])["Content-Type"] == "image/jpeg"
    for path in ["/img/9/candidate", "/img/-1/candidate", "/img/x/plate", "/img/0/other", "/img/0",
                 "/img/0/candidate/extra", "/img/../../etc/passwd", "/img/0/..%2F..%2Fx", "/nope"]:  # fmt: skip
        assert call("GET", path)[0] == 404, path


def test_files_outside_the_pair_roots_are_refused(shots, tmp_path):
    app = LabelApp(scan_folder(shots), tmp_path / "labels.jsonl", "tester")
    assert {r.name for r in app.roots} == {"t1", "t2"}
    app.roots = [r for r in app.roots if r.name == "t2"]  # as if t1's file now resolved somewhere else
    with pytest.raises(Exception) as err:
        app.image_path(0, "candidate")
    assert getattr(err.value, "status", None) == 403
    assert app.image_path(2, "candidate").name == "candidate.jpg"


def test_candidate_only_pair_has_no_plate(tmp_path, make_image):
    root = tmp_path / "solo"
    make_image(root / "a.png")
    httpd = make_server(scan_folder(root), tmp_path / "labels.jsonl", "tester", port=0)
    try:
        d = httpd.app.next(-1)
        assert d["has_plate"] is False and d["plate"] is None
        with pytest.raises(Exception) as err:
            httpd.app.image_path(0, "plate")
        assert getattr(err.value, "status", None) == 404
    finally:
        httpd.server_close()


def test_manifest_fields_land_in_the_record(manifest, tmp_path):
    out = tmp_path / "labels.jsonl"
    app = LabelApp(load_manifest(manifest), out, "tester")
    app.label({"index": 0, "take_id": "S3", "verdict": "render_it", "reasons": ["F6"], "seconds": 3})
    app.label({"index": 1, "take_id": "S4", "verdict": "ship", "seconds": 1})
    a, b = read_labels(out)
    assert a["run_id"] == "r12" and a["gate_report"] == "gates/r12.1.json"
    assert a["plate"] == "in/a_plate.png" and a["candidate"] == "out/a_ai.png"
    assert "run_id" not in b and "gate_report" not in b


def test_cross_site_requests_are_refused(live):
    call, out, *_ = live
    body = {"index": 0, "take_id": "t1", "verdict": "ship"}
    assert call("POST", "/api/label", body, {"Origin": "http://evil.example"})[0] == 403
    assert call("POST", "/api/label", body, {"Origin": "null"})[0] == 403
    assert call("POST", "/api/undo", {}, {"Origin": "http://evil.example"})[0] == 403
    assert call("GET", "/api/next", None, {"Host": "evil.example"})[0] == 403  # DNS rebinding
    assert call("GET", "/img/0/candidate", None, {"Host": "evil.example"})[0] == 403
    assert not out.exists()
    port = live[3].server_address[1]
    assert call("POST", "/api/label", body, {"Origin": f"http://127.0.0.1:{port}"})[0] == 200
    assert call("POST", "/api/label", body, {"Origin": f"http://localhost:{port}"})[0] == 200


def test_favicon_is_answered_quietly(live):
    assert live[0]("GET", "/favicon.ico")[0] == 204


def test_unknown_post_route_and_oversized_body(live):
    call, out, *_ = live
    assert call("POST", "/api/other", {})[0] == 404
    assert call("POST", "/api/label", None, {"Content-Length": str(10 << 20)})[0] == 413
    assert not out.exists()


def test_tiff_is_served_as_png_when_pillow_can_read_it(tmp_path, make_image):
    Image = pytest.importorskip("PIL.Image")
    root = tmp_path / "tiffs"
    make_image(root / "x" / "plate.png")
    Image.new("RGB", (4, 3), (200, 10, 10)).save(root / "x" / "candidate.tif")
    httpd = make_server(scan_folder(root), tmp_path / "labels.jsonl", "tester", port=0)
    try:
        body, ctype = httpd.app.image_bytes(httpd.app.image_path(0, "candidate"))
        assert ctype == "image/png" and body.startswith(b"\x89PNG")
    finally:
        httpd.server_close()


def test_sixteen_bit_rgb_tiff_master_is_previewed_too(tmp_path, make_image):
    np = pytest.importorskip("numpy")
    tifffile = pytest.importorskip("tifffile")
    pytest.importorskip("cv2")
    root = tmp_path / "masters"
    make_image(root / "x" / "plate.png")
    tifffile.imwrite(root / "x" / "candidate.tif", np.full((3, 4, 3), 40000, dtype=np.uint16))
    httpd = make_server(scan_folder(root), tmp_path / "labels.jsonl", "tester", port=0)
    try:
        body, ctype = httpd.app.image_bytes(httpd.app.image_path(0, "candidate"))
        assert ctype == "image/png" and body.startswith(b"\x89PNG")
    finally:
        httpd.server_close()
