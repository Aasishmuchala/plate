"""Wave 13 API hardening (router plan T154, decision 88): four attacker lows, each test the repro.

- The token: a `PLATE_TOKEN_FILE` that gained a second line ("# rotated ...") became a bearer token no client
  could send, and a `config.json` token with a space in it the same. `config.check_token` holds both to one line
  of printable ASCII without spaces, 16 to 256 characters, a `ConfigError` at start that never quotes it.
- A recipe named by path over the API: `POST /quote {recipe: "/some/where/secret.yaml"}` made the daemon open and
  parse any YAML file it could read. A path must now lie in the project's `recipes/` folder, the studio's or the
  shipped one (`recipes.recipe_roots`); the refusal says why and quotes no line of the file.
- A node's `params` was bounded only by the graph's 4 MiB: over `NODE_PARAMS_MAX_BYTES` (256 KiB) it is 422
  `params_too_large` with the node id, from `PUT /graph` and from the append-node route alike.
- The newline suite: every route that takes a name answers 422 for `"name\\n"` (`$` matches before a trailing
  newline, `fullmatch` does not), and nothing under the home or the export folder is ever named with one.

The tests that need a take run the real pipeline on `fixtures/packets/room_a` with the echoing fake engine, as
`test_stress_wave12_export.py` does.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from blake3 import blake3
from fastapi.testclient import TestClient

from plate_daemon import config, recipes
from plate_daemon.api import create_app
from plate_daemon.config import ConfigError, Paths, Settings, check_token, load_config, read_token_file
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.projects import NODE_PARAMS_MAX_BYTES, NodeParamsTooLarge, check_graph

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
KIB = 1024
TOKEN = "t0ken-0123456789-abcdef"

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent, so the take passes its gates."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        return self.sent[result_url[len(RESULT_SCHEME) :]]


@pytest.fixture
def api(plate_env, monkeypatch):
    """(client, auth headers, settings) on a daemon whose packets ingest inline."""
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    with TestClient(create_app(settings, EchoEngine()), base_url=BASE, raise_server_exceptions=False) as c:
        c.app.state.pipeline.sleep = lambda _s: None
        yield c, {"Authorization": f"Bearer {settings.token}"}, settings


def _project(c: TestClient, auth: dict) -> None:
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text


def _ingest(c: TestClient, auth: dict, packet: Path = ROOM_A) -> str:
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8-sig"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(packet), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    return r.json()["packet_id"]


def _graph(packet_id: str, **edit_params) -> dict:
    return {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake", **edit_params}},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}],
    }


def _setup(c: TestClient, auth: dict, **edit_params) -> str:
    """Project + room_a + Plate -> Edit `e` on the fake engine. The packet id."""
    _project(c, auth)
    packet_id = _ingest(c, auth)
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=_graph(packet_id, **edit_params), headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _quote(c: TestClient, auth: dict, **body):
    return c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json=body, headers=auth)


def _take(c: TestClient, auth: dict) -> str:
    """Quote `e`, approve it as an agent and run it to DONE: the run id."""
    r = _quote(c, auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
    assert r.status_code == 200, r.text
    assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "DONE"
    return run_id


# ---------------------------------------------------------------------------- (1) the token


def _token_file(tmp_path: Path, monkeypatch, text: str | bytes) -> Path:
    path = tmp_path / "token.txt"
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    monkeypatch.setenv("PLATE_TOKEN_FILE", str(path))
    return path


def test_a_token_file_with_a_second_line_is_a_config_error(tmp_path, monkeypatch):
    path = _token_file(tmp_path, monkeypatch, f"{TOKEN}\n# rotated 2026-10-01\n")
    with pytest.raises(ConfigError, match="one line") as e:
        read_token_file(path)
    assert "rotated" not in str(e.value) and TOKEN not in str(e.value)  # the message never quotes it
    with pytest.raises(ConfigError, match="one line"):
        Settings.load()  # the daemon does not start on it


def test_a_crlf_ending_and_a_bom_are_still_accepted(tmp_path, monkeypatch):
    path = _token_file(tmp_path, monkeypatch, f"{TOKEN}\r\n")
    assert read_token_file(path) == TOKEN
    path.write_bytes(("﻿" + TOKEN + "\r\n").encode("utf-8"))  # PowerShell 5.1 writes a BOM
    assert Settings.load().token == TOKEN


@pytest.mark.parametrize(
    "text, why",
    [
        ("x" * 300, "too long"),
        ("x" * 257, "one over the limit"),
        ("x" * 15, "one under the limit"),
        ("", "empty"),
        ("tok en-0123456789-abcdef", "a space"),
        ("tok\ten-0123456789-abcdef", "a tab"),
        ("tök-en-0123456789-abcdef", "not ASCII"),
        ("token-0123456789-abc\x7f", "a control character"),
    ],
)
def test_a_token_that_cannot_be_a_bearer_token_is_refused(tmp_path, monkeypatch, text, why):
    path = _token_file(tmp_path, monkeypatch, text)
    with pytest.raises(ConfigError, match="printable ASCII") as e:
        read_token_file(path)
    assert "16-256" in str(e.value) and str(path) in str(e.value), why


def test_the_limits_are_inclusive_and_a_binary_file_is_a_config_error(tmp_path, monkeypatch):
    for n in (config.MIN_TOKEN_FILE_LEN, config.MAX_TOKEN_FILE_LEN):
        assert check_token("a" * n, "test") == "a" * n
    path = _token_file(tmp_path, monkeypatch, b"\xff\xfe\x00\x80 not utf-8")
    with pytest.raises(ConfigError, match="cannot read"):
        read_token_file(path)


def test_a_config_json_token_with_a_space_names_config_json(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"token": "abc def-0123456789-abcdef", "daemon_url": "http://127.0.0.1:4610"}),
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(ConfigError, match="config.json") as e:
        load_config()
    assert "one line of printable ASCII" in str(e.value) and "abc def" not in str(e.value)
    with pytest.raises(ConfigError, match="config.json"):
        Settings.load()
    with pytest.raises(ConfigError, match="config.json"):
        Paths.from_env().token()
    cfg_path.write_text(json.dumps({"token": 12345678901234567890}), encoding="utf-8", newline="\n")
    with pytest.raises(ConfigError, match="config.json"):  # not even text
        load_config()


def test_a_token_file_wins_over_a_bad_config_json_token(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"token": "bad token"}), encoding="utf-8", newline="\n")
    _token_file(tmp_path, monkeypatch, TOKEN)
    assert Settings.load().token == TOKEN


def test_the_generated_token_passes_check_token(tmp_path):
    cfg = load_config()
    assert check_token(cfg["token"], "config.json") == cfg["token"]
    assert Settings.load().token == cfg["token"]
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["token"] == cfg["token"]


# ---------------------------------------------------------------------------- (2) recipe paths


SECRET = "kind: still_edit\nsecret: [NDA-material-list-line-2\n"


def _project_dir(c: TestClient) -> Path:
    return c.app.state.pipeline.project_dir(PROJECT)


@needs_room_a
def test_a_recipe_path_outside_the_recipe_folders_is_refused_before_it_is_read(api, tmp_path):
    c, auth, _ = api
    packet_id = _setup(c, auth)
    loose = tmp_path / "elsewhere" / "secret.yaml"
    loose.parent.mkdir()
    loose.write_text(SECRET, encoding="utf-8", newline="\n")
    r = _quote(c, auth, recipe=str(loose))
    assert r.status_code == 422 and r.json()["error"] == "unknown_recipe", r.text
    assert "must live in" in r.json()["detail"]
    assert "NDA-material" not in r.text and "unclosed" not in r.text and "still_edit" not in r.text
    # the same file, named from the node's own params, is refused the same way
    g = _graph(packet_id, recipe=str(loose))
    assert c.put(f"/api/v0/projects/{PROJECT}/graph", json=g, headers=auth).status_code == 200
    r = _quote(c, auth)
    assert r.status_code == 422 and "must live in" in r.json()["detail"], r.text


@needs_room_a
def test_the_same_file_in_the_project_recipes_folder_is_read_and_names_the_path_only(api):
    c, auth, _ = api
    _setup(c, auth)
    folder = _project_dir(c) / "recipes"
    folder.mkdir()
    inside = folder / "secret.yaml"
    inside.write_text(SECRET, encoding="utf-8", newline="\n")
    r = _quote(c, auth, recipe=str(inside))
    assert r.status_code == 422 and r.json()["error"] == "unknown_recipe", r.text
    assert r.json()["detail"] == f"recipe {inside}: not a readable runnable recipe file"  # decision 80's rule
    assert "NDA-material" not in r.text


@needs_room_a
def test_a_runnable_recipe_in_the_project_folder_quotes(api):
    c, auth, _ = api
    _setup(c, auth)
    doc = yaml.safe_load((recipes.RECIPES_DIR / "night_relight_changelist.yaml").read_text(encoding="utf-8"))
    (_project_dir(c) / "recipes").mkdir()
    mine = _project_dir(c) / "recipes" / f"{doc['id']}.yaml"
    mine.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    r = _quote(c, auth, recipe=str(mine))
    assert r.status_code == 200, r.text
    assert r.json()["run"]["state"] == "QUOTED"


@needs_room_a
def test_a_recipe_id_and_a_shipped_path_still_quote(api):
    c, auth, _ = api
    _setup(c, auth)
    assert _quote(c, auth, recipe="night_relight_changelist").status_code == 200
    shipped = recipes.RECIPES_DIR / "night_relight_changelist.yaml"
    assert _quote(c, auth, recipe=str(shipped)).status_code == 200
    studio = recipes.studio_dir()
    studio.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(shipped, studio / "night_relight_changelist.yaml")
    assert _quote(c, auth, recipe=str(studio / "night_relight_changelist.yaml")).status_code == 200


def test_load_runnable_confines_a_path_to_the_recipe_folders(tmp_path):
    loose = tmp_path / "loose.yaml"
    loose.write_text(SECRET, encoding="utf-8", newline="\n")
    with pytest.raises(recipes.RecipeError, match="must live in") as e:
        recipes.load_runnable(loose)
    assert "NDA-material" not in str(e.value) and str(tmp_path) not in str(
        e.value
    )  # the name, not the folder
    project = tmp_path / "proj"
    (project / "recipes").mkdir(parents=True)
    # another project's folder is not this project's
    other = tmp_path / "other" / "recipes"
    other.mkdir(parents=True)
    (other / "x.yaml").write_text(SECRET, encoding="utf-8", newline="\n")
    with pytest.raises(recipes.RecipeError, match="must live in"):
        recipes.load_runnable(other / "x.yaml", project)
    with pytest.raises(recipes.RecipeError, match="must live in"):
        recipes.load_runnable(project / "recipes" / ".." / ".." / "loose.yaml", project)  # `..` is resolved


def test_a_link_out_of_a_recipe_folder_is_judged_by_where_it_points(tmp_path):
    ln = shutil.which("ln")
    if sys.platform == "win32" or ln is None:
        pytest.skip("symlinks are a POSIX check (Windows needs a privilege to make one)")
    target = tmp_path / "outside.yaml"
    target.write_text(SECRET, encoding="utf-8", newline="\n")
    folder = recipes.studio_dir()
    folder.mkdir(parents=True)
    link = folder / "linked.yaml"
    made = subprocess.run([ln, "-s", str(target), str(link)], capture_output=True, check=False)
    if made.returncode != 0:
        pytest.skip(f"cannot make a symlink here: {made.stderr.decode('utf-8', errors='replace').strip()}")
    with pytest.raises(recipes.RecipeError, match="must live in"):
        recipes.load_runnable(link)


def test_a_path_that_cannot_be_resolved_is_a_recipe_error():
    with pytest.raises(recipes.RecipeError, match="must live in"):
        recipes.check_recipe_path("a\x00b.yaml")
    assert recipes.names_a_file("x" * 400) is False  # a name too long for the file system is no file


# ---------------------------------------------------------------------------- (3) the params cap


def _node(nid: str, **params) -> dict:
    return {"id": nid, "kind": "Colour", "label": nid, "params": params}


def test_check_graph_counts_a_nodes_params_as_compact_utf8_json():
    just_under = {"nodes": [_node("c", prompt="é" * (NODE_PARAMS_MAX_BYTES // 2 - 20))], "edges": []}
    assert check_graph(just_under) is just_under
    over = {"nodes": [_node("c", prompt="é" * (NODE_PARAMS_MAX_BYTES // 2))], "edges": []}  # 2 bytes each
    with pytest.raises(NodeParamsTooLarge) as e:
        check_graph(over)
    assert e.value.node_id == "c" and e.value.size > NODE_PARAMS_MAX_BYTES
    wrapped = {
        "nodes": [{"id": "w", "data": {"kind": "Colour", "params": {"p": "x" * 300 * KIB}}}],
        "edges": [],
    }
    with pytest.raises(NodeParamsTooLarge) as e:
        check_graph(wrapped)
    assert e.value.node_id == "w"
    lone = {"nodes": [_node("s", p="\ud800" * NODE_PARAMS_MAX_BYTES)], "edges": []}
    with pytest.raises(NodeParamsTooLarge):  # a lone surrogate is counted, never a UnicodeEncodeError
        check_graph(lone)


def test_a_300_kib_prompt_is_422_params_too_large_and_200_kib_is_stored(api):
    c, auth, _ = api
    _project(c, auth)
    big = {"nodes": [_node("c", prompt="x" * 300 * KIB)], "edges": []}
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=big, headers=auth)
    body = r.json()
    assert r.status_code == 422 and body["error"] == "params_too_large", r.text[:300]
    assert body["node_id"] == "c" and body["limit_bytes"] == NODE_PARAMS_MAX_BYTES
    assert body["bytes"] > NODE_PARAMS_MAX_BYTES and "256 KiB" in body["errors"][0]
    assert c.get(f"/api/v0/projects/{PROJECT}/graph", headers=auth).json()["nodes"] == []  # nothing stored
    ok = {"nodes": [_node("c", prompt="x" * 200 * KIB)], "edges": []}
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=ok, headers=auth)
    assert r.status_code == 200, r.text[:300]
    stored = c.get(f"/api/v0/projects/{PROJECT}/graph", headers=auth).json()
    assert len(stored["nodes"][0]["params"]["prompt"]) == 200 * KIB


def test_an_appended_node_is_held_to_the_same_cap(api):
    c, auth, _ = api
    _project(c, auth)
    r = c.post(
        f"/api/v0/projects/{PROJECT}/nodes",
        json={"kind": "Colour", "params": {"prompt": "x" * 300 * KIB}},
        headers=auth,
    )
    assert r.status_code == 422 and r.json()["error"] == "params_too_large", r.text[:300]
    assert r.json()["node_id"] == "colour-1"
    r = c.post(
        f"/api/v0/projects/{PROJECT}/nodes",
        json={"kind": "Colour", "params": {"prompt": "x" * 200 * KIB}},
        headers=auth,
    )
    assert r.status_code == 201, r.text[:300]


def test_many_small_nodes_are_not_held_to_the_per_node_cap(api):
    c, auth, _ = api
    _project(c, auth)
    nodes = [
        _node(f"c{i}", prompt="x" * 20 * KIB) for i in range(40)
    ]  # 800 KiB in all, under the graph's 4 MiB
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": nodes, "edges": []}, headers=auth)
    assert r.status_code == 200, r.text[:300]


# ---------------------------------------------------------------------------- (4) the newline suite


def _walk_names(*folders: Path) -> list[str]:
    bad = []
    for folder in folders:
        for root, dirs, files in os.walk(folder):
            bad += [os.path.join(root, n) for n in (*dirs, *files) if "\n" in n or "\r" in n]
    return bad


def _stage_packet(settings: Settings, **change) -> Path:
    """room_a copied with its manifest changed (and the legend, which names the packet, rehashed)."""
    dest = settings.home / "stage" / "nl_packet"
    shutil.copytree(ROOM_A, dest)
    m = json.loads((dest / "manifest.json").read_text(encoding="utf-8-sig"))
    old = m["packet_id"]
    m.update(change)
    legend = dest / "id_legend.json"
    data = legend.read_bytes().replace(old.encode(), m["packet_id"].replace("\n", "\\n").encode())
    legend.write_bytes(data)
    entry = m["files"]["id_legend"]
    entry["bytes"], entry["hash"]["hex"] = len(data), blake3(data).hexdigest()
    (dest / "manifest.json").write_text(json.dumps(m, indent=1) + "\n", encoding="utf-8", newline="\n")
    return dest


FRESH_PACKET_ID = "pkt_2ZHQ7DX53BCK1J9P8GJB9Z2V5H"  # a valid id that is not room_a's


def _packet_with(settings: Settings, field: str) -> Path:
    """room_a under a new packet id, `field` (packet_id, project_id or shot_id) ending in a newline."""
    m = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8-sig"))
    base = {"packet_id": FRESH_PACKET_ID, "project_id": m["project_id"], "shot_id": m["shot_id"]}
    base[field] += "\n"
    return _stage_packet(settings, **base)


def _post(c: TestClient, auth: dict, path: str, body: dict, headers: dict | None = None):
    return c.post(f"/api/v0{path}", json=body, headers={**auth, **(headers or {})})


# case id -> (callable(c, auth, settings, run_id, out) -> response, the error code that must come back or None)
Case = Callable[[TestClient, dict, Settings, str, Path], object]
CASES: dict[str, tuple[Case, str | None]] = {
    "project_id": (
        lambda c, a, s, run, out: _post(
            c, a, "/projects", {"project_id": "nl1\n", "custody": {"nda": False}}
        ),
        None,
    ),
    "export_name": (
        lambda c, a, s, run, out: _post(
            c, a, f"/runs/{run}/export", {"out_dir": str(out), "format": "png16", "name": "nlx\n"}
        ),
        "bad_name",
    ),
    "batch_name_pattern": (
        lambda c, a, s, run, out: _post(
            c,
            a,
            f"/projects/{PROJECT}/export",
            {"run_ids": [run], "out_dir": str(out), "format": "png16", "name_pattern": "{n}\n"},
        ),
        "bad_name_pattern",
    ),
    "cutdown_name": (
        lambda c, a, s, run, out: _post(
            c, a, f"/runs/{run}/cutdown", {"preset": "1:1", "out_dir": str(out), "name": "c\n"}
        ),
        "bad_name",
    ),
    "delivery_name": (
        lambda c, a, s, run, out: _post(
            c, a, f"/runs/{run}/deliver", {"format": "exr_sequence", "out_dir": str(out), "name": "d\n"}
        ),
        "bad_name",
    ),
    "tierc_shot_id": (
        lambda c, a, s, run, out: _post(
            c,
            a,
            f"/projects/{PROJECT}/tierc",
            {
                "plate_path": str(ROOM_A / "display.tif"),
                "shot_id": "s1\n",
                "consent_record": "consent-2026-10-19",
            },
        ),
        None,
    ),
    "node_kind": (
        lambda c, a, s, run, out: _post(c, a, f"/projects/{PROJECT}/nodes", {"kind": "Colour\n"}),
        "unknown_kind",
    ),
    "packet_id": (
        lambda c, a, s, run, out: _post(
            c,
            a,
            "/packets",
            {"packet_path": str(_packet_with(s, "packet_id")), "sender": {"name": "t", "version": "0"}},
            {"X-Plate-Packet-Version": "0.1"},
        ),
        None,
    ),
    "packet_shot_id": (
        lambda c, a, s, run, out: _post(
            c,
            a,
            "/packets",
            {"packet_path": str(_packet_with(s, "shot_id")), "sender": {"name": "t", "version": "0"}},
            {"X-Plate-Packet-Version": "0.1"},
        ),
        None,
    ),
    "packet_project_id": (
        lambda c, a, s, run, out: _post(
            c,
            a,
            "/packets",
            {"packet_path": str(_packet_with(s, "project_id")), "sender": {"name": "t", "version": "0"}},
            {"X-Plate-Packet-Version": "0.1"},
        ),
        None,
    ),
}


@needs_room_a
@pytest.mark.parametrize("case", list(CASES))
def test_a_name_ending_in_a_newline_is_422_and_leaves_no_such_path(api, tmp_path, case):
    c, auth, settings = api
    _setup(c, auth)
    run_id = _take(c, auth)
    out = tmp_path / "client"
    call, code = CASES[case]
    r = call(c, auth, settings, run_id, out)
    assert r.status_code == 422, (case, r.status_code, r.text[:300])
    if code is not None:
        assert r.json()["error"] == code, r.text[:300]
    assert not out.exists(), f"{case}: the export folder was made"
    assert _walk_names(settings.home, tmp_path) == [], case  # no file or folder was named with a newline


@needs_room_a
def test_a_packet_id_ending_in_a_newline_is_never_registered(api):
    c, auth, settings = api
    _project(c, auth)
    packet = _packet_with(settings, "packet_id")
    m = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    r = c.post(
        "/api/v0/packets",
        json={"packet_path": str(packet), "sender": {"name": "t", "version": "0"}},
        headers={**auth, "X-Plate-Packet-Version": m["packet_version"]},
    )
    assert r.status_code == 422, r.text[:300]
    assert c.get("/api/v0/packets/" + m["packet_id"].strip(), headers=auth).status_code == 404
    assert _walk_names(settings.home) == []


def test_a_recipe_id_ending_in_a_newline_is_not_a_recipe_id(api, tmp_path):
    c, auth, _ = api
    for bad in ("my_recipe\n", "x\n\n"):
        assert recipes.is_recipe_id(bad) is False, repr(bad)
    doc = {
        "schema": 1,
        "id": "nl_recipe\n",
        "title": "x",
        "kind": "still_edit",
        "engine": "nano-banana-2.1",
        "mask": {"editable_classes": ["sky"], "region": "none", "lock_rest": True},
        "prompt": "p",
    }
    with pytest.raises(recipes.RecipeError, match="lower_snake_case"):
        recipes.parse_runnable(doc, "studio")
    r = c.post("/api/v0/recipes", json={"scope": "studio", "recipe": doc}, headers=auth)
    assert r.status_code in (409, 422), r.text[:300]
    assert _walk_names(tmp_path) == []
