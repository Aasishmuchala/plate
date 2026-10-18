"""Wave 12 asset-upload fixes (router plan T136): the header is read before any pixel is decoded.

A 410 KiB PNG is a valid file that declares 12000 x 12000 x 3: `cv2.imdecode` allocates 432 MB for it, and the
reference path then copies it twice more. `plate_pixels.imghdr.header_size` names the declared size from the
first bytes, so `reference_png`, `mask_png`, `fit_mask` and `assets._imdecode` ask it first: no header it reads is
422 `bad_image` (`"not a PNG/JPEG/WebP/BMP/TIFF header"`), a declared `width * height` over
`assets.UPLOAD_MAX_PIXELS` (100 000 000) is 422 `{"error": "too_many_pixels", "declared": [w, h], "limit": N}`,
and the decode runs only after both.

The bombs are built here from zlib streams of zeros (nothing is committed): the 12000 x 12000 RGB one is 410 KiB.
`test_a_refused_upload_does_not_grow_the_daemon` runs the whole daemon in a fresh Python process, because
`ru_maxrss` is a high-water mark: in the test process an earlier test's peak would hide a decode. That process
also uploads an accepted 5000 x 5000 reference as a control, so the test fails loudly if the instrument cannot see
a decode at all. It is skipped on Windows (no `resource`).
"""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from plate_daemon import assets
from plate_daemon.cas import CAS
from plate_daemon.routes import assets as asset_routes
from plate_pixels import imghdr

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "stress-assets"
LIMIT = 100_000_000
MESSAGE = "not a PNG/JPEG/WebP/BMP/TIFF header"
PYTHONPATH_DIRS = ("spec", "daemon", "pixels", "engines", "gates", "plugins/max", "tools")


# ---------------------------------------------------------------------------- bombs and small images


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def zero_png(w: int, h: int, channels: int) -> bytes:
    """A valid 8-bit PNG of `w` x `h` black pixels, deflated from a stream of zeros without ever holding the
    decoded image (12000 x 12000 x 3 is 410 KiB on disk and 432 MB decoded)."""
    color_type = {1: 0, 3: 2, 4: 6}[channels]
    co = zlib.compressobj(9)
    row = bytes(w * channels + 1)  # filter byte 0 + zeros
    parts: list[bytes] = []
    left = h
    while left >= 100:
        parts.append(co.compress(row * 100))
        left -= 100
    if left:
        parts.append(co.compress(row * left))
    parts.append(co.flush())
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
        + _chunk(b"IDAT", b"".join(parts))
        + _chunk(b"IEND", b"")
    )


def declared_jpeg(w: int, h: int) -> bytes:
    """A small real JPEG whose start-of-frame header has been rewritten to declare `w` x `h`."""
    buf = io.BytesIO()
    Image.fromarray(np.full((48, 64, 3), 90, np.uint8), "RGB").save(buf, "JPEG", quality=85)
    data = bytearray(buf.getvalue())
    sof = data.index(b"\xff\xc0")  # marker, length (2), precision (1), height (2), width (2)
    data[sof + 5 : sof + 9] = struct.pack(">HH", h, w)
    return bytes(data)


def _png(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(arr))
    assert ok
    return bytes(buf)


def _gradient(h: int = 360, w: int = 640) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w]
    return np.stack([x * 255 // (w - 1), y * 255 // (h - 1), np.full_like(x, 90)], axis=-1).astype(np.uint8)


def _mask(h: int = 360, w: int = 640) -> np.ndarray:
    m = np.zeros((h, w), np.uint8)
    m[60:200, 100:420] = 255
    return m


@pytest.fixture(scope="module")
def bomb_12000() -> bytes:
    return zero_png(12000, 12000, 3)


@pytest.fixture
def project(client, auth):
    r = client.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    return PROJECT


def _upload(client, auth, kind: str, body: bytes):
    return client.post(f"/api/v0/assets?kind={kind}&project_id={PROJECT}", content=body, headers=auth)


def _too_many(r, declared: list[int], limit: int = LIMIT) -> None:
    assert r.status_code == 422, r.text
    assert r.json() == {"error": "too_many_pixels", "declared": declared, "limit": limit}


# ---------------------------------------------------------------------------- the generators say what they claim


def test_the_bomb_builders_declare_what_the_tests_assume(bomb_12000):
    assert 400 * 1024 < len(bomb_12000) < 420 * 1024  # "a 410 KiB PNG"
    assert imghdr.header_size(bomb_12000) == (12000, 12000)
    assert imghdr.header_size(zero_png(16000, 16000, 1)) == (16000, 16000)
    assert imghdr.header_size(declared_jpeg(50000, 50000)) == (50000, 50000)
    assert imghdr.header_size(declared_jpeg(50000, 40000)) == (50000, 40000)
    small = zero_png(32, 16, 3)
    assert cv2.imdecode(np.frombuffer(small, np.uint8), cv2.IMREAD_UNCHANGED).shape == (16, 32, 3)
    assert assets.UPLOAD_MAX_PIXELS == LIMIT


# ---------------------------------------------------------------------------- uploads: refused from the header


def test_a_410_kib_png_declaring_12000_square_is_refused_by_both_uploads(client, auth, project, bomb_12000):
    for kind in ("mask", "reference"):
        _too_many(_upload(client, auth, kind, bomb_12000), [12000, 12000])
    assert client.get(f"/api/v0/projects/{PROJECT}/references", headers=auth).json() == []
    assert not list((client.app.state.ctx.settings.projects_dir / PROJECT / "assets").rglob("*.png"))


def test_the_refusal_comes_before_cv2_imdecode(client, auth, project, bomb_12000, monkeypatch):
    calls: list[int] = []

    def boom(*_a, **_k):
        calls.append(1)
        raise AssertionError("cv2.imdecode ran on an image the header already refused")

    monkeypatch.setattr(cv2, "imdecode", boom)
    for kind in ("mask", "reference"):
        _too_many(_upload(client, auth, kind, bomb_12000), [12000, 12000])
        _too_many(_upload(client, auth, kind, declared_jpeg(50000, 50000)), [50000, 50000])
        assert _upload(client, auth, kind, b"GIF89a" + bytes(64)).status_code == 422
    assert calls == []


def test_a_16000_square_reference_is_refused_not_shrunk_to_4096(client, auth, project):
    bomb = zero_png(16000, 16000, 1)  # 256 MB decoded, 243 KiB on disk
    assert len(bomb) < 300 * 1024
    _too_many(_upload(client, auth, "reference", bomb), [16000, 16000])
    _too_many(_upload(client, auth, "mask", bomb), [16000, 16000])
    assert client.get(f"/api/v0/projects/{PROJECT}/references", headers=auth).json() == []


def test_a_jpeg_header_declaring_50000_square_with_a_small_real_payload_is_refused(client, auth, project):
    jpeg = declared_jpeg(50000, 50000)
    assert len(jpeg) < 5000
    _too_many(_upload(client, auth, "reference", jpeg), [50000, 50000])
    _too_many(_upload(client, auth, "mask", jpeg), [50000, 50000])
    # A wide strip is judged on w * h as well: 50000 x 40000, not on either side alone.
    _too_many(_upload(client, auth, "reference", declared_jpeg(50000, 40000)), [50000, 40000])


def test_uploads_inside_the_limit_still_land(client, auth, project):
    ref = _upload(client, auth, "reference", _png(_gradient()))
    assert ref.status_code == 201, ref.text
    assert (ref.json()["width"], ref.json()["height"]) == (640, 360)
    mask = _upload(client, auth, "mask", _png(_mask()))
    assert mask.status_code == 201, mask.text
    assert (mask.json()["width"], mask.json()["height"]) == (640, 360)
    # Masks take any format cv2 reads, so the header check lists them all: BMP and TIFF as well as PNG.
    for ext in (".bmp", ".tiff"):
        ok, buf = cv2.imencode(ext, _mask())
        assert ok
        r = _upload(client, auth, "mask", bytes(buf))
        assert r.status_code == 201, (ext, r.text)


def test_the_cap_is_on_width_times_height_and_is_inclusive(client, auth, project, monkeypatch):
    monkeypatch.setattr(assets, "UPLOAD_MAX_PIXELS", 640 * 360)
    assert _upload(client, auth, "mask", _png(_mask(360, 640))).status_code == 201  # exactly at the cap
    _too_many(_upload(client, auth, "mask", _png(_mask(360, 641))), [641, 360], 640 * 360)
    _too_many(_upload(client, auth, "reference", _png(_gradient(361, 640))), [640, 361], 640 * 360)


def test_bytes_with_no_image_header_are_bad_image(client, auth, project):
    for kind in ("mask", "reference"):
        for body in (b"", b"not an image", b"GIF89a" + bytes(64), b"\x89PNG\r\n\x1a\n" + bytes(8)):
            r = _upload(client, auth, kind, body)
            assert r.status_code == 422 and r.json()["error"] == "bad_image", (kind, body[:8], r.text)
    # A PNG or JPEG signature with the header cut off is the header message.
    r = _upload(client, auth, "reference", b"\x89PNG\r\n\x1a\n" + bytes(8))
    assert r.json() == {"error": "bad_image", "detail": MESSAGE}
    r = _upload(client, auth, "reference", b"\xff\xd8\xff\xe0" + bytes(10))
    assert r.json() == {"error": "bad_image", "detail": MESSAGE}
    r = _upload(client, auth, "mask", b"GIF89a" + bytes(64))
    assert r.json() == {"error": "bad_image", "detail": MESSAGE}
    # A reference is still a PNG or JPEG only: a TIFF has a header, but not one a reference takes.
    ok, tiff = cv2.imencode(".tiff", np.zeros((8, 8, 3), np.uint8))
    assert ok
    r = _upload(client, auth, "reference", bytes(tiff))
    assert r.status_code == 422 and r.json()["detail"] == "a reference is a PNG or JPEG image"


def test_a_good_header_on_undecodable_bytes_is_still_a_mask_refusal(client, auth, project):
    good = _png(_mask())
    r = _upload(client, auth, "mask", good[:40])  # IHDR intact, the pixel data gone
    assert r.status_code == 422 and r.json()["error"] == "bad_mask"
    r = _upload(client, auth, "reference", good[:40])
    assert r.status_code == 422 and r.json()["error"] == "bad_image"


# ---------------------------------------------------------------------------- the helpers, without a daemon


def test_check_header_and_the_decode_helpers(monkeypatch, bomb_12000):
    assert assets.check_header(_png(_mask())) == (640, 360)
    with pytest.raises(assets.BadImage, match="PNG/JPEG/WebP/BMP/TIFF"):
        assets.check_header(b"")
    with pytest.raises(assets.TooManyPixels) as e:
        assets.check_header(bomb_12000)
    assert (e.value.declared, e.value.limit) == ((12000, 12000), LIMIT)

    def boom(*_a, **_k):
        raise AssertionError("decoded")

    monkeypatch.setattr(cv2, "imdecode", boom)
    with pytest.raises(assets.TooManyPixels):
        assets._imdecode(bomb_12000)
    with pytest.raises(assets.BadImage):
        assets._imdecode(b"")
    with pytest.raises(assets.TooManyPixels):
        assets.mask_png(bomb_12000)
    with pytest.raises(assets.BadImage):
        assets.mask_png(b"GIF89a")
    with pytest.raises(assets.TooManyPixels):
        asset_routes.reference_png(bomb_12000)
    with pytest.raises(assets.TooManyPixels):
        asset_routes.fit_mask(bomb_12000, (640, 360))
    with pytest.raises(assets.BadImage):
        asset_routes.fit_mask(b"nope", (640, 360))
    # The stored-asset decode turns "no header" into the 415 path's error, and passes the pixel cap through.
    with pytest.raises(assets.AssetFormatError):
        assets._decode(b"")
    with pytest.raises(assets.TooManyPixels):
        assets._decode(bomb_12000)


def test_the_limit_is_read_at_call_time(monkeypatch):
    png = _png(_mask())
    assert assets.check_header(png) == (640, 360)
    monkeypatch.setattr(assets, "UPLOAD_MAX_PIXELS", 100)
    with pytest.raises(assets.TooManyPixels):
        assets.check_header(png)


# ---------------------------------------------------------------------------- stored assets are decoded behind the same check


def test_a_stored_asset_over_the_cap_is_not_decoded_on_get(client, auth, project, bomb_12000, settings):
    key = CAS(settings.projects_dir / PROJECT / "assets").put_bytes(bomb_12000, "png")
    url = f"/api/v0/assets/{key}?project_id={PROJECT}"
    for fmt in ("thumb", "rgba16f"):
        _too_many(client.get(f"{url}&fmt={fmt}", headers=auth), [12000, 12000])
    # The stored bytes themselves, and a PNG served as-is, never decode: both still answer.
    assert client.get(url, headers=auth).content == bomb_12000
    assert client.get(f"{url}&fmt=png", headers=auth).content == bomb_12000
    # An asset that is not an image at all keeps its 415.
    junk = CAS(settings.projects_dir / PROJECT / "assets").put_bytes(b"not an image", "png")
    r = client.get(f"/api/v0/assets/{junk}?project_id={PROJECT}&fmt=thumb", headers=auth)
    assert r.status_code == 415 and r.json()["error"] == "unsupported_format"


# ---------------------------------------------------------------------------- memory: a refusal costs a header read

CHILD = r'''
"""One daemon in this process: warm it, refuse the bomb, accept a 5000 x 5000 control; report ru_maxrss growth."""
import json
import resource
import sys
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from plate_daemon.api import create_app
from plate_daemon.config import Settings

bomb = Path(sys.argv[1]).read_bytes()
control = Path(sys.argv[2]).read_bytes()
settings = Settings.load()
auth = {"Authorization": "Bearer " + settings.token}


def peak() -> int:  # bytes: macOS reports ru_maxrss in bytes, Linux in KiB
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v if sys.platform == "darwin" else v * 1024


ok, small = cv2.imencode(".png", np.full((36, 64, 3), 90, np.uint8))
assert ok
with TestClient(create_app(settings), base_url="http://127.0.0.1:4610") as c:
    r = c.post("/api/v0/projects", json={"project_id": "rss-job", "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    url = "/api/v0/assets?kind=reference&project_id=rss-job"
    assert c.post(url, content=bytes(small), headers=auth).status_code == 201  # warm every import and thread
    p0 = peak()
    refused = c.post(url, content=bomb, headers=auth)
    p1 = peak()
    accepted = c.post(url, content=control, headers=auth)
    p2 = peak()
print(json.dumps({
    "refused": [refused.status_code, refused.json()],
    "growth_refused": p1 - p0,
    "accepted": accepted.status_code,
    "growth_accepted": p2 - p1,
}))
'''


@pytest.mark.skipif(sys.platform == "win32", reason="resource.getrusage does not exist on Windows")
def test_a_refused_upload_does_not_grow_the_daemon(tmp_path, bomb_12000, child_env):
    bomb = tmp_path / "bomb.png"
    control = tmp_path / "control.png"
    script = tmp_path / "rss_child.py"
    bomb.write_bytes(bomb_12000)
    control.write_bytes(zero_png(5000, 5000, 3))  # 75 MB decoded: under the cap, so it IS decoded
    script.write_text(CHILD, encoding="utf-8", newline="\n")
    env = child_env()
    env["PYTHONPATH"] = os.pathsep.join(str(ROOT / d) for d in PYTHONPATH_DIRS)
    done = subprocess.run(
        [sys.executable, str(script), str(bomb), str(control)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    mb = 1024 * 1024
    assert out["refused"] == [
        422,
        {"error": "too_many_pixels", "declared": [12000, 12000], "limit": LIMIT},
    ]
    assert out["growth_refused"] < 50 * mb, out
    # The instrument works: decoding the accepted 5000 x 5000 reference does move the high-water mark.
    assert out["accepted"] == 201 and out["growth_accepted"] > 50 * mb, out
