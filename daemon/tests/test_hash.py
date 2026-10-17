"""POST /api/v0/hash: BLAKE3 for the plugin, confined to the packets folders, links never followed."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import blake3
from fastapi.testclient import TestClient

import plate_spec
from plate_daemon import hashing
from plate_daemon.api import create_app
from plate_daemon.config import Settings


def _packet_file(settings, rel="pkt_01/beauty.exr", data=b"PLATE beauty\n" * 1000) -> Path:
    p = settings.packets_dirs[0] / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_inside_file_matches_plate_spec_blake3(client, auth, settings):
    f = _packet_file(settings, data=bytes(range(256)) * 9000)  # > 2 MiB: more than one chunk
    r = client.post("/api/v0/hash", json={"paths": [str(f)]}, headers=auth)
    assert r.status_code == 200
    got = r.json()["hashes"][str(f)]
    assert got == {"algo": "blake3", "hex": plate_spec._blake3_file(f), "bytes": f.stat().st_size}


def test_outside_the_packets_dirs_is_403(client, auth, settings, tmp_path):
    outside = tmp_path / "secrets.txt"
    outside.write_text("not yours", encoding="utf-8")
    r = client.post("/api/v0/hash", json={"paths": [str(outside)]}, headers=auth)
    assert r.status_code == 403 and r.json()["error"] == "forbidden_path"
    inside = _packet_file(settings)
    sneaky = str(settings.packets_dirs[0]) + "/pkt_01/../../secrets.txt"
    assert client.post("/api/v0/hash", json={"paths": [sneaky]}, headers=auth).status_code == 403
    # One bad path refuses the whole request.
    both = {"paths": [str(inside), str(outside)]}
    assert client.post("/api/v0/hash", json=both, headers=auth).status_code == 403
    assert (
        client.post("/api/v0/hash", json={"paths": [str(settings.packets_dirs[0])]}, headers=auth).status_code
        == 403
    )


def test_relative_missing_and_empty_requests(client, auth, settings):
    _packet_file(settings)
    assert client.post("/api/v0/hash", json={"paths": ["pkt_01/beauty.exr"]}, headers=auth).status_code == 422
    missing = settings.packets_dirs[0] / "pkt_01" / "nope.exr"
    assert client.post("/api/v0/hash", json={"paths": [str(missing)]}, headers=auth).status_code == 404
    assert client.post("/api/v0/hash", json={}, headers=auth).status_code == 422
    assert client.post("/api/v0/hash", json={"paths": [str(missing)]}).status_code == 401


def test_config_packets_dir_is_allowed_too(tmp_path, auth):
    nas = tmp_path / "nas" / "packets"
    (tmp_path / "config.json").write_text(
        json.dumps(
            {**json.loads((tmp_path / "config.json").read_text(encoding="utf-8")), "packets_dir": str(nas)}
        ),
        encoding="utf-8",
        newline="\n",
    )
    f = nas / "pkt_02" / "camera.json"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"{}")
    with TestClient(create_app(Settings.load()), base_url="http://127.0.0.1:4610") as c:
        r = c.post("/api/v0/hash", json={"paths": [str(f)]}, headers=auth)
    assert r.status_code == 200 and r.json()["hashes"][str(f)]["hex"] == blake3.blake3(b"{}").hexdigest()


def test_symlinked_component_is_refused(settings, monkeypatch):
    f = _packet_file(settings, rel="pkt_03/sub/file.bin")
    linky = (settings.packets_dirs[0].resolve() / "pkt_03" / "sub").resolve()
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == linky or real_is_symlink(self))
    try:
        hashing.contained_path(str(f), settings.packets_dirs)
    except hashing.HashRefused as e:
        assert "symlink" in str(e)
    else:
        raise AssertionError("a symlinked directory below the packets folder must be refused")


def test_payloads_are_hashed_and_capped(client, auth):
    scene = b'{"scene": "digest"}'
    view = bytes(1000)
    body = {
        "payloads": {
            "scene_hash": base64.b64encode(scene).decode(),
            "view_hash": base64.b64encode(view).decode(),
        }
    }
    r = client.post("/api/v0/hash", json=body, headers=auth)
    assert r.status_code == 200
    assert r.json()["hashes"] == {
        "scene_hash": {"algo": "blake3", "hex": blake3.blake3(scene).hexdigest(), "bytes": len(scene)},
        "view_hash": {"algo": "blake3", "hex": blake3.blake3(view).hexdigest(), "bytes": len(view)},
    }
    bad = {"payloads": {"x": "not base64!"}}
    assert client.post("/api/v0/hash", json=bad, headers=auth).status_code == 422
    half = base64.b64encode(bytes(hashing.MAX_PAYLOAD_BYTES // 2 + 1)).decode()
    big = {"payloads": {"a": half, "b": half}}
    r = client.post("/api/v0/hash", json=big, headers=auth)
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"


def test_paths_and_payloads_in_one_request(client, auth, settings):
    f = _packet_file(settings)
    body = {"paths": [str(f)], "payloads": {"view": base64.b64encode(b"v").decode()}}
    r = client.post("/api/v0/hash", json=body, headers=auth)
    assert r.status_code == 200 and set(r.json()["hashes"]) == {str(f), "view"}
