from __future__ import annotations

import http.client
import json
import threading

import pytest

from plate_label.server import make_server
from plate_label.store import file_blake3, read_labels


@pytest.fixture
def live(images, tmp_path):
    out = tmp_path / "labels.jsonl"
    httpd = make_server(images, out, "tester", "take-9", port=0)
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
        yield call, out, images, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_binds_loopback_only(live):
    _call, _out, _images, httpd = live
    assert httpd.server_address[0] == "127.0.0.1"


def test_index_page_is_self_contained(live):
    call, *_ = live
    status, headers, raw = call("GET", "/")
    html = raw.decode("utf-8")
    assert status == 200
    for key in ("S", "F", "R"):
        assert f'data-key="{key}"' in html
    for code in ("G01", "C03", "T03", "P02", "M03", "X01", "O01", "O99"):
        assert f'value="{code}"' in html
    assert "http://" not in html and "https://" not in html  # no CDN, no external fetch
    csp = dict(headers)["Content-Security-Policy"]
    assert "default-src 'none'" in csp


def test_next_walks_the_folder(live):
    call, out, images, _ = live
    status, _, raw = call("GET", "/api/next?after=-1")
    first = json.loads(raw)
    assert status == 200 and first["index"] == 0 and first["total"] == 3
    assert first["image"] == "A_first.jpg" and first["labelled"] is False
    second = json.loads(call("GET", "/api/next?after=0")[2])
    assert second["image"] == "b_second.PNG"
    last = json.loads(call("GET", "/api/next?after=99")[2])
    assert last["index"] == 2 and last["image"] == "sub/c_third.webp"
    back = json.loads(call("GET", "/api/next?after=-2")[2])
    assert back["index"] == 0
    assert call("GET", "/api/next?after=x")[0] == 400


def test_post_label_appends_and_next_unlabelled_skips(live):
    call, out, images, _ = live
    status, _, raw = call(
        "POST", "/api/label", {"image": "A_first.jpg", "verdict": "ship", "reasons": [], "note": ""}
    )
    assert status == 200 and json.loads(raw) == {"ok": True, "count": 1}
    status, _, raw = call(
        "POST", "/api/label",
        {"image": "sub/c_third.webp", "verdict": "fix", "reasons": ["G01", "O99"], "note": "stairs"},
    )  # fmt: skip
    assert status == 200 and json.loads(raw)["count"] == 2
    rows = read_labels(out)
    assert rows[0]["image_blake3"] == file_blake3(images / "A_first.jpg")
    assert rows[0]["rater"] == "tester" and rows[0]["take_id"] == "take-9"
    assert rows[1]["image"] == "sub/c_third.webp" and rows[1]["reasons"] == ["G01", "O99"]

    nxt = json.loads(call("GET", "/api/next?after=-1&unlabelled=1")[2])
    assert nxt["image"] == "b_second.PNG" and nxt["remaining"] == 1 and "all_done" not in nxt
    call("POST", "/api/label", {"image": "b_second.PNG", "verdict": "ship"})
    done = json.loads(call("GET", "/api/next?after=-1&unlabelled=1")[2])
    assert done["all_done"] is True and done["remaining"] == 0 and done["labelled"] is True


def test_bad_labels_are_400_and_write_nothing(live):
    call, out, *_ = live
    for body, needle in [
        ({"image": "A_first.jpg", "verdict": "fix", "reasons": ["Z99"]}, "Z99"),
        ({"image": "A_first.jpg", "verdict": "fix", "reasons": []}, "at least one"),
        ({"image": "A_first.jpg", "verdict": "fix", "reasons": ["O99"], "note": ""}, "O99"),
        ({"image": "A_first.jpg", "verdict": "maybe"}, "verdict"),
        ({"image": "A_first.jpg", "verdict": "ship", "reasons": "G01"}, "list"),
        ({"image": "A_first.jpg", "verdict": 3}, "verdict"),
    ]:
        status, _, raw = call("POST", "/api/label", body)
        assert status == 400, body
        assert needle in json.loads(raw)["error"]
    assert call("POST", "/api/label", None, {"Content-Length": "0"})[0] == 400  # empty body is not JSON
    assert not out.exists()


def test_image_path_rules(live):
    call, out, images, _ = live
    status, headers, raw = call("GET", "/img/A_first.jpg")
    assert status == 200 and raw == b"jpg-bytes-a" and dict(headers)["Content-Type"] == "image/jpeg"
    assert call("GET", "/img/sub/c_third.webp")[2] == b"webp-bytes-c"
    assert call("GET", "/img/../../etc/passwd")[0] == 403
    assert call("GET", "/img/..%2F..%2Fetc%2Fpasswd")[0] == 403
    assert call("GET", "/img/%2e%2e/shots.txt")[0] == 403
    assert call("GET", "/img/notes.txt")[0] == 403  # inside the folder but not an image
    assert call("GET", "/img/missing.png")[0] == 404
    assert call("GET", "/nope")[0] == 404
    for image, code in [("../escape.png", 403), ("missing.png", 404), ("notes.txt", 403)]:
        assert call("POST", "/api/label", {"image": image, "verdict": "ship"})[0] == code
    assert not out.exists()


def test_cross_site_requests_are_refused(live):
    call, out, *_ = live
    body = {"image": "A_first.jpg", "verdict": "ship"}
    assert call("POST", "/api/label", body, {"Origin": "http://evil.example"})[0] == 403
    assert call("POST", "/api/label", body, {"Origin": "null"})[0] == 403
    assert call("GET", "/api/next", None, {"Host": "evil.example"})[0] == 403  # DNS rebinding
    assert not out.exists()
    port = live[3].server_address[1]
    assert call("POST", "/api/label", body, {"Origin": f"http://127.0.0.1:{port}"})[0] == 200
    assert call("POST", "/api/label", body, {"Origin": f"http://localhost:{port}"})[0] == 200


def test_empty_folder(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    httpd = make_server(empty, tmp_path / "labels.jsonl", "tester", None, port=0)
    try:
        assert httpd.app.next(-1) == {"index": -1, "total": 0, "image": None, "labelled": False}
    finally:
        httpd.server_close()


def test_tiff_is_served_as_png_when_pillow_can_read_it(images, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    Image.new("RGB", (4, 3), (200, 10, 10)).save(images / "d_scan.tif")
    Image.new("I;16", (4, 3), 40000).save(images / "e_gray16.tiff")
    httpd = make_server(images, tmp_path / "labels.jsonl", "tester", None, port=0)
    try:
        for name in ("d_scan.tif", "e_gray16.tiff"):
            body, ctype = httpd.app.image_bytes(httpd.app.resolve_image(name))
            assert ctype == "image/png" and body.startswith(b"\x89PNG")
    finally:
        httpd.server_close()
