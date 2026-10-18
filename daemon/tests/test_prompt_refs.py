"""`@ref1..@refN` in prompts (router plan T113), on the committed Blender packet `room_a`.

An Edit's prompt names its Reference nodes by position: `@ref1` is the first Reference wired to the Edit's `refs`
input (edge order), `@ref2` the second. At the quote each token becomes the engine's own wording for that image:
the manifest's `reference_token` (on its `reference_images` input, else on the manifest; `{k}` marks the number),
default `reference image {k}`. `inputs.prompt` is the substituted text (what the quote shows, the ledger records
and the engine receives); `inputs.prompt_raw` keeps the artist's text. A token the quote cannot honour is a 422
`bad_ref_token` before any run row exists.

No registered manifest carries `reference_token` yet (the IOField model forbids extra keys and the live smoke
test has not confirmed any engine's wording), so the custom wordings are proven on the daemon's Recorder, which
stands in for an engine through a plain-dict manifest.

`Recorder` is the echoing FakeEngine that keeps every payload it was sent.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.pipeline import PipelineError

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
SKY = "o:929c9e95"  # editable on room_a
DEFAULT_WORDING = "reference image {k}"

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


def refs_engine(
    *, max_items: int | None = 4, input_token: str | None = None, manifest_token: str | None = None
):
    """A plain-dict manifest for a local engine that takes up to `max_items` references, at any size."""
    item: dict = {"name": "reference_images", "type": "images", "required": False}
    if max_items is not None:
        item["max_items"] = max_items
    if input_token is not None:
        item["reference_token"] = input_token
    manifest: dict = {
        "sizes": {"auto": True},
        "inputs": [{"name": "image", "type": "image", "required": True}, item],
    }
    if manifest_token is not None:
        manifest["reference_token"] = manifest_token
    return manifest


class Recorder(FakeEngine):
    """The echoing FakeEngine with a payload journal; `manifest` makes it stand in for an engine."""

    def __init__(self, *args, manifest=None, **kwargs):
        kwargs.setdefault("echo", True)
        super().__init__(*args, **kwargs)
        if manifest is not None:
            self.manifest = manifest
        self.payloads: list[dict] = []

    def submit(self, payload, idempotency_key):
        self.payloads.append(copy.deepcopy(payload))
        return super().submit(payload, idempotency_key)


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.delenv("PLATE_ROUTER_MODE", raising=False)
    return Settings.load()


def _client(settings: Settings, engine=None) -> TestClient:
    c = TestClient(create_app(settings, engine or Recorder()), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _ingest(c: TestClient, auth: dict, packet: Path) -> str:
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    r = c.post(
        "/api/v0/packets",
        json={"packet_path": str(packet), "sender": {"name": "test", "version": "0"}},
        headers=hdr,
    )
    assert r.status_code == 202, r.text
    return r.json()["packet_id"]


def _setup(c: TestClient, auth: dict) -> str:
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    return _ingest(c, auth, ROOM_A)


def _put_graph(c: TestClient, auth: dict, nodes: list[dict], edges: list[dict]) -> None:
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": nodes, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text


def _plate(packet_id: str) -> dict:
    return {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}


def _mask(nid: str, ids: list[str]) -> dict:
    return {"id": nid, "kind": "Mask", "params": {"ids": ids, "textLock": True}}


def _edit(nid: str = "e", engine: str = "fake", **params) -> dict:
    return {"id": nid, "kind": "Edit", "label": "Edit", "params": {"engine": engine, **params}}


def _reference(nid: str, key: str) -> dict:
    return {
        "id": nid,
        "kind": "Reference",
        "label": "Reference",
        "params": {"referenceHash": key, "role": "object"},
    }


def _edge(source: str, target: str, handle: str | None = None) -> dict:
    e = {"id": f"{source}->{target}", "source": source, "target": target}
    if handle is not None:
        e["targetHandle"] = handle
    return e


def _upload_reference(c: TestClient, auth: dict, colour: tuple[int, int, int]) -> str:
    img = np.zeros((30, 40, 3), np.uint8)
    img[:] = colour
    png = bytes(cv2.imencode(".png", img)[1])
    r = c.post(
        f"/api/v0/assets?kind=reference&project_id={PROJECT}",
        content=png,
        headers={**auth, "Content-Type": "image/png"},
    )
    assert r.status_code == 201, r.text
    return r.json()["hash"]


def _graph_with_refs(c: TestClient, auth: dict, n: int, **edit_params) -> None:
    """Plate -> Mask -> Edit with `n` Reference nodes (r1..rn, distinct images) wired to the Edit's refs input."""
    packet_id = _setup(c, auth)
    keys = [_upload_reference(c, auth, (40 * i + 20, 100, 200 - 30 * i)) for i in range(n)]
    nodes = [_plate(packet_id), _mask("m", [SKY]), *(_reference(f"r{i + 1}", k) for i, k in enumerate(keys))]
    edit = _edit(**edit_params)
    edges = [_edge("p", "m"), _edge("m", "e"), *(_edge(f"r{i + 1}", "e", "refs") for i in range(n))]
    _put_graph(c, auth, [*nodes, edit], edges)


def _quote(c: TestClient, body: dict, node: str = "e"):
    pl = c.app.state.pipeline
    with c.app.state.index.project(PROJECT) as conn:
        return pl.quote_node(conn, PROJECT, node, body)


def _refusal(c: TestClient, body: dict) -> PipelineError:
    with pytest.raises(PipelineError) as e:
        _quote(c, body)
    return e.value


def _run_count(c: TestClient) -> int:
    with c.app.state.index.project(PROJECT) as conn:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def _quote_http(c: TestClient, auth: dict, **body):
    return c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json=body, headers=auth)


def _ledger_quote(settings: Settings, run_id: str) -> dict:
    (rec,) = [
        r
        for r in Ledger(settings.projects_dir / PROJECT).records()
        if r.get("run_id") == run_id and r.get("kind") == "quoted"
    ]
    return rec


# ---------------------------------------------------------------------------- the substitution itself


def _pipeline(settings: Settings, engines: dict | None = None):
    c = _client(settings)
    pl = c.app.state.pipeline
    pl.engines.update(engines or {})
    return pl


def _fake_refs(n: int) -> list[dict]:
    return [{"hash": f"{i:064x}", "role": "object", "node": f"r{i}"} for i in range(1, n + 1)]


def test_each_token_becomes_the_default_wording_of_its_reference(settings):
    pl = _pipeline(settings, {"stand-in": Recorder(manifest=refs_engine())})
    out = pl.substitute_ref_tokens(
        "Move the sofa to where @ref2 shows it, in the style of @ref1; keep (@ref1).",
        "stand-in",
        _fake_refs(2),
    )
    assert out == (
        "Move the sofa to where reference image 2 shows it, in the style of reference image 1; "
        "keep (reference image 1)."
    )
    # The registry's nano-banana-2.1 states no wording, so it takes the default too.
    nano = pl.substitute_ref_tokens("like @ref1", "nano-banana-2.1", _fake_refs(1))
    assert nano == "like reference image 1"


def test_two_digit_tokens_are_read_whole(settings):
    pl = _pipeline(settings, {"stand-in": Recorder(manifest=refs_engine(max_items=13))})
    out = pl.substitute_ref_tokens("@ref10 then @ref1", "stand-in", _fake_refs(10))
    assert out == "reference image 10 then reference image 1"


def test_a_manifest_states_its_own_wording(settings):
    pl = _pipeline(
        settings,
        {
            "on-input": Recorder(manifest=refs_engine(input_token="image {k}")),
            "on-manifest": Recorder(manifest=refs_engine(manifest_token="[Image {k}]")),
            "no-number": Recorder(manifest=refs_engine(input_token="Picture")),
            "both": Recorder(manifest=refs_engine(input_token="input {k}", manifest_token="manifest {k}")),
            "blank": Recorder(manifest=refs_engine(input_token="  ")),
        },
    )
    sub = pl.substitute_ref_tokens
    assert sub("@ref2 and @ref1", "on-input", _fake_refs(2)) == "image 2 and image 1"
    assert sub("@ref1", "on-manifest", _fake_refs(1)) == "[Image 1]"
    assert sub("@ref2", "no-number", _fake_refs(2)) == "Picture 2"  # no {k}: the number follows
    assert sub("@ref1", "both", _fake_refs(1)) == "input 1"  # the input's own wording wins
    assert sub("@ref1", "blank", _fake_refs(1)) == "reference image 1"
    assert pl.reference_token("on-input") == "image {k}" and pl.reference_token("nano-banana-2.1") is None


def test_text_that_is_not_a_token_is_left_alone(settings):
    pl = _pipeline(settings, {"stand-in": Recorder(manifest=refs_engine())})
    for text in (
        "no tokens at all",
        "refine the @refined walls",
        "write to me@ref1.com",
        "a_@ref1",
        "the @ref_1 file",
        "reference image 1 already",
    ):
        assert pl.substitute_ref_tokens(text, "stand-in", _fake_refs(1)) == text
    # Nothing to resolve means nothing to refuse: an engine without references keeps such text too.
    assert pl.substitute_ref_tokens("refine the @refined walls", "fake", []) == "refine the @refined walls"


@pytest.mark.parametrize(
    ("text", "refs", "token"),
    [
        ("like @ref3", 2, "@ref3"),
        ("like @ref1", 0, "@ref1"),
        ("first @ref1 then @ref5", 2, "@ref5"),
        ("like @ref", 2, "@ref"),
        ("like @ref0", 2, "@ref0"),
        ("like @ref01", 2, "@ref01"),
        ("like @ref1a", 2, "@ref1a"),
    ],
)
def test_a_token_that_names_no_reference_is_refused_whole(settings, text, refs, token):
    pl = _pipeline(settings, {"stand-in": Recorder(manifest=refs_engine())})
    with pytest.raises(PipelineError) as e:
        pl.substitute_ref_tokens(text, "stand-in", _fake_refs(refs))
    assert (e.value.code, e.value.status) == ("bad_ref_token", 422)
    assert e.value.extra["token"] == token and e.value.extra["references"] == refs


def test_an_engine_with_no_reference_input_refuses_any_token(settings):
    pl = _pipeline(settings)
    with pytest.raises(PipelineError) as e:  # the daemon's plain fake declares no reference_images input
        pl.substitute_ref_tokens("like @ref1 and @ref2 and @ref1", "fake", _fake_refs(2))
    assert e.value.code == "bad_ref_token" and "takes no reference images" in str(e.value)
    assert e.value.extra["tokens"] == ["@ref1", "@ref2", "@ref1"]
    # A registered engine whose manifest declares none behaves the same way (no image editor is in that case).
    for engine_id in ("seedance-2.5-i2v", "real-esrgan"):
        assert not any(i.name == "reference_images" for i in pl.manifests[engine_id].inputs)
        with pytest.raises(PipelineError) as e:
            pl.substitute_ref_tokens("like @ref1", engine_id, [])
        assert e.value.code == "bad_ref_token"


# ---------------------------------------------------------------------------- at the quote


@needs_room_a
def test_the_quote_shows_ledgers_and_sends_the_substituted_prompt(sync_settings, auth):
    engine = Recorder(manifest=refs_engine(input_token="image {k}"))
    with _client(sync_settings, engine) as c:
        _graph_with_refs(c, auth, 2)
        raw = "Put the chair from @ref2 in the corner, lit like @ref1."
        r = _quote_http(c, auth, prompt=raw)
        assert r.status_code == 200, r.text
        run = r.json()["run"]
        want = "Put the chair from image 2 in the corner, lit like image 1."
        assert run["inputs"]["prompt"] == want and run["inputs"]["prompt_raw"] == raw
        assert run["inputs"]["prompt_source"] == "explicit"
        assert [x["node"] for x in run["inputs"]["reference_images"]] == ["r1", "r2"]
        assert run["route"]["reference_token"] == "image {k}"

        rec = _ledger_quote(sync_settings, run["id"])
        assert rec["data"]["inputs"]["prompt"] == want and rec["data"]["inputs"]["prompt_raw"] == raw

        a = c.post(f"/api/v0/runs/{run['id']}/approve", json={"by": "human"}, headers=auth)
        assert a.status_code == 200, a.text
        done = c.get(f"/api/v0/runs/{run['id']}", headers=auth).json()
        assert done["state"] == "DONE", done.get("error")
        sent = engine.payloads[-1]["inputs"]
        assert sent["prompt"] == want and len(sent["reference_images"]) == 2
        assert "prompt_raw" not in sent and "@ref" not in json.dumps(sent["prompt"])


@needs_room_a
def test_the_default_wording_applies_when_the_manifest_states_none(sync_settings, auth):
    with _client(sync_settings, Recorder(manifest=refs_engine())) as c:
        _graph_with_refs(c, auth, 1)
        run, _q = _quote(c, {"prompt": "swap the cushion for @ref1"})
        assert run.inputs["prompt"] == "swap the cushion for reference image 1"
        assert run.inputs["prompt_raw"] == "swap the cushion for @ref1"
        assert "reference_token" not in run.route_dict


@needs_room_a
def test_a_registered_engine_gets_the_default_wording(sync_settings, auth):
    with _client(sync_settings) as c:
        _graph_with_refs(c, auth, 2, engine="nano-banana-2.1")
        run, q = _quote(c, {"prompt": "relight like @ref2, materials of @ref1"})
        assert run.route.engine_id == "nano-banana-2.1" and q.run_id == run.id
        assert run.inputs["prompt"] == "relight like reference image 2, materials of reference image 1"


@needs_room_a
def test_a_prompt_without_tokens_is_untouched(sync_settings, auth):
    with _client(sync_settings, Recorder(manifest=refs_engine())) as c:
        _graph_with_refs(c, auth, 2)
        run, _q = _quote(c, {"prompt": "late blue hour, keep the refined walls"})
        assert run.inputs["prompt"] == "late blue hour, keep the refined walls"
        assert "prompt_raw" not in run.inputs


@needs_room_a
def test_a_prompt_with_no_references_wired_is_untouched(sync_settings, auth):
    with _client(sync_settings) as c:
        _graph_with_refs(c, auth, 0)
        run, _q = _quote(c, {"prompt": "late blue hour"})
        assert run.inputs["prompt"] == "late blue hour" and "prompt_raw" not in run.inputs


@needs_room_a
def test_tokens_in_a_recipe_prompt_are_resolved_too(sync_settings, auth):
    with _client(sync_settings) as c:
        _graph_with_refs(
            c,
            auth,
            1,
            engine="nano-banana-2.1",
            recipe="season_swap",
            tokens={"[SEASON]": "the look of @ref1"},
        )
        run, _q = _quote(c, {})
        assert run.inputs["prompt_source"] == "recipe"
        assert "Shift the season to the look of reference image 1 at the same hour" in run.inputs["prompt"]
        assert "@ref1" in run.inputs["prompt_raw"]


@needs_room_a
def test_a_bad_token_is_refused_at_the_quote_before_any_run_row(sync_settings, auth):
    with _client(sync_settings, Recorder(manifest=refs_engine())) as c:
        _graph_with_refs(c, auth, 2)
        for prompt in ("use @ref3", "use @ref", "use @ref0"):
            r = _quote_http(c, auth, prompt=prompt)
            assert r.status_code == 422 and r.json()["error"] == "bad_ref_token", r.text
        e = _refusal(c, {"prompt": "use @ref3"})
        assert (e.code, e.status) == ("bad_ref_token", 422) and "wired" in str(e)
        assert _run_count(c) == 0
        run, _q = _quote(c, {"prompt": "use @ref2"})  # and the good one still quotes
        assert run.inputs["prompt"] == "use reference image 2" and _run_count(c) == 1


@needs_room_a
def test_a_token_with_no_reference_wired_is_refused(sync_settings, auth):
    with _client(sync_settings, Recorder(manifest=refs_engine())) as c:  # declares references, none wired
        _graph_with_refs(c, auth, 0)
        e = _refusal(c, {"prompt": "like @ref1"})
        assert e.code == "bad_ref_token" and e.extra["references"] == 0 and "none is wired" in str(e)
        assert _run_count(c) == 0


@needs_room_a
def test_any_token_on_an_engine_with_no_reference_input_is_refused(sync_settings, auth):
    with _client(sync_settings, Recorder()) as c:  # the plain fake declares no reference_images input
        _graph_with_refs(c, auth, 0)
        e = _refusal(c, {"prompt": "like @ref1"})
        assert (e.code, e.status) == ("bad_ref_token", 422) and "takes no reference images" in str(e)
        r = _quote_http(c, auth, prompt="like @ref1")
        assert r.status_code == 422 and r.json()["error"] == "bad_ref_token", r.text
        assert _run_count(c) == 0
        run, _q = _quote(c, {"prompt": "no tokens here"})
        assert run.inputs["prompt"] == "no tokens here"
        assert _run_count(c) == 1


@needs_room_a
def test_wired_references_the_engine_cannot_take_are_refused_first(sync_settings, auth):
    with _client(sync_settings, Recorder()) as c:
        _graph_with_refs(c, auth, 1)
        e = _refusal(c, {"prompt": "like @ref1"})
        assert e.code == "references_unsupported"  # the wiring is refused before the prompt is read
        assert _run_count(c) == 0
